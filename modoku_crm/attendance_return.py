"""Public "Return Attendance Form" flow — no login required.

After training ends, the trainer collects the signed T3 attendance sheet
from participants and needs to get it back to the office, usually by
photographing/scanning it on their phone. Rather than emailing files back
and forth, they open this page, type in the short code stamped on the
printed form (see sessions.py's t3_attendance_form + db.generate_session_code),
see the class it belongs to as confirmation, then snap/upload the photo(s)
directly — landing in Modoku Hub against that class for the office to see.
Multiple files can be submitted at once (one photo per page is the common
case for a multi-page sheet), or a single already-compiled PDF works too —
see uploadutil.RETURN_ATTENDANCE_EXTENSIONS.

If AI attendance matching is configured (see ai_match.py), submitting here
also triggers it immediately and automatically: each photo/PDF is read, and
whoever signed is marked attended (and their e-Certificate generated) with
no staff review step — see auto_mark_attendance for the one guardrail kept
(a name that can't be confidently matched is left for a quick manual look
on the AI Match Attendance page, rather than guessed).

Each submitted photo (not PDFs) is first run through image_compress.py —
resized/re-encoded to a much smaller file with no visible loss in quality,
since a modern phone's 30-50 MP camera produces a far bigger JPEG than
anyone reading a signed attendance sheet needs. That compressed copy is
what's actually stored as `filename` and treated as "the original" from
then on (best-effort: if compression fails for any reason, the raw upload
is stored as-is instead).

Each submitted photo (not PDFs - see scan_enhance.py) also gets a "scan
mode" version generated alongside it - straightened and contrast-enhanced
so it's easier to read back in the office than a raw phone photo. That's a
SEPARATE file (attendance_returns.enhanced_filename); the stored `filename`
copy is never modified or replaced by scan mode, so staff can always fall
back to it.
"""
import os
import uuid

from flask import (Blueprint, current_app, flash, redirect, render_template,
                    request, url_for)
from werkzeug.utils import secure_filename

from . import ai_match, attendance_days, db, doc_sanity, image_compress, mailer, notifications, scan_enhance, uploadutil
from . import fmtdate, fmtdaterange
from . import settings as settings_module

bp = Blueprint("attendance_return", __name__, url_prefix="/attendance")


def _session_dir(session_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "sessions", str(session_id))
    os.makedirs(path, exist_ok=True)
    return path


def _find_session(code):
    if not code:
        return None
    return db.query(
        """SELECT cs.*, c.title AS course_title, t.name AS trainer_name FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN trainers t ON t.id = cs.trainer_id
           WHERE cs.session_code = ?""",
        (code.strip().upper(),), one=True,
    )


@bp.route("/", methods=("GET", "POST"))
def lookup():
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        session_row = _find_session(code)
        if session_row is None:
            flash("That code wasn't found, double check the code printed on the attendance form.", "danger")
            return redirect(url_for("attendance_return.lookup"))
        return redirect(url_for("attendance_return.details", code=code.strip().upper()))
    return render_template("attendance_return/lookup.html")


def _day_choices(session_row):
    """Fix105: [(iso, "Day 2 · Tue, 7 Oct 2026"), ...] for a multi-day class
    - the "Which day?" dropdown - or [] for a one-day class, which needs no
    question."""
    days = attendance_days.training_days_for_session(session_row)
    if len(days) < 2:
        return []
    return [(d.isoformat(), f"Day {i} · {d.strftime('%a')}, {fmtdate(d.isoformat())}")
            for i, d in enumerate(days, start=1)]


def _photos_needing_day(session_row, return_ids):
    """Fix107: of the photos just submitted, the ones where knowing the
    training day is all that's missing - a multi-day class's sheet whose
    date (and "(Day N)") couldn't be read, or whose read failed outright.
    The uploader is asked which day these are for, straight after
    submitting. A sheet that reads as the wrong class, or carries a date
    outside the class, isn't asked about: a day wouldn't fix that, so it
    stays with the office."""
    valid_days = attendance_days.training_days_iso_for_session(session_row)
    if len(valid_days) < 2 or not return_ids:
        return []
    marks = ",".join("?" * len(return_ids))
    rows = db.query(
        f"""SELECT * FROM attendance_returns WHERE session_id = ? AND id IN ({marks})
            AND declared_date IS NULL ORDER BY id""",
        (session_row["id"], *return_ids),
    )
    needing = []
    for row in rows:
        if row["ai_analyzed_at"] is None:
            if row["ai_error"]:
                needing.append(row)
        elif row["ai_action"] == "mismatch" and not row["ai_detected_date"]:
            _, reason = ai_match.resolve_return_date(session_row, row["ai_detected_title"], None,
                                                     declared_date=valid_days[0])
            if reason is None:  # the title checks out; only the day is unknown
                needing.append(row)
    return needing


@bp.route("/<code>")
def details(code):
    session_row = _find_session(code)
    if session_row is None:
        flash("That code wasn't found, double check the code printed on the attendance form.", "danger")
        return redirect(url_for("attendance_return.lookup"))
    return render_template("attendance_return/details.html", s=session_row, code=code.strip().upper())


@bp.route("/<code>/submit", methods=("POST",))
def submit(code):
    session_row = _find_session(code)
    if session_row is None:
        flash("That code wasn't found, double check the code printed on the attendance form.", "danger")
        return redirect(url_for("attendance_return.lookup"))

    if session_row["status"] not in ("Ongoing", "Completed"):
        flash(
            "This class hasn't started yet. The attendance form can only be submitted once "
            "training is underway or finished.", "danger",
        )
        return redirect(url_for("attendance_return.details", code=code))

    files = [f for f in request.files.getlist("photos") if f and f.filename]
    if not files:
        flash("Choose or take at least one photo of the signed form (or upload a PDF) first.", "danger")
        return redirect(url_for("attendance_return.details", code=code))

    note = request.form.get("note", "").strip() or None
    # Fix105/106: "Which day's form are you uploading?" (multi-day classes) -
    # a day means every file here is that day's sheet; "all" means several
    # days' sheets at once, each read for its own date.
    declared_date = None
    day_choices = dict(_day_choices(session_row))
    choice = request.form.get("declared_date")
    if day_choices and request.form.get("single_day") is None and choice is None:
        choice = "all"  # a form page loaded before this question existed
    if day_choices and choice != "all":
        if choice not in day_choices:
            flash("Choose which day's form you're uploading.", "danger")
            return redirect(url_for("attendance_return.details", code=code))
        declared_date = choice
    saved_count = 0
    saved_ids = []
    sanity_warnings = []
    for file_storage in files:
        # Compress before the size check - a photo (not a PDF) gets resized/
        # re-encoded first, so a raw multi-megapixel phone photo gets a
        # chance to shrink under the cap instead of being rejected outright.
        file_storage = image_compress.maybe_compress(file_storage)
        error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.RETURN_ATTENDANCE_EXTENSIONS,
                                            max_bytes=uploadutil.RETURN_ATTENDANCE_MAX_BYTES)
        if error:
            flash(error, "danger")
            return redirect(url_for("attendance_return.details", code=code))
        safe_name = secure_filename(file_storage.filename)
        stored_name = f"return_{uuid.uuid4().hex[:8]}_{safe_name}"
        saved_path = os.path.join(_session_dir(session_row["id"]), stored_name)
        file_storage.save(saved_path)

        # "Scan mode": a separate, straightened + contrast-enhanced copy for
        # easier reading - images only (a PDF is usually already a compiled
        # scan itself, and isn't what scan_enhance.py's document-detection
        # is built for). Never touches saved_path itself. Best-effort: a
        # failure here just means no enhanced copy for this one file, never
        # a blocked submission.
        enhanced_filename = None
        ext = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
        if ext in uploadutil.IMAGE_EXTENSIONS:
            candidate_name = f"scan_{uuid.uuid4().hex[:8]}.jpg"
            candidate_path = os.path.join(_session_dir(session_row["id"]), candidate_name)
            if scan_enhance.enhance_to_scan(saved_path, candidate_path):
                enhanced_filename = candidate_name

        new_id = db.execute(
            "INSERT INTO attendance_returns (session_id, filename, original_name, submitted_by_note, enhanced_filename, "
            "declared_date) VALUES (?,?,?,?,?,?)",
            (session_row["id"], stored_name, file_storage.filename, note, enhanced_filename, declared_date),
        )
        saved_count += 1
        saved_ids.append(new_id)
        warning = doc_sanity.check_document(saved_path, "t3_attendance")
        if warning:
            sanity_warnings.append((file_storage.filename, warning))

    if not saved_count:
        flash("Those files couldn't be saved. Use a photo (PNG/JPG) or a PDF.", "danger")
        return redirect(url_for("attendance_return.details", code=code))

    # AI auto-attendance: read the just-submitted photo(s) and mark whoever
    # signed as attended, fully automatically — no staff review gate. Only
    # runs at all if an admin has configured ANTHROPIC_API_KEY; if not, this
    # is a silent no-op and staff fall back to the manual Attendance List,
    # exactly as before this feature existed. Wrapped end-to-end so any AI
    # hiccup can never block the trainer's submission from succeeding.
    ai_summary = None
    if ai_match.is_configured():
        try:
            ai_match.analyze_unprocessed_returns(session_row["id"])
            ai_summary = ai_match.auto_mark_attendance(session_row["id"])
        except Exception:  # noqa: BLE001 - AI matching must never block the trainer's submission
            current_app.logger.exception("AI auto-attendance failed for session %s", session_row["id"])
    needing_day = _photos_needing_day(session_row, saved_ids) if ai_summary is not None else []

    date_range = fmtdaterange(session_row["start_date"], session_row["end_date"])
    ai_line = ""
    if ai_summary is not None:
        ai_line = f"\nAI auto-marked {ai_summary['marked']} of {ai_summary['total_read']} participant(s) attended from the photo."
        if ai_summary["unmatched"]:
            ai_line += (f" {len(ai_summary['unmatched'])} name(s) couldn't be confidently matched. "
                        f"Check the AI Match Attendance page on this class.")
        if ai_summary["mismatches"]:
            ai_line += (f" {len(ai_summary['mismatches'])} photo(s) looked like the wrong sheet "
                        f"(wrong class or date) and were NOT auto-marked. Check the AI Match "
                        f"Attendance page.")
    if needing_day:
        ai_line += (f"\nThe uploader was asked which day {len(needing_day)} photo(s) are for, as the day "
                    f"couldn't be read off the sheet.")
    sanity_line = ""
    if sanity_warnings:
        sanity_line = "\n\nNote (AI sanity-check):\n" + "\n".join(
            f"- {name}: {warning}" for name, warning in sanity_warnings
        )
    try:
        subject = f"Attendance form returned - {session_row['course_title']} ({date_range})"
        body = (
            f"The trainer has submitted {saved_count} photo(s) of the signed attendance form for:\n\n"
            f"Class: {session_row['course_title']}\n"
            f"Date: {date_range}\n"
            f"Trainer: {session_row['trainer_name'] or '-'}\n"
            + (f"Form for: {day_choices[declared_date]}\n" if declared_date else "")
            + (f"Note from trainer: {note}\n" if note else "") +
            ai_line +
            sanity_line +
            f"\n\nView it in Modoku Hub under this class's page."
        )
        notify_to = ", ".join(settings_module.get_notification_emails())
        if notify_to:
            mailer.send_email(notify_to, subject, body,
                               related_type="course_session", related_id=session_row["id"])
    except Exception:  # noqa: BLE001 - notification must never block the trainer's submission
        current_app.logger.exception("Failed to send attendance-return notification for session %s", session_row["id"])

    if ai_summary is not None:
        title = f"AI marked {ai_summary['marked']} attended - {session_row['course_title']}"
        notif_body = f"From the photo just returned by the trainer."
        if ai_summary["unmatched"]:
            notif_body += f" {len(ai_summary['unmatched'])} name(s) need a quick manual look."
        if ai_summary["mismatches"]:
            notif_body += f" {len(ai_summary['mismatches'])} photo(s) may be the wrong sheet. Check them."
        notifications.notify_admins(
            "ai_attendance_matched", title, body=notif_body,
            link=url_for("sessions.view", session_id=session_row["id"]),
        )

    return _render_success(session_row, code, ai_summary, needing_day)


def _render_success(session_row, code, ai_summary, needing_day):
    needing_ids = {row["id"] for row in needing_day}
    other_mismatches = [m for m in (ai_summary or {}).get("mismatches", []) if m["return_id"] not in needing_ids]
    return render_template("attendance_return/success.html", s=session_row, code=code.strip().upper(),
                            ai_summary=ai_summary, other_mismatches=other_mismatches,
                            needing_day=needing_day, day_choices=_day_choices(session_row))


@bp.route("/<code>/day", methods=("POST",))
def set_day(code):
    """Fix107: the uploader's answer to "Which day is this sheet for?",
    asked on the confirmation page only for photos whose day couldn't be
    read. Accepted only for this class's photos that still need a day and
    were submitted in the last few hours."""
    session_row = _find_session(code)
    if session_row is None:
        flash("That code wasn't found, double check the code printed on the attendance form.", "danger")
        return redirect(url_for("attendance_return.lookup"))
    valid_days = attendance_days.training_days_iso_for_session(session_row)
    recent = db.query(
        """SELECT id FROM attendance_returns WHERE session_id = ? AND declared_date IS NULL
           AND created_at >= datetime('now', '-6 hours')""",
        (session_row["id"],),
    )
    allowed = {row["id"] for row in _photos_needing_day(session_row, [r["id"] for r in recent])}
    answered = []
    for return_id in allowed:
        day = request.form.get(f"day_{return_id}")
        if day in valid_days:
            # Names already read stay as read: only the day is re-decided
            # (auto_mark re-resolves it). A failed read is retried below.
            db.execute(
                """UPDATE attendance_returns SET declared_date = ?, ai_action = NULL, ai_mismatch = 0,
                       ai_mismatch_reason = NULL WHERE id = ?""",
                (day, return_id),
            )
            answered.append(return_id)
    if not answered:
        flash("Choose the day for each photo.", "danger")
        return _render_success(session_row, code, None,
                               _photos_needing_day(session_row, list(allowed)))
    ai_summary = None
    try:
        ai_match.analyze_unprocessed_returns(session_row["id"])
        ai_summary = ai_match.auto_mark_attendance(session_row["id"])
    except Exception:  # noqa: BLE001 - never block the uploader
        current_app.logger.exception("AI auto-attendance failed for session %s", session_row["id"])
    return _render_success(session_row, code, ai_summary,
                           _photos_needing_day(session_row, list(allowed - set(answered))))
