from flask import Blueprint, flash, redirect, render_template, request, url_for

from . import activity, db
from .auth import admin_required, login_required
from .csvutil import csv_response

bp = Blueprint("enrollments", __name__, url_prefix="/enrollments")

STATUSES = ["Registered", "Attended", "Completed", "Cancelled", "No-show"]
HRDF_STATUSES = ["Not Applicable", "Pending", "Approved", "Claimed", "Rejected"]
GENDERS = ["Male", "Female"]
CITIZENSHIPS = ["Malaysian", "Non-Malaysian"]


def _filtered_enrollments():
    hrdf = request.args.get("hrdf", "")
    sql = """SELECT e.*, cs.start_date, c.title AS course_title, co.name AS company_name
              FROM enrollments e
              JOIN course_sessions cs ON cs.id = e.session_id
              JOIN courses c ON c.id = cs.course_id
              LEFT JOIN companies co ON co.id = e.company_id
              WHERE 1=1"""
    args = []
    if hrdf:
        sql += " AND e.hrdf_claim_status = ?"
        args.append(hrdf)
    sql += " ORDER BY e.created_at DESC"
    return db.query(sql, args), hrdf


@bp.route("/")
@login_required
def index():
    enrollments, hrdf = _filtered_enrollments()
    return render_template("enrollments/list.html", enrollments=enrollments,
                            hrdf_statuses=HRDF_STATUSES, current_hrdf=hrdf)


@bp.route("/export")
@admin_required
def export():
    enrollments, _hrdf = _filtered_enrollments()
    rows = (
        (e["participant_name"], e["course_title"], e["start_date"], e["company_name"] or "",
         e["participant_email"] or "", e["participant_phone"] or "", e["status"],
         e["hrdf_claim_status"], e["hrdf_claim_no"] or "", e["amount"])
        for e in enrollments
    )
    return csv_response(
        "enrollments.csv",
        ["Participant", "Course", "Class Date", "Company", "Email", "Phone", "Status",
         "HRDF Claim Status", "HRDF Claim No", "Amount"],
        rows,
    )


@bp.route("/new", methods=("GET", "POST"))
@login_required
def new():
    sessions = db.query(
        """SELECT cs.id, cs.start_date, c.title FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           WHERE cs.status != 'Cancelled' ORDER BY cs.start_date DESC"""
    )
    companies = db.query("SELECT id, name FROM companies ORDER BY name")
    leads = db.query("SELECT id, name FROM leads ORDER BY name")
    preselect_session = request.args.get("session_id", type=int)

    if request.method == "POST":
        session_id = request.form.get("session_id")
        participant_name = request.form.get("participant_name", "").strip()
        if not session_id or not participant_name:
            flash("Session and participant name are required.", "danger")
        else:
            eid = db.execute(
                """INSERT INTO enrollments (session_id, lead_id, company_id, participant_name,
                       participant_email, participant_phone, ic_no, gender, citizenship,
                       status, hrdf_claim_status, hrdf_claim_no, amount, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    session_id,
                    request.form.get("lead_id") or None,
                    request.form.get("company_id") or None,
                    participant_name,
                    request.form.get("participant_email") or None,
                    request.form.get("participant_phone") or None,
                    request.form.get("ic_no") or None,
                    request.form.get("gender") or None,
                    request.form.get("citizenship") or "Malaysian",
                    request.form.get("status") or "Registered",
                    request.form.get("hrdf_claim_status") or "Not Applicable",
                    request.form.get("hrdf_claim_no") or None,
                    request.form.get("amount") or 0,
                    request.form.get("notes") or None,
                ),
            )
            activity.log("create", "enrollment", eid, f"Enrolled {participant_name}")
            flash("Participant enrolled.", "success")
            return redirect(url_for("sessions.view", session_id=session_id))

    return render_template("enrollments/form.html", enrollment=None, sessions=sessions,
                            companies=companies, leads=leads, statuses=STATUSES,
                            hrdf_statuses=HRDF_STATUSES, genders=GENDERS,
                            citizenships=CITIZENSHIPS, preselect_session=preselect_session)


@bp.route("/<int:enrollment_id>/edit", methods=("GET", "POST"))
@login_required
def edit(enrollment_id):
    enrollment = db.query("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,), one=True)
    if enrollment is None:
        flash("Enrollment not found.", "danger")
        return redirect(url_for("enrollments.index"))
    sessions = db.query(
        """SELECT cs.id, cs.start_date, c.title FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id ORDER BY cs.start_date DESC"""
    )
    companies = db.query("SELECT id, name FROM companies ORDER BY name")
    leads = db.query("SELECT id, name FROM leads ORDER BY name")

    if request.method == "POST":
        session_id = request.form.get("session_id")
        participant_name = request.form.get("participant_name", "").strip()
        if not session_id or not participant_name:
            flash("Session and participant name are required.", "danger")
        else:
            db.execute(
                """UPDATE enrollments SET session_id=?, lead_id=?, company_id=?, participant_name=?,
                       participant_email=?, participant_phone=?, ic_no=?, gender=?, citizenship=?,
                       status=?, hrdf_claim_status=?, hrdf_claim_no=?, amount=?, notes=? WHERE id=?""",
                (
                    session_id,
                    request.form.get("lead_id") or None,
                    request.form.get("company_id") or None,
                    participant_name,
                    request.form.get("participant_email") or None,
                    request.form.get("participant_phone") or None,
                    request.form.get("ic_no") or None,
                    request.form.get("gender") or None,
                    request.form.get("citizenship") or "Malaysian",
                    request.form.get("status") or "Registered",
                    request.form.get("hrdf_claim_status") or "Not Applicable",
                    request.form.get("hrdf_claim_no") or None,
                    request.form.get("amount") or 0,
                    request.form.get("notes") or None,
                    enrollment_id,
                ),
            )
            activity.log("update", "enrollment", enrollment_id, f"Updated enrollment for {participant_name}")
            flash("Enrollment updated.", "success")
            return redirect(url_for("sessions.view", session_id=session_id))

    return render_template("enrollments/form.html", enrollment=enrollment, sessions=sessions,
                            companies=companies, leads=leads, statuses=STATUSES,
                            hrdf_statuses=HRDF_STATUSES, genders=GENDERS,
                            citizenships=CITIZENSHIPS, preselect_session=None)


@bp.route("/<int:enrollment_id>/delete", methods=("POST",))
@login_required
def delete(enrollment_id):
    e = db.query("SELECT session_id, participant_name FROM enrollments WHERE id = ?", (enrollment_id,), one=True)
    db.execute("DELETE FROM enrollments WHERE id = ?", (enrollment_id,))
    activity.log("delete", "enrollment", enrollment_id,
                  f"Removed enrollment {e['participant_name']}" if e else f"Removed enrollment #{enrollment_id}")
    flash("Enrollment removed.", "success")
    if e:
        return redirect(url_for("sessions.view", session_id=e["session_id"]))
    return redirect(url_for("enrollments.index"))


def _parse_bulk_lines(raw_text):
    """Parses the bulk-add textarea: one participant per line, fields
    separated by commas — Name[, IC No][, Gender]. Only Name is required;
    blank lines are skipped. Gender is normalized loosely (m/male -> Male,
    f/female -> Female, anything else is left as typed)."""
    participants = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        name = parts[0] if parts else ""
        if not name:
            continue
        ic_no = parts[1] if len(parts) > 1 and parts[1] else None
        gender_raw = parts[2].lower() if len(parts) > 2 and parts[2] else ""
        if gender_raw in ("m", "male"):
            gender = "Male"
        elif gender_raw in ("f", "female"):
            gender = "Female"
        else:
            gender = parts[2].strip() if len(parts) > 2 and parts[2] else None
        participants.append((name, ic_no, gender))
    return participants


@bp.route("/session/<int:session_id>/bulk-add", methods=("GET", "POST"))
@login_required
def bulk_add(session_id):
    session_row = db.query(
        """SELECT cs.*, c.title AS course_title FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?""",
        (session_id,), one=True,
    )
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))

    companies = db.query("SELECT id, name FROM companies ORDER BY name")

    if request.method == "POST":
        participants = _parse_bulk_lines(request.form.get("bulk_text", ""))
        if not participants:
            flash("Paste at least one participant — one per line.", "danger")
            return render_template("enrollments/bulk_add.html", session=session_row, companies=companies)

        company_id = request.form.get("company_id") or None
        status = request.form.get("status") or "Registered"
        for name, ic_no, gender in participants:
            db.execute(
                """INSERT INTO enrollments (session_id, company_id, participant_name, ic_no, gender,
                       citizenship, status, hrdf_claim_status)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (session_id, company_id, name, ic_no, gender, "Malaysian", status, "Not Applicable"),
            )
        flash(f"Added {len(participants)} participant(s).", "success")
        return redirect(url_for("sessions.view", session_id=session_id))

    return render_template("enrollments/bulk_add.html", session=session_row, companies=companies)


@bp.route("/bulk-delete", methods=("POST",))
@login_required
def bulk_delete():
    ids = request.form.getlist("enrollment_ids")
    session_id = request.form.get("session_id") or None
    if ids:
        placeholders = ",".join("?" * len(ids))
        db.execute(f"DELETE FROM enrollments WHERE id IN ({placeholders})", ids)
        flash(f"Removed {len(ids)} participant(s).", "success")
    else:
        flash("Select at least one participant first.", "warning")
    if session_id:
        return redirect(url_for("sessions.view", session_id=session_id))
    return redirect(url_for("enrollments.index"))


@bp.route("/bulk-update", methods=("POST",))
@login_required
def bulk_update():
    ids = request.form.getlist("enrollment_ids")
    session_id = request.form.get("session_id") or None
    status = request.form.get("bulk_status") or ""
    hrdf_status = request.form.get("bulk_hrdf_status") or ""

    if not ids:
        flash("Select at least one participant first.", "warning")
    elif not status and not hrdf_status:
        flash("Choose a status to apply.", "warning")
    else:
        placeholders = ",".join("?" * len(ids))
        if status:
            db.execute(f"UPDATE enrollments SET status = ? WHERE id IN ({placeholders})", [status] + ids)
        if hrdf_status:
            db.execute(f"UPDATE enrollments SET hrdf_claim_status = ? WHERE id IN ({placeholders})",
                       [hrdf_status] + ids)
        flash(f"Updated {len(ids)} participant(s).", "success")

    if session_id:
        return redirect(url_for("sessions.view", session_id=session_id))
    return redirect(url_for("enrollments.index"))
