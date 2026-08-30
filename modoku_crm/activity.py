"""Lightweight activity trail — records who did what, for the admin-only
Activity Log page. Best-effort: a logging failure never breaks the action
being logged, but it IS written to the server log so a broken activity
trail (e.g. a missing/out-of-date table on a long-running process) shows
up somewhere instead of failing completely silently.
"""
from flask import current_app, g

from . import db


def log(action, entity_type=None, entity_id=None, description=None):
    try:
        user_id = g.user["id"] if getattr(g, "user", None) else None
        db.execute(
            "INSERT INTO activity_log (user_id, action, entity_type, entity_id, description) "
            "VALUES (?,?,?,?,?)",
            (user_id, action, entity_type, entity_id, description),
        )
    except Exception:  # noqa: BLE001 - never let logging break the real action
        try:
            current_app.logger.exception(
                "activity.log failed for action=%s entity_type=%s entity_id=%s", action, entity_type, entity_id
            )
        except Exception:  # noqa: BLE001 - even logging the failure must never break the action
            pass
