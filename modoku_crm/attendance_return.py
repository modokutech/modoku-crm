"""Public "Return Attendance Form" flow — no login required.

After training ends, the trainer collects the signed T3 attendance sheet
from participants and needs to get it back to the office, usually by
photographing/scanning it on their phone. Rather than emailing files back
and forth, they open this page, type in the short code stamped on the
printed form (see sessions.py's t3_attendance_form + db.generate_session_code),
see the class it belongs to as confirmation, then snap/upload the photo(s)
directly — landing in Modoku Hub against that class for the office to see.

If AI attendance matching is configured (see ai_match.py), submitting here
also triggers it immediately and automatically: the photo is read, and
whoever signed is marked attended (and their e-Certificate generated) with
no staff review step — see auto_mark_attendance for the one guardrail kept
(a name that can't be confidently matched is left for a quick manual look
on the AI Match Attendance page, rather than guessed).
"""
import os
import uuid

from flask import (Blueprint, current_app, flash, redirect, render_template,
                    request, url_for)
from werkzeug.utils import secure_filename

from . import ai_match, db, mailer, notifications, uploadutil
from . import fmtdaterange
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
            flash("That code wasn't found — double check the code printed on the attendance form.", "danger")
            return redirect(url_for("attendance_return.lookup"))
        return redirect(url_for("attendance_return.details", code=code.strip().upper()))
    return render_template("attendance_return/lookup.html")


@bp.route("/<code>")
def details(code):
    session_row = _find_session(code)
    if session_row is None:
        flash("That code wasn't found — double check the code printed on the attendance form.", "danger")
        return redirect(url_for("attendance_return.lookup"))
    return render_template("attendance_return/details.html", s=session_row, code=code.strip().upper())


@bp.route("/<code>/submit", methods=("POST",))
def submit(code):
    session_row = _find_session(code)
    if session_row is None:
        flash("That code wasn't found — double check the code printed on the attendance form.", "danger")
        return redirect(url_for("attendance_return.lookup"))

    if session_row["status"] not in ("Ongoing", "Completed"):
        flash(
            "This class hasn't started yet — the attendance form can only be submitted once "
            "training is underway or finished.", "danger",
        )
        return redirect(url_for("attendance_return.details", code=code))

    files = [f for f in request.files.getlist("photos") if f and f.filename]
    if not files:
        flash("Choose or take at least one photo of the signed form first.", "danger")
        return redirect(url_for("attendance_return.details", code=code))

    note = request.form.get("note", "").strip() or None
    saved_count = 0
    for file_storage in files:
        error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.IMAGE_EXTENSIONS)
        if error:
            flash(error, "danger")
            return redirect(url_for("attendance_return.details", code=code))
        safe_name = secure_filename(file_storage.filename)
        stored_name = f"return_{uuid.uuid4().hex[:8]}_{safe_name}"
        file_storage.save(os.path.join(_session_dir(session_row["id"]), stored_name))
        db.execute(
            "INSERT INTO attendance_returns (session_id, filename, original_name, submitted_by_note) VALUES (?,?,?,?)",
            (session_row["id"], stored_name, file_storage.filename, note),
        )
        saved_count += 1

    if not saved_count:
        flash("Those files couldn't be saved — use a photo (PNG/JPG) or a PDF.", "danger")
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

    date_range = fmtdaterange(session_row["start_date"], session_row["end_date"])
    ai_line = ""
    if ai_summary is not None:
        ai_line = f"\nAI auto-marked {ai_summary['marked']} of {ai_summary['total_read']} participant(s) attended from the photo."
        if ai_summary["unmatched"]:
            ai_line += (f" {len(ai_summary['unmatched'])} name(s) couldn't be confidently matched — "
                        f"check the AI Match Attendance page on this class.")
        if ai_summary["mismatches"]:
            ai_line += (f" {len(ai_summary['mismatches'])} photo(s) looked like the wrong sheet "
                        f"(wrong class or date) and were NOT auto-marked — check the AI Match "
                        f"Attendance page.")
    try:
        subject = f"Attendance form returned — {session_row['course_title']} ({date_range})"
        body = (
            f"The trainer has submitted {saved_count} photo(s) of the signed attendance form for:\n\n"
            f"Class: {session_row['course_title']}\n"
            f"Date: {date_range}\n"
            f"Trainer: {session_row['trainer_name'] or '-'}\n"
            + (f"Note from trainer: {note}\n" if note else "") +
            ai_line +
            f"\n\nView it in Modoku Hub under this class's page."
        )
        notify_to = ", ".join(settings_module.get_notification_emails())
        if notify_to:
            mailer.send_email(notify_to, subject, body,
                               related_type="course_session", related_id=session_row["id"])
    except Exception:  # noqa: BLE001 - notification must never block the trainer's submission
        current_app.logger.exception("Failed to send attendance-return notification for session %s", session_row["id"])

    if ai_summary is not None:
        title = f"AI marked {ai_summary['marked']} attended — {session_row['course_title']}"
        notif_body = f"From the photo just returned by the trainer."
        if ai_summary["unmatched"]:
            notif_body += f" {len(ai_summary['unmatched'])} name(s) need a quick manual look."
        if ai_summary["mismatches"]:
            notif_body += f" {len(ai_summary['mismatches'])} photo(s) may be the wrong sheet — check them."
        notifications.notify_admins(
            "ai_attendance_matched", title, body=notif_body,
            link=url_for("sessions.view", session_id=session_row["id"]),
        )

    return render_template("attendance_return/success.html", s=session_row, ai_summary=ai_summary)
