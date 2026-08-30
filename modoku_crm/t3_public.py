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
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

from . import db, uploadutil
from .csvutil import csv_response
from .sessions import t3_form_is_editable
from .t3 import GENDERS, CITIZENSHIPS, _ic_taken, _normalize_gender, _parse_csv

bp = Blueprint("t3_public", __name__, url_prefix="/t3-form")


def _find_session(token):
    if not token:
        return None
    return db.query(
        """SELECT cs.*, c.title AS course_title FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id WHERE cs.t3_public_token = ?""",
        (token,), one=True,
    )


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
    return render_template("t3_public/form.html", s=session_row, participants=participants, token=token,
                            editable=t3_form_is_editable(session_row), genders=GENDERS,
                            citizenships=CITIZENSHIPS)


@bp.route("/<token>/add", methods=("POST",))
def add(token):
    session_row = _find_session(token)
    if session_row is None:
        return render_template("t3_public/not_found.html")
    if not t3_form_is_editable(session_row):
        flash("This attendance list is now locked — training has already started. Contact us if you need "
              "a change.", "danger")
        return redirect(url_for("t3_public.form", token=token))

    name = request.form.get("name", "").strip()
    ic_no = request.form.get("ic_no") or None
    if not name:
        flash("Name is required.", "danger")
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
        flash("This attendance list is now locked — training has already started. Contact us if you need "
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
        flash("Couldn't read that CSV file — check the format and try again.", "danger")
        return redirect(url_for("t3_public.form", token=token))

    if not participants:
        flash("No participant rows found in that CSV.", "danger")
        return redirect(url_for("t3_public.form", token=token))

    seen_ics = set()
    added = 0
    skipped = 0
    for name, ic_no, employer, gender, citizenship in participants:
        ic_key = (ic_no or "").strip().lower()
        if ic_key and (ic_key in seen_ics or _ic_taken(session_row["id"], ic_no)):
            skipped += 1
            continue
        if ic_key:
            seen_ics.add(ic_key)
        db.execute(
            """INSERT INTO t3_participants (session_id, name, ic_no, employer_name, gender, citizenship)
               VALUES (?,?,?,?,?,?)""",
            (session_row["id"], name, ic_no, employer, gender, citizenship or "Malaysian"),
        )
        added += 1

    if skipped:
        flash(f"Imported {added} participant(s). Skipped {skipped} with a duplicate IC number already "
              f"on this attendance list.", "warning" if added else "danger")
    else:
        flash(f"Imported {added} participant(s) from CSV.", "success")
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
        flash("This attendance list is now locked — training has already started. Contact us if you need "
              "a change.", "danger")
        return redirect(url_for("t3_public.form", token=token))

    name = request.form.get("name", "").strip()
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
        flash("This attendance list is now locked — training has already started. Contact us if you need "
              "a change.", "danger")
        return redirect(url_for("t3_public.form", token=token))

    db.execute("DELETE FROM t3_participants WHERE id = ? AND session_id = ?", (participant_id, session_row["id"]))
    flash("Participant removed.", "success")
    return redirect(url_for("t3_public.form", token=token))
