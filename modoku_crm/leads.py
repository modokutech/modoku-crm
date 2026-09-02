import os
import uuid

from flask import (Blueprint, current_app, flash, g, jsonify, redirect, render_template,
                    request, send_from_directory, url_for)
from werkzeug.utils import secure_filename

from . import activity, db, uploadutil
from .auth import admin_required, login_required
from .csvutil import csv_response

bp = Blueprint("leads", __name__, url_prefix="/leads")

STATUSES = ["New", "Contacted", "Had Meeting", "Proposal Sent", "Deal Closed", "Lost"]
SOURCES = ["Website", "Referral", "Social Media", "Phone", "Walk-in", "Email", "Other"]


def _lead_upload_dir(lead_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "leads", str(lead_id))
    os.makedirs(path, exist_ok=True)
    return path


def _handle_namecard_upload(lead_id):
    file_storage = request.files.get("namecard_file")
    if not file_storage or not file_storage.filename:
        return
    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.IMAGE_EXTENSIONS)
    if error:
        flash(error, "danger")
        return
    safe_name = secure_filename(file_storage.filename)
    stored_name = f"namecard_{uuid.uuid4().hex[:8]}_{safe_name}"
    file_storage.save(os.path.join(_lead_upload_dir(lead_id), stored_name))
    db.execute("UPDATE leads SET namecard_file = ? WHERE id = ?", (stored_name, lead_id))


def _handle_proposal_upload(lead_id):
    file_storage = request.files.get("proposal_file")
    if not file_storage or not file_storage.filename:
        return False
    error = uploadutil.validate_upload(
        file_storage,
        allowed_extensions=uploadutil.DEFAULT_EXTENSIONS,
        max_bytes=uploadutil.PROPOSAL_DECK_MAX_BYTES,
    )
    if error:
        flash(error, "danger")
        return False
    safe_name = secure_filename(file_storage.filename)
    stored_name = f"proposal_{uuid.uuid4().hex[:8]}_{safe_name}"
    file_storage.save(os.path.join(_lead_upload_dir(lead_id), stored_name))
    db.execute("UPDATE leads SET proposal_file = ? WHERE id = ?", (stored_name, lead_id))
    return True


def _filtered_leads():
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip()

    sql = """SELECT l.*, co.name AS company_name, u.name AS assigned_name
              FROM leads l
              LEFT JOIN companies co ON co.id = l.company_id
              LEFT JOIN users u ON u.id = l.assigned_to
              WHERE 1=1"""
    args = []
    if status:
        sql += " AND l.status = ?"
        args.append(status)
    if q:
        sql += " AND (l.name LIKE ? OR l.email LIKE ? OR l.phone LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += " ORDER BY l.created_at DESC"
    return db.query(sql, args), status, q


@bp.route("/")
@login_required
def index():
    leads, status, q = _filtered_leads()
    return render_template("leads/list.html", leads=leads, statuses=STATUSES,
                            current_status=status, q=q)


@bp.route("/export")
@admin_required
def export():
    leads, _status, _q = _filtered_leads()
    rows = (
        (l["name"], l["role"] or "", l["company_name"] or "", l["email"] or "", l["phone"] or "",
         l["status"], l["source"] or "", l["assigned_name"] or "", l["next_follow_up"] or "",
         l["created_at"])
        for l in leads
    )
    return csv_response(
        "leads.csv",
        ["Name", "Role", "Company", "Email", "Phone", "Status", "Source", "Assigned To",
         "Next Follow-up", "Created At"],
        rows,
    )


@bp.route("/call-list")
@login_required
def call_list():
    """Sales worklist: who to call/follow up with next, sorted by urgency."""
    mine_only = request.args.get("mine") == "1"
    sql = """SELECT l.*, co.name AS company_name, u.name AS assigned_name,
                     (SELECT COUNT(*) FROM lead_activities a WHERE a.lead_id = l.id AND a.activity_type='Call') AS call_count,
                     (SELECT COUNT(*) FROM lead_activities a WHERE a.lead_id = l.id AND a.activity_type='Email') AS email_count,
                     (SELECT MAX(a.created_at) FROM lead_activities a WHERE a.lead_id = l.id) AS last_activity_at
              FROM leads l
              LEFT JOIN companies co ON co.id = l.company_id
              LEFT JOIN users u ON u.id = l.assigned_to
              WHERE l.status NOT IN ('Deal Closed','Lost')"""
    args = []
    if mine_only:
        sql += " AND l.assigned_to = ?"
        args.append(g.user["id"])
    sql += """ ORDER BY
        CASE WHEN l.next_follow_up IS NULL THEN 1 ELSE 0 END,
        l.next_follow_up ASC,
        l.created_at ASC"""
    leads = db.query(sql, args)
    return render_template("leads/call_list.html", leads=leads, mine_only=mine_only)


@bp.route("/quick-add", methods=("POST",))
@login_required
def quick_add():
    """Create a PIC (a lead tied to a client company) from a small inline
    modal — e.g. from the Schedule a Class form, when the contact you need
    isn't in the list yet — without navigating away and losing whatever
    else was being filled in. Mirrors companies.quick_add."""
    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required."}), 400
    company_id = request.form.get("company_id") or None
    lead_id = db.execute(
        """INSERT INTO leads (name, role, email, phone, company_id)
           VALUES (?,?,?,?,?)""",
        (name, request.form.get("role") or None, request.form.get("email") or None,
         request.form.get("phone") or None, company_id),
    )
    activity.log("create", "lead", lead_id, f"Added PIC {name} via quick-add")
    return jsonify({"id": lead_id, "name": name, "email": request.form.get("email") or "",
                     "company_id": company_id})


@bp.route("/new", methods=("GET", "POST"))
@login_required
def new():
    companies = db.query("SELECT id, name FROM companies ORDER BY name")
    users = db.query("SELECT id, name FROM users WHERE active = 1 ORDER BY name")
    courses = db.query("SELECT id, title FROM courses WHERE active = 1 ORDER BY title")
    # Optional ?company_id= — used when this page is opened from a link that
    # already knows which company the new lead is for (e.g. the "add a new
    # Lead" link on the New Quotation page's Attention To field).
    try:
        preselect_company_id = int(request.args.get("company_id") or 0) or None
    except ValueError:
        preselect_company_id = None

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Lead name is required.", "danger")
        else:
            status = request.form.get("status") or "New"
            lead_id = db.execute(
                """INSERT INTO leads (name, role, email, phone, company_id, source, status,
                                       assigned_to, interested_course_id, next_follow_up,
                                       linkedin_url, lost_reason, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    name,
                    request.form.get("role") or None,
                    request.form.get("email") or None,
                    request.form.get("phone") or None,
                    request.form.get("company_id") or None,
                    request.form.get("source") or None,
                    status,
                    request.form.get("assigned_to") or None,
                    request.form.get("interested_course_id") or None,
                    request.form.get("next_follow_up") or None,
                    request.form.get("linkedin_url") or None,
                    request.form.get("lost_reason") or None if status == "Lost" else None,
                    request.form.get("notes") or None,
                ),
            )
            _handle_namecard_upload(lead_id)
            activity.log("create", "lead", lead_id, f"Created lead {name}")
            flash("Lead added.", "success")
            return redirect(url_for("leads.index"))

    return render_template("leads/form.html", lead=None, companies=companies,
                            users=users, courses=courses, statuses=STATUSES, sources=SOURCES,
                            preselect_company_id=preselect_company_id)


@bp.route("/<int:lead_id>")
@login_required
def view(lead_id):
    lead = db.query(
        """SELECT l.*, co.name AS company_name, u.name AS assigned_name,
                  c.title AS course_title
           FROM leads l
           LEFT JOIN companies co ON co.id = l.company_id
           LEFT JOIN users u ON u.id = l.assigned_to
           LEFT JOIN courses c ON c.id = l.interested_course_id
           WHERE l.id = ?""",
        (lead_id,), one=True,
    )
    if lead is None:
        flash("Lead not found.", "danger")
        return redirect(url_for("leads.index"))

    activities = db.query(
        """SELECT a.*, u.name AS author FROM lead_activities a
           LEFT JOIN users u ON u.id = a.created_by
           WHERE a.lead_id = ? ORDER BY a.created_at DESC""",
        (lead_id,),
    )
    return render_template("leads/view.html", lead=lead, activities=activities)


@bp.route("/<int:lead_id>/activity", methods=("POST",))
@login_required
def add_activity(lead_id):
    note = request.form.get("note", "").strip()
    if note:
        db.execute(
            "INSERT INTO lead_activities (lead_id, activity_type, note, created_by) VALUES (?,?,?,?)",
            (lead_id, request.form.get("activity_type", "Note"), note, g.user["id"]),
        )
        db.execute("UPDATE leads SET updated_at = datetime('now') WHERE id = ?", (lead_id,))
        flash("Activity logged.", "success")
    return redirect(url_for("leads.view", lead_id=lead_id))


@bp.route("/<int:lead_id>/edit", methods=("GET", "POST"))
@login_required
def edit(lead_id):
    lead = db.query("SELECT * FROM leads WHERE id = ?", (lead_id,), one=True)
    if lead is None:
        flash("Lead not found.", "danger")
        return redirect(url_for("leads.index"))

    companies = db.query("SELECT id, name FROM companies ORDER BY name")
    users = db.query("SELECT id, name FROM users WHERE active = 1 ORDER BY name")
    courses = db.query("SELECT id, title FROM courses WHERE active = 1 ORDER BY title")

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Lead name is required.", "danger")
        else:
            status = request.form.get("status") or "New"
            db.execute(
                """UPDATE leads SET name=?, role=?, email=?, phone=?, company_id=?, source=?,
                       status=?, assigned_to=?, interested_course_id=?, next_follow_up=?,
                       linkedin_url=?, lost_reason=?, notes=?,
                       updated_at=datetime('now')
                   WHERE id=?""",
                (
                    name,
                    request.form.get("role") or None,
                    request.form.get("email") or None,
                    request.form.get("phone") or None,
                    request.form.get("company_id") or None,
                    request.form.get("source") or None,
                    status,
                    request.form.get("assigned_to") or None,
                    request.form.get("interested_course_id") or None,
                    request.form.get("next_follow_up") or None,
                    request.form.get("linkedin_url") or None,
                    request.form.get("lost_reason") or None if status == "Lost" else None,
                    request.form.get("notes") or None,
                    lead_id,
                ),
            )
            _handle_namecard_upload(lead_id)
            activity.log("update", "lead", lead_id, f"Updated lead {name}")
            flash("Lead updated.", "success")
            return redirect(url_for("leads.view", lead_id=lead_id))

    return render_template("leads/form.html", lead=lead, companies=companies,
                            users=users, courses=courses, statuses=STATUSES, sources=SOURCES)


@bp.route("/<int:lead_id>/namecard")
@login_required
def namecard(lead_id):
    lead = db.query("SELECT namecard_file FROM leads WHERE id = ?", (lead_id,), one=True)
    if lead is None or not lead["namecard_file"]:
        flash("No namecard uploaded yet.", "danger")
        return redirect(url_for("leads.view", lead_id=lead_id))
    return send_from_directory(_lead_upload_dir(lead_id), lead["namecard_file"], as_attachment=False)


@bp.route("/<int:lead_id>/proposal", methods=("POST",))
@login_required
def upload_proposal(lead_id):
    lead = db.query("SELECT id, name FROM leads WHERE id = ?", (lead_id,), one=True)
    if lead is None:
        flash("Lead not found.", "danger")
        return redirect(url_for("leads.index"))
    if _handle_proposal_upload(lead_id):
        activity.log("update", "lead", lead_id, f"Uploaded proposal deck for {lead['name']}")
        flash("Proposal deck uploaded.", "success")
    return redirect(url_for("leads.view", lead_id=lead_id))


@bp.route("/<int:lead_id>/proposal/download")
@login_required
def proposal(lead_id):
    lead = db.query("SELECT proposal_file FROM leads WHERE id = ?", (lead_id,), one=True)
    if lead is None or not lead["proposal_file"]:
        flash("No proposal deck uploaded yet.", "danger")
        return redirect(url_for("leads.view", lead_id=lead_id))
    return send_from_directory(_lead_upload_dir(lead_id), lead["proposal_file"], as_attachment=True)


@bp.route("/<int:lead_id>/delete", methods=("POST",))
@login_required
def delete(lead_id):
    lead = db.query("SELECT name FROM leads WHERE id = ?", (lead_id,), one=True)
    db.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
    activity.log("delete", "lead", lead_id, f"Deleted lead {lead['name'] if lead else lead_id}")
    flash("Lead deleted.", "success")
    return redirect(url_for("leads.index"))
