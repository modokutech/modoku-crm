import os
import uuid

from flask import (Blueprint, current_app, flash, redirect, render_template,
                    request, send_from_directory, url_for)
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename

from . import activity, db, uploadutil
from .auth import admin_required, is_allowed_registration_email
from .csvutil import csv_response

MIN_PASSWORD_LENGTH = 8

bp = Blueprint("users", __name__, url_prefix="/staff")


def _user_upload_dir(user_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "users", str(user_id))
    os.makedirs(path, exist_ok=True)
    return path


def _handle_avatar_upload(user_id):
    file_storage = request.files.get("avatar_file")
    if not file_storage or not file_storage.filename:
        return
    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.IMAGE_EXTENSIONS)
    if error:
        flash(error, "danger")
        return
    safe_name = secure_filename(file_storage.filename)
    stored_name = f"avatar_{uuid.uuid4().hex[:8]}_{safe_name}"
    file_storage.save(os.path.join(_user_upload_dir(user_id), stored_name))
    db.execute("UPDATE users SET avatar_file = ? WHERE id = ?", (stored_name, user_id))


@bp.route("/")
@admin_required
def index():
    users = db.query("SELECT * FROM users ORDER BY role, name")
    return render_template("users/list.html", users=users)


@bp.route("/export")
@admin_required
def export():
    users = db.query("SELECT * FROM users ORDER BY role, name")
    rows = (
        (u["name"], u["email"], u["role"], "Yes" if u["active"] else "No", u["position"] or "",
         u["contact_phone"] or "")
        for u in users
    )
    return csv_response(
        "staff_users.csv",
        ["Name", "Email", "Role", "Active", "Position", "Contact Phone"],
        rows,
    )


@bp.route("/new", methods=("GET", "POST"))
@admin_required
def new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "staff")

        error = None
        if not name or not email or not password:
            error = "Name, email and password are required."
        elif not is_allowed_registration_email(email):
            error = "Staff accounts must use a @modoku.tech email address."
        elif len(password) < MIN_PASSWORD_LENGTH:
            error = f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        elif db.query("SELECT id FROM users WHERE lower(email) = ?", (email,), one=True):
            error = "A staff account with that email already exists."

        if error:
            flash(error, "danger")
        else:
            new_user_id = db.execute(
                "INSERT INTO users (name, email, password_hash, role) VALUES (?,?,?,?)",
                (name, email, generate_password_hash(password), role),
            )
            _handle_avatar_upload(new_user_id)
            activity.log("create", "user", new_user_id, f"Created staff account for {name} ({email})")
            flash("Staff account created.", "success")
            return redirect(url_for("users.index"))

    return render_template("users/form.html", user=None)


@bp.route("/<int:user_id>/edit", methods=("GET", "POST"))
@admin_required
def edit(user_id):
    user = db.query("SELECT * FROM users WHERE id = ?", (user_id,), one=True)
    if user is None:
        flash("Staff account not found.", "danger")
        return redirect(url_for("users.index"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        role = request.form.get("role", "staff")
        active = 1 if request.form.get("active") else 0
        password = request.form.get("password", "")

        if not name:
            flash("Name is required.", "danger")
        elif password and len(password) < MIN_PASSWORD_LENGTH:
            flash(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", "danger")
        else:
            if password:
                db.execute(
                    "UPDATE users SET name=?, role=?, active=?, password_hash=? WHERE id=?",
                    (name, role, active, generate_password_hash(password), user_id),
                )
            else:
                db.execute(
                    "UPDATE users SET name=?, role=?, active=? WHERE id=?",
                    (name, role, active, user_id),
                )
            _handle_avatar_upload(user_id)
            activity.log("update", "user", user_id, f"Updated staff account for {name}")
            flash("Staff account updated.", "success")
            return redirect(url_for("users.index"))

    return render_template("users/form.html", user=user)


@bp.route("/<int:user_id>/avatar")
@admin_required
def avatar(user_id):
    user = db.query("SELECT avatar_file FROM users WHERE id = ?", (user_id,), one=True)
    if user is None or not user["avatar_file"]:
        flash("No display picture uploaded yet.", "danger")
        return redirect(url_for("users.index"))
    return send_from_directory(_user_upload_dir(user_id), user["avatar_file"], as_attachment=False)
