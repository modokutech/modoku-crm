"""Admin-only viewers for the activity trail and outgoing mail log — read-only,
paginated-by-limit (most recent 300 rows) so a busy instance doesn't render an
unbounded table.
"""
from flask import Blueprint, render_template, request

from . import db
from .auth import admin_required, login_required

bp = Blueprint("admin_logs", __name__, url_prefix="/admin")

ROW_LIMIT = 300


@bp.route("/activity")
@login_required
@admin_required
def activity():
    user_id = request.args.get("user_id", type=int)
    entity_type = request.args.get("entity_type", "").strip()

    sql = (
        "SELECT a.*, u.name AS user_name FROM activity_log a "
        "LEFT JOIN users u ON u.id = a.user_id WHERE 1=1"
    )
    params = []
    if user_id:
        sql += " AND a.user_id = ?"
        params.append(user_id)
    if entity_type:
        sql += " AND a.entity_type = ?"
        params.append(entity_type)
    sql += " ORDER BY a.created_at DESC LIMIT ?"
    params.append(ROW_LIMIT)

    entries = db.query(sql, tuple(params))
    users = db.query("SELECT id, name FROM users ORDER BY name")
    entity_types = db.query(
        "SELECT DISTINCT entity_type FROM activity_log WHERE entity_type IS NOT NULL ORDER BY entity_type"
    )
    return render_template(
        "admin_logs/activity.html", entries=entries, users=users, entity_types=entity_types,
        selected_user_id=user_id, selected_entity_type=entity_type, row_limit=ROW_LIMIT,
    )


@bp.route("/mail")
@login_required
@admin_required
def mail():
    status = request.args.get("status", "").strip()

    sql = "SELECT m.*, u.name AS sent_by_name FROM mail_log m LEFT JOIN users u ON u.id = m.sent_by WHERE 1=1"
    params = []
    if status:
        sql += " AND m.status = ?"
        params.append(status)
    sql += " ORDER BY m.created_at DESC LIMIT ?"
    params.append(ROW_LIMIT)

    entries = db.query(sql, tuple(params))
    return render_template("admin_logs/mail.html", entries=entries, selected_status=status, row_limit=ROW_LIMIT)
