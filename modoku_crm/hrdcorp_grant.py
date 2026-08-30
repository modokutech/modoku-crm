"""Public "Enter Your HRDCorp Grant ID" page — no login required.

Linked from the HRDCorp Grant Documents email sent from a class's page
(sessions.py), once staff have manually sent the client their Course
Outline, Trainer Profile, Accredited Certificate and Quotation. Rather than
the client emailing the approved Grant ID back and forth, they submit it
directly here — it's saved straight onto the class record for staff to see
on the class's own page, and the class owner gets a Notification.
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

from . import db, notifications

bp = Blueprint("hrdcorp_grant", __name__, url_prefix="/hrdcorp-grant")


def _find_session(token):
    if not token:
        return None
    return db.query(
        """SELECT cs.*, c.title AS course_title FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id WHERE cs.grant_docs_token = ?""",
        (token,), one=True,
    )


@bp.route("/<token>")
def form(token):
    session_row = _find_session(token)
    if session_row is None:
        return render_template("hrdcorp_grant/not_found.html")
    return render_template("hrdcorp_grant/form.html", s=session_row, token=token)


@bp.route("/<token>/submit", methods=("POST",))
def submit(token):
    session_row = _find_session(token)
    if session_row is None:
        return render_template("hrdcorp_grant/not_found.html")

    grant_id = (request.form.get("hrdcorp_grant_id") or "").strip()
    if not grant_id:
        flash("Enter your HRDCorp Grant ID first.", "danger")
        return redirect(url_for("hrdcorp_grant.form", token=token))

    db.execute(
        "UPDATE course_sessions SET hrdcorp_grant_id = ?, hrdcorp_grant_id_updated_at = datetime('now') WHERE id = ?",
        (grant_id, session_row["id"]),
    )
    if session_row["owner_user_id"]:
        notifications.notify(
            session_row["owner_user_id"], "hrdcorp_grant_id_submitted",
            f"HRDCorp Grant ID submitted — {session_row['course_title']}",
            body=f"The client submitted their HRDCorp Grant ID ({grant_id}) for this class.",
            link=url_for("sessions.view", session_id=session_row["id"]),
        )
    flash("Thank you — your HRDCorp Grant ID has been recorded.", "success")
    return redirect(url_for("hrdcorp_grant.form", token=token))
