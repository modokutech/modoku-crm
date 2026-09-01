import os
import uuid

from flask import (Blueprint, current_app, flash, redirect, render_template,
                    request, send_from_directory, url_for)
from werkzeug.utils import secure_filename

from . import activity, db, uploadutil
from .auth import admin_required, login_required
from .csvutil import csv_response

bp = Blueprint("courses", __name__, url_prefix="/courses")


def _course_upload_dir(course_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "courses", str(course_id))
    os.makedirs(path, exist_ok=True)
    return path


def _handle_outline_upload(course_id):
    file_storage = request.files.get("outline_file")
    if not file_storage or not file_storage.filename:
        return
    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.DEFAULT_EXTENSIONS)
    if error:
        flash(error, "danger")
        return
    safe_name = secure_filename(file_storage.filename)
    stored_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
    file_storage.save(os.path.join(_course_upload_dir(course_id), stored_name))
    db.execute("UPDATE courses SET outline_file = ? WHERE id = ?", (stored_name, course_id))


def _filtered_courses():
    q = request.args.get("q", "").strip()
    sql = """SELECT c.*, (SELECT COUNT(*) FROM course_sessions cs WHERE cs.course_id = c.id) AS session_count
              FROM courses c WHERE 1=1"""
    args = []
    if q:
        sql += " AND (c.title LIKE ? OR c.code LIKE ? OR c.category LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += " ORDER BY c.title"
    return db.query(sql, args), q


@bp.route("/")
@login_required
def index():
    courses, q = _filtered_courses()
    return render_template("courses/list.html", courses=courses, q=q)


@bp.route("/export")
@admin_required
def export():
    courses, _q = _filtered_courses()
    rows = (
        (c["code"] or "", c["title"], c["category"] or "", c["duration_days"],
         c["price_inhouse"], c["price_public"], c["hrdcorp_programme_no"] or "",
         "Yes" if c["hrdf_claimable"] else "No", "Yes" if c["active"] else "No", c["session_count"])
        for c in courses
    )
    return csv_response(
        "courses.csv",
        ["Code", "Title", "Category", "Duration (Days)", "Price (In-house)", "Price (Public, per pax/day)",
         "HRDCorp Programme No.", "HRDF Claimable", "Active", "Sessions"],
        rows,
    )


@bp.route("/new", methods=("GET", "POST"))
@login_required
def new():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        outline = request.files.get("outline_file")
        if not title:
            flash("Course title is required.", "danger")
        elif not outline or not outline.filename:
            flash("Course Outline document is required.", "danger")
        else:
            course_id = db.execute(
                """INSERT INTO courses (code, title, category, focus, description, duration_days,
                       price_inhouse, price_public, hrdcorp_programme_no, hrdf_claimable, active)
                   VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
                (
                    request.form.get("code") or None,
                    title,
                    request.form.get("category") or None,
                    request.form.get("focus") or None,
                    request.form.get("description") or None,
                    request.form.get("duration_days") or 1,
                    request.form.get("price_inhouse") or 0,
                    request.form.get("price_public") or 0,
                    request.form.get("hrdcorp_programme_no") or None,
                    1 if request.form.get("hrdf_claimable") else 0,
                ),
            )
            _handle_outline_upload(course_id)
            activity.log("create", "course", course_id, f"Created course {title}")
            flash("Course added.", "success")
            return redirect(url_for("courses.index"))
    return render_template("courses/form.html", course=None)


@bp.route("/<int:course_id>")
@login_required
def view(course_id):
    course = db.query("SELECT * FROM courses WHERE id = ?", (course_id,), one=True)
    if course is None:
        flash("Course not found.", "danger")
        return redirect(url_for("courses.index"))
    sessions = db.query(
        """SELECT cs.*, t.name AS trainer_name,
                  (SELECT COUNT(*) FROM enrollments e WHERE e.session_id = cs.id) AS enrolled_count
           FROM course_sessions cs LEFT JOIN trainers t ON t.id = cs.trainer_id
           WHERE cs.course_id = ? ORDER BY cs.start_date DESC""",
        (course_id,),
    )
    return render_template("courses/view.html", course=course, sessions=sessions)


@bp.route("/<int:course_id>/edit", methods=("GET", "POST"))
@login_required
def edit(course_id):
    course = db.query("SELECT * FROM courses WHERE id = ?", (course_id,), one=True)
    if course is None:
        flash("Course not found.", "danger")
        return redirect(url_for("courses.index"))
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        outline = request.files.get("outline_file")
        has_new_outline = bool(outline and outline.filename)
        if not title:
            flash("Course title is required.", "danger")
        elif not has_new_outline and not course["outline_file"]:
            flash("Course Outline document is required.", "danger")
        else:
            db.execute(
                """UPDATE courses SET code=?, title=?, category=?, focus=?, description=?, duration_days=?,
                       price_inhouse=?, price_public=?, hrdcorp_programme_no=?, hrdf_claimable=?, active=? WHERE id=?""",
                (
                    request.form.get("code") or None,
                    title,
                    request.form.get("category") or None,
                    request.form.get("focus") or None,
                    request.form.get("description") or None,
                    request.form.get("duration_days") or 1,
                    request.form.get("price_inhouse") or 0,
                    request.form.get("price_public") or 0,
                    request.form.get("hrdcorp_programme_no") or None,
                    1 if request.form.get("hrdf_claimable") else 0,
                    1 if request.form.get("active") else 0,
                    course_id,
                ),
            )
            _handle_outline_upload(course_id)
            activity.log("update", "course", course_id, f"Updated course {title}")
            flash("Course updated.", "success")
            return redirect(url_for("courses.view", course_id=course_id))
    return render_template("courses/form.html", course=course)


@bp.route("/<int:course_id>/outline")
@login_required
def download_outline(course_id):
    course = db.query("SELECT outline_file FROM courses WHERE id = ?", (course_id,), one=True)
    if course is None or not course["outline_file"]:
        flash("No course outline uploaded yet.", "danger")
        return redirect(url_for("courses.view", course_id=course_id))
    return send_from_directory(_course_upload_dir(course_id), course["outline_file"], as_attachment=False)


@bp.route("/<int:course_id>/delete", methods=("POST",))
@login_required
def delete(course_id):
    course = db.query("SELECT title FROM courses WHERE id = ?", (course_id,), one=True)
    db.execute("DELETE FROM courses WHERE id = ?", (course_id,))
    activity.log("delete", "course", course_id,
                  f"Deleted course {course['title'] if course else course_id}")
    flash("Course deleted.", "success")
    return redirect(url_for("courses.index"))
