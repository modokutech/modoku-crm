"""Per-user notification inbox — a lightweight, in-app alternative/companion
to email for things staff need to act on: a quotation gone quiet, an invoice
overdue, an evaluation report overdue, etc. Other modules call notify()
(usually with a dedupe_key so a recurring background check doesn't spam the
same person every request); this module only owns the inbox itself (list,
unread count, mark read) and the nav bell.
"""
from flask import Blueprint, g, redirect, render_template, request, url_for

from . import db
from .auth import login_required

bp = Blueprint("notifications", __name__, url_prefix="/notifications")


def notify(user_id, notif_type, title, body=None, link=None, dedupe_key=None):
    """Creates a notification for one user. If dedupe_key is given and a
    notification with that (user_id, dedupe_key) pair already exists, this
    is a silent no-op — safe to call on every request from a background
    check without flooding the inbox with duplicates. Never raises: a
    notification failing to save should never break the caller's own
    request."""
    if not user_id:
        return
    try:
        if dedupe_key:
            existing = db.query(
                "SELECT id FROM notifications WHERE user_id = ? AND dedupe_key = ?",
                (user_id, dedupe_key), one=True,
            )
            if existing:
                return
        db.execute(
            "INSERT INTO notifications (user_id, type, title, body, link, dedupe_key) VALUES (?,?,?,?,?,?)",
            (user_id, notif_type, title, body, link, dedupe_key),
        )
    except Exception:  # noqa: BLE001 - a notification is never allowed to break the caller
        from flask import current_app
        current_app.logger.exception("Failed to create notification for user %s", user_id)


def notify_admins(notif_type, title, body=None, link=None, dedupe_key=None):
    """Notifies every active admin — used for office-wide events that don't
    have one obvious single owner (a trainer/vendor confirming or rejecting
    a PO, a vendor uploading invoice documents, and similar) so the whole
    office sees it in their inbox, not just whoever happens to check email.
    dedupe_key is shared across all recipients as given (notify() already
    scopes the dedupe check per-user, so each admin still gets exactly one
    notification even though they share a dedupe_key)."""
    admins = db.query("SELECT id FROM users WHERE role = 'admin' AND active = 1")
    for admin in admins:
        notify(admin["id"], notif_type, title, body=body, link=link, dedupe_key=dedupe_key)


def unread_count(user_id):
    if not user_id:
        return 0
    row = db.query(
        "SELECT COUNT(*) AS n FROM notifications WHERE user_id = ? AND read_at IS NULL",
        (user_id,), one=True,
    )
    return row["n"] if row else 0


@bp.route("/")
@login_required
def index():
    items = db.query(
        "SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC LIMIT 100",
        (g.user["id"],),
    )
    db.execute(
        "UPDATE notifications SET read_at = datetime('now') WHERE user_id = ? AND read_at IS NULL",
        (g.user["id"],),
    )
    return render_template("notifications/index.html", items=items)


@bp.route("/<int:notif_id>/open")
@login_required
def open_notification(notif_id):
    """Marks one notification read and forwards to its link, if any — used
    when a notification is opened from somewhere other than the inbox list
    (e.g. a future dropdown) where index()'s bulk mark-as-read wouldn't run."""
    row = db.query("SELECT * FROM notifications WHERE id = ? AND user_id = ?", (notif_id, g.user["id"]), one=True)
    if row is None:
        return redirect(url_for("notifications.index"))
    if not row["read_at"]:
        db.execute("UPDATE notifications SET read_at = datetime('now') WHERE id = ?", (notif_id,))
    return redirect(row["link"] or url_for("notifications.index"))


@bp.route("/mark-all-read", methods=("POST",))
@login_required
def mark_all_read():
    db.execute(
        "UPDATE notifications SET read_at = datetime('now') WHERE user_id = ? AND read_at IS NULL",
        (g.user["id"],),
    )
    return redirect(request.referrer or url_for("notifications.index"))
