"""Self-service profile for the logged-in staff member — position, signature
and contact number, used to auto-fill the "Authorised by" block on Purchase
Orders and the closing signature on Quotations. Separate from the
admin-only Staff Users module (users.py), since any staff member should be
able to keep their own signature/position up to date without needing admin
access.
"""
import os
import uuid

from flask import (Blueprint, current_app, flash, g, redirect, render_template,
                    request, send_from_directory, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from . import calendar_integration, db, uploadutil
from .auth import login_required

bp = Blueprint("profile", __name__, url_prefix="/profile")


def _user_upload_dir(user_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "users", str(user_id))
    os.makedirs(path, exist_ok=True)
    return path


def _handle_signature_upload(user_id):
    file_storage = request.files.get("signature_file")
    if not file_storage or not file_storage.filename:
        return
    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.IMAGE_EXTENSIONS)
    if error:
        flash(error, "danger")
        return
    safe_name = secure_filename(file_storage.filename)
    stored_name = f"signature_{uuid.uuid4().hex[:8]}_{safe_name}"
    file_storage.save(os.path.join(_user_upload_dir(user_id), stored_name))
    db.execute("UPDATE users SET signature_file = ? WHERE id = ?", (stored_name, user_id))


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


@bp.route("/", methods=("GET", "POST"))
@login_required
def edit():
    user_id = g.user["id"]
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        position = request.form.get("position", "").strip()
        contact_phone = request.form.get("contact_phone", "").strip()
        new_password = request.form.get("new_password", "")
        current_password = request.form.get("current_password", "")

        if not name:
            flash("Name is required.", "danger")
        elif new_password and not check_password_hash(g.user["password_hash"], current_password):
            flash("Current password is incorrect — new password not changed.", "danger")
        else:
            if new_password:
                db.execute(
                    "UPDATE users SET name=?, position=?, contact_phone=?, password_hash=? WHERE id=?",
                    (name, position or None, contact_phone or None, generate_password_hash(new_password), user_id),
                )
            else:
                db.execute(
                    "UPDATE users SET name=?, position=?, contact_phone=? WHERE id=?",
                    (name, position or None, contact_phone or None, user_id),
                )
            _handle_signature_upload(user_id)
            _handle_avatar_upload(user_id)
            flash("Profile updated.", "success")
            return redirect(url_for("profile.edit"))

    user = db.query("SELECT * FROM users WHERE id = ?", (user_id,), one=True)
    return render_template(
        "profile/edit.html", user=user,
        google_configured=calendar_integration.is_google_configured(),
        microsoft_configured=calendar_integration.is_microsoft_configured(),
        google_connection=calendar_integration.get_connection(user_id, "google"),
        microsoft_connection=calendar_integration.get_connection(user_id, "microsoft"),
    )


@bp.route("/signature")
@login_required
def signature():
    user = db.query("SELECT signature_file FROM users WHERE id = ?", (g.user["id"],), one=True)
    if user is None or not user["signature_file"]:
        flash("No signature uploaded yet.", "danger")
        return redirect(url_for("profile.edit"))
    return send_from_directory(_user_upload_dir(g.user["id"]), user["signature_file"], as_attachment=False)


@bp.route("/avatar")
@login_required
def avatar():
    """Serves the logged-in user's own display picture. A separate route from
    users.avatar (which is admin-only) so a non-admin staff member can see
    their own avatar on their self-service profile page — the admin-only
    route was redirecting them to the dashboard instead of the image."""
    user = db.query("SELECT avatar_file FROM users WHERE id = ?", (g.user["id"],), one=True)
    if user is None or not user["avatar_file"]:
        flash("No display picture uploaded yet.", "danger")
        return redirect(url_for("profile.edit"))
    return send_from_directory(_user_upload_dir(g.user["id"]), user["avatar_file"], as_attachment=False)


@bp.route("/<int:user_id>/signature")
@login_required
def signature_for(user_id):
    """Serves any staff member's signature image — used to render it on
    documents (Purchase Orders, Quotations) authored by that staff member."""
    user = db.query("SELECT signature_file FROM users WHERE id = ?", (user_id,), one=True)
    if user is None or not user["signature_file"]:
        flash("No signature on file.", "danger")
        return redirect(url_for("dashboard.index"))
    return send_from_directory(_user_upload_dir(user_id), user["signature_file"], as_attachment=False)
