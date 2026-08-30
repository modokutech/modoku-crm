import os
import secrets
import uuid
from datetime import date, datetime, timedelta

from flask import (Blueprint, Response, current_app, flash, g, redirect, render_template,
                    request, send_from_directory, url_for)
from werkzeug.utils import secure_filename

from . import activity, db, mailer, notifications, uploadutil
from . import fmtdaterange
from . import sessions as _sessions
from . import settings as settings_module
from .csvutil import csv_response
from .docutil import content_disposition
from .auth import admin_required, login_required

bp = Blueprint("quotations", __name__, url_prefix="/quotations")

STATUSES = ["Draft", "Sent", "Follow-up", "Accepted", "Rejected"]
FOLLOW_UP_AFTER_DAYS = 14
TRAINING_TYPES = ["In-house Training", "Public Training", "Workshop", "Conference"]
TRAINING_MODES = ["Physical", "Virtual", "Hybrid"]

DEFAULT_TERMS_TEMPLATE = (
    "The training will be conducted {mode} at {venue} as agreed between both parties.\n"
    "This course is HRDC SBL-Khas Claimable.\n"
    "Any balance amount not eligible for HRDC claim shall be borne and paid by the client.\n"
    "Any cancellation / reschedule must be informed at least 14 working days prior to the training date. "
    "Otherwise, a 50% cancellation fee will be imposed. Last minute rescheduling is subject to a 50% "
    "penalty surcharge.\n"
    "This above-mentioned quotation is valid until {valid_until}."
)


@bp.before_request
def _require_module_enabled():
    if not g.modules.get("quotations", True):
        flash("The Quotations module is currently disabled. Ask an admin to re-enable it under Settings.", "warning")
        return redirect(url_for("dashboard.index"))


def _fmt_full_date(iso_date):
    """'2026-09-01' -> '1 September 2026' — used for dates read as prose
    (e.g. the Terms & Conditions 'valid until' line), as opposed to
    _fmt_ddmyyyy's compact '1.9.2026' which is used for filenames/quote
    numbers where a short, unambiguous naming convention matters more."""
    if not iso_date:
        return ""
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d")
    except ValueError:
        return iso_date
    return f"{d.day} {d.strftime('%B')} {d.year}"


def _default_terms(training_mode, venue, valid_until):
    return DEFAULT_TERMS_TEMPLATE.format(
        mode=(training_mode or "<training mode>").lower(),
        venue=venue or "<venue>",
        valid_until=_fmt_full_date(valid_until) if valid_until else "<7 days from the quotation date>",
    )


def _fmt_ddmyyyy(iso_date):
    """'2026-08-30' -> '30.8.2026' — matches the naming convention example."""
    if not iso_date:
        return ""
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d")
    except ValueError:
        return iso_date
    return f"{d.day}.{d.month}.{d.year}"


def _next_quote_no(base_date, revision):
    dt = datetime.strptime(base_date, "%Y-%m-%d")
    return f"#{dt.strftime('%d%m%Y')}{revision:02d}"


def _next_seq_for_date(base_date):
    """The next available 2-digit sequence number for this date's quote_no —
    scoped to base_date rather than hardcoded to 1, so multiple quotations
    created (or revised) on the same day never collide on the UNIQUE
    quote_no constraint. For a date with no quotations yet this is 1; for a
    quotation being revised it's one past the highest number already issued
    that date (which also keeps a revised quote's number climbing correctly
    even if other quotations share its date)."""
    row = db.query("SELECT MAX(revision) AS max_rev FROM quotations WHERE base_date = ?", (base_date,), one=True)
    return (row["max_rev"] or 0) + 1


def _document_title(q):
    # Hardened against ever producing "None" as a subject/filename (an
    # earlier report of quotation emails going out with subject "None" and
    # an attachment named "None.pdf" — course_title is often blank on a
    # quotation, and f-string-interpolating None produces the literal text
    # "None") — every branch below now has an explicit fallback, and the
    # function never returns anything falsy. Format: "Quotation #<no>
    # Modoku x <Client> — <Course Title> <Date>", matching the same string
    # used for both the email subject and the PDF attachment filename.
    if q["title_override"]:
        return q["title_override"]
    client_name = q["company_name_override"] or q["client_company_name"] or "Client"
    course_title = q["course_title"] or "Training"
    d = _fmt_ddmyyyy(q["quote_date"])
    quote_no = q["quote_no"] or ""
    title = f"Quotation {quote_no} Modoku x {client_name} — {course_title} {d}".replace("  ", " ").strip()
    return title or "Quotation"


def _default_email_body(q, return_url=None):
    link_para = ""
    if return_url:
        link_para = (
            "\n\nOnce you're ready to proceed, please sign and return this quotation via the link below:\n"
            f"{return_url}\n"
        )
    return (
        f"Dear {q['attention_to'] or 'Sir/Madam'},\n\n"
        "Thank you for giving us the opportunity to propose the below program to your organization. "
        "Please find attached our quotation for your review."
        f"{link_para}\n"
        "We look forward to your favorable reply. Should you have any questions, please feel free to "
        "contact us.\n\n"
        "Cheers!"
    )


def _quotation_upload_dir(quotation_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "quotations", str(quotation_id))
    os.makedirs(path, exist_ok=True)
    return path


def _ensure_return_token(quotation_id):
    """Every quotation gets a long, unguessable token the first time it's
    needed — the public 'return your signed quotation here' link included in
    the client email. Generated lazily (on view/send) rather than at
    creation so old quotations pick one up transparently too."""
    row = db.query("SELECT return_token FROM quotations WHERE id = ?", (quotation_id,), one=True)
    if row and row["return_token"]:
        return row["return_token"]
    token = secrets.token_urlsafe(24)
    db.execute("UPDATE quotations SET return_token = ? WHERE id = ?", (token, quotation_id))
    return token


def _handle_quotation_signed(quotation_id, client_email=None):
    """Runs once a signed quotation has been received, however it arrived —
    a client's self-service upload via the public return link, or a staff
    manual upload. Advances the linked class out of 'Proposed' if needed,
    emails the client their T3 Attendance Form link plus a calendar invite,
    and lets the office know. Best-effort throughout: a notification failure
    must never break the upload/return flow that triggered it."""
    q = db.query(
        """SELECT q.*, co.email AS client_company_email, cu.email AS created_by_email
           FROM quotations q
           LEFT JOIN companies co ON co.id = q.client_company_id
           LEFT JOIN users cu ON cu.id = q.created_by
           WHERE q.id = ?""",
        (quotation_id,), one=True,
    )
    if q is None:
        return
    to_email = client_email or q["sent_to_email"] or q["client_company_email"]
    # Office-wide notification addresses (Settings), plus whoever created
    # this quotation — they're the one who'll usually follow up with the
    # client, so they shouldn't have to rely on the shared office inbox.
    notify_list = list(settings_module.get_notification_emails())
    if q["created_by_email"] and q["created_by_email"] not in notify_list:
        notify_list.append(q["created_by_email"])
    notify_to = ", ".join(notify_list)

    quotation_link = url_for("quotations.view", quotation_id=quotation_id)
    if q["created_by"]:
        notifications.notify(
            q["created_by"], "quotation_signed",
            f"Signed quotation received — {q['quote_no']}",
            body=f"A signed copy of {q['quote_no']} has been received" + (f" from {to_email}" if to_email else "") + ".",
            link=quotation_link,
            dedupe_key=f"quotation:{quotation_id}:signed",
        )
    notifications.notify_admins(
        "quotation_signed",
        f"Signed quotation received — {q['quote_no']}",
        body=f"A signed copy of {q['quote_no']} has been received" + (f" from {to_email}" if to_email else "") + ".",
        link=quotation_link,
        dedupe_key=f"quotation:{quotation_id}:signed",
    )

    if not q["session_id"]:
        # No class linked yet — the signed copy is recorded, but there's
        # nowhere to point a T3 form / calendar invite at. Office is
        # notified so someone can link a Class and follow up manually.
        try:
            if not notify_to:
                return
            mailer.send_email(
                notify_to,
                f"Signed quotation received — {q['quote_no']} (no class linked yet)",
                f"A signed copy of quotation {q['quote_no']} has been received"
                + (f" from {to_email}" if to_email else "") + ", but it isn't linked to a Class yet, so the "
                "T3 Attendance Form link and calendar invite couldn't be sent automatically.\n\n"
                "Link it to a Class on the quotation's Edit page (add a 'Proposed' class first if one "
                "doesn't exist yet), then use 'Resend confirmation' on the quotation page.",
                related_type="quotation", related_id=quotation_id,
            )
        except Exception:  # noqa: BLE001 - notification must never break the caller
            current_app.logger.exception("Failed to send no-class-linked notice for quotation %s", quotation_id)
        return

    session_row = db.query(
        """SELECT cs.*, c.title AS course_title, c.hrdcorp_programme_no, c.hrdf_claimable
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?""",
        (q["session_id"],), one=True,
    )
    if session_row is None:
        return

    if session_row["status"] == "Proposed":
        db.execute("UPDATE course_sessions SET status = 'Scheduled' WHERE id = ?", (session_row["id"],))
        activity.log("update", "session", session_row["id"],
                      f"Class auto-advanced to Scheduled — signed quotation {q['quote_no']} received")
        try:
            from . import calendar_integration
            scheduled_row = db.query(
                "SELECT cs.*, c.title AS course_title FROM course_sessions cs "
                "JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?", (session_row["id"],), one=True)
            calendar_integration.block_calendar_for_session(scheduled_row)
        except Exception:  # noqa: BLE001 - calendar blocking must never break the signed-quotation flow
            current_app.logger.exception("Failed to block calendar for session %s", session_row["id"])

    t3_token = _sessions.ensure_t3_public_token(session_row["id"])
    t3_url = url_for("t3_public.form", token=t3_token, _external=True)
    date_range = fmtdaterange(session_row["start_date"], session_row["end_date"])

    if to_email:
        try:
            subject = f"Thank you — next step for {session_row['course_title']} ({date_range})"
            meeting_link_line = ""
            if session_row["training_mode"] in ("Virtual", "Hybrid") and session_row["meeting_link"]:
                meeting_link_line = f"Meeting Link: {session_row['meeting_link']}\n"
            hrdcorp_para = ""
            if session_row["hrdf_claimable"]:
                hrdcorp_para = (
                    "We'll send a separate email with the necessary HRDCorp documents for your grant "
                    "application (including the HRDCorp Programme No.) once they're ready.\n\n"
                )
            body = (
                f"Dear {q['attention_to'] or 'Sir/Madam'},\n\n"
                "Thank you for returning the signed quotation. Your training is now confirmed as follows:\n\n"
                f"Training: {session_row['course_title']}\n"
                f"Date: {date_range}\n"
                f"Time: {session_row['training_time'] or 'To be confirmed'}\n"
                f"Venue: {session_row['venue'] or 'To be confirmed'}\n"
                f"{meeting_link_line}\n"
                f"{hrdcorp_para}"
                "Please complete the attendance list for your participants here — you're welcome to come "
                f"back and update it anytime up until the day of training:\n{t3_url}\n\n"
                "A calendar invite for the training date(s) is attached.\n\n"
                "Should you have any questions, please feel free to contact us.\n\n"
                "Cheers!"
            )
            ics_bytes = _sessions.build_session_ics(session_row)
            mailer.send_email(
                to_email, subject, body,
                attachments=[(f"{session_row['course_title']}.ics", ics_bytes, "text/calendar")],
                related_type="quotation", related_id=quotation_id,
            )
            db.execute("UPDATE quotations SET t3_link_sent_at = datetime('now') WHERE id = ?", (quotation_id,))
        except Exception:  # noqa: BLE001
            current_app.logger.exception("Failed to send T3 form link for quotation %s", quotation_id)

    try:
        if to_email:
            office_body = (
                f"A signed copy of quotation {q['quote_no']} has been received from {to_email}.\n\n"
                f"Class: {session_row['course_title']}\nDate: {date_range}\n\n"
                "The client has been sent their T3 Attendance Form link and a calendar invite."
            )
        else:
            office_body = (
                f"A signed copy of quotation {q['quote_no']} has been received, but no client email was "
                "on file to send the T3 Attendance Form link and calendar invite to — send them manually."
            )
        if notify_to:
            mailer.send_email(notify_to, f"Signed quotation received — {q['quote_no']}", office_body,
                               related_type="quotation", related_id=quotation_id)
    except Exception:  # noqa: BLE001
        current_app.logger.exception("Failed to send office notification for quotation %s", quotation_id)


def _totals(items, sst_rate):
    subtotal = sum(item["investment_fee"] for item in items)
    sst_amount = round(subtotal * (sst_rate or 0) / 100, 2)
    return subtotal, sst_amount, subtotal + sst_amount


def _has_valid_item(form):
    """At least one item needs a programme name AND a nonzero investment
    fee — a quotation with no priced item isn't something a client could
    actually sign off on."""
    programmes = form.getlist("item_programme")
    fees = form.getlist("item_fee")
    for programme, fee in zip(programmes, fees):
        try:
            fee_val = float(fee or 0)
        except ValueError:
            fee_val = 0
        if programme.strip() and fee_val > 0:
            return True
    return False


def _items_from_form(form):
    """Rebuilds the items table rows from a rejected submission (failed
    _has_valid_item check) so the form can be re-shown with whatever the
    user already typed still in place, instead of making them start the
    items table over from scratch."""
    programmes = form.getlist("item_programme")
    paxs = form.getlist("item_pax")
    types = form.getlist("item_training_type")
    durations = form.getlist("item_duration")
    dates_ = form.getlist("item_date")
    dates_end = form.getlist("item_date_end")
    times_start = form.getlist("item_time_start")
    times_end = form.getlist("item_time_end")
    fees = form.getlist("item_fee")
    n = len(programmes)
    items = []
    for i in range(n):
        items.append({
            "programme": programmes[i] if i < len(programmes) else "",
            "no_of_pax": paxs[i] if i < len(paxs) else "1",
            "training_type": types[i] if i < len(types) else "",
            "duration": durations[i] if i < len(durations) else "",
            "item_date": dates_[i] if i < len(dates_) else "",
            "item_date_end": dates_end[i] if i < len(dates_end) else "",
            "item_time": _sessions.format_training_time(
                times_start[i] if i < len(times_start) else "",
                times_end[i] if i < len(times_end) else "",
            ),
            "investment_fee": fees[i] if i < len(fees) else "0",
        })
    return items


def _save_items(quotation_id, form):
    programmes = form.getlist("item_programme")
    paxs = form.getlist("item_pax")
    types = form.getlist("item_training_type")
    durations = form.getlist("item_duration")
    dates_ = form.getlist("item_date")
    dates_end = form.getlist("item_date_end")
    times_start = form.getlist("item_time_start")
    times_end = form.getlist("item_time_end")
    fees = form.getlist("item_fee")
    for programme, pax, ttype, duration, item_date, item_date_end, time_start, time_end, fee in zip(
        programmes, paxs, types, durations, dates_, dates_end, times_start, times_end, fees
    ):
        if not programme.strip():
            continue
        # An end date only makes sense if it's a distinct, later day than the
        # start date — a same-day (or blank) end date is treated as "1 day"
        # and ignored, per the "if 1 day then ignore the end date" request.
        date_end = item_date_end or None
        if not item_date or not date_end or date_end <= item_date:
            date_end = None
        item_time = _sessions.format_training_time(time_start, time_end)
        db.execute(
            """INSERT INTO quotation_items (quotation_id, programme, no_of_pax, training_type,
                   duration, item_date, item_date_end, item_time, investment_fee) VALUES (?,?,?,?,?,?,?,?,?)""",
            (quotation_id, programme.strip(), int(pax or 1), ttype or None, duration or None,
             item_date or None, date_end, item_time, float(fee or 0)),
        )


def _form_common(request_form):
    client_company_id = request_form.get("client_company_id") or None
    quote_date = request_form.get("quote_date") or date.today().isoformat()
    valid_until = request_form.get("valid_until") or (
        (datetime.strptime(quote_date, "%Y-%m-%d") + timedelta(days=7)).date().isoformat()
    )
    training_mode = request_form.get("training_mode") or "Physical"
    venue = request_form.get("venue") or None
    terms = request_form.get("terms") or _default_terms(training_mode, venue, valid_until)
    return {
        "client_company_id": client_company_id,
        "session_id": request_form.get("session_id") or None,
        "attention_to": request_form.get("attention_to") or None,
        "company_name_override": request_form.get("company_name_override") or None,
        "address": request_form.get("address") or None,
        "tel": request_form.get("tel") or None,
        "quote_date": quote_date,
        "ref_no": request_form.get("ref_no") or None,
        "course_title": request_form.get("course_title") or None,
        "is_hrdcorp": 1 if request_form.get("is_hrdcorp") else 0,
        "title_override": request_form.get("title_override") or None,
        "training_mode": training_mode,
        "venue": venue,
        "valid_until": valid_until,
        "terms": terms,
        "sst_rate": float(request_form.get("sst_rate") or 0),
        "status": request_form.get("status") or "Draft",
        "notes": request_form.get("notes") or None,
    }


def _linkable_sessions(include_id=None):
    """Classes a quotation can be tied to — 'Proposed' and 'Scheduled' only,
    since the point of linking is to drive the not-yet-confirmed workflow
    (auto-send T3 form + calendar invite once the quotation comes back
    signed); a Completed/Cancelled/Ongoing class has already moved past
    that point. include_id keeps an already-linked class in the dropdown
    even if it has since moved past Proposed/Scheduled, so editing a
    quotation never silently drops its existing link."""
    return db.query(
        """SELECT cs.id, cs.start_date, cs.end_date, cs.status, c.title AS course_title,
                  cs.client_company_id, cs.venue, pic.id AS pic_lead_id, pic.name AS pic_name
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN leads pic ON pic.id = cs.pic_lead_id
           WHERE cs.status IN ('Proposed', 'Scheduled') OR cs.id = ?
           ORDER BY cs.start_date""",
        (include_id,),
    )


def _leads_for_dropdown():
    """Every lead with the company it belongs to — embedded as JSON on the
    quotation form so the 'Attention To' dropdown (leads under whichever
    Client Company is selected) can be populated client-side without a
    round trip. Leads with no company aren't attributable to any company's
    dropdown, so they're left out."""
    rows = db.query(
        "SELECT id, name, company_id FROM leads WHERE company_id IS NOT NULL ORDER BY name COLLATE NOCASE"
    )
    return [{"id": r["id"], "name": r["name"], "company_id": r["company_id"]} for r in rows]


def _filtered_quotations():
    quotes = db.query(
        """SELECT q.*, co.name AS client_company_name, u.name AS created_by_name,
                  COALESCE((SELECT SUM(investment_fee) FROM quotation_items
                            WHERE quotation_id = q.id), 0) AS subtotal
           FROM quotations q
           LEFT JOIN companies co ON co.id = q.client_company_id
           LEFT JOIN users u ON u.id = q.created_by
           ORDER BY q.created_at DESC"""
    )
    return quotes


def _auto_advance_quotation_statuses():
    """A 'Sent' quotation that's gone quiet for FOLLOW_UP_AFTER_DAYS with no
    client response moves to 'Follow-up' — flagging it for staff to chase up
    rather than silently sitting as 'Sent' forever — and its creator gets a
    Notification. Never touches Draft/Accepted/Rejected, and never moves a
    quotation backwards. Runs once per request, same pattern as classes'
    _auto_advance_statuses in sessions.py."""
    cutoff = (datetime.now() - timedelta(days=FOLLOW_UP_AFTER_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    due_rows = db.query(
        "SELECT id, quote_no, created_by FROM quotations WHERE status = 'Sent' AND sent_at IS NOT NULL AND sent_at <= ?",
        (cutoff,),
    )
    if not due_rows:
        return
    ids = [row["id"] for row in due_rows]
    placeholders = ",".join("?" * len(ids))
    db.execute(f"UPDATE quotations SET status = 'Follow-up' WHERE id IN ({placeholders})", ids)
    for row in due_rows:
        notifications.notify(
            row["created_by"], "quotation_followup",
            f"Quotation {row['quote_no']} needs a follow-up",
            body=f"Sent over {FOLLOW_UP_AFTER_DAYS} days ago with no response from the client yet.",
            link=url_for("quotations.view", quotation_id=row["id"]),
            dedupe_key=f"quotation:{row['id']}:followup",
        )


@bp.before_app_request
def _sync_quotation_statuses():
    try:
        _auto_advance_quotation_statuses()
    except Exception:  # noqa: BLE001 - never let this housekeeping break a request
        current_app.logger.exception("Failed to auto-advance quotation statuses")


@bp.route("/")
@login_required
def index():
    quotes = _filtered_quotations()
    return render_template("quotations/list.html", quotes=quotes, statuses=STATUSES)


@bp.route("/export")
@admin_required
def export():
    quotes = _filtered_quotations()
    rows = (
        (q["quote_no"], q["quote_date"], q["client_company_name"] or q["company_name_override"] or "",
         q["course_title"] or "", q["training_mode"], q["status"], q["sst_rate"], q["created_by_name"] or "")
        for q in quotes
    )
    return csv_response(
        "quotations.csv",
        ["Quote No", "Quote Date", "Client", "Course", "Training Mode", "Status", "SST Rate", "Created By"],
        rows,
    )


@bp.route("/new", methods=("GET", "POST"))
@login_required
def new():
    companies = db.query("SELECT * FROM companies ORDER BY name")
    courses = db.query("SELECT id, title FROM courses WHERE active = 1 ORDER BY title")
    preselect_company = request.args.get("company_id", type=int)

    if request.method == "POST":
        fields = _form_common(request.form)
        if not _has_valid_item(request.form):
            flash("Add at least one item with a programme name and an investment fee before saving.", "danger")
            return render_template(
                "quotations/form.html", quotation=fields, is_edit=False, companies=companies, courses=courses,
                statuses=STATUSES, training_types=TRAINING_TYPES, training_modes=TRAINING_MODES,
                today=fields["quote_date"], default_valid_until=fields["valid_until"],
                preselect_company=int(fields["client_company_id"]) if fields["client_company_id"] else None,
                default_terms=fields["terms"], items=_items_from_form(request.form),
                linkable_sessions=_linkable_sessions(), leads=_leads_for_dropdown(),
            )
        base_date = fields["quote_date"]
        revision = _next_seq_for_date(base_date)
        quote_no = _next_quote_no(base_date, revision)
        quotation_id = db.execute(
            """INSERT INTO quotations (quote_no, base_date, revision, client_company_id, session_id, attention_to,
                   company_name_override, address, tel, quote_date, ref_no, course_title, is_hrdcorp,
                   title_override, training_mode, venue, valid_until, terms, sst_rate, status, notes, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                quote_no, base_date, revision, fields["client_company_id"], fields["session_id"],
                fields["attention_to"], fields["company_name_override"], fields["address"], fields["tel"],
                fields["quote_date"], fields["ref_no"], fields["course_title"], fields["is_hrdcorp"],
                fields["title_override"], fields["training_mode"], fields["venue"], fields["valid_until"],
                fields["terms"], fields["sst_rate"], fields["status"], fields["notes"], g.user["id"],
            ),
        )
        _save_items(quotation_id, request.form)
        activity.log("create", "quotation", quotation_id, f"Created quotation {quote_no}")
        flash("Quotation created.", "success")
        return redirect(url_for("quotations.view", quotation_id=quotation_id))

    today = date.today().isoformat()
    valid_until = (date.today() + timedelta(days=7)).isoformat()
    return render_template(
        "quotations/form.html", quotation=None, is_edit=False, companies=companies, courses=courses,
        statuses=STATUSES, training_types=TRAINING_TYPES, training_modes=TRAINING_MODES,
        today=today, default_valid_until=valid_until, preselect_company=preselect_company,
        default_terms=_default_terms("Physical", None, valid_until), linkable_sessions=_linkable_sessions(),
        leads=_leads_for_dropdown(),
    )


@bp.route("/<int:quotation_id>")
@login_required
def view(quotation_id):
    q = db.query(
        """SELECT q.*, co.name AS client_company_name, co.address AS client_company_address,
                  co.phone AS client_company_phone, co.email AS client_company_email,
                  u.name AS created_by_name, u.position AS created_by_position,
                  u.contact_phone AS created_by_phone, u.signature_file AS created_by_signature
           FROM quotations q
           LEFT JOIN companies co ON co.id = q.client_company_id
           LEFT JOIN users u ON u.id = q.created_by
           WHERE q.id = ?""",
        (quotation_id,), one=True,
    )
    if q is None:
        flash("Quotation not found.", "danger")
        return redirect(url_for("quotations.index"))
    items = db.query("SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY id", (quotation_id,))
    subtotal, sst_amount, grand_total = _totals(items, q["sst_rate"])
    title = _document_title({**dict(q), "client_company_name": q["client_company_name"]})

    linked_session = None
    if q["session_id"]:
        linked_session = db.query(
            """SELECT cs.*, c.title AS course_title FROM course_sessions cs
               JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?""",
            (q["session_id"],), one=True,
        )
    return_token = _ensure_return_token(quotation_id)
    return_url = url_for("quotation_return.details", token=return_token, _external=True)
    return render_template("quotations/view.html", q=q, items=items, subtotal=subtotal,
                            sst_amount=sst_amount, grand_total=grand_total,
                            statuses=STATUSES, title=title, mail_configured=mailer.is_configured(),
                            default_email_subject=title, default_email_body=_default_email_body(q, return_url),
                            email_attachment_names=[f"{title}.pdf"],
                            linked_session=linked_session, return_url=return_url)


@bp.route("/<int:quotation_id>/download")
@login_required
def download(quotation_id):
    q = db.query(
        """SELECT q.*, co.name AS client_company_name, co.address AS client_company_address,
                  co.phone AS client_company_phone, co.email AS client_company_email,
                  u.name AS created_by_name, u.position AS created_by_position,
                  u.contact_phone AS created_by_phone, u.signature_file AS created_by_signature
           FROM quotations q
           LEFT JOIN companies co ON co.id = q.client_company_id
           LEFT JOIN users u ON u.id = q.created_by
           WHERE q.id = ?""",
        (quotation_id,), one=True,
    )
    if q is None:
        flash("Quotation not found.", "danger")
        return redirect(url_for("quotations.index"))
    items = db.query("SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY id", (quotation_id,))
    subtotal, sst_amount, grand_total = _totals(items, q["sst_rate"])
    title = _document_title({**dict(q), "client_company_name": q["client_company_name"]})
    try:
        from . import pdfgen
        pdf_bytes = pdfgen.generate_quotation_pdf(q, items, subtotal, title)
    except Exception:  # noqa: BLE001 - surface a clean message rather than a 500
        current_app.logger.exception("Failed to generate quotation PDF for %s", q["quote_no"])
        flash("Couldn't generate the PDF — is wkhtmltopdf installed on the server?", "danger")
        return redirect(url_for("quotations.view", quotation_id=quotation_id))
    return Response(
        pdf_bytes, mimetype="application/pdf",
        headers={"Content-Disposition": content_disposition(f"{title}.pdf")},
    )


@bp.route("/<int:quotation_id>/edit", methods=("GET", "POST"))
@login_required
def edit(quotation_id):
    q = db.query("SELECT * FROM quotations WHERE id = ?", (quotation_id,), one=True)
    if q is None:
        flash("Quotation not found.", "danger")
        return redirect(url_for("quotations.index"))
    companies = db.query("SELECT * FROM companies ORDER BY name")
    courses = db.query("SELECT id, title FROM courses WHERE active = 1 ORDER BY title")

    if request.method == "POST":
        fields = _form_common(request.form)
        if not _has_valid_item(request.form):
            flash("Add at least one item with a programme name and an investment fee before saving.", "danger")
            reentry = {**dict(q), **fields}
            return render_template(
                "quotations/form.html", quotation=reentry, is_edit=True, companies=companies, courses=courses,
                statuses=STATUSES, training_types=TRAINING_TYPES, training_modes=TRAINING_MODES,
                today=fields["quote_date"], default_valid_until=fields["valid_until"], preselect_company=None,
                default_terms=fields["terms"], items=_items_from_form(request.form),
                linkable_sessions=_linkable_sessions(q["session_id"]), leads=_leads_for_dropdown(),
            )
        db.execute(
            """UPDATE quotations SET client_company_id=?, session_id=?, attention_to=?, company_name_override=?,
                   address=?, tel=?, quote_date=?, ref_no=?, course_title=?, is_hrdcorp=?,
                   title_override=?, training_mode=?, venue=?, valid_until=?, terms=?, sst_rate=?,
                   status=?, notes=?
               WHERE id=?""",
            (
                fields["client_company_id"], fields["session_id"], fields["attention_to"],
                fields["company_name_override"], fields["address"], fields["tel"], fields["quote_date"],
                fields["ref_no"], fields["course_title"], fields["is_hrdcorp"], fields["title_override"],
                fields["training_mode"], fields["venue"], fields["valid_until"], fields["terms"],
                fields["sst_rate"], fields["status"], fields["notes"], quotation_id,
            ),
        )
        db.execute("DELETE FROM quotation_items WHERE quotation_id = ?", (quotation_id,))
        _save_items(quotation_id, request.form)
        activity.log("update", "quotation", quotation_id, f"Updated quotation {q['quote_no']}")
        flash("Quotation updated.", "success")
        return redirect(url_for("quotations.view", quotation_id=quotation_id))

    items = db.query("SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY id", (quotation_id,))
    return render_template(
        "quotations/form.html", quotation=q, is_edit=True, companies=companies, courses=courses,
        statuses=STATUSES, training_types=TRAINING_TYPES, training_modes=TRAINING_MODES,
        today=q["quote_date"], default_valid_until=q["valid_until"], preselect_company=None,
        default_terms=q["terms"], items=items, linkable_sessions=_linkable_sessions(q["session_id"]),
        leads=_leads_for_dropdown(),
    )


@bp.route("/<int:quotation_id>/revise", methods=("POST",))
@login_required
def revise(quotation_id):
    q = db.query("SELECT * FROM quotations WHERE id = ?", (quotation_id,), one=True)
    if q is None:
        flash("Quotation not found.", "danger")
        return redirect(url_for("quotations.index"))
    new_revision = _next_seq_for_date(q["base_date"])
    new_quote_no = _next_quote_no(q["base_date"], new_revision)
    db.execute(
        "UPDATE quotations SET revision = ?, quote_no = ?, status = 'Draft' WHERE id = ?",
        (new_revision, new_quote_no, quotation_id),
    )
    activity.log("update", "quotation", quotation_id, f"Revised quotation to {new_quote_no}")
    flash(f"New revision created — now {new_quote_no}.", "success")
    return redirect(url_for("quotations.view", quotation_id=quotation_id))


@bp.route("/<int:quotation_id>/status", methods=("POST",))
@login_required
def update_status(quotation_id):
    status = request.form.get("status")
    if status in STATUSES:
        db.execute("UPDATE quotations SET status = ? WHERE id = ?", (status, quotation_id))
        flash(f"Quotation marked as {status}.", "success")
    return redirect(url_for("quotations.view", quotation_id=quotation_id))


@bp.route("/<int:quotation_id>/send-email", methods=("POST",))
@login_required
def send_email(quotation_id):
    q = db.query(
        """SELECT q.*, co.name AS client_company_name, co.address AS client_company_address,
                  co.phone AS client_company_phone, co.email AS client_company_email,
                  u.name AS created_by_name, u.position AS created_by_position,
                  u.contact_phone AS created_by_phone, u.signature_file AS created_by_signature
           FROM quotations q
           LEFT JOIN companies co ON co.id = q.client_company_id
           LEFT JOIN users u ON u.id = q.created_by
           WHERE q.id = ?""",
        (quotation_id,), one=True,
    )
    if q is None:
        flash("Quotation not found.", "danger")
        return redirect(url_for("quotations.index"))

    to_email = request.form.get("to_email") or q["client_company_email"]
    if not to_email:
        flash("No client email on file — add one on the client's profile, or type an address to send to.", "danger")
        return redirect(url_for("quotations.view", quotation_id=quotation_id))

    items = db.query("SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY id", (quotation_id,))
    subtotal, sst_amount, grand_total = _totals(items, q["sst_rate"])
    title = _document_title({**dict(q), "client_company_name": q["client_company_name"]})

    return_token = _ensure_return_token(quotation_id)
    return_url = url_for("quotation_return.details", token=return_token, _external=True)

    subject = (request.form.get("subject") or "").strip() or title
    body = (request.form.get("body") or "").strip() or _default_email_body(q, return_url)
    cc_email = (request.form.get("cc_email") or "").strip() or None

    attachments = []
    try:
        from . import pdfgen
        pdf_bytes = pdfgen.generate_quotation_pdf(q, items, subtotal, title)
        attachments.append((f"{title}.pdf", pdf_bytes, "application/pdf"))
    except Exception:  # noqa: BLE001 - PDF generation is a nice-to-have, never block the email
        current_app.logger.exception("Failed to generate quotation PDF for %s", q["quote_no"])

    try:
        mailer.send_email(to_email, subject, body, attachments=attachments,
                           related_type="quotation", related_id=quotation_id, cc_email=cc_email)
    except mailer.MailNotConfigured as exc:
        flash(str(exc), "danger")
        return redirect(url_for("quotations.view", quotation_id=quotation_id))
    except mailer.MailSendError as exc:
        flash(f"Email failed to send: {exc}", "danger")
        return redirect(url_for("quotations.view", quotation_id=quotation_id))

    db.execute(
        "UPDATE quotations SET sent_at = datetime('now'), sent_to_email = ?, "
        "status = CASE WHEN status = 'Draft' THEN 'Sent' ELSE status END WHERE id = ?",
        (to_email, quotation_id),
    )
    activity.log("send_email", "quotation", quotation_id, f"Emailed quotation {q['quote_no']} to {to_email}")
    flash(f"Quotation emailed to {to_email}.", "success")
    return redirect(url_for("quotations.view", quotation_id=quotation_id))


@bp.route("/<int:quotation_id>/upload-signed", methods=("POST",))
@login_required
def upload_signed(quotation_id):
    """Staff-side manual alternative to the client self-service return link
    — for cases where the client emails/hands over the signed copy some
    other way. Triggers the exact same downstream automation (T3 form link
    + calendar invite to the client, office notified) as the public flow."""
    q = db.query("SELECT * FROM quotations WHERE id = ?", (quotation_id,), one=True)
    if q is None:
        flash("Quotation not found.", "danger")
        return redirect(url_for("quotations.index"))

    file_storage = request.files.get("signed_file")
    if not file_storage or not file_storage.filename:
        flash("Choose the signed quotation file first.", "danger")
        return redirect(url_for("quotations.view", quotation_id=quotation_id))
    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.DEFAULT_EXTENSIONS)
    if error:
        flash(error, "danger")
        return redirect(url_for("quotations.view", quotation_id=quotation_id))

    safe_name = secure_filename(file_storage.filename)
    stored_name = f"signed_{uuid.uuid4().hex[:8]}_{safe_name}"
    file_storage.save(os.path.join(_quotation_upload_dir(quotation_id), stored_name))

    client_email = (request.form.get("client_email") or "").strip() or None
    db.execute(
        "UPDATE quotations SET signed_file = ?, signed_received_at = datetime('now'), "
        "signed_received_via = 'staff_upload', status = 'Accepted' WHERE id = ?",
        (stored_name, quotation_id),
    )
    activity.log("update", "quotation", quotation_id, f"Uploaded signed copy of quotation {q['quote_no']}")
    _handle_quotation_signed(quotation_id, client_email)
    flash("Signed quotation recorded — the client has been sent their T3 Attendance Form link and a "
          "calendar invite (if a class was linked and an email was on file).", "success")
    return redirect(url_for("quotations.view", quotation_id=quotation_id))


@bp.route("/<int:quotation_id>/signed-file")
@login_required
def download_signed(quotation_id):
    q = db.query("SELECT signed_file FROM quotations WHERE id = ?", (quotation_id,), one=True)
    if q is None or not q["signed_file"]:
        flash("No signed copy on file for this quotation.", "danger")
        return redirect(url_for("quotations.view", quotation_id=quotation_id))
    return send_from_directory(_quotation_upload_dir(quotation_id), q["signed_file"], as_attachment=False)


@bp.route("/<int:quotation_id>/delete", methods=("POST",))
@login_required
def delete(quotation_id):
    q = db.query("SELECT quote_no FROM quotations WHERE id = ?", (quotation_id,), one=True)
    db.execute("DELETE FROM quotations WHERE id = ?", (quotation_id,))
    activity.log("delete", "quotation", quotation_id, f"Deleted quotation {q['quote_no'] if q else quotation_id}")
    flash("Quotation deleted.", "success")
    return redirect(url_for("quotations.index"))
