import difflib
import os
import re
import secrets
import uuid
from datetime import date, datetime, timedelta

from flask import (Blueprint, Response, current_app, flash, g, jsonify, redirect, render_template,
                    request, send_from_directory, url_for)
from werkzeug.utils import secure_filename

from . import activity, db, doc_sanity, mailer, notifications, uploadutil
from . import schedule as _schedule
from . import fmtdaterange, fmtmoney, parse_money
from . import sessions as _sessions
from . import settings as settings_module
from .csvutil import csv_response
from .docutil import content_disposition
from .auth import admin_required, login_required

bp = Blueprint("quotations", __name__, url_prefix="/quotations")

# Fix100: Rejected = the client said no. Cancelled = it was agreed, then called
# off (keeps the win on record). Expired = the validity date passed with no
# answer. Expired is set by hand (Fix100), never automatically.
STATUSES = ["Draft", "Sent", "Follow-up", "Accepted", "Rejected", "Cancelled", "Expired"]
FOLLOW_UP_AFTER_DAYS = 14
# Fix116: a new quotation's "Valid Until" defaults to this many days after
# its quotation date (was 7).
VALID_FOR_DAYS = 30
TRAINING_TYPES = ["In-house Training", "Public Training", "Workshop", "Conference"]
TRAINING_MODES = ["Physical", "Virtual", "Hybrid"]

DEFAULT_TERMS_TEMPLATE = (
    "{delivery} as agreed between both parties.\n"
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


# Fix222: the first T&C line reads properly for each mode ("virtually on
# Microsoft Teams / Zoom", not "virtual at"). Keep in step with
# deliverySentence() in templates/quotations/form.html.
_DELIVERY_SENTENCES = {
    "physical": "The training will be conducted physically at {venue}",
    "virtual": "The training will be conducted virtually on {venue}",
    "hybrid": "The training will be conducted in hybrid mode, at {venue} and online,",
}


def _delivery_sentence(training_mode, venue):
    venue = venue or "<venue>"
    template = _DELIVERY_SENTENCES.get((training_mode or "").strip().lower())
    if template is None:
        return f"The training will be conducted {(training_mode or '<training mode>').lower()} at {venue}"
    return template.format(venue=venue)


def _default_terms(training_mode, venue, valid_until):
    return DEFAULT_TERMS_TEMPLATE.format(
        delivery=_delivery_sentence(training_mode, venue),
        valid_until=_fmt_full_date(valid_until) if valid_until else f"<{VALID_FOR_DAYS} days from the quotation date>",
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
    title = f"Quotation {quote_no} Modoku x {client_name} - {course_title} {d}".replace("  ", " ").strip()
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


GRANT_QUOTE_AUTO_PREFIX = "grantquote_signed_"


def _copy_signed_to_grant_docs(quotation_id, session_id):
    """Fix140: puts a quotation's client-signed copy into its class's HRDCorp
    Grant Documents as the Quotation, so it needn't be uploaded twice. Never
    replaces a quotation staff uploaded there themselves - only an empty
    slot, or one this same automatic copy filled earlier (a newer signed
    copy then wins). Returns True if it copied. Best-effort, never raises."""
    try:
        q = db.query("SELECT signed_file FROM quotations WHERE id = ?", (quotation_id,), one=True)
        cs = db.query("SELECT grant_quotation_file FROM course_sessions WHERE id = ?", (session_id,), one=True)
        if not q or not q["signed_file"] or cs is None:
            return False
        current = cs["grant_quotation_file"] or ""
        if current and not current.startswith((GRANT_QUOTE_AUTO_PREFIX, "grantquote_class_")):
            return False
        src = os.path.join(_quotation_upload_dir(quotation_id), q["signed_file"])
        if not os.path.exists(src):
            return False
        import shutil
        stored_name = f"{GRANT_QUOTE_AUTO_PREFIX}{uuid.uuid4().hex[:8]}_{secure_filename(q['signed_file'])}"
        shutil.copyfile(src, os.path.join(_sessions._attendance_dir(session_id), stored_name))
        db.execute("UPDATE course_sessions SET grant_quotation_file = ? WHERE id = ?", (stored_name, session_id))
        activity.log("update", "session", session_id,
                     "Signed quotation added to the HRDCorp Grant Documents automatically")
        return True
    except Exception:  # noqa: BLE001 - a convenience copy must never break the signed-quotation flow
        current_app.logger.exception("Failed to copy signed quotation %s into grant documents", quotation_id)
        return False


def _handle_quotation_signed(quotation_id, client_email=None, ai_warning=None):
    """Runs once a signed quotation has been received, however it arrived —
    a client's self-service upload via the public return link, or a staff
    manual upload. Advances the linked class out of 'Proposed' if needed,
    emails the client their T3 Attendance Form link plus a calendar invite,
    and lets the office know. Best-effort throughout: a notification failure
    must never break the upload/return flow that triggered it.

    ai_warning: an optional AI sanity-check warning (see doc_sanity.py) to
    fold into the office notifications below — the main way a warning ever
    reaches staff when the upload came in through the public, unauthenticated
    return link (quotation_return.py), where there's no staff session to
    flash it to directly."""
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

    warning_suffix = f"\n\nNote (AI sanity-check): {ai_warning}" if ai_warning else ""

    quotation_link = url_for("quotations.view", quotation_id=quotation_id)
    if q["created_by"]:
        notifications.notify(
            q["created_by"], "quotation_signed",
            f"Signed quotation received - {q['quote_no']}",
            body=f"A signed copy of {q['quote_no']} has been received" + (f" from {to_email}" if to_email else "") + "." + warning_suffix,
            link=quotation_link,
            dedupe_key=f"quotation:{quotation_id}:signed",
        )
    notifications.notify_admins(
        "quotation_signed",
        f"Signed quotation received - {q['quote_no']}",
        body=f"A signed copy of {q['quote_no']} has been received" + (f" from {to_email}" if to_email else "") + "." + warning_suffix,
        link=quotation_link,
        dedupe_key=f"quotation:{quotation_id}:signed",
    )

    session_ids = linked_session_ids(quotation_id)
    if not session_ids:
        # No class linked yet — the signed copy is recorded, but there's
        # nowhere to point a T3 form / calendar invite at. Office is
        # notified so someone can link a Class and follow up manually.
        try:
            if not notify_to:
                return
            mailer.send_email(
                notify_to,
                f"Signed quotation received - {q['quote_no']} (no class linked yet)",
                f"A signed copy of quotation {q['quote_no']} has been received"
                + (f" from {to_email}" if to_email else "") + ", but it isn't linked to a Class yet, so the "
                "T3 Attendance Form link and calendar invite couldn't be sent automatically.\n\n"
                "Link its lines to Classes with \"Link or create classes\" on the quotation page (or "
                "the Class picker on each line in Edit), then send the T3 links from each class page." + warning_suffix,
                related_type="quotation", related_id=quotation_id,
            )
        except Exception:  # noqa: BLE001 - notification must never break the caller
            current_app.logger.exception("Failed to send no-class-linked notice for quotation %s", quotation_id)
        return

    # Fix193: a quotation can cover several classes (batches) - each one is
    # confirmed, and the client gets ONE email listing them all.
    classes = []
    for sid in session_ids:
        row = db.query(
            """SELECT cs.*, c.title AS course_title, c.hrdcorp_programme_no, c.hrdf_claimable
               FROM course_sessions cs
               JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?""",
            (sid,), one=True,
        )
        if row is not None:
            classes.append(row)
    if not classes:
        return

    confirmed = []
    for session_row in classes:
        # Fix140/Fix193: the signed copy (one class) or this class's own lines
        # (several classes) go into its Grant Documents as the Quotation.
        fill_grant_quotation(quotation_id, session_row["id"])

        if session_row["status"] == "Proposed":
            db.execute("UPDATE course_sessions SET status = 'Scheduled' WHERE id = ?", (session_row["id"],))
            activity.log("update", "session", session_row["id"],
                          f"Class auto-advanced to Scheduled, signed quotation {q['quote_no']} received")
            try:
                from . import calendar_integration
                scheduled_row = db.query(
                    "SELECT cs.*, c.title AS course_title FROM course_sessions cs "
                    "JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?", (session_row["id"],), one=True)
                calendar_integration.block_calendar_for_session(scheduled_row)
            except Exception:  # noqa: BLE001 - calendar blocking must never break the signed-quotation flow
                current_app.logger.exception("Failed to block calendar for session %s", session_row["id"])

        t3_token = _sessions.ensure_t3_public_token(session_row["id"])
        confirmed.append({
            "row": session_row,
            "t3_url": url_for("t3_public.form", token=t3_token, _external=True),
            "date_range": _schedule.jinja_dates(session_row),
        })

    if to_email:
        try:
            subject, body = _confirmation_email(q, confirmed)
            if len(confirmed) == 1:
                ics_bytes = _sessions.build_session_ics(confirmed[0]["row"])
                ics_name = f"{confirmed[0]['row']['course_title']}.ics"
            else:
                ics_bytes = _combined_ics([c["row"] for c in confirmed])
                ics_name = "Training dates.ics"
            mailer.send_email(
                to_email, subject, body,
                attachments=[(ics_name, ics_bytes, "text/calendar")],
                related_type="quotation", related_id=quotation_id,
            )
            db.execute("UPDATE quotations SET t3_link_sent_at = datetime('now') WHERE id = ?", (quotation_id,))
            for c in confirmed:
                db.execute("UPDATE course_sessions SET t3_link_sent_at = datetime('now'), t3_link_sent_to = ? "
                           "WHERE id = ?", (f"{to_email} (automatic, after the signed quotation)", c["row"]["id"]))
        except Exception:  # noqa: BLE001
            current_app.logger.exception("Failed to send T3 form link for quotation %s", quotation_id)

    try:
        class_lines = "\n".join(f"Class: {c['row']['course_title']}\nDate: {c['date_range']}\n" for c in confirmed)
        if to_email:
            office_body = (
                f"A signed copy of quotation {q['quote_no']} has been received from {to_email}.\n\n"
                f"{class_lines}\n"
                "The client has been sent their T3 Attendance Form link"
                + ("s" if len(confirmed) > 1 else "") + " and a calendar invite."
            )
        else:
            office_body = (
                f"A signed copy of quotation {q['quote_no']} has been received, but no client email was "
                "on file to send the T3 Attendance Form link and calendar invite to. Send them manually.\n\n"
                f"{class_lines}"
            )
        if notify_to:
            mailer.send_email(notify_to, f"Signed quotation received - {q['quote_no']}", office_body,
                               related_type="quotation", related_id=quotation_id)
    except Exception:  # noqa: BLE001
        current_app.logger.exception("Failed to send office notification for quotation %s", quotation_id)


def _confirmation_email(q, confirmed):
    """The client's "thank you, next step" email after signing: the original
    one-class wording, or (Fix193) one email listing every class with its
    own T3 Attendance Form link."""
    greeting = f"Dear {q['attention_to'] or 'Sir/Madam'},\n\n"
    hrdcorp = any(c["row"]["hrdcorp_claimable"] for c in confirmed)
    hrdcorp_para = (
        "We'll send a separate email with the necessary HRDCorp documents for your grant "
        "application (including the HRDCorp Programme No.) once they're ready.\n\n"
    ) if hrdcorp else ""

    def meeting_line(row, indent=""):
        if row["training_mode"] in ("Virtual", "Hybrid") and row["meeting_link"]:
            return f"{indent}Meeting Link: {row['meeting_link']}\n"
        return ""

    def venue(row):
        """Fix206: a virtual class says it's online instead of "To be confirmed"."""
        if row["training_mode"] == "Virtual":
            return "Online (see meeting link below)" if row["meeting_link"] else "Online (meeting link to follow)"
        return row["venue"] or "To be confirmed"

    # Fix206: the same course more than once (batches) gets "(Batch 1)",
    # "(Batch 2)"... numbered by date, so the classes are easy to tell apart.
    batch = {}
    by_course = {}
    for c in confirmed:
        by_course.setdefault(c["row"]["course_title"], []).append(c["row"])
    for rows in by_course.values():
        if len(rows) > 1:
            for n, r in enumerate(sorted(rows, key=lambda r: (r["start_date"] or "", r["id"])), 1):
                batch[r["id"]] = f" (Batch {n})"

    if len(confirmed) == 1:
        c = confirmed[0]
        row = c["row"]
        subject = f"Thank you, next step for {row['course_title']} ({c['date_range']})"
        body = (
            greeting
            + "Thank you for returning the signed quotation. Your training is now confirmed as follows:\n\n"
            f"Training: {row['course_title']}\n"
            f"Date: {c['date_range']}\n"
            f"Time: {_schedule.row_time_text(row, sep='; ') or 'To be confirmed'}\n"
            f"Venue: {venue(row)}\n"
            f"{meeting_line(row)}\n"
            f"{hrdcorp_para}"
            "Please complete the attendance list for your participants here, you're welcome to come "
            f"back and update it anytime up until the day of training:\n{c['t3_url']}\n\n"
            "A calendar invite for the training date(s) is attached.\n\n"
            "Should you have any questions, please feel free to contact us.\n\n"
            "Cheers!"
        )
        return subject, body

    blocks = []
    confirmed = sorted(confirmed, key=lambda c: (c["row"]["start_date"] or "", c["row"]["id"]))  # by date
    for i, c in enumerate(confirmed, 1):
        row = c["row"]
        blocks.append(
            f"{i}. {row['course_title']}{batch.get(row['id'], '')}\n"
            f"   Date: {c['date_range']}\n"
            f"   Time: {_schedule.row_time_text(row, sep='; ') or 'To be confirmed'}\n"
            f"   Venue: {venue(row)}\n"
            f"{meeting_line(row, '   ')}"
            f"   Attendance list: {c['t3_url']}\n"
        )
    subject = f"Thank you, next steps for your {len(confirmed)} trainings (Quotation {q['quote_no']})"
    body = (
        greeting
        + f"Thank you for returning the signed quotation. Your {len(confirmed)} trainings are now confirmed "
        "as follows:\n\n"
        + "\n".join(blocks) + "\n"
        + "Please complete the attendance list for each training at its own link above. You're welcome to "
        "come back and update them anytime up until the day of training.\n\n"
        + hrdcorp_para
        + "A calendar invite with all the training dates is attached.\n\n"
        "Should you have any questions, please feel free to contact us.\n\n"
        "Cheers!"
    )
    return subject, body


def _combined_ics(rows):
    """One .ics holding every class's event (each class's own invite, merged)."""
    events = []
    for row in rows:
        text = _sessions.build_session_ics(row).decode("utf-8")
        start, end = text.index("BEGIN:VEVENT"), text.rindex("END:VEVENT") + len("END:VEVENT\r\n")
        events.append(text[start:end])
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Modoku Hub//Class//EN\r\n"
        "CALSCALE:GREGORIAN\r\nMETHOD:PUBLISH\r\n" + "".join(events) + "END:VCALENDAR\r\n"
    ).encode("utf-8")


# ---- Fix193/Fix201: one quotation, several classes --------------------------
# Each quotation line is linked to the class(es) it's for, in
# quotation_item_classes. One line can cover several classes (batches): its fee
# is split equally between them, unless a class has its own amount.
# quotations.session_id is kept as the FIRST linked class, so everything that
# shows "the" class of a quotation (list, client return page) still works.

def item_class_links(quotation_id):
    """{item_id: [(session_id, custom_amount_or_None), ...]} in link order."""
    rows = db.query(
        """SELECT qic.item_id, qic.session_id, qic.amount FROM quotation_item_classes qic
           JOIN quotation_items qi ON qi.id = qic.item_id
           WHERE qi.quotation_id = ? ORDER BY qi.id, qic.id""", (quotation_id,))
    out = {}
    for r in rows:
        out.setdefault(r["item_id"], []).append((r["session_id"], r["amount"]))
    return out


def linked_session_ids(quotation_id):
    """The classes on a quotation's lines, in line order (no repeats)."""
    out = []
    for links in item_class_links(quotation_id).values():
        for sid, _amount in links:
            if sid not in out:
                out.append(sid)
    return out


def class_shares(fee, links):
    """Splits one line's fee between its classes: {session_id: amount}. A class
    with its own amount keeps it; the rest of the fee is split equally between
    the others (the last one takes the rounding remainder, so the shares add
    up to the fee to the sen)."""
    shares = {sid: round(amount, 2) for sid, amount in links if amount is not None}
    free = [sid for sid, amount in links if amount is None]
    rest = round((fee or 0) - sum(shares.values()), 2)
    if free:
        each = max(round(rest / len(free), 2), 0.0)
        for sid in free[:-1]:
            shares[sid] = each
        shares[free[-1]] = max(round(rest - each * (len(free) - 1), 2), 0.0)
    return shares


def share_problems(items, links_by_item):
    """Lines whose own class amounts don't fit the line's fee - for a warning."""
    out = []
    for item in items:
        links = links_by_item.get(item["id"], [])
        custom = [a for _s, a in links if a is not None]
        if len(links) < 2 or not custom:
            continue
        total = round(sum(custom), 2)
        fee = round(item["investment_fee"] or 0, 2)
        if total > fee + 0.005 or (len(custom) == len(links) and abs(total - fee) > 0.005):
            out.append(f"{item['programme']}: the class amounts add up to RM {fmtmoney(total)}, "
                       f"but the line is RM {fmtmoney(fee)}")
    return out


def class_items(quotation_id, session_id, items=None):
    """The lines that make up one class's price. A quotation for a single
    class: all of it (extra unlinked lines such as meals belong to that class).
    Several classes: only the lines linked to this class - and a line shared by
    several classes counts with this class's share of the fee, its own dates
    and time (Fix201), and the class's own pax (Fix236), so the class's grant
    quotation shows that batch only."""
    if items is None:
        items = db.query("SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY id", (quotation_id,))
    links = item_class_links(quotation_id)
    ids = linked_session_ids(quotation_id)
    if len(ids) <= 1:
        return list(items) if session_id in ids else []
    cls = db.query("SELECT start_date, end_date, training_time, day_times, capacity FROM course_sessions WHERE id = ?",
                   (session_id,), one=True)
    out = []
    for item in items:
        item_links = links.get(item["id"], [])
        if session_id not in [sid for sid, _a in item_links]:
            continue
        line = dict(item)
        # Fix236: the pax is the class's own (its No. of Pax), not the line's
        # total - e.g. one line of 60 pax quoted for 2 batches of 30.
        if cls is not None and cls["capacity"]:
            line["no_of_pax"] = cls["capacity"]
        if len(item_links) == 1:
            out.append(line)
            continue
        line["investment_fee"] = class_shares(item["investment_fee"], item_links)[session_id]
        if cls is not None:
            line["item_date"] = cls["start_date"]
            line["item_date_end"] = cls["end_date"] if cls["end_date"] and cls["end_date"] > cls["start_date"] else None
            line["item_time"] = cls["training_time"] or item["item_time"]
            line["day_times"] = cls["day_times"]
        out.append(line)
    return out


def _sync_primary_session(quotation_id):
    ids = linked_session_ids(quotation_id)
    db.execute("UPDATE quotations SET session_id = ? WHERE id = ?", (ids[0] if ids else None, quotation_id))
    return ids


def _quotation_for_pdf(quotation_id):
    return db.query(
        """SELECT q.*, co.name AS client_company_name, co.address AS client_company_address,
                  co.city AS client_company_city, co.postcode AS client_company_postcode,
                  co.state AS client_company_state,
                  co.phone AS client_company_phone, co.email AS client_company_email,
                  u.name AS created_by_name, u.position AS created_by_position,
                  u.contact_phone AS created_by_phone, u.signature_file AS created_by_signature
           FROM quotations q
           LEFT JOIN companies co ON co.id = q.client_company_id
           LEFT JOIN users u ON u.id = q.created_by
           WHERE q.id = ?""",
        (quotation_id,), one=True,
    )


def class_quotation_pdf(quotation_id, session_id):
    """Fix193: a quotation PDF with only one class's lines and amount (same
    quote number) - for that class's HRDCorp Grant Documents, since each
    batch's grant application needs its own quotation and amount. Returns
    (filename, pdf_bytes), or None if the class isn't on this quotation."""
    q = _quotation_for_pdf(quotation_id)
    if q is None:
        return None
    items = class_items(quotation_id, session_id)
    if not items:
        return None
    # Fix206: anything in brackets is left out of the programme name on the
    # grant copy - "Effective Leadership (4 batches)" next to one batch's
    # amount would confuse HRDCorp. The client's own quotation is unchanged.
    items = [dict(i, programme=_strip_brackets(i["programme"])) for i in items]
    cls = db.query("SELECT c.title, cs.start_date, cs.end_date FROM course_sessions cs "
                   "JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?", (session_id,), one=True)
    # Fix237: a class with a single line shows the class's own course name, so
    # renaming the class (or its course) carries through to its grant copy.
    # With several lines (e.g. training + meals) each keeps its own name.
    if cls and cls["title"] and len(items) == 1:
        items[0]["programme"] = cls["title"]
    subtotal = _totals(items, q["sst_rate"], q["sst_inclusive"])[0]
    data = dict(q)
    data["course_title"] = (cls["title"] if cls else None) or items[0]["programme"]
    data["title_override"] = None
    title = _document_title(data)
    from . import pdfgen
    # the class's dates in the filename tell batches of the same course apart
    when = f" ({fmtdaterange(cls['start_date'], cls['end_date'])})" if cls else ""
    return f"{title}{when}.pdf", pdfgen.generate_quotation_pdf(q, items, subtotal, title, client_sign=False)


def _strip_brackets(text):
    """"Effective Leadership (4 batches)" -> "Effective Leadership". Leaves the
    text as it was if nothing would be left."""
    stripped = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", text or "")
    stripped = re.sub(r"\s{2,}", " ", stripped).strip(" -,")
    return stripped or text


GRANT_QUOTE_CLASS_PREFIX = "grantquote_class_"


def fill_grant_quotation(quotation_id, session_id):
    """Puts the right quotation into a class's Grant Documents: the client's
    signed copy when the quotation is for this class only (Fix140), or this
    class's own lines as a PDF when it covers several classes (Fix193).
    Never replaces a file staff uploaded there themselves. Best-effort."""
    if len(linked_session_ids(quotation_id)) <= 1:
        return _copy_signed_to_grant_docs(quotation_id, session_id)
    try:
        cs = db.query("SELECT grant_quotation_file FROM course_sessions WHERE id = ?", (session_id,), one=True)
        if cs is None:
            return False
        current = cs["grant_quotation_file"] or ""
        if current and not current.startswith((GRANT_QUOTE_AUTO_PREFIX, GRANT_QUOTE_CLASS_PREFIX)):
            return False
        made = class_quotation_pdf(quotation_id, session_id)
        if made is None:
            return False
        filename, pdf_bytes = made
        stored_name = f"{GRANT_QUOTE_CLASS_PREFIX}{uuid.uuid4().hex[:8]}_{secure_filename(filename)}"
        with open(os.path.join(_sessions._attendance_dir(session_id), stored_name), "wb") as f:
            f.write(pdf_bytes)
        db.execute("UPDATE course_sessions SET grant_quotation_file = ? WHERE id = ?", (stored_name, session_id))
        activity.log("update", "session", session_id,
                     "This class's quotation (its own lines and amount) added to the HRDCorp Grant Documents")
        return True
    except Exception:  # noqa: BLE001 - a convenience copy must never break the signed-quotation flow
        current_app.logger.exception("Failed to add class quotation %s/%s to grant documents",
                                     quotation_id, session_id)
        return False


def _totals(items, sst_rate, sst_inclusive=False):
    """Sums the items' investment fees and splits out SST.

    Normally (sst_inclusive=False) the typed fees are treated as
    pre-SST amounts: subtotal = sum of fees, SST is added on top, and the
    grand total is subtotal + SST.

    When sst_inclusive is set, the typed fees are treated as already
    including SST (e.g. a client-quoted "RM21,000 all-in" price) — the
    grand total stays exactly what was typed, and the subtotal/SST are
    backed out of it instead: subtotal = total / (1 + rate), SST = total -
    subtotal. E.g. RM21,000 at 8% becomes subtotal RM19,444.44 + SST
    RM1,555.56 = RM21,000 again, rather than RM21,000 + 8% = RM22,680."""
    entered_total = sum(item["investment_fee"] for item in items)
    rate = sst_rate or 0
    if sst_inclusive and rate:
        grand_total = round(entered_total, 2)
        subtotal = round(entered_total / (1 + rate / 100), 2)
        sst_amount = round(grand_total - subtotal, 2)
    else:
        subtotal = entered_total
        sst_amount = round(subtotal * rate / 100, 2)
        grand_total = round(subtotal + sst_amount, 2)
    return subtotal, sst_amount, grand_total


def _has_valid_item(form):
    """At least one item needs a programme name AND a nonzero investment
    fee — a quotation with no priced item isn't something a client could
    actually sign off on."""
    programmes = form.getlist("item_programme")
    fees = form.getlist("item_fee")
    for programme, fee in zip(programmes, fees):
        try:
            fee_val = parse_money(fee)
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
    classes_ = form.getlist("item_classes")
    day_times_ = form.getlist("item_day_times")
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
            "links": _parse_links(classes_[i] if i < len(classes_) else ""),
            "day_times": day_times_[i] if i < len(day_times_) else "",
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
    # Fix201: each line's classes and their own amounts ("6=;7=12000"), one
    # hidden value per row so it lines up with the other columns
    classes_ = form.getlist("item_classes") or [""] * len(programmes)
    day_times_ = form.getlist("item_day_times") or [""] * len(programmes)  # Fix204
    for programme, pax, ttype, duration, item_date, item_date_end, time_start, time_end, fee, cls, dts in zip(
        programmes, paxs, types, durations, dates_, dates_end, times_start, times_end, fees, classes_, day_times_
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
        item_date, date_end, item_time, day_times, _err = _sessions.schedule_from_form(
            dts, item_date or None, date_end, item_time)
        links = _parse_links(cls)
        item_id = db.execute(
            """INSERT INTO quotation_items (quotation_id, programme, no_of_pax, training_type,
                   duration, item_date, item_date_end, item_time, investment_fee, session_id, day_times)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (quotation_id, programme.strip(), int(pax or 1), ttype or None, duration or None,
             item_date or None, date_end, item_time, parse_money(fee), links[0][0] if links else None, day_times),
        )
        _save_links(item_id, links)
    return _sync_primary_session(quotation_id)


def _with_links(quotation_id, items):
    links = item_class_links(quotation_id)
    return [dict(i, links=links.get(i["id"], [])) for i in items]


def _flash_share_problems(quotation_id):
    items = db.query("SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY id", (quotation_id,))
    for problem in share_problems(items, item_class_links(quotation_id)):
        flash(f"Check the class amounts. {problem}.", "warning")


def _parse_links(value):
    """"6=;7=12000" -> [(6, None), (7, 12000.0)] (no repeats; a blank or
    unreadable amount means "equal share")."""
    out, seen = [], set()
    for part in (value or "").split(";"):
        sid, _eq, amount = part.partition("=")
        sid = sid.strip()
        if not sid.isdigit() or int(sid) in seen:
            continue
        seen.add(int(sid))
        try:
            amt = parse_money(amount) if amount.strip() else None
        except ValueError:
            amt = None
        out.append((int(sid), amt))
    return out


def _save_links(item_id, links):
    db.execute("DELETE FROM quotation_item_classes WHERE item_id = ?", (item_id,))
    for sid, amount in links:
        if db.query("SELECT 1 FROM course_sessions WHERE id = ?", (sid,), one=True):
            db.execute("INSERT INTO quotation_item_classes (item_id, session_id, amount) VALUES (?,?,?)",
                       (item_id, sid, amount))
    first = db.query("SELECT session_id FROM quotation_item_classes WHERE item_id = ? ORDER BY id LIMIT 1",
                     (item_id,), one=True)
    db.execute("UPDATE quotation_items SET session_id = ? WHERE id = ?", (first["session_id"] if first else None, item_id))


def _clean(value):
    """Strips a submitted form field and returns None for blank input —
    and, defensively, also for the literal text "None". A template bug
    used to render an unset (Python None) field's value as the literal
    string "None" in its HTML value="" attribute; if the user didn't touch
    that field, the browser would submit that literal text back, which then
    got saved as the real value (hence "Company None" / "None.pdf"). The
    template rendering is now fixed so this can't happen going forward, but
    this guard also quietly self-heals any quotation that already got
    corrupted that way before the fix, the next time it's saved."""
    text = (value or "").strip()
    return None if text in ("", "None") else text


def _form_common(request_form):
    client_company_id = request_form.get("client_company_id") or None
    quote_date = request_form.get("quote_date") or date.today().isoformat()
    valid_until = request_form.get("valid_until") or (
        (datetime.strptime(quote_date, "%Y-%m-%d") + timedelta(days=VALID_FOR_DAYS)).date().isoformat()
    )
    training_mode = request_form.get("training_mode") or "Physical"
    venue = _clean(request_form.get("venue"))
    terms = request_form.get("terms") or _default_terms(training_mode, venue, valid_until)
    return {
        "client_company_id": client_company_id,
        "session_id": None,  # Fix193: set from the lines' classes after saving (_sync_primary_session)
        "attention_to": _clean(request_form.get("attention_to")),
        "company_name_override": _clean(request_form.get("company_name_override")),
        "address": _clean(request_form.get("address")),
        "tel": _clean(request_form.get("tel")),
        "quote_date": quote_date,
        "ref_no": _clean(request_form.get("ref_no")),
        "course_title": _clean(request_form.get("course_title")),
        "is_hrdcorp": 1 if request_form.get("is_hrdcorp") else 0,
        "title_override": _clean(request_form.get("title_override")),
        "training_mode": training_mode,
        "venue": venue,
        "valid_until": valid_until,
        "terms": terms,
        "sst_rate": float(request_form.get("sst_rate") or 0),
        "sst_inclusive": 1 if request_form.get("sst_inclusive") else 0,
        "status": request_form.get("status") or "Draft",
        "notes": _clean(request_form.get("notes")),
    }


def quoted_price_for_session(session_id):
    """The price actually quoted to the client for one class, for showing on
    the class page beside the course's own list price. Returns
    {"quotation_id", "quote_no", "status", "subtotal", "grand_total",
    "sst_rate"} or None when no quotation is linked to that class.

    The class page shows `subtotal` - the figure EXCLUDING SST - since that
    is the training revenue itself rather than what the client is billed.
    `grand_total` is returned alongside it for anything that wants the
    billed amount.

    A class can carry several quotations (a revision supersedes its
    predecessor, and the quote number increments), so this picks the one that
    represents the agreed figure: an **Accepted** quotation wins outright;
    failing that, the most recent one by date, then by id. A Rejected
    quotation is only used if it's the only thing on file — worth showing
    what was asked rather than nothing at all, and the status travels with
    the figure so the page can say which it is.

    Uses the same _totals() the quotation itself displays, so the number here
    and the number on the quotation can never disagree (including the
    SST-inclusive case, where the grand total is what was typed rather than
    the fees plus tax).
    """
    rows = db.query(
        """SELECT id, quote_no, status, sst_rate, sst_inclusive, quote_date
           FROM quotations
           WHERE session_id = ? OR id IN (SELECT qi.quotation_id FROM quotation_items qi
                                          JOIN quotation_item_classes qic ON qic.item_id = qi.id
                                          WHERE qic.session_id = ?)
           ORDER BY CASE status WHEN 'Accepted' THEN 0 ELSE 1 END,
                    quote_date DESC, id DESC
           LIMIT 1""",
        (session_id, session_id),
    )
    if not rows:
        return None
    q = rows[0]
    # Fix193: on a quotation covering several classes, only this class's lines.
    items = class_items(q["id"], session_id)
    subtotal, _sst, grand_total = _totals(items, q["sst_rate"], q["sst_inclusive"])
    return {
        "quotation_id": q["id"], "quote_no": q["quote_no"], "status": q["status"],
        "subtotal": subtotal, "grand_total": grand_total, "sst_rate": q["sst_rate"],
    }


def _linkable_sessions(include_ids=None):
    """Classes a quotation can be tied to — 'Proposed' and 'Scheduled' only,
    since the point of linking is to drive the not-yet-confirmed workflow
    (auto-send T3 form + calendar invite once the quotation comes back
    signed); a Completed/Cancelled/Ongoing class has already moved past
    that point. include_id keeps an already-linked class in the dropdown
    even if it has since moved past Proposed/Scheduled, so editing a
    quotation never silently drops its existing link."""
    keep = [int(i) for i in (include_ids or []) if i]
    rows = db.query(
        f"""SELECT cs.id, cs.start_date, cs.end_date, cs.status, c.title AS course_title,
                  cs.client_company_id, cs.venue, pic.id AS pic_lead_id, pic.name AS pic_name,
                  cs.capacity, cs.training_type, cs.training_time, c.price_inhouse, c.duration_days,
                  co.name AS client_name, cs.day_times
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN leads pic ON pic.id = cs.pic_lead_id
           LEFT JOIN companies co ON co.id = cs.client_company_id
           WHERE cs.status IN ('Proposed', 'Scheduled') OR cs.id IN ({",".join("?" * len(keep)) or "NULL"})
           ORDER BY cs.start_date""",
        keep,
    )
    return [{**dict(r), "duration": _duration_label(r)} for r in rows]


def _duration_label(session_row):
    """Fix128: the item Duration a linked Class pre-fills, e.g. "2 days".
    The Course's duration, unless the Class's own dates span more days
    (a course left at the default 1 day but scheduled over 2), so a
    half-day course still reads "0.5 day" rather than a whole day."""
    course_days = session_row["duration_days"] or 0
    class_days = 0
    try:
        start = datetime.strptime(session_row["start_date"], "%Y-%m-%d").date()
        end = datetime.strptime(session_row["end_date"], "%Y-%m-%d").date() if session_row["end_date"] else start
        class_days = max((end - start).days + 1, 1)
    except (TypeError, ValueError):
        pass
    days = class_days if class_days > 1 and class_days > course_days else course_days
    if not days:
        return ""
    days = int(days) if float(days).is_integer() else days
    return f"{days} day" if days <= 1 else f"{days} days"


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


PRICING_MIN_SIMILARITY = 0.5
PRICING_SUGGESTION_LIMIT = 5


def _similar_priced_items(programme, pax=None, exclude_quotation_id=None, limit=PRICING_SUGGESTION_LIMIT):
    """Historical quotation_items with a Programme name similar to the one
    being typed, most-similar first — the "pricing assistant" on the
    quotation form.

    quotation_items.programme is free text (not a foreign key to courses),
    so there's no clean join to lean on here. This does a plain fuzzy
    string match (difflib, the same approach ai_match.py already uses for
    matching handwritten attendance names) against every past item with a
    real fee on file, rather than anything AI-based — deliberately the
    lighter first version of this feature. A pax count that's close to the
    one being quoted now nudges otherwise-tied matches ahead, since a
    programme quoted for a very different group size is a weaker price
    reference even when the name matches well.
    """
    programme = (programme or "").strip()
    if len(programme) < 3:
        return []
    rows = db.query(
        """SELECT qi.programme, qi.no_of_pax, qi.training_type, qi.investment_fee,
                  q.id AS quotation_id, q.quote_no, q.quote_date, q.status,
                  co.name AS client_company_name
           FROM quotation_items qi
           JOIN quotations q ON q.id = qi.quotation_id
           LEFT JOIN companies co ON co.id = q.client_company_id
           WHERE qi.investment_fee > 0 AND qi.programme IS NOT NULL AND qi.programme != ''"""
    )
    query_lower = programme.lower()
    scored = []
    for r in rows:
        if exclude_quotation_id and r["quotation_id"] == exclude_quotation_id:
            continue
        score = difflib.SequenceMatcher(None, query_lower, r["programme"].strip().lower()).ratio()
        if score < PRICING_MIN_SIMILARITY:
            continue
        if pax and r["no_of_pax"]:
            try:
                pax_diff = abs(int(pax) - int(r["no_of_pax"]))
                score += max(0, 0.1 - pax_diff * 0.02)  # small nudge, never enough to beat a real name match
            except (TypeError, ValueError):
                pass
        scored.append((score, r))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    results = []
    seen_quote_programme = set()
    for score, r in scored:
        dedupe_key = (r["quotation_id"], r["programme"])
        if dedupe_key in seen_quote_programme:
            continue
        seen_quote_programme.add(dedupe_key)
        results.append({
            "quotation_id": r["quotation_id"],
            "quote_no": r["quote_no"],
            "quote_date": r["quote_date"],
            "status": r["status"],
            "client_company_name": r["client_company_name"],
            "programme": r["programme"],
            "no_of_pax": r["no_of_pax"],
            "training_type": r["training_type"],
            "investment_fee": r["investment_fee"],
            "similarity": round(min(score, 1.0), 2),
        })
        if len(results) >= limit:
            break
    return results


def _filtered_quotations():
    quotes = db.query(
        """SELECT q.*, co.name AS client_company_name, u.name AS created_by_name,
                  COALESCE((SELECT SUM(investment_fee) FROM quotation_items
                            WHERE quotation_id = q.id), 0) AS subtotal,
                  cs.start_date AS training_start_date, cs.end_date AS training_end_date
           FROM quotations q
           LEFT JOIN companies co ON co.id = q.client_company_id
           LEFT JOIN users u ON u.id = q.created_by
           LEFT JOIN course_sessions cs ON cs.id = q.session_id
           ORDER BY q.created_at DESC"""
    )
    return quotes


def _auto_advance_quotation_statuses():
    """A 'Sent' quotation that's gone quiet for FOLLOW_UP_AFTER_DAYS with no
    client response moves to 'Follow-up' — flagging it for staff to chase up
    rather than silently sitting as 'Sent' forever — and its creator gets a
    Notification. Only ever touches 'Sent', and never moves a
    quotation backwards. Runs once per request, same pattern as classes'
    _auto_advance_statuses in sessions.py.

    sent_at is stored via SQL's datetime('now'), which is always UTC — so
    the cutoff here is computed in UTC too (not the server's own local
    clock, which may or may not actually be UTC) to compare correctly
    against it."""
    cutoff = (datetime.utcnow() - timedelta(days=FOLLOW_UP_AFTER_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
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


@bp.route("/pricing-suggestions")
@login_required
def pricing_suggestions():
    """AJAX endpoint backing the "pricing assistant" on the quotation form
    (see form.html) — given a Programme name being typed (and optionally a
    pax count), returns the most similar past quotation line items with a
    real fee on file, most-similar first. GET-only and read-only, so no
    CSRF token is needed here (see security.py's before_request hook,
    which only checks state-changing methods)."""
    results = _similar_priced_items(
        request.args.get("programme", ""),
        pax=request.args.get("pax", type=int),
        exclude_quotation_id=request.args.get("exclude_quotation_id", type=int),
    )
    return jsonify({"results": results})


@bp.route("/new", methods=("GET", "POST"))
@login_required
def new():
    companies = db.query("SELECT * FROM companies ORDER BY name")
    courses = db.query("SELECT id, title FROM courses WHERE active = 1 ORDER BY title")
    preselect_company = request.args.get("company_id", type=int)
    # Optional ?session_id= — set by the "+ Quotation for Client" button on a
    # class page, so the quotation opens already tied to that class (and the
    # form's own JS then fills course title, client, venue, dates and pax from
    # it). Passed to _linkable_sessions as include_id so the class is in the
    # dropdown even when its status is past Proposed/Scheduled; otherwise the
    # button would silently land on an unlinked form.
    preselect_session = request.args.get("session_id", type=int)

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
                linkable_sessions=_linkable_sessions(
                    [sid for i in _items_from_form(request.form) for sid, _a in i["links"]] + [preselect_session]),
                preselect_session=None,
                leads=_leads_for_dropdown(),
            )
        base_date = fields["quote_date"]
        revision = _next_seq_for_date(base_date)
        quote_no = _next_quote_no(base_date, revision)
        quotation_id = db.execute(
            """INSERT INTO quotations (quote_no, base_date, revision, client_company_id, session_id, attention_to,
                   company_name_override, address, tel, quote_date, ref_no, course_title, is_hrdcorp,
                   title_override, training_mode, venue, valid_until, terms, sst_rate, sst_inclusive, status,
                   notes, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                quote_no, base_date, revision, fields["client_company_id"], fields["session_id"],
                fields["attention_to"], fields["company_name_override"], fields["address"], fields["tel"],
                fields["quote_date"], fields["ref_no"], fields["course_title"], fields["is_hrdcorp"],
                fields["title_override"], fields["training_mode"], fields["venue"], fields["valid_until"],
                fields["terms"], fields["sst_rate"], fields["sst_inclusive"], fields["status"],
                fields["notes"], g.user["id"],
            ),
        )
        _save_items(quotation_id, request.form)
        _flash_share_problems(quotation_id)
        activity.log("create", "quotation", quotation_id, f"Created quotation {quote_no}")
        flash("Quotation created.", "success")
        return redirect(url_for("quotations.view", quotation_id=quotation_id))

    today = date.today().isoformat()
    valid_until = (date.today() + timedelta(days=VALID_FOR_DAYS)).isoformat()
    return render_template(
        "quotations/form.html", quotation=None, is_edit=False, companies=companies, courses=courses,
        statuses=STATUSES, training_types=TRAINING_TYPES, training_modes=TRAINING_MODES,
        today=today, default_valid_until=valid_until, preselect_company=preselect_company,
        default_terms=_default_terms("Physical", None, valid_until),
        linkable_sessions=_linkable_sessions([preselect_session]),
        preselect_session=preselect_session,
        leads=_leads_for_dropdown(),
    )


@bp.route("/<int:quotation_id>")
@login_required
def view(quotation_id):
    q = db.query(
        """SELECT q.*, co.name AS client_company_name, co.address AS client_company_address,
                  co.city AS client_company_city, co.postcode AS client_company_postcode,
                  co.state AS client_company_state,
                  co.phone AS client_company_phone, co.email AS client_company_email,
                  u.name AS created_by_name, u.position AS created_by_position,
                  u.contact_phone AS created_by_phone, u.signature_file AS created_by_signature,
                  (SELECT l.email FROM leads l
                     WHERE l.company_id = q.client_company_id AND l.name = q.attention_to
                     ORDER BY l.email IS NULL, l.id LIMIT 1) AS pic_email
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
    subtotal, sst_amount, grand_total = _totals(items, q["sst_rate"], q["sst_inclusive"])
    title = _document_title({**dict(q), "client_company_name": q["client_company_name"]})

    linked_session = None
    if q["session_id"]:
        linked_session = db.query(
            """SELECT cs.*, c.title AS course_title FROM course_sessions cs
               JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?""",
            (q["session_id"],), one=True,
        )
    linked_classes = _classes_summary(q, items)
    # lines with no class: all of them while nothing is linked; after that only
    # dated ones (an undated extra like meals belongs with the classes)
    links = item_class_links(quotation_id)
    unlinked_items = [i for i in items if not links.get(i["id"]) and (not linked_classes or i["item_date"])]
    return_token = _ensure_return_token(quotation_id)
    return_url = url_for("quotation_return.details", token=return_token, _external=True)
    return render_template("quotations/view.html", q=q, items=items, subtotal=subtotal,
                            sst_amount=sst_amount, grand_total=grand_total,
                            statuses=STATUSES, title=title, mail_configured=mailer.is_configured(),
                            default_email_subject=title, default_email_body=_default_email_body(q, return_url),
                            email_attachment_names=[f"{title}.pdf"],
                            linked_session=linked_session, return_url=return_url,
                            linked_classes=linked_classes, unlinked_items=unlinked_items)


def _classes_summary(q, items):
    """Fix193: each class on the quotation with its status and its own amount."""
    out = []
    for sid in linked_session_ids(q["id"]):
        row = db.query(
            """SELECT cs.id, cs.start_date, cs.end_date, cs.status, cs.grant_quotation_file, c.title AS course_title
               FROM course_sessions cs JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?""", (sid,), one=True)
        if row is None:
            continue
        mine = class_items(q["id"], sid, items)
        out.append(dict(row, lines=len(mine), subtotal=_totals(mine, q["sst_rate"], q["sst_inclusive"])[0]))
    return out


@bp.route("/<int:quotation_id>/download")
@login_required
def download(quotation_id):
    q = db.query(
        """SELECT q.*, co.name AS client_company_name, co.address AS client_company_address,
                  co.city AS client_company_city, co.postcode AS client_company_postcode,
                  co.state AS client_company_state,
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
    subtotal, sst_amount, grand_total = _totals(items, q["sst_rate"], q["sst_inclusive"])
    title = _document_title({**dict(q), "client_company_name": q["client_company_name"]})
    try:
        from . import pdfgen
        pdf_bytes = pdfgen.generate_quotation_pdf(q, items, subtotal, title)
    except Exception:  # noqa: BLE001 - surface a clean message rather than a 500
        current_app.logger.exception("Failed to generate quotation PDF for %s", q["quote_no"])
        flash("Couldn't generate the PDF. Is wkhtmltopdf installed on the server?", "danger")
        return redirect(url_for("quotations.view", quotation_id=quotation_id))
    return Response(
        pdf_bytes, mimetype="application/pdf",
        headers={"Content-Disposition": content_disposition(f"{title}.pdf")},
    )


def _signed_total(q):
    """Fix125: the grand total currently on a quotation the client has
    already accepted/signed (None otherwise) - shown as a warning on the
    Edit page, since changing amounts there doesn't change what they signed."""
    if not (q["status"] == "Accepted" or q["signed_file"]):
        return None
    items = db.query("SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY id", (q["id"],))
    return _totals(items, q["sst_rate"], q["sst_inclusive"])[2]


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
                linkable_sessions=_linkable_sessions(
                    [sid for i in _items_from_form(request.form) for sid, _a in i["links"]]
                    + linked_session_ids(quotation_id)),
                leads=_leads_for_dropdown(), preselect_session=None, signed_total=_signed_total(q),
            )
        db.execute(
            """UPDATE quotations SET client_company_id=?, attention_to=?, company_name_override=?,
                   address=?, tel=?, quote_date=?, ref_no=?, course_title=?, is_hrdcorp=?,
                   title_override=?, training_mode=?, venue=?, valid_until=?, terms=?, sst_rate=?,
                   sst_inclusive=?, status=?, notes=?
               WHERE id=?""",
            (
                fields["client_company_id"], fields["attention_to"],
                fields["company_name_override"], fields["address"], fields["tel"], fields["quote_date"],
                fields["ref_no"], fields["course_title"], fields["is_hrdcorp"], fields["title_override"],
                fields["training_mode"], fields["venue"], fields["valid_until"], fields["terms"],
                fields["sst_rate"], fields["sst_inclusive"], fields["status"], fields["notes"], quotation_id,
            ),
        )
        old_items = db.query("SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY id", (quotation_id,))
        old_total = _totals(old_items, q["sst_rate"], q["sst_inclusive"])[2]
        old_ids = linked_session_ids(quotation_id)
        db.execute("DELETE FROM quotation_items WHERE quotation_id = ?", (quotation_id,))
        new_ids = _save_items(quotation_id, request.form)
        _flash_share_problems(quotation_id)
        new_items = db.query("SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY id", (quotation_id,))
        new_total = _totals(new_items, fields["sst_rate"], fields["sst_inclusive"])[2]
        # Fix125: every amount change is in the Activity Log with old -> new,
        # so a price that moves is always traceable to who changed it and when.
        detail = ""
        if round(old_total, 2) != round(new_total, 2):
            detail = f" - total changed from RM {fmtmoney(old_total)} to RM {fmtmoney(new_total)}"
        activity.log("update", "quotation", quotation_id, f"Updated quotation {q['quote_no']}{detail}")
        # Fix140: a signed quotation linked to a class only now still fills
        # that class's Grant Documents Quotation slot (no emails sent).
        if q["signed_file"]:
            added = [sid for sid in new_ids if sid not in old_ids and fill_grant_quotation(quotation_id, sid)]
            if added:
                flash(f"The quotation was added to the HRDCorp Grant Documents of {len(added)} newly linked "
                      f"class{'es' if len(added) > 1 else ''}.", "info")
        # Fix193: a class taken off the quotation (e.g. a dropped batch) is
        # left as it is - say so, in case it should be cancelled.
        dropped = [sid for sid in old_ids if sid not in new_ids]
        for sid in dropped:
            row = db.query("SELECT cs.status, c.title FROM course_sessions cs JOIN courses c ON c.id = cs.course_id "
                           "WHERE cs.id = ?", (sid,), one=True)
            if row and row["status"] not in ("Cancelled", "Completed"):
                flash(f"{row['title']} is no longer on this quotation. Its class is still {row['status']}: "
                      "cancel it on the class page if that batch is off.", "warning")
        flash("Quotation updated.", "success")
        if detail and (q["status"] == "Accepted" or q["signed_file"]):
            flash(f"Heads up: the client signed this quotation at RM {fmtmoney(old_total)}. The total is now "
                  f"RM {fmtmoney(new_total)}, which isn't what they signed. Use Revise if this is a new offer.",
                  "warning")
        return redirect(url_for("quotations.view", quotation_id=quotation_id))

    items = db.query("SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY id", (quotation_id,))
    return render_template(
        "quotations/form.html", quotation=q, is_edit=True, companies=companies, courses=courses,
        statuses=STATUSES, training_types=TRAINING_TYPES, training_modes=TRAINING_MODES,
        today=q["quote_date"], default_valid_until=q["valid_until"], preselect_company=None,
        default_terms=q["terms"], items=_with_links(quotation_id, items),
        linkable_sessions=_linkable_sessions(linked_session_ids(quotation_id)),
        leads=_leads_for_dropdown(),
        preselect_session=None, signed_total=_signed_total(q),
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
    flash(f"New revision created, now {new_quote_no}.", "success")
    return redirect(url_for("quotations.view", quotation_id=quotation_id))


@bp.route("/<int:quotation_id>/status", methods=("POST",))
@login_required
def update_status(quotation_id):
    status = request.form.get("status")
    if status in STATUSES:
        db.execute("UPDATE quotations SET status = ? WHERE id = ?", (status, quotation_id))
        flash(f"Quotation marked as {status}.", "success")
        if status == "Cancelled":
            # Fix100: the page then offers to cancel the linked class too
            # (and from there its POs); nothing else is cancelled on its own.
            open_classes = [sid for sid in linked_session_ids(quotation_id) if (db.query(
                "SELECT status FROM course_sessions WHERE id = ?", (sid,), one=True) or {"status": "Cancelled"}
            )["status"] != "Cancelled"]
            if len(open_classes) == 1:
                flash("Its class is still open. Cancel the class too if the training is off.", "warning")
            elif open_classes:
                flash(f"{len(open_classes)} of its classes are still open. Cancel them on their class pages "
                      "if the trainings are off.", "warning")
    return redirect(url_for("quotations.view", quotation_id=quotation_id))


@bp.route("/<int:quotation_id>/send-email", methods=("POST",))
@login_required
def send_email(quotation_id):
    q = db.query(
        """SELECT q.*, co.name AS client_company_name, co.address AS client_company_address,
                  co.city AS client_company_city, co.postcode AS client_company_postcode,
                  co.state AS client_company_state,
                  co.phone AS client_company_phone, co.email AS client_company_email,
                  u.name AS created_by_name, u.position AS created_by_position,
                  u.contact_phone AS created_by_phone, u.signature_file AS created_by_signature,
                  (SELECT l.email FROM leads l
                     WHERE l.company_id = q.client_company_id AND l.name = q.attention_to
                     ORDER BY l.email IS NULL, l.id LIMIT 1) AS pic_email
           FROM quotations q
           LEFT JOIN companies co ON co.id = q.client_company_id
           LEFT JOIN users u ON u.id = q.created_by
           WHERE q.id = ?""",
        (quotation_id,), one=True,
    )
    if q is None:
        flash("Quotation not found.", "danger")
        return redirect(url_for("quotations.index"))

    # Prefer the PIC's (Attention To) own email over the company's generic
    # one — a quotation is addressed to a specific person, so that's who it
    # should default to being emailed to; the company email is only a
    # fallback for when that PIC isn't in Leads / has no email on file.
    to_email = request.form.get("to_email") or q["pic_email"] or q["client_company_email"]
    if not to_email:
        flash("No client email on file. Add one on the client's profile, or type an address to send to.", "danger")
        return redirect(url_for("quotations.view", quotation_id=quotation_id))

    items = db.query("SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY id", (quotation_id,))
    subtotal, sst_amount, grand_total = _totals(items, q["sst_rate"], q["sst_inclusive"])
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
    saved_path = os.path.join(_quotation_upload_dir(quotation_id), stored_name)
    file_storage.save(saved_path)

    client_email = (request.form.get("client_email") or "").strip() or None
    db.execute(
        "UPDATE quotations SET signed_file = ?, signed_received_at = datetime('now'), "
        "signed_received_via = 'staff_upload', status = 'Accepted' WHERE id = ?",
        (stored_name, quotation_id),
    )
    activity.log("update", "quotation", quotation_id, f"Uploaded signed copy of quotation {q['quote_no']}")
    ai_warning = doc_sanity.check_document(saved_path, "signed_quotation")
    _handle_quotation_signed(quotation_id, client_email, ai_warning=ai_warning)
    flash("Signed quotation recorded. The client has been sent their T3 Attendance Form link and a "
          "calendar invite (if a class was linked and an email was on file).", "success")
    if ai_warning:
        flash(ai_warning, "warning")
    return redirect(url_for("quotations.view", quotation_id=quotation_id))


@bp.route("/<int:quotation_id>/signed-file")
@login_required
def download_signed(quotation_id):
    q = db.query("SELECT signed_file FROM quotations WHERE id = ?", (quotation_id,), one=True)
    if q is None or not q["signed_file"]:
        flash("No signed copy on file for this quotation.", "danger")
        return redirect(url_for("quotations.view", quotation_id=quotation_id))
    return send_from_directory(_quotation_upload_dir(quotation_id), q["signed_file"], as_attachment=False)


@bp.route("/<int:quotation_id>/class/<int:session_id>/pdf")
@login_required
def class_pdf(quotation_id, session_id):
    """Fix193: one class's lines and amount as its own quotation PDF (what
    goes into that class's HRDCorp Grant Documents)."""
    try:
        made = class_quotation_pdf(quotation_id, session_id)
    except Exception:  # noqa: BLE001 - surface a clean message rather than a 500
        current_app.logger.exception("Failed to generate class quotation PDF %s/%s", quotation_id, session_id)
        flash("Couldn't generate the PDF. Is wkhtmltopdf installed on the server?", "danger")
        return redirect(url_for("quotations.view", quotation_id=quotation_id))
    if made is None:
        flash("That class isn't on this quotation.", "warning")
        return redirect(url_for("quotations.view", quotation_id=quotation_id))
    filename, pdf_bytes = made
    return Response(pdf_bytes, mimetype="application/pdf",
                    headers={"Content-Disposition": content_disposition(filename)})


def _guess_course(programme, courses):
    """The course whose title best matches a line's programme (or None)."""
    best, best_score = None, 0.0
    text = (programme or "").strip().lower()
    for c in courses:
        title = c["title"].strip().lower()
        score = 1.0 if title == text else difflib.SequenceMatcher(None, text, title).ratio()
        if title and (title in text or text in title):
            score = max(score, 0.85)
        if score > best_score:
            best, best_score = c, score
    return best["id"] if best is not None and best_score >= 0.6 else None


def _class_schedule_from_line(item, start, end):
    """Fix204: a new class made from a quotation line gets the line's per-day
    times. For another batch (a different start date) the same day-by-day
    pattern is moved to its own dates. Returns (end_date, day_times)."""
    from datetime import date as _date
    from . import schedule
    if not item["day_times"]:
        return end, None
    day_list = schedule.days(None, None, None, item["day_times"])
    if start == item["item_date"]:
        return end or (item["item_date_end"]), item["day_times"]
    try:
        first = _date.fromisoformat(start)
    except ValueError:
        return end, None
    offset = first - day_list[0][0]
    moved = [(d + offset, s, e) for d, s, e in day_list]
    return moved[-1][0].isoformat(), schedule.to_json(moved)


def _guess_existing(item, q, course_id, existing):
    """Fix198: an open class that looks like this line's - same client and
    start date (and the same course when more than one does), or None."""
    if not item["item_date"]:
        return None
    same = [s for s in existing if s["start_date"] == item["item_date"]
            and (not q["client_company_id"] or s["client_company_id"] == q["client_company_id"])]
    if len(same) > 1 and course_id:
        same = [s for s in same if s["course_id"] == course_id] or same
    return same[0]["id"] if len(same) == 1 else None


@bp.route("/<int:quotation_id>/create-classes", methods=("GET", "POST"))
@login_required
def create_classes(quotation_id):
    """Fix193/Fix198/Fix201: link each line that has no class yet to its
    class(es) - existing Proposed/Scheduled classes, or new Proposed classes
    made from the line (course and dates per class; time, pax and training
    type from the line; client, contact, venue and mode from the quotation).
    One line can get several classes (e.g. one line quoted for 4 batches);
    its fee is then split equally between them (editable on the quotation)."""
    q = db.query("SELECT * FROM quotations WHERE id = ?", (quotation_id,), one=True)
    if q is None:
        flash("Quotation not found.", "danger")
        return redirect(url_for("quotations.index"))
    items = db.query(
        """SELECT * FROM quotation_items WHERE quotation_id = ?
             AND id NOT IN (SELECT item_id FROM quotation_item_classes) ORDER BY id""", (quotation_id,))
    courses = db.query("SELECT id, title FROM courses WHERE active = 1 ORDER BY title")
    if not items:
        flash("Every line on this quotation is already linked to a class.", "info")
        return redirect(url_for("quotations.view", quotation_id=quotation_id))
    existing = [dict(r) for r in db.query(
        """SELECT cs.id, cs.course_id, cs.client_company_id, cs.start_date, cs.end_date, cs.status,
                  c.title AS course_title, co.name AS client_name
           FROM course_sessions cs JOIN courses c ON c.id = cs.course_id
           LEFT JOIN companies co ON co.id = cs.client_company_id
           WHERE cs.status IN ('Proposed', 'Scheduled')
           ORDER BY (cs.client_company_id = ?) DESC, cs.start_date""", (q["client_company_id"] or 0,))]
    existing_ids = {r["id"] for r in existing}

    if request.method == "POST":
        chosen = set(request.form.getlist("create"))
        plan, errors = [], []  # plan: (item, [("link", sid) | ("new", course_id, start, end), ...])
        for item in items:
            key = str(item["id"])
            if key not in chosen:
                continue
            slots = []
            for n in request.form.getlist(f"slot_{key}"):
                picked = request.form.get(f"existing_{key}_{n}", type=int)
                if picked in existing_ids:
                    if ("link", picked) not in slots:
                        slots.append(("link", picked))
                    continue
                course_id = request.form.get(f"course_{key}_{n}", type=int)
                start = (request.form.get(f"start_{key}_{n}") or "").strip()
                end = (request.form.get(f"end_{key}_{n}") or "").strip() or None
                if not course_id or not start:
                    errors.append(item["programme"])
                    break
                slots.append(("new", course_id, start, end if end and end > start else None))
            if slots:
                plan.append((item, slots))
        if errors:
            flash(f"Pick an existing class, or choose the course and the start date, for: "
                  f"{', '.join(dict.fromkeys(errors))}.", "danger")
        elif not plan:
            flash("Tick at least one line to link to a class.", "warning")
        else:
            pic = db.query("SELECT id FROM leads WHERE company_id = ? AND name = ? ORDER BY id LIMIT 1",
                           (q["client_company_id"], q["attention_to"]), one=True) if q["client_company_id"] else None
            mode = q["training_mode"] or "Physical"
            linked, created, touched = 0, 0, []
            for item, slots in plan:
                sids = []
                for slot in slots:
                    if slot[0] == "link":
                        sid = slot[1]
                        linked += 1
                        activity.log("update", "session", sid,
                                     f"Linked to quotation {q['quote_no']} ({item['programme']})")
                    else:
                        _kind, course_id, start, end = slot
                        end, day_times = _class_schedule_from_line(item, start, end)
                        sid = db.execute(
                            """INSERT INTO course_sessions (course_id, client_company_id, pic_lead_id, venue,
                                   start_date, end_date, training_time, training_type, training_mode, capacity,
                                   status, session_code, owner_user_id, hrdcorp_claimable, day_times)
                               VALUES (?,?,?,?,?,?,?,?,?,?,'Proposed',?,?,?,?)""",
                            (course_id, q["client_company_id"], pic["id"] if pic else None,
                             q["venue"] if mode != "Virtual" else None, start, end, item["item_time"],
                             item["training_type"], mode, item["no_of_pax"] or 20, db.generate_session_code(),
                             g.user["id"], 1 if q["is_hrdcorp"] else 0, day_times),
                        )
                        created += 1
                        activity.log("create", "session", sid,
                                     f"Proposed class created from quotation {q['quote_no']} ({start})")
                    if sid not in sids:
                        sids.append(sid)
                _save_links(item["id"], [(sid, None) for sid in sids])
                touched += sids
            _sync_primary_session(quotation_id)
            if q["signed_file"]:
                for sid in dict.fromkeys(touched):
                    fill_grant_quotation(quotation_id, sid)
            parts = []
            if linked:
                parts.append(f"{linked} existing class{'es' if linked > 1 else ''} linked")
            if created:
                parts.append(f"{created} Proposed class{'es' if created > 1 else ''} created "
                             "(add the trainer on each class page)")
            if any(len(slots) > 1 for _i, slots in plan):
                parts.append("a line with several classes is split equally between them (change the amounts "
                             "on the quotation's Edit page)")
            flash("Done: " + "; ".join(parts) + ".", "success")
            return redirect(url_for("quotations.view", quotation_id=quotation_id))

    lines = []
    for i in items:
        guess = _guess_course(i["programme"], courses)
        lines.append(dict(i, course_guess=guess, existing_guess=_guess_existing(i, q, guess, existing)))
    return render_template("quotations/create_classes.html", q=q, lines=lines, courses=courses, existing=existing)


@bp.route("/<int:quotation_id>/delete", methods=("POST",))
@login_required
def delete(quotation_id):
    q = db.query("SELECT quote_no FROM quotations WHERE id = ?", (quotation_id,), one=True)
    db.execute("DELETE FROM quotations WHERE id = ?", (quotation_id,))
    activity.log("delete", "quotation", quotation_id, f"Deleted quotation {q['quote_no'] if q else quotation_id}")
    flash("Quotation deleted.", "success")
    return redirect(url_for("quotations.index"))
