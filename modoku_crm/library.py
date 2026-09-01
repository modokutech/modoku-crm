"""Files/Library — a shared cabinet of company documents (SSM registration,
HRDCorp accreditation letters, bank documents, LHDN/Kastam correspondence,
and anything else the company needs to keep handy and find quickly), not
tied to any specific lead/session/trainer/course. Every file is tagged with
exactly one category and can optionally be pinned to the top of the list.
"""
import os
import uuid

from flask import (Blueprint, current_app, flash, g, redirect, render_template,
                    request, send_from_directory, url_for)
from werkzeug.utils import secure_filename

from . import activity, db, uploadutil
from .auth import login_required

bp = Blueprint("library", __name__, url_prefix="/library")

# Fixed tag choices — one tag per file. (label, badge color class)
TAGS = [
    ("SSM", "primary"),
    ("HRDC", "success"),
    ("LHDN", "danger"),
    ("Kastam", "warning"),
    ("Bank", "info"),
    ("Others", "secondary"),
]
TAG_LABELS = [label for label, _color in TAGS]
TAG_COLORS = dict(TAGS)


def _upload_dir():
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "library")
    os.makedirs(path, exist_ok=True)
    return path


@bp.route("/")
@login_required
def index():
    tag = request.args.get("tag", "").strip()
    q = request.args.get("q", "").strip()
    sql = """SELECT f.*, u.name AS uploaded_by_name FROM company_files f
             LEFT JOIN users u ON u.id = f.uploaded_by WHERE 1=1"""
    args = []
    if tag and tag in TAG_LABELS:
        sql += " AND f.tag = ?"
        args.append(tag)
    if q:
        sql += " AND (f.original_name LIKE ? OR f.description LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY f.pinned DESC, f.created_at DESC"
    files = db.query(sql, args)
    return render_template("library/index.html", files=files, tags=TAGS, tag=tag, q=q,
                            tag_colors=TAG_COLORS)


@bp.route("/upload", methods=("POST",))
@login_required
def upload():
    file_storage = request.files.get("file")
    tag = request.form.get("tag", "").strip()
    description = request.form.get("description", "").strip()
    if not file_storage or not file_storage.filename:
        flash("Choose a file to upload.", "danger")
        return redirect(url_for("library.index"))
    if tag not in TAG_LABELS:
        tag = "Others"
    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.DEFAULT_EXTENSIONS)
    if error:
        flash(error, "danger")
        return redirect(url_for("library.index"))
    safe_name = secure_filename(file_storage.filename)
    stored_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
    file_storage.save(os.path.join(_upload_dir(), stored_name))
    file_id = db.execute(
        """INSERT INTO company_files (filename, original_name, description, tag, uploaded_by)
           VALUES (?,?,?,?,?)""",
        (stored_name, file_storage.filename, description or None, tag, g.user["id"] if g.user else None),
    )
    activity.log("create", "company_file", file_id, f"Uploaded file {file_storage.filename}")
    flash("File uploaded.", "success")
    return redirect(url_for("library.index"))


@bp.route("/<int:file_id>/edit", methods=("POST",))
@login_required
def edit(file_id):
    row = db.query("SELECT * FROM company_files WHERE id = ?", (file_id,), one=True)
    if row is None:
        flash("File not found.", "danger")
        return redirect(url_for("library.index"))
    tag = request.form.get("tag", "").strip()
    if tag not in TAG_LABELS:
        tag = row["tag"]
    description = request.form.get("description", "").strip()
    db.execute("UPDATE company_files SET description = ?, tag = ? WHERE id = ?",
               (description or None, tag, file_id))
    activity.log("update", "company_file", file_id, f"Updated file {row['original_name']}")
    flash("File updated.", "success")
    return redirect(url_for("library.index"))


@bp.route("/<int:file_id>/pin", methods=("POST",))
@login_required
def toggle_pin(file_id):
    row = db.query("SELECT * FROM company_files WHERE id = ?", (file_id,), one=True)
    if row is None:
        flash("File not found.", "danger")
        return redirect(url_for("library.index"))
    db.execute("UPDATE company_files SET pinned = ? WHERE id = ?", (0 if row["pinned"] else 1, file_id))
    return redirect(url_for("library.index"))


@bp.route("/<int:file_id>/download")
@login_required
def download(file_id):
    row = db.query("SELECT * FROM company_files WHERE id = ?", (file_id,), one=True)
    if row is None:
        flash("File not found.", "danger")
        return redirect(url_for("library.index"))
    return send_from_directory(_upload_dir(), row["filename"], as_attachment=False,
                                download_name=row["original_name"])


@bp.route("/<int:file_id>/delete", methods=("POST",))
@login_required
def delete(file_id):
    row = db.query("SELECT * FROM company_files WHERE id = ?", (file_id,), one=True)
    if row is None:
        flash("File not found.", "danger")
        return redirect(url_for("library.index"))
    db.execute("DELETE FROM company_files WHERE id = ?", (file_id,))
    try:
        os.remove(os.path.join(_upload_dir(), row["filename"]))
    except OSError:
        pass
    activity.log("delete", "company_file", file_id, f"Deleted file {row['original_name']}")
    flash("File deleted.", "success")
    return redirect(url_for("library.index"))
