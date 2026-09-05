import mimetypes
import os
import re
import secrets
import uuid
from datetime import date, datetime, time as dtime, timedelta

from flask import (Blueprint, current_app, flash, g, redirect, render_template,
                    request, send_from_directory, url_for)
from werkzeug.utils import secure_filename

from . import activity, ai_match, banner, db, doc_sanity, mailer, notifications, pdfgen, poster, uploadutil, settings as settings_module
# NOTE: calendar_integration is imported lazily (inside edit(), where it's
# used) rather than at module level — calendar_integration imports from this
# module (split_training_time), so a top-level import here would be circular.
from . import APP_TZ, fmtdaterange
from .auth import admin_required, login_required
from .csvutil import csv_response

bp = Blueprint("sessions", __name__, url_prefix="/sessions")

# 'Proposed' is first: a class can exist before it's confirmed — created just
# so a Quotation (or other early-stage workflow) has somewhere to point to —
# then moves to 'Scheduled' once confirmed (manually, or automatically when a
# linked quotation's signed copy comes back — see quotations._handle_quotation_signed).
STATUSES = ["Proposed", "Scheduled", "Ongoing", "Completed", "Cancelled"]
TRAINING_MODES = ["Physical", "Virtual", "Hybrid"]
TRAINING_TYPES = ["In-house Training", "Public Training", "Workshop", "Conference"]
ROOM_SETUP_OPTIONS = ["Classroom", "Banquet", "U-Shape", "Theatre", "Boardroom", "Conference"]

# Notified whenever a signed/completed document lands on a class — so the
# office knows a form is ready without having to check every class page.
# The actual recipient list is admin-configurable under Settings; this
# constant is kept only as the last-resort fallback the settings module
# itself falls back to. Use settings_module.get_notification_emails()
# everywhere a notification is actually sent, not this constant directly.
DOCUMENT_NOTIFY_EMAILS = ["eriktajudin@modoku.tech", "hello@modoku.tech"]


def _fmt_time_12h(hhmm):
    """'09:00' (HTML <input type="time"> value, 24h) -> '9:00 AM'."""
    try:
        return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0").replace(" 0", " ")
    except ValueError:
        return hhmm


def format_training_time(start_hhmm, end_hhmm):
    """Builds the consistent, always-correctly-formatted training_time string
    stored on a class, from two 24h HTML time-input values, e.g.
    ('09:00', '17:00') -> '9:00 AM - 5:00 PM'."""
    if not start_hhmm or not end_hhmm:
        return None
    return f"{_fmt_time_12h(start_hhmm)} - {_fmt_time_12h(end_hhmm)}"


_TIME_RE = re.compile(
    r"(?P<h>\d{1,2}):(?P<m>\d{2})\s*(?P<ampm>[AaPp]\.?[Mm]\.?)?"
)


def _parse_time_to_hhmm(text, default_ampm=None):
    """Best-effort reverse of format_training_time — pulls a single clock
    time out of free text (old data may have been entered before the time
    fields were split/validated) back into a 24h 'HH:MM' value so an edit
    form can pre-fill the <input type="time"> fields. Returns '' if nothing
    parseable is found."""
    if not text:
        return ""
    m = _TIME_RE.search(text)
    if not m:
        return ""
    hour = int(m.group("h"))
    minute = int(m.group("m"))
    ampm = (m.group("ampm") or default_ampm or "").lower().replace(".", "")
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return ""
    return f"{hour:02d}:{minute:02d}"


def split_training_time(training_time):
    """Reverse of format_training_time, for pre-filling the edit form's two
    time inputs from whatever is currently stored (old free-text or the new
    canonical format)."""
    if not training_time:
        return "", ""
    parts = re.split(r"\s*(?:-|–|to)\s*", training_time, maxsplit=1)
    start_text = parts[0] if parts else ""
    end_text = parts[1] if len(parts) > 1 else ""
    # If the start time has no AM/PM of its own, borrow the end time's —
    # e.g. "9:00 - 5:00 PM" implies the first is also PM-adjacent context,
    # but far more commonly a bare morning start ("9:00") pairs with a PM
    # end, so this is only used as a last-resort fallback below.
    start_hhmm = _parse_time_to_hhmm(start_text)
    end_hhmm = _parse_time_to_hhmm(end_text)
    return start_hhmm, end_hhmm


def ensure_t3_public_token(session_id):
    """Every class gets a long, unguessable token the first time it's needed
    — used for the public, always-editable T3 Attendance Form link (client
    self-service, and the 'send to trainer' button). Distinct from the short
    hand-typed session_code (Return Attendance Form) since this one only
    ever travels as a clickable link, never gets typed in."""
    row = db.query("SELECT t3_public_token FROM course_sessions WHERE id = ?", (session_id,), one=True)
    if row and row["t3_public_token"]:
        return row["t3_public_token"]
    token = secrets.token_urlsafe(24)
    db.execute("UPDATE course_sessions SET t3_public_token = ? WHERE id = ?", (token, session_id))
    return token


def ensure_jd14_return_token(session_id):
    """Every class gets a long, unguessable token the first time it's needed
    — used for the public JD14 return-upload page (jd14_return.py), so the
    client/trainer can upload their single signed copy directly instead of
    emailing it back and forth."""
    row = db.query("SELECT jd14_return_token FROM course_sessions WHERE id = ?", (session_id,), one=True)
    if row and row["jd14_return_token"]:
        return row["jd14_return_token"]
    token = secrets.token_urlsafe(24)
    db.execute("UPDATE course_sessions SET jd14_return_token = ? WHERE id = ?", (token, session_id))
    return token


def ensure_grant_docs_token(session_id):
    """Every class gets a long, unguessable token the first time it's
    needed — used for the public HRDCorp Grant ID entry page (hrdcorp_grant.py),
    sent as part of the HRDCorp Grant Documents email."""
    row = db.query("SELECT grant_docs_token FROM course_sessions WHERE id = ?", (session_id,), one=True)
    if row and row["grant_docs_token"]:
        return row["grant_docs_token"]
    token = secrets.token_urlsafe(24)
    db.execute("UPDATE course_sessions SET grant_docs_token = ? WHERE id = ?", (token, session_id))
    return token


def t3_form_is_editable(session_row):
    """The public T3 Attendance Form stays open for real-time edits by the
    client right up until the day of class, then locks — matching the
    'always-editable until the day of class' requirement. Never editable
    once a class is cancelled."""
    if session_row["status"] == "Cancelled":
        return False
    try:
        start = datetime.strptime(session_row["start_date"], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return True
    return date.today() <= start


# Malaysia is UTC+8 with no daylight saving, so a local training time can be
# converted straight to UTC (no VTIMEZONE block needed) and every calendar
# app renders it correctly in the viewer's own local time.
_MY_UTC_OFFSET_HOURS = 8


def ics_datetime_lines(start_date, end_date, training_time):
    """Returns the (DTSTART, DTEND) lines for an .ics VEVENT, in UTC, so the
    invite always carries a real time-of-day — not just a bare date. Falls
    back to an all-day event only if there's genuinely no parseable time on
    file (e.g. pre-existing data from before training time was compulsory).
    Shared by every calendar invite this app sends (T3 link + calendar
    invite to the client, and the trainer's PO invite) so 'the invite must
    have a time' is fixed in exactly one place."""
    last_date = end_date or start_date
    start_hhmm, end_hhmm = split_training_time(training_time) if training_time else ("", "")
    if start_hhmm and end_hhmm:
        start_local = datetime.strptime(f"{start_date} {start_hhmm}", "%Y-%m-%d %H:%M")
        end_local = datetime.strptime(f"{last_date} {end_hhmm}", "%Y-%m-%d %H:%M")
        start_utc = start_local - timedelta(hours=_MY_UTC_OFFSET_HOURS)
        end_utc = end_local - timedelta(hours=_MY_UTC_OFFSET_HOURS)
        return (
            f"DTSTART:{start_utc.strftime('%Y%m%dT%H%M%SZ')}\r\n",
            f"DTEND:{end_utc.strftime('%Y%m%dT%H%M%SZ')}\r\n",
        )
    # Fallback: all-day event (DTEND is exclusive, so bump by one day).
    end_dt = datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)
    return (
        f"DTSTART;VALUE=DATE:{start_date.replace('-', '')}\r\n",
        f"DTEND;VALUE=DATE:{end_dt.strftime('%Y%m%d')}\r\n",
    )


def build_session_ics(session_row, description_extra=""):
    """Builds a minimal .ics calendar invite for a class's training dates —
    shared by the signed-quotation-return flow and any other feature that
    needs to hand a client/trainer a calendar invite for a class."""
    dtstart_line, dtend_line = ics_datetime_lines(
        session_row["start_date"], session_row["end_date"], session_row["training_time"]
    )
    now_stamp = date.today().strftime("%Y%m%d") + "T000000Z"
    summary = f"{session_row['course_title']} — Training".replace("\n", " ")
    location = (session_row["venue"] or "").replace("\n", " ")
    description = f"Training: {session_row['course_title']}"
    if session_row["training_time"]:
        description += f"\\nTime: {session_row['training_time']}"
    if description_extra:
        description += f"\\n{description_extra}"
    uid = f"session-{session_row['id']}@modoku.tech"
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Modoku Hub//Class//EN\r\n"
        "CALSCALE:GREGORIAN\r\n"
        "METHOD:PUBLISH\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{now_stamp}\r\n"
        f"{dtstart_line}"
        f"{dtend_line}"
        f"SUMMARY:{summary}\r\n"
        f"LOCATION:{location}\r\n"
        f"DESCRIPTION:{description}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    ).encode("utf-8")


def _default_t3_email_subject(session_row):
    date_range = fmtdaterange(session_row["start_date"], session_row["end_date"])
    return f"T3 Attendance Form — {session_row['course_title']} ({date_range})"


def _default_t3_email_body(session_row, t3_url):
    date_range = fmtdaterange(session_row["start_date"], session_row["end_date"])
    greeting_name = session_row["pic_name"] if "pic_name" in session_row.keys() else None
    meeting_link_line = ""
    if session_row["training_mode"] in ("Virtual", "Hybrid") and session_row["meeting_link"]:
        meeting_link_line = f"Meeting Link: {session_row['meeting_link']}\n"
    return (
        f"Hi {greeting_name or 'there'},\n\n"
        "Here's the link to the T3 Attendance Form for this training — participants can be added, "
        "edited, or removed anytime up until the day of training:\n\n"
        f"{t3_url}\n\n"
        f"Training: {session_row['course_title']}\n"
        f"Date: {date_range}\n"
        f"Time: {session_row['training_time'] or 'To be confirmed'}\n"
        f"Venue: {session_row['venue'] or 'To be confirmed'}\n"
        f"{meeting_link_line}\n"
        "Should you have any questions, please feel free to contact us.\n\n"
        "Cheers!"
    )


def _default_jd14_return_email_subject(session_row):
    date_range = fmtdaterange(session_row["start_date"], session_row["end_date"])
    return f"JD14 Form — please return the signed copy — {session_row['course_title']} ({date_range})"


def _default_jd14_return_email_body(session_row, return_url):
    date_range = fmtdaterange(session_row["start_date"], session_row["end_date"])
    greeting_name = session_row["pic_name"] if "pic_name" in session_row.keys() and session_row["pic_name"] else None
    return (
        f"Hi {greeting_name or 'there'},\n\n"
        "Once you have signed the HRDCorp Joint Declaration Form (PSMB/SBL-KHAS/JD/14), simply "
        "upload the signed copy directly here for our reference. There is no need to email it "
        f"separately:\n{return_url}\n\n"
        f"Training: {session_row['course_title']}\n"
        f"Date: {date_range}\n\n"
        "Should you have any questions, please feel free to contact us.\n\n"
        "Cheers!"
    )


def _default_jd14_email_subject(session_row):
    date_range = fmtdaterange(session_row["start_date"], session_row["end_date"])
    return f"JD14 Form — {session_row['course_title']} ({date_range})"


def _default_jd14_email_body(session_row):
    date_range = fmtdaterange(session_row["start_date"], session_row["end_date"])
    return (
        "Hi,\n\n"
        "Please find attached the signed HRDCorp Joint Declaration Form (PSMB/SBL-KHAS/JD/14) for the "
        "training below.\n\n"
        f"Training: {session_row['course_title']}\n"
        f"Date: {date_range}\n\n"
        "Should you have any questions, please feel free to contact us.\n\n"
        "Cheers!"
    )


def _default_evaluation_email_subject(session_row):
    date_range = fmtdaterange(session_row["start_date"], session_row["end_date"])
    return f"Training Evaluation Report — {session_row['course_title']} ({date_range})"


def _default_evaluation_email_body(session_row):
    date_range = fmtdaterange(session_row["start_date"], session_row["end_date"])
    greeting_name = session_row["pic_name"] if "pic_name" in session_row.keys() and session_row["pic_name"] else None
    return (
        f"Hi {greeting_name or 'there'},\n\n"
        "Please find attached the training evaluation report for the training below.\n\n"
        f"Training: {session_row['course_title']}\n"
        f"Date: {date_range}\n\n"
        "Do let us know if you have any questions.\n\n"
        "Thank you."
    )


def _default_grant_email_subject(session_row):
    date_range = fmtdaterange(session_row["start_date"], session_row["end_date"])
    return f"HRDCorp Grant Documents — {session_row['course_title']} ({date_range})"


def _default_grant_email_body(session_row, grant_url):
    greeting_name = session_row["pic_name"] if "pic_name" in session_row.keys() and session_row["pic_name"] else None
    # "Training" and "HRDCorp Programme No." sit flush against each other
    # (no blank line between them) — they read as one small info block,
    # with the usual blank-line paragraph spacing only after the block.
    info_lines = [f"Training: {session_row['course_title']}"]
    if "hrdcorp_programme_no" in session_row.keys() and session_row["hrdcorp_programme_no"]:
        info_lines.append(f"HRDCorp Programme No.: {session_row['hrdcorp_programme_no']}")
    info_block = "\n".join(info_lines)
    return (
        f"Hi {greeting_name or 'there'},\n\n"
        "Please find attached the documents needed for your HRDCorp grant application: the Course "
        "Outline, Trainer Profile, Accredited Certificate, and Quotation.\n\n"
        f"{info_block}\n\n"
        "Once HRDCorp has approved the grant and issued a Grant ID, please submit it to us here — no "
        f"need to email it separately:\n{grant_url}\n\n"
        "Should you have any questions, please feel free to contact us.\n\n"
        "Cheers!"
    )


def _default_t3_form_email_subject(session_row):
    date_range = fmtdaterange(session_row["start_date"], session_row["end_date"])
    return f"T3 Attendance Form (printable) — {session_row['course_title']} ({date_range})"


def _default_t3_form_email_body(session_row):
    date_range = fmtdaterange(session_row["start_date"], session_row["end_date"])
    greeting_name = session_row["trainer_name"] if "trainer_name" in session_row.keys() else None
    meeting_link_line = ""
    if session_row["training_mode"] in ("Virtual", "Hybrid") and session_row["meeting_link"]:
        meeting_link_line = f"Meeting Link: {session_row['meeting_link']}\n"
    # Link straight to this class's Return Attendance Form page (skips the
    # code-entry step since we already know the code) so the trainer knows
    # exactly where "send the signed copy back to us" means, rather than
    # having to guess they should photograph and email it separately.
    if session_row["session_code"]:
        return_url = url_for("attendance_return.details", code=session_row["session_code"], _external=True)
    else:
        return_url = url_for("attendance_return.lookup", _external=True)
    return (
        f"Hi {greeting_name or 'there'},\n\n"
        "Attached is the printable T3 Attendance Form for this training — in case the client "
        "isn't able to fill in the online version, please print this out, get it signed by "
        "participants on the day, and send the signed copy back to us — you can upload via "
        f"this link, no need to email it separately:\n{return_url}\n\n"
        f"Training: {session_row['course_title']}\n"
        f"Date: {date_range}\n"
        f"Time: {session_row['training_time'] or 'To be confirmed'}\n"
        f"Venue: {session_row['venue'] or 'To be confirmed'}\n"
        f"{meeting_link_line}\n"
        "Should you have any questions, please feel free to contact us.\n\n"
        "Cheers!"
    )


def _t3_form_pdf_filename(session_row):
    date_slug = re.sub(r"[^A-Za-z0-9]+", "_", session_row["start_date"] or "").strip("_")
    title_slug = re.sub(r"[^A-Za-z0-9]+", "_", session_row["course_title"] or "").strip("_")
    return f"T3_Attendance_Form_{title_slug}_{date_slug}.pdf"


def _notify_document_uploaded(session_id, doc_label, ai_warning=None):
    """Emails the admin-configured notification addresses (Settings) that a
    document is ready on a class. Best-effort — a notification failure (or
    email not being configured at all) must never block the upload that
    triggered it.

    ai_warning: an optional AI sanity-check warning (see doc_sanity.py) to
    fold into this same email — used for uploads that come in through a
    public, unauthenticated link (e.g. jd14_return.py), where there's no
    logged-in staff session to flash a warning to directly."""
    try:
        session_row = db.query(
            """SELECT cs.start_date, cs.end_date, c.title AS course_title FROM course_sessions cs
               JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?""",
            (session_id,), one=True,
        )
        if session_row is None:
            return
        date_range = fmtdaterange(session_row["start_date"], session_row["end_date"])
        subject = f"{doc_label} ready — {session_row['course_title']} ({date_range})"
        body = (
            f"{doc_label} has just been uploaded for:\n\n"
            f"Class: {session_row['course_title']}\n"
            f"Date: {date_range}\n\n"
            f"View it in Modoku Hub under Classes."
        )
        if ai_warning:
            body += f"\n\nNote (AI sanity-check): {ai_warning}"
        notify_to = ", ".join(settings_module.get_notification_emails())
        if not notify_to:
            return
        mailer.send_email(notify_to, subject, body,
                           related_type="course_session", related_id=session_id)
    except Exception:  # noqa: BLE001 - notification must never block the upload
        current_app.logger.exception("Failed to send document-ready notification for session %s", session_id)


def _notify_trainers_invoice_due(session_id):
    """Fired once, the moment a class's status becomes Completed (from
    either the manual Edit Class form or the automatic date-based status
    advance below) — emails every trainer assigned to the class a unique
    link to the public invoice-submission page (trainer_invoice.py) where
    they can upload their invoice/claim documents. Guarded by
    trainer_invoice_email_sent_at so it only ever fires once per class.

    This can run from a before_app_request hook, so it's re-checked on
    every request — including the several static-asset requests one page
    load triggers — which could otherwise let more than one near-
    simultaneous request see "not sent yet" before the first one's UPDATE
    landed, firing duplicate emails. Fixed by claiming the row (the
    UPDATE ... WHERE ... IS NULL below) BEFORE sending anything: only the
    request whose UPDATE actually affects a row proceeds. Best-effort
    throughout: a mail hiccup must never block the status update that
    triggered it."""
    try:
        session_row = db.query(
            """SELECT cs.*, c.title AS course_title FROM course_sessions cs
               JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?""",
            (session_id,), one=True,
        )
        if session_row is None or session_row["trainer_invoice_email_sent_at"]:
            return

        assigned_trainers = db.query(
            """SELECT t.id, t.name, t.email FROM session_trainers st
               JOIN trainers t ON t.id = st.trainer_id
               WHERE st.session_id = ? ORDER BY t.name""",
            (session_id,),
        )
        if not assigned_trainers and session_row["trainer_id"]:
            assigned_trainers = db.query(
                "SELECT id, name, email FROM trainers WHERE id = ?", (session_row["trainer_id"],)
            )
        recipients = [t for t in assigned_trainers if t["email"]]
        if not recipients:
            return

        # Claim first — only the request whose UPDATE actually changes a row
        # goes on to email trainers.
        cur = db.get_db().execute(
            "UPDATE course_sessions SET trainer_invoice_email_sent_at = datetime('now') "
            "WHERE id = ? AND trainer_invoice_email_sent_at IS NULL",
            (session_id,),
        )
        db.get_db().commit()
        if cur.rowcount == 0:
            return  # another concurrent request already claimed this one

        from .trainer_invoice import ensure_token
        token = ensure_token(session_id)
        link = url_for("trainer_invoice.form", token=token, _external=True)
        date_range = fmtdaterange(session_row["start_date"], session_row["end_date"])
        subject = f"Please submit your invoice — {session_row['course_title']} ({date_range})"
        for trainer in recipients:
            body = (
                f"Hi {trainer['name']},\n\n"
                f"The class below has now been marked Completed — please submit your invoice "
                f"(and any claims) using the link below. You can upload PDF, Word, or Excel files, "
                f"and more than one file if you have separate invoice/claim documents.\n\n"
                f"Class: {session_row['course_title']}\n"
                f"Date: {date_range}\n\n"
                f"Submit your invoice here:\n{link}\n\n"
                f"Thank you,\nModoku Tech"
            )
            try:
                mailer.send_email(trainer["email"], subject, body,
                                   related_type="course_session", related_id=session_id)
            except (mailer.MailNotConfigured, mailer.MailSendError):
                current_app.logger.exception(
                    "Failed to send invoice-request email to trainer %s for session %s",
                    trainer["id"], session_id)
    except Exception:  # noqa: BLE001 - must never block the status change that triggered this
        current_app.logger.exception("Failed to notify trainers to submit invoice for session %s", session_id)


def _leads_for_form():
    """Every lead (a company's contact person, i.e. a PIC), with its
    company name for grouping in the Class form's PIC dropdown — not
    filtered to the currently-selected client company since that's chosen
    in the same form and there's no reliable way to filter client-side
    without JS wiring, so all PICs are listed, grouped for clarity."""
    return db.query(
        """SELECT l.id, l.name, l.email, l.role, l.company_id, co.name AS company_name
           FROM leads l LEFT JOIN companies co ON co.id = l.company_id
           ORDER BY co.name IS NULL, co.name, l.name"""
    )


def _set_session_trainers(session_id, trainer_ids):
    """Replaces the full assigned-trainer roster for a session. trainer_id
    on course_sessions is kept as the "primary" trainer (first selected) for
    backward compatibility with existing single-trainer displays."""
    trainer_ids = [t for t in dict.fromkeys(trainer_ids) if t]  # dedupe, keep order
    db.execute("DELETE FROM session_trainers WHERE session_id = ?", (session_id,))
    for tid in trainer_ids:
        db.execute("INSERT OR IGNORE INTO session_trainers (session_id, trainer_id) VALUES (?,?)",
                   (session_id, tid))
    db.execute("UPDATE course_sessions SET trainer_id = ? WHERE id = ?",
               (trainer_ids[0] if trainer_ids else None, session_id))

SORTABLE_COLUMNS = {
    "course": "c.title",
    "date": "cs.start_date",
    "venue": "cs.venue",
    "trainer": "t.name",
    "status": "cs.status",
}


def _attendance_dir(session_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "sessions", str(session_id))
    os.makedirs(path, exist_ok=True)
    return path


def _handle_banner_upload(session_id):
    file_storage = request.files.get("training_banner_file")
    if not file_storage or not file_storage.filename:
        return
    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.IMAGE_EXTENSIONS)
    if error:
        flash(error, "danger")
        return
    safe_name = secure_filename(file_storage.filename)
    stored_name = f"banner_{uuid.uuid4().hex[:8]}_{safe_name}"
    file_storage.save(os.path.join(_attendance_dir(session_id), stored_name))
    db.execute("UPDATE course_sessions SET training_banner_file = ? WHERE id = ?", (stored_name, session_id))


def _handle_client_logo_upload(session_id):
    """Returns True if a new client logo was actually saved."""
    file_storage = request.files.get("client_logo_file")
    if not file_storage or not file_storage.filename:
        return False
    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.IMAGE_EXTENSIONS)
    if error:
        flash(error, "danger")
        return False
    safe_name = secure_filename(file_storage.filename)
    stored_name = f"clientlogo_{uuid.uuid4().hex[:8]}_{safe_name}"
    file_storage.save(os.path.join(_attendance_dir(session_id), stored_name))
    db.execute("UPDATE course_sessions SET client_logo_file = ? WHERE id = ?", (stored_name, session_id))
    return True


def _handle_jd14_upload(session_id):
    """Returns the stored filename if a new signed JD14 file was actually
    saved (vs. no file chosen, or a rejected file type), or None — so the
    caller only sends the "form ready" notification when something really
    was uploaded, and can point the AI sanity-check at the saved file."""
    file_storage = request.files.get("jd14_file")
    if not file_storage or not file_storage.filename:
        return None
    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.DEFAULT_EXTENSIONS)
    if error:
        flash(error, "danger")
        return None
    safe_name = secure_filename(file_storage.filename)
    stored_name = f"jd14_{uuid.uuid4().hex[:8]}_{safe_name}"
    file_storage.save(os.path.join(_attendance_dir(session_id), stored_name))
    db.execute("UPDATE course_sessions SET jd14_file = ? WHERE id = ?", (stored_name, session_id))
    return stored_name


def _session_start_datetime(row):
    """The exact moment a class starts — training_time's start clock time on
    start_date, or midnight if no parseable time is on file (legacy data
    from before training time was compulsory)."""
    try:
        base_date = datetime.strptime(row["start_date"], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    start_hhmm, _ = split_training_time(row["training_time"])
    if start_hhmm:
        hour, minute = (int(part) for part in start_hhmm.split(":"))
        return datetime.combine(base_date, dtime(hour, minute))
    return datetime.combine(base_date, dtime.min)


def _session_end_datetime(row):
    """The exact moment a class ends — training_time's end clock time on the
    last training day (end_date, or start_date for a single-day class), or
    the very end of that day if no parseable time is on file, so legacy
    data without a time keeps its old 'once the day has passed' behavior."""
    end_date_str = row["end_date"] or row["start_date"]
    try:
        base_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    _, end_hhmm = split_training_time(row["training_time"])
    if end_hhmm:
        hour, minute = (int(part) for part in end_hhmm.split(":"))
        return datetime.combine(base_date, dtime(hour, minute))
    return datetime.combine(base_date, dtime.max)


def _auto_advance_statuses():
    """Keeps class status in sync with the calendar, so nobody has to flip it
    by hand: a 'Scheduled' class becomes 'Ongoing' exactly when its training
    start date AND time arrive, and moves on to 'Completed' exactly when its
    (last) training day's end date AND time have passed — not just once the
    calendar date has changed. Never touches 'Cancelled', and never moves a
    class backwards. Runs once per request (cheap once a day's classes have
    already been advanced) so status is always correct wherever it's read —
    the class list, the dashboard, the public Return Attendance flow, etc.

    start_date/training_time are wall-clock times as staff enter them —
    always Malaysia time, since that's where every class happens — so
    "now" here has to be actual current time in Malaysia too, not whatever
    timezone the server's own clock happens to be set to (a VPS defaults
    to UTC unless someone changes it, which would otherwise flip classes
    to Ongoing/Completed 8 hours too early)."""
    now = datetime.now(APP_TZ).replace(tzinfo=None)
    today = now.date().isoformat()

    scheduled_rows = db.query(
        "SELECT id, start_date, end_date, training_time FROM course_sessions "
        "WHERE status = 'Scheduled' AND start_date <= ?",
        (today,),
    )
    to_ongoing_ids = [row["id"] for row in scheduled_rows
                       if (start_dt := _session_start_datetime(row)) and now >= start_dt]
    if to_ongoing_ids:
        placeholders = ",".join("?" * len(to_ongoing_ids))
        db.execute(f"UPDATE course_sessions SET status = 'Ongoing' WHERE id IN ({placeholders})", to_ongoing_ids)

    candidate_rows = db.query(
        "SELECT id, start_date, end_date, training_time FROM course_sessions "
        "WHERE status IN ('Scheduled', 'Ongoing') AND COALESCE(end_date, start_date) <= ?",
        (today,),
    )
    newly_completed_ids = [row["id"] for row in candidate_rows
                            if (end_dt := _session_end_datetime(row)) and now >= end_dt]
    if newly_completed_ids:
        placeholders = ",".join("?" * len(newly_completed_ids))
        db.execute(f"UPDATE course_sessions SET status = 'Completed' WHERE id IN ({placeholders})",
                   newly_completed_ids)
    for session_id in newly_completed_ids:
        _notify_trainers_invoice_due(session_id)


EVALUATION_REPORT_REMINDER_AFTER_DAYS = 21


def _notify_overdue_evaluation_reports():
    """Once a class has been Completed for over
    EVALUATION_REPORT_REMINDER_AFTER_DAYS days with no evaluation report
    uploaded yet, nudge its creator/owner with a Notification. Never sends
    anything by itself — sending the report to the client is always a
    separate, manual click (see send_evaluation_email)."""
    cutoff = (date.today() - timedelta(days=EVALUATION_REPORT_REMINDER_AFTER_DAYS)).isoformat()
    overdue_rows = db.query(
        """SELECT cs.id, cs.owner_user_id, c.title AS course_title
           FROM course_sessions cs JOIN courses c ON c.id = cs.course_id
           WHERE cs.status = 'Completed' AND (cs.evaluation_report_file IS NULL OR cs.evaluation_report_file = '')
             AND COALESCE(cs.end_date, cs.start_date) <= ?""",
        (cutoff,),
    )
    for row in overdue_rows:
        notifications.notify(
            row["owner_user_id"], "evaluation_report_overdue",
            f"Evaluation report still missing — {row['course_title']}",
            body=f"This class was completed over {EVALUATION_REPORT_REMINDER_AFTER_DAYS} days ago and its "
                 "evaluation report hasn't been uploaded yet.",
            link=url_for("sessions.view", session_id=row["id"]),
            dedupe_key=f"session:{row['id']}:eval_report_overdue",
        )


GRANT_DOCS_LEAD_DAYS = 21        # nudge starts this many days before training —
                                  # a buffer ahead of HRDC's own ~14-day deadline
                                  # so the client isn't rushed on their application
GRANT_DOCS_URGENT_DAYS = 7
GRANT_DOCS_CRITICAL_DAYS = 1


def _notify_pending_grant_docs():
    """Nudges a class's owner if the HRDCorp Grant Documents pack still
    hasn't been sent (grant_docs_sent_at IS NULL) as training approaches.
    Escalates through three tiers (21/7/1 days left) plus a final "missed"
    flag once training has already started, each dedupe_key-gated to fire
    at most once per class/tier — a handful of increasingly urgent nudges
    rather than one easy-to-miss ping or a daily spam loop. Runs on every
    request (same pattern as _notify_overdue_evaluation_reports above), so
    a class added last-minute — already inside the window the day it's
    created — is flagged immediately rather than waiting for a calendar
    date that's already in the past."""
    today = date.today()
    rows = db.query(
        """SELECT cs.id, cs.owner_user_id, cs.start_date, c.title AS course_title
           FROM course_sessions cs JOIN courses c ON c.id = cs.course_id
           WHERE cs.status != 'Cancelled'
             AND (cs.grant_docs_sent_at IS NULL OR cs.grant_docs_sent_at = '')
             AND cs.start_date IS NOT NULL AND cs.start_date != ''"""
    )
    for row in rows:
        try:
            start = date.fromisoformat(row["start_date"][:10])
        except ValueError:
            continue
        days_left = (start - today).days
        if days_left > GRANT_DOCS_LEAD_DAYS:
            continue
        if days_left > GRANT_DOCS_URGENT_DAYS:
            tier, title, body = (
                "21",
                f"Send Grant Documents soon — {row['course_title']}",
                f"HRDC recommends sending the Grant Documents pack well before training so the client has "
                f"time to apply. {days_left} day(s) left and it hasn't been sent yet.",
            )
        elif days_left > GRANT_DOCS_CRITICAL_DAYS:
            tier, title, body = (
                "7",
                f"Grant Documents still not sent — {row['course_title']}",
                f"Only {days_left} day(s) left until training and the Grant Documents pack still hasn't gone out.",
            )
        elif days_left >= 0:
            tier, title, body = (
                "1",
                f"Urgent: Grant Documents not sent — {row['course_title']}",
                f"Training starts in {days_left} day(s) and the Grant Documents pack still hasn't been sent.",
            )
        else:
            tier, title, body = (
                "missed",
                f"Grant Documents were never sent — {row['course_title']}",
                "Training has already started and this class's Grant Documents pack was never sent — "
                "check with the client directly if their grant is affected.",
            )
        notifications.notify(
            row["owner_user_id"], "grant_docs_pending", title, body=body,
            link=url_for("sessions.view", session_id=row["id"]),
            dedupe_key=f"session:{row['id']}:grantdocs:{tier}",
        )


@bp.before_app_request
def _sync_session_statuses():
    try:
        _auto_advance_statuses()
    except Exception:  # noqa: BLE001 - never let this housekeeping break a request
        current_app.logger.exception("Failed to auto-advance class statuses")
    try:
        _notify_overdue_evaluation_reports()
    except Exception:  # noqa: BLE001 - never let this housekeeping break a request
        current_app.logger.exception("Failed to check for overdue evaluation reports")
    try:
        _notify_pending_grant_docs()
    except Exception:  # noqa: BLE001 - never let this housekeeping break a request
        current_app.logger.exception("Failed to check for pending grant documents")


@bp.route("/")
@login_required
def index():
    sort = request.args.get("sort", "date")
    direction = request.args.get("dir", "asc")
    if sort not in SORTABLE_COLUMNS:
        sort = "date"
    if direction not in ("asc", "desc"):
        direction = "asc"

    sessions = db.query(
        f"""SELECT cs.*, c.title AS course_title, c.code AS course_code, c.hrdf_claimable,
                  t.name AS trainer_name, co.name AS client_name,
                  (SELECT COUNT(*) FROM enrollments e WHERE e.session_id = cs.id) AS enrolled_count
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN trainers t ON t.id = cs.trainer_id
           LEFT JOIN companies co ON co.id = cs.client_company_id
           ORDER BY {SORTABLE_COLUMNS[sort]} {direction.upper()}, cs.start_date ASC"""
    )

    # Also hand the template plain JSON-able event data for the calendar view.
    # FullCalendar treats an all-day event's "end" as EXCLUSIVE (the event
    # visually spans up to, but not including, that date) — so a 2-day
    # class running 29-30 Aug, passed straight through as end_date=30 Aug,
    # rendered as covering only the 29th. Add one day to the last actual
    # training day so the event correctly spans through it.
    def _calendar_end(s):
        last_day = s["end_date"] or s["start_date"]
        try:
            return (date.fromisoformat(last_day) + timedelta(days=1)).isoformat()
        except (TypeError, ValueError):
            return last_day

    calendar_events = [
        {
            "id": s["id"],
            "title": f"{s['course_title']} ({s['status']})",
            "start": s["start_date"],
            "end": _calendar_end(s),
            "url": url_for("sessions.view", session_id=s["id"]),
            "color": {
                "Proposed": "#8a8a8a",
                "Completed": "#198754",
                "Cancelled": "#dc3545",
                "Ongoing": "#fbaf17",
            }.get(s["status"], "#0c45a6"),
        }
        for s in sessions
    ]

    return render_template("sessions/list.html", sessions=sessions, sort=sort, direction=direction,
                            calendar_events=calendar_events)


@bp.route("/export")
@admin_required
def export():
    sort = request.args.get("sort", "date")
    direction = request.args.get("dir", "asc")
    if sort not in SORTABLE_COLUMNS:
        sort = "date"
    if direction not in ("asc", "desc"):
        direction = "asc"
    sessions_rows = db.query(
        f"""SELECT cs.*, c.title AS course_title, t.name AS trainer_name, co.name AS client_name,
                  (SELECT COUNT(*) FROM enrollments e WHERE e.session_id = cs.id) AS enrolled_count
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN trainers t ON t.id = cs.trainer_id
           LEFT JOIN companies co ON co.id = cs.client_company_id
           ORDER BY {SORTABLE_COLUMNS[sort]} {direction.upper()}, cs.start_date ASC"""
    )
    rows = (
        (s["course_title"], s["start_date"], s["end_date"] or "", s["venue"] or "",
         s["trainer_name"] or "", s["client_name"] or "", s["status"], s["training_mode"],
         s["training_type"] or "", s["capacity"], s["enrolled_count"])
        for s in sessions_rows
    )
    return csv_response(
        "classes.csv",
        ["Course", "Start Date", "End Date", "Venue", "Trainer", "Client", "Status", "Training Mode",
         "Training Type", "Capacity", "Enrolled"],
        rows,
    )


@bp.route("/new", methods=("GET", "POST"))
@login_required
def new():
    courses = db.query("SELECT id, title FROM courses WHERE active = 1 ORDER BY title")
    trainers = db.query("SELECT id, name FROM trainers ORDER BY name")
    companies = db.query("SELECT id, name FROM companies ORDER BY name")
    leads = _leads_for_form()
    preselect_course = request.args.get("course_id", type=int)

    if request.method == "POST":
        course_id = request.form.get("course_id")
        start_date = request.form.get("start_date")
        trainer_ids = [int(t) for t in request.form.getlist("trainer_ids") if t]
        training_time = format_training_time(
            request.form.get("training_time_start"), request.form.get("training_time_end")
        )
        if not course_id or not start_date:
            flash("Course and start date are required.", "danger")
        elif not training_time:
            flash("Training time (start and end) is required.", "danger")
        else:
            status = request.form.get("status") or "Scheduled"
            requires_laptop_rental = 1 if request.form.get("requires_laptop_rental") else 0
            laptop_rental_qty = request.form.get("laptop_rental_qty") or None
            if not requires_laptop_rental:
                laptop_rental_qty = None
            has_exam = 1 if request.form.get("has_exam") else 0
            exam_participants = request.form.get("exam_participants") or None
            if not has_exam:
                exam_participants = None
            sid = db.execute(
                """INSERT INTO course_sessions (course_id, trainer_id, client_company_id, pic_lead_id,
                       venue, start_date, end_date, training_time, training_type, training_mode,
                       meeting_link, capacity, status, notes, evaluation_form_link, session_code,
                       requires_laptop_rental, laptop_rental_qty, has_exam, exam_participants,
                       room_setup, owner_user_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    course_id,
                    trainer_ids[0] if trainer_ids else None,
                    request.form.get("client_company_id") or None,
                    request.form.get("pic_lead_id") or None,
                    request.form.get("venue") or None,
                    start_date,
                    request.form.get("end_date") or None,
                    training_time,
                    request.form.get("training_type") or None,
                    request.form.get("training_mode") or "Physical",
                    request.form.get("meeting_link") or None,
                    request.form.get("capacity") or 20,
                    status,
                    request.form.get("notes") or None,
                    request.form.get("evaluation_form_link") or None,
                    db.generate_session_code(),
                    requires_laptop_rental,
                    laptop_rental_qty,
                    has_exam,
                    exam_participants,
                    request.form.get("room_setup") or None,
                    g.user["id"],
                ),
            )
            _set_session_trainers(sid, trainer_ids)
            _handle_banner_upload(sid)
            course = db.query("SELECT title FROM courses WHERE id = ?", (course_id,), one=True)
            activity.log("create", "session", sid,
                          f"Scheduled class {course['title'] if course else ''} ({start_date})".strip())
            if status == "Scheduled":
                try:
                    from . import calendar_integration
                    new_row = db.query("SELECT cs.*, c.title AS course_title FROM course_sessions cs "
                                        "JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?", (sid,), one=True)
                    calendar_integration.block_calendar_for_session(new_row)
                except Exception:  # noqa: BLE001 - calendar blocking must never break saving a class
                    current_app.logger.exception("Failed to block calendar for new session %s", sid)
            flash("Training session scheduled.", "success")
            return redirect(url_for("sessions.view", session_id=sid))

    return render_template("sessions/form.html", session=None, courses=courses, trainers=trainers,
                            companies=companies, leads=leads, statuses=STATUSES,
                            training_modes=TRAINING_MODES, training_types=TRAINING_TYPES,
                            room_setup_options=ROOM_SETUP_OPTIONS,
                            preselect_course=preselect_course,
                            selected_trainer_ids=[], training_time_start="", training_time_end="")


@bp.route("/<int:session_id>")
@login_required
def view(session_id):
    session_row = db.query(
        """SELECT cs.*, c.title AS course_title,
                  CASE WHEN cs.training_type = 'Public Training' THEN c.price_public ELSE c.price_inhouse END
                      AS course_price,
                  c.price_inhouse AS course_price_inhouse, c.price_public AS course_price_public,
                  c.hrdf_claimable, c.hrdcorp_programme_no, c.outline_file AS course_outline_file,
                  t.name AS trainer_name, t.email AS trainer_email, cl.name AS client_name, cl.email AS client_email,
                  pic.name AS pic_name, pic.email AS pic_email, owner.name AS created_by_name,
                  t.profile_file AS trainer_profile_file, t.accredited_cert_file AS trainer_accredited_cert_file
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN trainers t ON t.id = cs.trainer_id
           LEFT JOIN companies cl ON cl.id = cs.client_company_id
           LEFT JOIN leads pic ON pic.id = cs.pic_lead_id
           LEFT JOIN users owner ON owner.id = cs.owner_user_id
           WHERE cs.id = ?""",
        (session_id,), one=True,
    )
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))

    enrollments = db.query(
        """SELECT e.*, co.name AS company_name FROM enrollments e
           LEFT JOIN companies co ON co.id = e.company_id
           WHERE e.session_id = ? ORDER BY e.created_at""",
        (session_id,),
    )
    assigned_trainers = db.query(
        """SELECT t.id, t.name FROM session_trainers st
           JOIN trainers t ON t.id = st.trainer_id
           WHERE st.session_id = ? ORDER BY t.name""",
        (session_id,),
    )
    attendance_returns = db.query(
        "SELECT * FROM attendance_returns WHERE session_id = ? ORDER BY created_at DESC", (session_id,)
    )
    t3_token = ensure_t3_public_token(session_id)
    t3_url = url_for("t3_public.form", token=t3_token, _external=True)
    grant_docs_token = ensure_grant_docs_token(session_id)
    grant_docs_url = url_for("hrdcorp_grant.form", token=grant_docs_token, _external=True)
    jd14_return_token = ensure_jd14_return_token(session_id)
    jd14_return_url = url_for("jd14_return.details", token=jd14_return_token, _external=True)
    return render_template("sessions/view.html", s=session_row, enrollments=enrollments,
                            mail_configured=mailer.is_configured(), assigned_trainers=assigned_trainers,
                            attendance_returns=attendance_returns, t3_url=t3_url,
                            ai_configured=ai_match.is_configured(),
                            grant_docs_url=grant_docs_url,
                            default_grant_email_subject=_default_grant_email_subject(session_row),
                            default_grant_email_body=_default_grant_email_body(session_row, grant_docs_url),
                            default_t3_email_subject=_default_t3_email_subject(session_row),
                            default_t3_email_body=_default_t3_email_body(session_row, t3_url),
                            default_evaluation_email_subject=_default_evaluation_email_subject(session_row),
                            default_evaluation_email_body=_default_evaluation_email_body(session_row),
                            default_jd14_email_subject=_default_jd14_email_subject(session_row),
                            default_jd14_email_body=_default_jd14_email_body(session_row),
                            default_jd14_return_email_subject=_default_jd14_return_email_subject(session_row),
                            default_jd14_return_email_body=_default_jd14_return_email_body(session_row, jd14_return_url))


@bp.route("/<int:session_id>/trainer-invoice-documents/<int:doc_id>/download")
@login_required
def download_trainer_invoice_document(session_id, doc_id):
    from .trainer_invoice import _upload_dir as _trainer_invoice_upload_dir
    doc = db.query("SELECT * FROM trainer_invoice_documents WHERE id = ? AND session_id = ?",
                    (doc_id, session_id), one=True)
    if doc is None:
        flash("Document not found.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))
    return send_from_directory(_trainer_invoice_upload_dir(session_id), doc["filename"], as_attachment=False,
                                download_name=doc["original_name"])


def _training_days_for_session(session_row):
    """Multi-day training needs its own signed sheet per day (participants
    sign a fresh page each day they attend) — one form set per calendar
    day between start_date and end_date inclusive, each dated e.g.
    "13 Sep 2026 (Day 1)" so it's clear which day's sheet it is."""
    import datetime as _dt
    start = _dt.datetime.strptime(session_row["start_date"], "%Y-%m-%d").date()
    end = start
    if session_row["end_date"]:
        try:
            end = _dt.datetime.strptime(session_row["end_date"], "%Y-%m-%d").date()
        except ValueError:
            end = start
    if end < start:
        end = start
    num_days = (end - start).days + 1
    return [start + _dt.timedelta(days=i) for i in range(num_days)]


def _t3_form_session_and_participants(session_id):
    """Shared lookup for the printable T3 form's page + its "Email to
    Trainer" action — both need the same session (with trainer info),
    participant list, and per-day breakdown."""
    session_row = db.query(
        """SELECT cs.*, c.title AS course_title, t.name AS trainer_name, t.email AS trainer_email
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN trainers t ON t.id = cs.trainer_id
           WHERE cs.id = ?""",
        (session_id,), one=True,
    )
    if session_row is None:
        return None, None, None
    participants = db.query(
        "SELECT * FROM t3_participants WHERE session_id = ? ORDER BY id", (session_id,)
    )
    training_days = _training_days_for_session(session_row)
    return session_row, participants, training_days


@bp.route("/<int:session_id>/t3-attendance")
@login_required
def t3_attendance_form(session_id):
    session_row, participants, training_days = _t3_form_session_and_participants(session_id)
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))

    # Extra blank rows on top of the usual minimum-6-rows padding — for
    # walk-in participants to fill in by hand on the printed sheet. A plain
    # GET query param (not stored) so it only affects this viewing/printing,
    # never the saved participant list.
    extra_blank_rows = max(0, min(request.args.get("extra_blank_rows", 0, type=int) or 0, 50))

    return render_template("sessions/t3_attendance_form.html", s=session_row, participants=participants,
                            training_days=training_days, mail_configured=mailer.is_configured(),
                            default_t3_form_email_subject=_default_t3_form_email_subject(session_row),
                            default_t3_form_email_body=_default_t3_form_email_body(session_row),
                            t3_form_pdf_filename=_t3_form_pdf_filename(session_row),
                            extra_blank_rows=extra_blank_rows)


@bp.route("/<int:session_id>/email-t3-form", methods=("POST",))
@login_required
def email_t3_form(session_id):
    """Emails the actual printable T3 Attendance Form (a PDF attachment,
    generated fresh from the current participant list) straight to the
    trainer — for when the client hasn't filled in the online version and
    the trainer needs to print a physical copy and get it signed manually.
    Distinct from send_t3_form above, which emails a link to the online
    form (usually to the client's PIC) rather than a PDF (usually to the
    trainer)."""
    session_row, participants, training_days = _t3_form_session_and_participants(session_id)
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))

    to_email = (request.form.get("to_email") or session_row["trainer_email"] or "").strip()
    if not to_email:
        flash("No trainer email on file for this class — assign a trainer on the class's Edit page, "
              "or type an address to send to.", "danger")
        return redirect(url_for("sessions.t3_attendance_form", session_id=session_id))

    subject = (request.form.get("subject") or "").strip() or _default_t3_form_email_subject(session_row)
    body = (request.form.get("body") or "").strip() or _default_t3_form_email_body(session_row)
    extra_blank_rows = max(0, min(request.form.get("extra_blank_rows", 0, type=int) or 0, 50))
    cc_email = (request.form.get("cc_email") or "").strip() or None

    try:
        pdf_bytes = pdfgen.generate_t3_form_pdf(session_row, participants, training_days,
                                                 extra_blank_rows=extra_blank_rows)
        attachments = [(_t3_form_pdf_filename(session_row), pdf_bytes, "application/pdf")]
        mailer.send_email(to_email, subject, body, attachments=attachments,
                           related_type="course_session", related_id=session_id, cc_email=cc_email)
    except mailer.MailNotConfigured as exc:
        flash(str(exc), "danger")
        return redirect(url_for("sessions.t3_attendance_form", session_id=session_id))
    except mailer.MailSendError as exc:
        flash(f"Email failed to send: {exc}", "danger")
        return redirect(url_for("sessions.t3_attendance_form", session_id=session_id))
    except Exception:  # noqa: BLE001 - surface a clean message rather than a 500
        current_app.logger.exception("Failed to generate/send T3 form PDF for session %s", session_id)
        flash("Couldn't generate the T3 form PDF — is wkhtmltopdf installed on the server?", "danger")
        return redirect(url_for("sessions.t3_attendance_form", session_id=session_id))

    activity.log("send_email", "session", session_id, f"Emailed printable T3 Attendance Form to {to_email}")
    flash(f"T3 Attendance Form emailed to {to_email}.", "success")
    return redirect(url_for("sessions.t3_attendance_form", session_id=session_id))


@bp.route("/<int:session_id>/send-t3-form", methods=("POST",))
@login_required
def send_t3_form(session_id):
    """Emails the public, always-editable T3 Attendance Form link — defaults
    to the assigned trainer's address, but any address can be typed in
    instead (e.g. to send it straight to the client)."""
    session_row = db.query(
        """SELECT cs.*, c.title AS course_title, t.name AS trainer_name, t.email AS trainer_email,
                  pic.name AS pic_name, pic.email AS pic_email
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN trainers t ON t.id = cs.trainer_id
           LEFT JOIN leads pic ON pic.id = cs.pic_lead_id
           WHERE cs.id = ?""",
        (session_id,), one=True,
    )
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))

    to_email = (request.form.get("to_email") or session_row["pic_email"] or "").strip()
    if not to_email:
        flash("No PIC email on file for this class — select a PIC on the class's Edit page, "
              "or type an address to send to.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))

    t3_token = ensure_t3_public_token(session_id)
    t3_url = url_for("t3_public.form", token=t3_token, _external=True)
    subject = (request.form.get("subject") or "").strip() or _default_t3_email_subject(session_row)
    body = (request.form.get("body") or "").strip() or _default_t3_email_body(session_row, t3_url)
    cc_email = (request.form.get("cc_email") or "").strip() or None

    try:
        mailer.send_email(to_email, subject, body, related_type="course_session", related_id=session_id,
                           cc_email=cc_email)
    except mailer.MailNotConfigured as exc:
        flash(str(exc), "danger")
        return redirect(url_for("sessions.view", session_id=session_id))
    except mailer.MailSendError as exc:
        flash(f"Email failed to send: {exc}", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))

    activity.log("send_email", "session", session_id, f"Sent T3 Attendance Form link to {to_email}")
    flash(f"T3 Attendance Form link sent to {to_email}.", "success")
    return redirect(url_for("sessions.view", session_id=session_id))


@bp.route("/<int:session_id>/edit", methods=("GET", "POST"))
@login_required
def edit(session_id):
    session_row = db.query("SELECT * FROM course_sessions WHERE id = ?", (session_id,), one=True)
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))
    courses = db.query("SELECT id, title FROM courses ORDER BY title")
    trainers = db.query("SELECT id, name FROM trainers ORDER BY name")
    companies = db.query("SELECT id, name FROM companies ORDER BY name")
    leads = _leads_for_form()

    if request.method == "POST":
        course_id = request.form.get("course_id")
        start_date = request.form.get("start_date")
        trainer_ids = [int(t) for t in request.form.getlist("trainer_ids") if t]
        training_time = format_training_time(
            request.form.get("training_time_start"), request.form.get("training_time_end")
        )
        if not course_id or not start_date:
            flash("Course and start date are required.", "danger")
        elif not training_time:
            flash("Training time (start and end) is required.", "danger")
        else:
            previous_status = session_row["status"]
            new_status = request.form.get("status") or "Scheduled"
            requires_laptop_rental = 1 if request.form.get("requires_laptop_rental") else 0
            laptop_rental_qty = request.form.get("laptop_rental_qty") or None
            if not requires_laptop_rental:
                laptop_rental_qty = None
            db.execute(
                """UPDATE course_sessions SET course_id=?, trainer_id=?, client_company_id=?, pic_lead_id=?,
                       venue=?, start_date=?, end_date=?, training_time=?, training_type=?,
                       training_mode=?, meeting_link=?, capacity=?, status=?, notes=?, evaluation_form_link=?,
                       requires_laptop_rental=?, laptop_rental_qty=?, room_setup=?
                   WHERE id=?""",
                (
                    course_id,
                    trainer_ids[0] if trainer_ids else None,
                    request.form.get("client_company_id") or None,
                    request.form.get("pic_lead_id") or None,
                    request.form.get("venue") or None,
                    start_date,
                    request.form.get("end_date") or None,
                    training_time,
                    request.form.get("training_type") or None,
                    request.form.get("training_mode") or "Physical",
                    request.form.get("meeting_link") or None,
                    request.form.get("capacity") or 20,
                    new_status,
                    request.form.get("notes") or None,
                    # No longer edited on this form — it now lives on the
                    # Training Evaluation QR Poster card (Class details
                    # page) instead, set together with generating the
                    # poster. Preserve whatever's already there so saving
                    # this form (for an unrelated field) can't wipe it.
                    session_row["evaluation_form_link"],
                    requires_laptop_rental,
                    laptop_rental_qty,
                    request.form.get("room_setup") or None,
                    session_id,
                ),
            )
            _set_session_trainers(session_id, trainer_ids)
            _handle_banner_upload(session_id)
            course = db.query("SELECT title FROM courses WHERE id = ?", (course_id,), one=True)
            activity.log("update", "session", session_id,
                          f"Updated class {course['title'] if course else ''} ({start_date})".strip())
            if new_status == "Scheduled" and previous_status != "Scheduled":
                try:
                    from . import calendar_integration
                    updated_row = db.query(
                        "SELECT cs.*, c.title AS course_title FROM course_sessions cs "
                        "JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?", (session_id,), one=True)
                    calendar_integration.block_calendar_for_session(updated_row)
                except Exception:  # noqa: BLE001 - calendar blocking must never break saving a class
                    current_app.logger.exception("Failed to block calendar for session %s", session_id)
            if new_status == "Completed" and previous_status != "Completed":
                _notify_trainers_invoice_due(session_id)
            flash("Training session updated.", "success")
            return redirect(url_for("sessions.view", session_id=session_id))

    selected_trainer_ids = [row["trainer_id"] for row in db.query(
        "SELECT trainer_id FROM session_trainers WHERE session_id = ?", (session_id,)
    )]
    if not selected_trainer_ids and session_row["trainer_id"]:
        selected_trainer_ids = [session_row["trainer_id"]]
    time_start, time_end = split_training_time(session_row["training_time"])
    return render_template("sessions/form.html", session=session_row, courses=courses, trainers=trainers,
                            companies=companies, leads=leads, statuses=STATUSES,
                            training_modes=TRAINING_MODES, training_types=TRAINING_TYPES, preselect_course=None,
                            room_setup_options=ROOM_SETUP_OPTIONS,
                            selected_trainer_ids=selected_trainer_ids,
                            training_time_start=time_start, training_time_end=time_end)


@bp.route("/<int:session_id>/attendance", methods=("POST",))
@login_required
def upload_attendance(session_id):
    session_row = db.query("SELECT id FROM course_sessions WHERE id = ?", (session_id,), one=True)
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))

    file_storage = request.files.get("attendance_file")
    if not file_storage or not file_storage.filename:
        flash("Choose a file to upload first.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))

    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.DEFAULT_EXTENSIONS)
    if error:
        flash(error, "danger")
        return redirect(url_for("sessions.view", session_id=session_id))

    safe_name = secure_filename(file_storage.filename)
    stored_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
    saved_path = os.path.join(_attendance_dir(session_id), stored_name)
    file_storage.save(saved_path)
    db.execute("UPDATE course_sessions SET attendance_file = ? WHERE id = ?", (stored_name, session_id))
    _notify_document_uploaded(session_id, "Signed T3 Attendance Form")
    flash("Attendance sheet uploaded.", "success")
    warning = doc_sanity.check_document(saved_path, "t3_attendance")
    if warning:
        flash(warning, "warning")
    return redirect(url_for("sessions.view", session_id=session_id))


@bp.route("/<int:session_id>/attendance/download")
@login_required
def download_attendance(session_id):
    session_row = db.query("SELECT attendance_file FROM course_sessions WHERE id = ?",
                            (session_id,), one=True)
    if session_row is None or not session_row["attendance_file"]:
        flash("No attendance sheet uploaded yet.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))
    return send_from_directory(_attendance_dir(session_id), session_row["attendance_file"], as_attachment=False)


@bp.route("/<int:session_id>/returns/<int:return_id>/download")
@login_required
def download_return(session_id, return_id):
    """Views one photo a trainer submitted through the public Return
    Attendance Form page (see attendance_return.py)."""
    ret = db.query("SELECT * FROM attendance_returns WHERE id = ? AND session_id = ?",
                    (return_id, session_id), one=True)
    if ret is None:
        flash("That submitted photo wasn't found.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))
    return send_from_directory(_attendance_dir(session_id), ret["filename"], as_attachment=False,
                                download_name=ret["original_name"] or ret["filename"])


@bp.route("/<int:session_id>/grant-documents/upload", methods=("POST",))
@login_required
def upload_grant_quotation(session_id):
    """Uploads the one manually-supplied item in the HRDCorp Grant Documents
    pack — the other three (Course Outline, Trainer Profile, Accredited
    Certificate) are auto-derived from the Course/Trainer modules and never
    stored here."""
    session_row = db.query("SELECT id FROM course_sessions WHERE id = ?", (session_id,), one=True)
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))

    file_storage = request.files.get("grant_quotation_file")
    if not file_storage or not file_storage.filename:
        flash("Choose a file to upload first.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))

    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.DEFAULT_EXTENSIONS)
    if error:
        flash(error, "danger")
        return redirect(url_for("sessions.view", session_id=session_id))

    safe_name = secure_filename(file_storage.filename)
    stored_name = f"grantquote_{uuid.uuid4().hex[:8]}_{safe_name}"
    saved_path = os.path.join(_attendance_dir(session_id), stored_name)
    file_storage.save(saved_path)
    db.execute("UPDATE course_sessions SET grant_quotation_file = ? WHERE id = ?", (stored_name, session_id))
    flash("Quotation uploaded for the HRDCorp Grant Documents pack.", "success")
    warning = doc_sanity.check_document(saved_path, "grant_quotation")
    if warning:
        flash(warning, "warning")
    return redirect(url_for("sessions.view", session_id=session_id))


@bp.route("/<int:session_id>/grant-documents/quotation/download")
@login_required
def download_grant_quotation(session_id):
    session_row = db.query("SELECT grant_quotation_file FROM course_sessions WHERE id = ?", (session_id,), one=True)
    if session_row is None or not session_row["grant_quotation_file"]:
        flash("No quotation uploaded yet for the HRDCorp Grant Documents pack.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))
    return send_from_directory(_attendance_dir(session_id), session_row["grant_quotation_file"], as_attachment=False)


@bp.route("/<int:session_id>/grant-documents/send-email", methods=("POST",))
@login_required
def send_grant_documents_email(session_id):
    """Manually triggered only — attaches whichever of the 4 HRDCorp Grant
    Documents are on file (Course Outline from the Course module, Trainer
    Profile and Accredited Certificate from the Trainer module, and the
    manually-uploaded Quotation here) and emails them to the client along
    with the public link to submit their HRDCorp Grant ID once approved."""
    session_row = db.query(
        """SELECT cs.*, c.title AS course_title, c.outline_file AS course_outline_file,
                  c.hrdcorp_programme_no,
                  t.profile_file AS trainer_profile_file, t.accredited_cert_file AS trainer_accredited_cert_file,
                  cl.email AS client_email, pic.name AS pic_name, pic.email AS pic_email
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN trainers t ON t.id = cs.trainer_id
           LEFT JOIN companies cl ON cl.id = cs.client_company_id
           LEFT JOIN leads pic ON pic.id = cs.pic_lead_id
           WHERE cs.id = ?""",
        (session_id,), one=True,
    )
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))

    to_email = (request.form.get("to_email") or session_row["pic_email"] or session_row["client_email"] or "").strip()
    if not to_email:
        flash("No client email on file for this class — add one, or type an address to send to.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))

    grant_docs_token = ensure_grant_docs_token(session_id)
    grant_docs_url = url_for("hrdcorp_grant.form", token=grant_docs_token, _external=True)
    subject = (request.form.get("subject") or "").strip() or _default_grant_email_subject(session_row)
    body = (request.form.get("body") or "").strip() or _default_grant_email_body(session_row, grant_docs_url)
    cc_email = (request.form.get("cc_email") or "").strip() or None

    attachments = []
    missing = []
    candidates = [
        ("Course Outline", session_row["course_outline_file"],
         os.path.join(current_app.config["UPLOAD_FOLDER"], "courses", str(session_row["course_id"]))),
        ("Trainer Profile", session_row["trainer_profile_file"],
         os.path.join(current_app.config["UPLOAD_FOLDER"], "trainers", str(session_row["trainer_id"]))
         if session_row["trainer_id"] else None),
        ("Accredited Certificate", session_row["trainer_accredited_cert_file"],
         os.path.join(current_app.config["UPLOAD_FOLDER"], "trainers", str(session_row["trainer_id"]))
         if session_row["trainer_id"] else None),
        ("Quotation", session_row["grant_quotation_file"], _attendance_dir(session_id)),
    ]
    for label, filename, folder in candidates:
        if not filename or not folder:
            missing.append(label)
            continue
        try:
            with open(os.path.join(folder, filename), "rb") as f:
                file_bytes = f.read()
            mimetype, _ = mimetypes.guess_type(filename)
            attachments.append((filename, file_bytes, mimetype or "application/octet-stream"))
        except OSError:
            missing.append(label)

    # All 4 documents are mandatory — HRDCorp expects the complete set, so
    # sending a partial pack just creates a follow-up headache. Block the
    # send and say exactly what's still missing (and where to add it).
    if missing:
        flash(
            "Can't send yet — these HRDCorp Grant Documents aren't on file: " + ", ".join(missing) + ". "
            "Course Outline is uploaded on the Course's Edit page; Trainer Profile and Accredited "
            "Certificate on the Trainer's Edit page; Quotation right here on this class.", "danger",
        )
        return redirect(url_for("sessions.view", session_id=session_id))

    try:
        mailer.send_email(to_email, subject, body, attachments=attachments,
                           related_type="course_session", related_id=session_id, cc_email=cc_email)
    except mailer.MailNotConfigured as exc:
        flash(str(exc), "danger")
        return redirect(url_for("sessions.view", session_id=session_id))
    except mailer.MailSendError as exc:
        flash(f"Email failed to send: {exc}", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))

    db.execute(
        "UPDATE course_sessions SET grant_docs_sent_at = datetime('now'), grant_docs_sent_to = ? WHERE id = ?",
        (to_email, session_id),
    )
    activity.log("send_email", "session", session_id, f"Emailed HRDCorp Grant Documents to {to_email}")
    flash(f"HRDCorp Grant Documents emailed to {to_email}.", "success")
    return redirect(url_for("sessions.view", session_id=session_id))


@bp.route("/<int:session_id>/evaluation", methods=("POST",))
@login_required
def upload_evaluation(session_id):
    session_row = db.query("SELECT id FROM course_sessions WHERE id = ?", (session_id,), one=True)
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))

    file_storage = request.files.get("evaluation_file")
    if not file_storage or not file_storage.filename:
        flash("Choose a file to upload first.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))

    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.DEFAULT_EXTENSIONS)
    if error:
        flash(error, "danger")
        return redirect(url_for("sessions.view", session_id=session_id))

    safe_name = secure_filename(file_storage.filename)
    stored_name = f"eval_{uuid.uuid4().hex[:8]}_{safe_name}"
    saved_path = os.path.join(_attendance_dir(session_id), stored_name)
    file_storage.save(saved_path)
    db.execute("UPDATE course_sessions SET evaluation_report_file = ? WHERE id = ?", (stored_name, session_id))
    _notify_document_uploaded(session_id, "Evaluation Report")
    flash("Evaluation report uploaded.", "success")
    warning = doc_sanity.check_document(saved_path, "evaluation_report")
    if warning:
        flash(warning, "warning")
    return redirect(url_for("sessions.view", session_id=session_id))


@bp.route("/<int:session_id>/evaluation/send-email", methods=("POST",))
@login_required
def send_evaluation_email(session_id):
    """Emails the uploaded evaluation report to the client — always a
    manual, staff-clicked action (editable subject/body/recipient), never
    sent automatically. Distinct from the 21-day overdue Notification
    reminder (_notify_overdue_evaluation_reports), which only nudges staff
    to come send/upload it, never sends anything on its own."""
    session_row = db.query(
        """SELECT cs.*, c.title AS course_title, cl.email AS client_email, pic.name AS pic_name,
                  pic.email AS pic_email
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN companies cl ON cl.id = cs.client_company_id
           LEFT JOIN leads pic ON pic.id = cs.pic_lead_id
           WHERE cs.id = ?""",
        (session_id,), one=True,
    )
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))
    if not session_row["evaluation_report_file"]:
        flash("Upload the evaluation report first, then send it.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))

    to_email = (request.form.get("to_email") or session_row["pic_email"] or session_row["client_email"] or "").strip()
    if not to_email:
        flash("No client email on file for this class — add one, or type an address to send to.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))

    subject = (request.form.get("subject") or "").strip() or _default_evaluation_email_subject(session_row)
    body = (request.form.get("body") or "").strip() or _default_evaluation_email_body(session_row)
    cc_email = (request.form.get("cc_email") or "").strip() or None

    try:
        report_path = os.path.join(_attendance_dir(session_id), session_row["evaluation_report_file"])
        with open(report_path, "rb") as f:
            file_bytes = f.read()
        mimetype, _ = mimetypes.guess_type(session_row["evaluation_report_file"])
        attachments = [(session_row["evaluation_report_file"], file_bytes, mimetype or "application/octet-stream")]
        mailer.send_email(to_email, subject, body, attachments=attachments,
                           related_type="course_session", related_id=session_id, cc_email=cc_email)
    except mailer.MailNotConfigured as exc:
        flash(str(exc), "danger")
        return redirect(url_for("sessions.view", session_id=session_id))
    except mailer.MailSendError as exc:
        flash(f"Email failed to send: {exc}", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))
    except OSError:
        current_app.logger.exception("Failed to read evaluation report file for session %s", session_id)
        flash("Couldn't read the uploaded evaluation report file.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))

    db.execute(
        "UPDATE course_sessions SET evaluation_sent_at = datetime('now'), evaluation_sent_to = ? WHERE id = ?",
        (to_email, session_id),
    )
    activity.log("send_email", "session", session_id, f"Emailed Evaluation Report to {to_email}")
    flash(f"Evaluation report emailed to {to_email}.", "success")
    return redirect(url_for("sessions.view", session_id=session_id))


@bp.route("/<int:session_id>/evaluation/download")
@login_required
def download_evaluation(session_id):
    session_row = db.query("SELECT evaluation_report_file FROM course_sessions WHERE id = ?",
                            (session_id,), one=True)
    if session_row is None or not session_row["evaluation_report_file"]:
        flash("No evaluation report uploaded yet.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))
    return send_from_directory(_attendance_dir(session_id), session_row["evaluation_report_file"], as_attachment=False)


@bp.route("/<int:session_id>/banner/download")
@login_required
def download_banner(session_id):
    session_row = db.query("SELECT training_banner_file FROM course_sessions WHERE id = ?",
                            (session_id,), one=True)
    if session_row is None or not session_row["training_banner_file"]:
        flash("No training banner uploaded yet.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))
    return send_from_directory(_attendance_dir(session_id), session_row["training_banner_file"], as_attachment=False)


def _auto_send_jd14_return_link(session_row):
    """After staff upload the JD14 form, automatically emails the client the
    self-service return-upload link — JD14 needs both parties' signatures,
    so this covers the common case without staff also having to click
    "Send Return Link" separately below. Silently skipped (never blocks the
    upload itself) once the client's signed copy has already been
    received, or if there's no client email on file; a failed send (e.g.
    mail not configured) just falls back to a flash telling staff to send
    it manually."""
    if session_row["jd14_received_at"]:
        return
    to_email = session_row["pic_email"] or session_row["client_email"]
    if not to_email:
        flash("JD14 form uploaded. No client email is on file, so send the return link manually below.", "warning")
        return
    return_token = ensure_jd14_return_token(session_row["id"])
    return_url = url_for("jd14_return.details", token=return_token, _external=True)
    try:
        mailer.send_email(
            to_email,
            _default_jd14_return_email_subject(session_row),
            _default_jd14_return_email_body(session_row, return_url),
            related_type="course_session", related_id=session_row["id"],
        )
        flash(f"JD14 form uploaded, and the return link was automatically emailed to {to_email} for their signature.", "success")
    except (mailer.MailNotConfigured, mailer.MailSendError):
        flash("JD14 form uploaded, but the automatic return-link email couldn't be sent — send it manually below.", "warning")


@bp.route("/<int:session_id>/jd14", methods=("POST",))
@login_required
def upload_jd14(session_id):
    session_row = db.query(
        """SELECT cs.*, c.title AS course_title, cl.email AS client_email, pic.name AS pic_name,
                  pic.email AS pic_email
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN companies cl ON cl.id = cs.client_company_id
           LEFT JOIN leads pic ON pic.id = cs.pic_lead_id
           WHERE cs.id = ?""",
        (session_id,), one=True,
    )
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))
    if not request.files.get("jd14_file") or not request.files["jd14_file"].filename:
        flash("Choose a file to upload first.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))
    stored_name = _handle_jd14_upload(session_id)
    if stored_name:
        _notify_document_uploaded(session_id, "JD14 Form")
        _auto_send_jd14_return_link(session_row)
        warning = doc_sanity.check_document(os.path.join(_attendance_dir(session_id), stored_name), "jd14")
        if warning:
            flash(warning, "warning")
    return redirect(url_for("sessions.view", session_id=session_id))


@bp.route("/<int:session_id>/jd14/send-return-link", methods=("POST",))
@login_required
def send_jd14_return_link(session_id):
    """Emails the client/trainer the public JD14 return-upload link (single
    file only) — the self-service counterpart to the manual Upload button
    above, mirroring the T3 Attendance Form link email."""
    session_row = db.query(
        """SELECT cs.*, c.title AS course_title, cl.email AS client_email, pic.name AS pic_name,
                  pic.email AS pic_email
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN companies cl ON cl.id = cs.client_company_id
           LEFT JOIN leads pic ON pic.id = cs.pic_lead_id
           WHERE cs.id = ?""",
        (session_id,), one=True,
    )
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))

    to_email = (request.form.get("to_email") or "").strip()
    if not to_email:
        flash("Type an email address to send the return link to.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))

    return_token = ensure_jd14_return_token(session_id)
    return_url = url_for("jd14_return.details", token=return_token, _external=True)
    subject = (request.form.get("subject") or "").strip() or _default_jd14_return_email_subject(session_row)
    body = (request.form.get("body") or "").strip() or _default_jd14_return_email_body(session_row, return_url)
    cc_email = (request.form.get("cc_email") or "").strip() or None

    try:
        mailer.send_email(to_email, subject, body, related_type="course_session", related_id=session_id,
                           cc_email=cc_email)
    except mailer.MailNotConfigured as exc:
        flash(str(exc), "danger")
        return redirect(url_for("sessions.view", session_id=session_id))
    except mailer.MailSendError as exc:
        flash(f"Email failed to send: {exc}", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))

    flash(f"Return link emailed to {to_email}.", "success")
    return redirect(url_for("sessions.view", session_id=session_id))


@bp.route("/<int:session_id>/jd14/download")
@login_required
def download_jd14(session_id):
    session_row = db.query("SELECT jd14_file FROM course_sessions WHERE id = ?",
                            (session_id,), one=True)
    if session_row is None or not session_row["jd14_file"]:
        flash("No signed JD14 form uploaded yet.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))
    return send_from_directory(_attendance_dir(session_id), session_row["jd14_file"], as_attachment=False)


@bp.route("/<int:session_id>/jd14/send-email", methods=("POST",))
@login_required
def send_jd14_email(session_id):
    """Emails the uploaded signed JD14 claim form — always a manual,
    staff-clicked action (editable subject/body/recipient), mirroring the
    Evaluation Report send-email flow."""
    session_row = db.query(
        """SELECT cs.*, c.title AS course_title, cl.email AS client_email, pic.name AS pic_name,
                  pic.email AS pic_email
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN companies cl ON cl.id = cs.client_company_id
           LEFT JOIN leads pic ON pic.id = cs.pic_lead_id
           WHERE cs.id = ?""",
        (session_id,), one=True,
    )
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))
    if not session_row["jd14_file"]:
        flash("Upload the signed JD14 form first, then send it.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))

    to_email = (request.form.get("to_email") or "").strip()
    if not to_email:
        flash("Type an email address to send the signed JD14 form to.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))

    subject = (request.form.get("subject") or "").strip() or _default_jd14_email_subject(session_row)
    body = (request.form.get("body") or "").strip() or _default_jd14_email_body(session_row)
    cc_email = (request.form.get("cc_email") or "").strip() or None

    try:
        jd14_path = os.path.join(_attendance_dir(session_id), session_row["jd14_file"])
        with open(jd14_path, "rb") as f:
            file_bytes = f.read()
        mimetype, _ = mimetypes.guess_type(session_row["jd14_file"])
        attachments = [(session_row["jd14_file"], file_bytes, mimetype or "application/octet-stream")]
        mailer.send_email(to_email, subject, body, attachments=attachments,
                           related_type="course_session", related_id=session_id, cc_email=cc_email)
    except mailer.MailNotConfigured as exc:
        flash(str(exc), "danger")
        return redirect(url_for("sessions.view", session_id=session_id))
    except mailer.MailSendError as exc:
        flash(f"Email failed to send: {exc}", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))
    except OSError:
        current_app.logger.exception("Failed to read signed JD14 file for session %s", session_id)
        flash("Couldn't read the uploaded signed JD14 file.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))

    db.execute(
        "UPDATE course_sessions SET jd14_sent_at = datetime('now'), jd14_sent_to = ? WHERE id = ?",
        (to_email, session_id),
    )
    activity.log("send_email", "session", session_id, f"Emailed JD14 Form to {to_email}")
    flash(f"Signed JD14 form emailed to {to_email}.", "success")
    return redirect(url_for("sessions.view", session_id=session_id))


@bp.route("/<int:session_id>/client-logo", methods=("POST",))
@login_required
def upload_client_logo(session_id):
    session_row = db.query("SELECT id FROM course_sessions WHERE id = ?", (session_id,), one=True)
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))
    if _handle_client_logo_upload(session_id):
        flash("Client logo uploaded.", "success")
    return redirect(url_for("sessions.view", session_id=session_id))


@bp.route("/<int:session_id>/client-logo/view")
@login_required
def view_client_logo(session_id):
    session_row = db.query("SELECT client_logo_file FROM course_sessions WHERE id = ?", (session_id,), one=True)
    if session_row is None or not session_row["client_logo_file"]:
        flash("No client logo uploaded yet.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))
    return send_from_directory(_attendance_dir(session_id), session_row["client_logo_file"], as_attachment=False)


@bp.route("/<int:session_id>/banner/generate", methods=("POST",))
@login_required
def generate_banner(session_id):
    session_row = db.query(
        """SELECT cs.*, c.title AS course_title FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?""",
        (session_id,), one=True,
    )
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))

    modoku_logo_path = os.path.join(current_app.root_path, "static", "img", "logo.png")
    client_logo_path = None
    if session_row["client_logo_file"]:
        client_logo_path = os.path.join(_attendance_dir(session_id), session_row["client_logo_file"])

    png_bytes = banner.generate_banner(
        session_row["course_title"],
        session_row["training_time"],
        session_row["start_date"],
        session_row["end_date"],
        session_row["venue"],
        modoku_logo_path=modoku_logo_path,
        client_logo_path=client_logo_path,
    )
    stored_name = f"banner_{uuid.uuid4().hex[:8]}.png"
    with open(os.path.join(_attendance_dir(session_id), stored_name), "wb") as f:
        f.write(png_bytes)
    db.execute("UPDATE course_sessions SET training_banner_file = ? WHERE id = ?", (stored_name, session_id))
    flash("Training banner generated.", "success")
    return redirect(url_for("sessions.view", session_id=session_id))


@bp.route("/<int:session_id>/evaluation-poster/generate", methods=("POST",))
@login_required
def generate_evaluation_poster(session_id):
    session_row = db.query(
        """SELECT cs.*, c.title AS course_title FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?""",
        (session_id,), one=True,
    )
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))

    # The Evaluation Form link is entered right here, alongside the
    # Generate button, rather than on the Schedule/Edit Class form — pasting
    # the link and generating the poster is one single step. A submitted
    # value updates what's on file; leaving it blank falls back to
    # whatever's already saved (e.g. a plain "Regenerate" click).
    submitted_link = (request.form.get("evaluation_form_link") or "").strip()
    link = submitted_link or session_row["evaluation_form_link"]
    if not link:
        flash("Paste an Evaluation Form link first, then generate the poster.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))
    if submitted_link and submitted_link != session_row["evaluation_form_link"]:
        db.execute("UPDATE course_sessions SET evaluation_form_link = ? WHERE id = ?", (submitted_link, session_id))
    session_row = dict(session_row)
    session_row["evaluation_form_link"] = link

    # Full date range (e.g. "1 - 2 September 2026"), not just the start date —
    # reuses the same formatter the training banner uses so both stay
    # consistent for multi-day classes.
    date_text = banner._fmt_date_range(session_row["start_date"], session_row["end_date"])

    logo_path = os.path.join(current_app.root_path, "static", "img", "logo.png")
    jpeg_bytes = poster.generate_evaluation_poster(
        session_row["course_title"], date_text, session_row["evaluation_form_link"], logo_path=logo_path
    )
    stored_name = f"evalposter_{uuid.uuid4().hex[:8]}.jpg"
    with open(os.path.join(_attendance_dir(session_id), stored_name), "wb") as f:
        f.write(jpeg_bytes)
    db.execute("UPDATE course_sessions SET evaluation_qr_poster_file = ? WHERE id = ?", (stored_name, session_id))
    flash("Training Evaluation QR poster generated.", "success")
    return redirect(url_for("sessions.view", session_id=session_id))


@bp.route("/<int:session_id>/evaluation-poster/download")
@login_required
def download_evaluation_poster(session_id):
    session_row = db.query("SELECT evaluation_qr_poster_file FROM course_sessions WHERE id = ?",
                            (session_id,), one=True)
    if session_row is None or not session_row["evaluation_qr_poster_file"]:
        flash("No evaluation poster generated yet.", "danger")
        return redirect(url_for("sessions.view", session_id=session_id))
    return send_from_directory(_attendance_dir(session_id), session_row["evaluation_qr_poster_file"], as_attachment=False)


@bp.route("/<int:session_id>/delete", methods=("POST",))
@login_required
def delete(session_id):
    session_row = db.query(
        """SELECT cs.start_date, c.title FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?""",
        (session_id,), one=True,
    )
    db.execute("DELETE FROM course_sessions WHERE id = ?", (session_id,))
    activity.log("delete", "session", session_id,
                  f"Deleted class {session_row['title']} ({session_row['start_date']})" if session_row else f"Deleted session #{session_id}")
    flash("Session deleted.", "success")
    return redirect(url_for("sessions.index"))
