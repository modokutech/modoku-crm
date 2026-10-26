"""Public, always-editable "T3 Attendance Form" — no login required.

Reached via a per-class link (course_sessions.t3_public_token — see
sessions.ensure_t3_public_token), sent to the client automatically once
their signed quotation comes back (quotations._handle_quotation_signed), and
also sendable to the trainer via a button on the class page. Unlike the
Return Attendance Form (attendance_return.py, a one-time "submit the signed
photo" flow) this page is real-time: participants can be added, edited, or
removed at any time right up until the day of class (sessions.t3_form_is_editable),
matching the client's own copy of the list kept in Modoku Hub — it *is* the
same t3_participants list staff manage internally from t3.py.

When a class has e-Signature attendance turned on (t3.toggle_e_signature),
this SAME page also becomes where participants sign their own row on a
scheduled training day — see `sign()` below. It's deliberately still the
one shared list link, not a separate form: everyone sees the same
participant list they already know, and just taps their own name to sign
it, the same way they'd sign a printed sheet passed around the room.
"""
import os
import uuid
from datetime import date, datetime, timedelta

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for

from . import APP_TZ, attendance_days, db, uploadutil
from . import certificates as _certificates
from .csvutil import csv_response
from .sessions import t3_form_is_editable
from .t3 import (
    GENDERS, CITIZENSHIPS, _ic_taken, _normalize_gender, _parse_csv,
    _insert_participants, _bulk_result_flash, t3_remaining_capacity,
    check_and_record_sign_attempt, decode_signature_png, _t3_signature_dir,
    _title_case_name, class_code, check_and_record_code_attempt, sign_in_png_response,
)

bp = Blueprint("t3_public", __name__, url_prefix="/t3-form")


def _find_session(token):
    if not token:
        return None
    return db.query(
        """SELECT cs.*, c.title AS course_title FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id WHERE cs.t3_public_token = ?""",
        (token,), one=True,
    )


@bp.route("/<token>/sign-in-qr")
def sign_in_qr(token):
    """Fix157: the full-screen sign-in QR, public - a link staff can give the
    trainer (no login) to put on the training-room screen. Shows the QR only
    while the class's e-Signature is on."""
    session_row = _find_session(token)
    if session_row is None:
        return render_template("t3_public/not_found.html")
    code = class_code(session_row)
    return render_template("t3/sign_in_qr.html", s=session_row, public=True, token=token, code=code,
                            sign_url=url_for("t3_public.form", token=token, _external=True,
                                             **({"code": code} if code else {})))


@bp.route("/<token>/sign-in-qr.png")
def sign_in_qr_png(token):
    session_row = _find_session(token)
    if session_row is None or not session_row["e_signature_enabled"]:
        return Response(status=404)
    code = class_code(session_row)  # Fix207: the QR carries today's class code
    return sign_in_png_response(session_row, url_for("t3_public.form", token=token, _external=True,
                                                     **({"code": code} if code else {})), code)


@bp.route("/example.csv")
def example_csv():
    """A ready-to-fill example CSV, so a client isn't guessing the expected
    column order — matches _parse_csv's recognized headers exactly."""
    rows = [
        ("Ahmad Bin Ali", "900101-10-1234", "Modoku Tech Sdn Bhd", "Male", "Malaysian"),
        ("Siti Nurhaliza", "920215-14-5678", "Modoku Tech Sdn Bhd", "Female", "Malaysian"),
    ]
    return csv_response("t3_attendance_example.csv",
                         ["Name", "IC No", "Employer", "Gender", "Citizenship"], rows)


@bp.route("/<token>")
def form(token):
    session_row = _find_session(token)
    if session_row is None:
        return render_template("t3_public/not_found.html")
    participants = db.query(
        "SELECT * FROM t3_participants WHERE session_id = ? ORDER BY id", (session_row["id"],)
    )
    # e-Signature is only ever offered on an actual scheduled training day —
    # never early (nothing to sign for yet) and never after the fact (that's
    # what the AI-match / manual "Mark Attended" paths on the staff side are
    # for). Outside that window the page looks exactly like it always has.
    today_iso = date.today().isoformat()
    e_sign_open = bool(session_row["e_signature_enabled"]) and \
        today_iso in attendance_days.training_days_iso_for_session(session_row)
    signed_today = set()
    if e_sign_open:
        rows = db.query(
            """SELECT tda.participant_id FROM t3_day_attendance tda
               JOIN t3_participants p ON p.id = tda.participant_id
               WHERE p.session_id = ? AND tda.training_date = ?""",
            (session_row["id"], today_iso),
        )
        signed_today = {r["participant_id"] for r in rows}
    code_mode = bool(session_row["code_attendance"])
    certificates_ready = code_mode and certificates_released(session_row) and bool(db.query(
        "SELECT 1 FROM t3_participants WHERE session_id = ? AND attended = 1 LIMIT 1", (session_row["id"],), one=True))
    return render_template("t3_public/form.html", s=session_row, participants=participants, token=token,
                            editable=t3_form_is_editable(session_row), genders=GENDERS,
                            citizenships=CITIZENSHIPS, remaining=t3_remaining_capacity(session_row),
                            e_sign_open=e_sign_open, signed_today=signed_today, today_iso=today_iso,
                            code_mode=code_mode, prefill_code=(request.args.get("code") or "").strip().upper(),
                            certificates_ready=certificates_ready)


@bp.route("/<token>/<int:participant_id>/sign", methods=("POST",))
def sign(token, participant_id):
    """A participant signs their own row on today's training day, from
    their own phone. Deliberately narrow: the server — never the client —
    decides what "today" is and whether signing is even open right now, the
    signature image is validated server-side regardless of what the canvas
    claims, and the identity check (re-entering the IC number already on
    file) is the same check every time, with the same lockout after
    repeated wrong guesses. A successful sign is just another
    attendance_days.mark_day_attended() call, source="e-signature" — it
    plugs into the exact same multi-day certificate-eligibility rollup the
    AI-match and manual "Mark Attended" flows already use."""
    session_row = _find_session(token)
    if session_row is None:
        return render_template("t3_public/not_found.html")

    if not session_row["e_signature_enabled"]:
        flash("e-Signature attendance isn't turned on for this class.", "danger")
        return redirect(url_for("t3_public.form", token=token))

    today_iso = date.today().isoformat()
    if today_iso not in attendance_days.training_days_iso_for_session(session_row):
        flash("Signing is only open on a scheduled training day.", "danger")
        return redirect(url_for("t3_public.form", token=token))

    participant = db.query("SELECT * FROM t3_participants WHERE id = ? AND session_id = ?",
                            (participant_id, session_row["id"]), one=True)
    if participant is None:
        flash("Participant not found on this attendance list.", "danger")
        return redirect(url_for("t3_public.form", token=token))

    # Fix207: a Code Attendance class checks today's class code, not the IC
    code_mode = bool(session_row["code_attendance"])
    back = url_for("t3_public.form", token=token,
                   **({"code": request.form.get("class_code", "").strip().upper()} if code_mode else {}))
    if code_mode:
        ok, error = check_and_record_code_attempt(participant, session_row, request.form.get("class_code", ""))
    else:
        ok, error = check_and_record_sign_attempt(participant, request.form.get("ic_no", ""))
    if not ok:
        flash(error, "danger")
        return redirect(back)

    raw_png = decode_signature_png(request.form.get("signature_data", ""))
    if raw_png is None:
        flash("We couldn't capture that signature. Please sign again and submit.", "danger")
        return redirect(back)

    stored_name = f"{participant_id}-{today_iso}-{uuid.uuid4().hex[:8]}.png"
    with open(os.path.join(_t3_signature_dir(session_row["id"]), stored_name), "wb") as fh:
        fh.write(raw_png)

    newly_fully_attended = attendance_days.mark_day_attended(
        participant_id, session_row["id"], today_iso, source="e-signature",
        signature_file=stored_name, signed_ip=request.remote_addr,
    )
    if newly_fully_attended:
        _certificates.generate_and_store_certificate(participant_id)

    flash(f"Thanks, {participant['name']}. Your attendance is signed for today.", "success")
    if code_mode and newly_fully_attended:
        if certificates_released(session_row):
            flash("Your certificate is ready. Tap \"Download your certificate\" below.", "success")
        else:
            flash(f"Your certificate will be ready to download from {release_text(session_row)}, "
                  "on this same page.", "info")
    return redirect(back)


CERT_RELEASE_BEFORE_END = timedelta(hours=1)


def certificates_release_at(session_row):
    """Fix208: Code Attendance certificates open 1 hour before the end of the
    last training day (that day's own end time; 5 PM if no time is set), so
    nobody downloads theirs in the morning and leaves. Malaysia time."""
    from . import schedule
    day_list = schedule.row_days(session_row)
    if not day_list:
        return None
    last_day, _start, end = day_list[-1]
    end_hhmm = end or "17:00"
    end_dt = datetime.combine(last_day, datetime.strptime(end_hhmm, "%H:%M").time())
    return end_dt - CERT_RELEASE_BEFORE_END


def certificates_released(session_row):
    release_at = certificates_release_at(session_row)
    return release_at is None or datetime.now(APP_TZ).replace(tzinfo=None) >= release_at


def release_text(session_row):
    release_at = certificates_release_at(session_row)
    if release_at is None:
        return ""
    return release_at.strftime("%-I:%M %p on %-d %b %Y")


@bp.route("/<token>/certificates")
def certificate_list(token):
    """Fix207: for a Code Attendance class (participants without IC): the
    names whose certificate is ready - each person taps their own name to
    download it. Same unguessable class link as the attendance list itself."""
    session_row = _find_session(token)
    if session_row is None or not session_row["code_attendance"]:
        return render_template("t3_public/not_found.html")
    if not certificates_released(session_row):
        return render_template("t3_public/certificates.html", s=session_row, token=token, ready=[],
                               not_yet=release_text(session_row))
    ready = db.query("SELECT id, name FROM t3_participants WHERE session_id = ? AND attended = 1 "
                     "ORDER BY name COLLATE NOCASE", (session_row["id"],))
    return render_template("t3_public/certificates.html", s=session_row, token=token, ready=ready)


@bp.route("/<token>/certificates/<int:participant_id>")
def certificate_download(token, participant_id):
    session_row = _find_session(token)
    if session_row is None or not session_row["code_attendance"]:
        return render_template("t3_public/not_found.html")
    if not certificates_released(session_row):
        flash(f"Certificates will be available from {release_text(session_row)}.", "warning")
        return redirect(url_for("t3_public.certificate_list", token=token))
    participant = db.query("SELECT id, attended FROM t3_participants WHERE id = ? AND session_id = ?",
                           (participant_id, session_row["id"]), one=True)
    if participant is None or not participant["attended"]:
        flash("That certificate isn't available yet.", "warning")
        return redirect(url_for("t3_public.certificate_list", token=token))
    download_token = _certificates.ensure_download_token(participant_id)
    if not download_token:
        flash("We couldn't prepare that certificate just now. Please try again, or contact us.", "danger")
        return redirect(url_for("t3_public.certificate_list", token=token))
    return redirect(url_for("certificates.download", token=download_token))


@bp.route("/<token>/add", methods=("POST",))
def add(token):
    session_row = _find_session(token)
    if session_row is None:
        return render_template("t3_public/not_found.html")
    if not t3_form_is_editable(session_row):
        flash("This attendance list is now locked. Training has already started. Contact us if you need "
              "a change.", "danger")
        return redirect(url_for("t3_public.form", token=token))

    name = _title_case_name(request.form.get("name", "").strip())
    ic_no = request.form.get("ic_no") or None
    if not name:
        flash("Name is required.", "danger")
        return redirect(url_for("t3_public.form", token=token))
    remaining = t3_remaining_capacity(session_row)
    if remaining is not None and remaining <= 0:
        flash(f"This class's attendance list is already full ({session_row['capacity']} pax capacity). "
              f"Contact us if you need to add someone else.", "danger")
        return redirect(url_for("t3_public.form", token=token))
    if _ic_taken(session_row["id"], ic_no):
        flash(f"IC number {ic_no} is already on this attendance list.", "danger")
        return redirect(url_for("t3_public.form", token=token))

    db.execute(
        """INSERT INTO t3_participants (session_id, name, ic_no, employer_name, gender, citizenship)
           VALUES (?,?,?,?,?,?)""",
        (session_row["id"], name, ic_no, request.form.get("employer_name") or None,
         _normalize_gender(request.form.get("gender")), request.form.get("citizenship") or "Malaysian"),
    )
    flash(f"{name} added to the attendance list.", "success")
    return redirect(url_for("t3_public.form", token=token))


@bp.route("/<token>/csv-upload", methods=("POST",))
def csv_upload(token):
    session_row = _find_session(token)
    if session_row is None:
        return render_template("t3_public/not_found.html")
    if not t3_form_is_editable(session_row):
        flash("This attendance list is now locked. Training has already started. Contact us if you need "
              "a change.", "danger")
        return redirect(url_for("t3_public.form", token=token))

    file_storage = request.files.get("csv_file")
    if not file_storage or not file_storage.filename:
        flash("Choose a CSV file first.", "danger")
        return redirect(url_for("t3_public.form", token=token))
    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.CSV_EXTENSIONS)
    if error:
        flash(error, "danger")
        return redirect(url_for("t3_public.form", token=token))

    try:
        participants = _parse_csv(file_storage)
    except Exception:
        flash("Couldn't read that CSV file. Check the format and try again.", "danger")
        return redirect(url_for("t3_public.form", token=token))

    if not participants:
        flash("No participant rows found in that CSV.", "danger")
        return redirect(url_for("t3_public.form", token=token))

    remaining = t3_remaining_capacity(session_row)
    added, skipped_dup, skipped_capacity = _insert_participants(session_row["id"], participants, remaining)
    msg, category = _bulk_result_flash(added, skipped_dup, skipped_capacity, session_row["capacity"], verb="Imported")
    flash(msg, category)
    return redirect(url_for("t3_public.form", token=token))


@bp.route("/<token>/<int:participant_id>/edit", methods=("POST",))
def edit(token, participant_id):
    session_row = _find_session(token)
    if session_row is None:
        return render_template("t3_public/not_found.html")
    participant = db.query("SELECT * FROM t3_participants WHERE id = ? AND session_id = ?",
                            (participant_id, session_row["id"]), one=True)
    if participant is None:
        flash("Participant not found.", "danger")
        return redirect(url_for("t3_public.form", token=token))
    if not t3_form_is_editable(session_row):
        flash("This attendance list is now locked. Training has already started. Contact us if you need "
              "a change.", "danger")
        return redirect(url_for("t3_public.form", token=token))

    name = _title_case_name(request.form.get("name", "").strip())
    ic_no = request.form.get("ic_no") or None
    if not name:
        flash("Name is required.", "danger")
        return redirect(url_for("t3_public.form", token=token))
    if _ic_taken(session_row["id"], ic_no, exclude_participant_id=participant_id):
        flash(f"IC number {ic_no} is already on this attendance list.", "danger")
        return redirect(url_for("t3_public.form", token=token))

    db.execute(
        """UPDATE t3_participants SET name=?, ic_no=?, employer_name=?, gender=?, citizenship=? WHERE id=?""",
        (name, ic_no, request.form.get("employer_name") or None,
         _normalize_gender(request.form.get("gender")), request.form.get("citizenship") or "Malaysian",
         participant_id),
    )
    flash("Participant updated.", "success")
    return redirect(url_for("t3_public.form", token=token))


@bp.route("/<token>/<int:participant_id>/delete", methods=("POST",))
def delete(token, participant_id):
    session_row = _find_session(token)
    if session_row is None:
        return render_template("t3_public/not_found.html")
    if not t3_form_is_editable(session_row):
        flash("This attendance list is now locked. Training has already started. Contact us if you need "
              "a change.", "danger")
        return redirect(url_for("t3_public.form", token=token))

    db.execute("DELETE FROM t3_participants WHERE id = ? AND session_id = ?", (participant_id, session_row["id"]))
    flash("Participant removed.", "success")
    return redirect(url_for("t3_public.form", token=token))
