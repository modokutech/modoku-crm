import os
import uuid

from flask import (Blueprint, current_app, flash, g, redirect, render_template,
                    request, send_from_directory, url_for)
from werkzeug.utils import secure_filename

from . import activity, db, doc_sanity, uploadutil
from .auth import admin_required, login_required
from .csvutil import csv_response

bp = Blueprint("trainers", __name__, url_prefix="/trainers")

# (form field name, DB column, human label)
DOCUMENT_FIELDS = [
    ("profile_file", "profile_file", "Trainer Profile"),
    ("ttt_cert_file", "ttt_cert_file", "TTT Certificate"),
    ("accredited_cert_file", "accredited_cert_file", "Accredited Certificate"),
]


def _trainer_upload_dir(trainer_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "trainers", str(trainer_id))
    os.makedirs(path, exist_ok=True)
    return path


def _save_document(trainer_id, file_storage, label=None):
    if not file_storage or not file_storage.filename:
        return None
    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.DEFAULT_EXTENSIONS,
                                        max_bytes=uploadutil.TRAINER_DOCUMENT_MAX_BYTES)
    if error:
        flash(error, "danger")
        return None
    safe_name = secure_filename(file_storage.filename)
    stored_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
    saved_path = os.path.join(_trainer_upload_dir(trainer_id), stored_name)
    file_storage.save(saved_path)
    warning = doc_sanity.check_document(saved_path, "trainer_credential")
    if warning:
        flash(f"{label + ': ' if label else ''}{warning}", "warning")
    return stored_name


def _handle_document_uploads(trainer_id):
    updates = {}
    for form_field, column, label in DOCUMENT_FIELDS:
        stored = _save_document(trainer_id, request.files.get(form_field), label=label)
        if stored:
            updates[column] = stored
    if updates:
        set_clause = ", ".join(f"{col} = ?" for col in updates)
        db.execute(f"UPDATE trainers SET {set_clause} WHERE id = ?",
                   (*updates.values(), trainer_id))


def _handle_avatar_upload(trainer_id):
    file_storage = request.files.get("avatar_file")
    if not file_storage or not file_storage.filename:
        return
    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.IMAGE_EXTENSIONS)
    if error:
        flash(error, "danger")
        return
    safe_name = secure_filename(file_storage.filename)
    stored_name = f"avatar_{uuid.uuid4().hex[:8]}_{safe_name}"
    file_storage.save(os.path.join(_trainer_upload_dir(trainer_id), stored_name))
    db.execute("UPDATE trainers SET avatar_file = ? WHERE id = ?", (stored_name, trainer_id))


def _record_rate_change(trainer_id, old_rate, new_rate):
    """Logs a trainer_rate_history row whenever rate_per_day actually
    changes (including the very first time it's set on a new trainer, with
    old_rate left NULL — nothing to compare against yet). No-ops if the
    rate is unchanged, so editing a trainer's other fields never adds a
    spurious history entry."""
    old_rate = float(old_rate or 0)
    new_rate = float(new_rate or 0)
    if old_rate == new_rate:
        return
    db.execute(
        "INSERT INTO trainer_rate_history (trainer_id, old_rate, new_rate, changed_by) VALUES (?,?,?,?)",
        (trainer_id, old_rate if old_rate else None, new_rate, g.user["id"] if g.user else None),
    )


def _filtered_trainers():
    q = request.args.get("q", "").strip()
    sql = """SELECT t.*, (SELECT COUNT(*) FROM course_sessions cs WHERE cs.trainer_id = t.id) AS session_count
             FROM trainers t WHERE 1=1"""
    args = []
    if q:
        sql += " AND (t.name LIKE ? OR t.email LIKE ? OR t.specialization LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += " ORDER BY t.name COLLATE NOCASE"
    return db.query(sql, args), q


@bp.route("/")
@login_required
def index():
    trainers, q = _filtered_trainers()
    return render_template("trainers/list.html", trainers=trainers, q=q)


@bp.route("/export")
@admin_required
def export():
    trainers, _q = _filtered_trainers()
    rows = (
        (t["name"], t["email"] or "", t["phone"] or "", t["specialization"] or "", t["rate_per_day"],
         t["session_count"])
        for t in trainers
    )
    return csv_response(
        "trainers.csv",
        ["Name", "Email", "Phone", "Specialization", "Rate Per Day", "Sessions"],
        rows,
    )


@bp.route("/new", methods=("GET", "POST"))
@login_required
def new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        profile_upload = request.files.get("profile_file")
        accredited_upload = request.files.get("accredited_cert_file")
        if not name:
            flash("Trainer name is required.", "danger")
        elif not profile_upload or not profile_upload.filename:
            flash("Trainer Profile document is required.", "danger")
        elif not accredited_upload or not accredited_upload.filename:
            flash("Accredited Certificate document is required.", "danger")
        else:
            rate_per_day = request.form.get("rate_per_day") or 0
            trainer_id = db.execute(
                """INSERT INTO trainers (name, email, phone, specialization, notes, rate_per_day,
                       half_day_rate, outstation_rate) VALUES (?,?,?,?,?,?,?,?)""",
                (name, request.form.get("email") or None, request.form.get("phone") or None,
                 request.form.get("specialization") or None, request.form.get("notes") or None,
                 rate_per_day, request.form.get("half_day_rate") or 0,
                 request.form.get("outstation_rate") or 0),
            )
            _record_rate_change(trainer_id, 0, rate_per_day)
            _handle_document_uploads(trainer_id)
            _handle_avatar_upload(trainer_id)
            activity.log("create", "trainer", trainer_id, f"Added trainer {name}")
            flash("Trainer added.", "success")
            return redirect(url_for("trainers.view", trainer_id=trainer_id))
    return render_template("trainers/form.html", trainer=None)


@bp.route("/<int:trainer_id>")
@login_required
def view(trainer_id):
    trainer = db.query("SELECT * FROM trainers WHERE id = ?", (trainer_id,), one=True)
    if trainer is None:
        flash("Trainer not found.", "danger")
        return redirect(url_for("trainers.index"))
    sessions = db.query(
        """SELECT cs.*, c.title AS course_title FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           WHERE cs.trainer_id = ? ORDER BY cs.start_date DESC""",
        (trainer_id,),
    )
    purchase_orders = db.query(
        """SELECT po.*, c.title AS course_title, cs.start_date FROM purchase_orders po
           JOIN course_sessions cs ON cs.id = po.session_id
           JOIN courses c ON c.id = cs.course_id
           WHERE po.trainer_id = ? ORDER BY po.issue_date DESC""",
        (trainer_id,),
    )
    rate_history = db.query(
        """SELECT h.*, u.name AS changed_by_name FROM trainer_rate_history h
           LEFT JOIN users u ON u.id = h.changed_by
           WHERE h.trainer_id = ? ORDER BY h.changed_at DESC""",
        (trainer_id,),
    )
    return render_template("trainers/view.html", trainer=trainer, sessions=sessions,
                            purchase_orders=purchase_orders, document_fields=DOCUMENT_FIELDS,
                            rate_history=rate_history)


@bp.route("/<int:trainer_id>/edit", methods=("GET", "POST"))
@login_required
def edit(trainer_id):
    trainer = db.query("SELECT * FROM trainers WHERE id = ?", (trainer_id,), one=True)
    if trainer is None:
        flash("Trainer not found.", "danger")
        return redirect(url_for("trainers.index"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        profile_upload = request.files.get("profile_file")
        accredited_upload = request.files.get("accredited_cert_file")
        has_new_profile = bool(profile_upload and profile_upload.filename)
        has_new_accredited = bool(accredited_upload and accredited_upload.filename)
        if not name:
            flash("Trainer name is required.", "danger")
        elif not has_new_profile and not trainer["profile_file"]:
            flash("Trainer Profile document is required.", "danger")
        elif not has_new_accredited and not trainer["accredited_cert_file"]:
            flash("Accredited Certificate document is required.", "danger")
        else:
            new_rate = request.form.get("rate_per_day") or 0
            db.execute(
                """UPDATE trainers SET name=?, email=?, phone=?, specialization=?, notes=?, rate_per_day=?,
                       half_day_rate=?, outstation_rate=? WHERE id=?""",
                (name, request.form.get("email") or None, request.form.get("phone") or None,
                 request.form.get("specialization") or None, request.form.get("notes") or None,
                 new_rate, request.form.get("half_day_rate") or 0,
                 request.form.get("outstation_rate") or 0, trainer_id),
            )
            _record_rate_change(trainer_id, trainer["rate_per_day"], new_rate)
            _handle_document_uploads(trainer_id)
            _handle_avatar_upload(trainer_id)
            activity.log("update", "trainer", trainer_id, f"Updated trainer {name}")
            flash("Trainer updated.", "success")
            return redirect(url_for("trainers.view", trainer_id=trainer_id))
    return render_template("trainers/form.html", trainer=trainer)


@bp.route("/<int:trainer_id>/documents/<field>")
@login_required
def download_document(trainer_id, field):
    valid_columns = {col for _form, col, _label in DOCUMENT_FIELDS}
    if field not in valid_columns:
        flash("Unknown document.", "danger")
        return redirect(url_for("trainers.index"))
    trainer = db.query(f"SELECT {field} AS filename FROM trainers WHERE id = ?", (trainer_id,), one=True)
    if trainer is None or not trainer["filename"]:
        flash("That document hasn't been uploaded yet.", "danger")
        return redirect(url_for("trainers.view", trainer_id=trainer_id))
    return send_from_directory(_trainer_upload_dir(trainer_id), trainer["filename"], as_attachment=False)


@bp.route("/<int:trainer_id>/avatar")
@login_required
def avatar(trainer_id):
    trainer = db.query("SELECT avatar_file FROM trainers WHERE id = ?", (trainer_id,), one=True)
    if trainer is None or not trainer["avatar_file"]:
        flash("No display picture uploaded yet.", "danger")
        return redirect(url_for("trainers.view", trainer_id=trainer_id))
    return send_from_directory(_trainer_upload_dir(trainer_id), trainer["avatar_file"], as_attachment=False)


@bp.route("/<int:trainer_id>/delete", methods=("POST",))
@login_required
def delete(trainer_id):
    trainer = db.query("SELECT name FROM trainers WHERE id = ?", (trainer_id,), one=True)
    db.execute("DELETE FROM trainers WHERE id = ?", (trainer_id,))
    activity.log("delete", "trainer", trainer_id,
                  f"Deleted trainer {trainer['name'] if trainer else trainer_id}")
    flash("Trainer deleted.", "success")
    return redirect(url_for("trainers.index"))
