"""Public "Return Signed JD14 Form" flow — no login required.

Each class gets a unique, unguessable link (see sessions.ensure_jd14_return_token)
that staff can email to the client/trainer once the HRDCorp Joint Declaration
Form (PSMB/SBL-KHAS/JD/14) needs to be signed and sent back. The client opens
it, sees a summary of the class, and uploads the signed copy directly — a
single file only, unlike the multi-photo Return Attendance Form flow — landing
in Modoku Hub against that class exactly as if staff had used the manual
Upload button on the class page (sessions._handle_jd14_upload), and staff get
the usual "document ready" notification email.
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

from . import db
from .sessions import _handle_jd14_upload, _notify_document_uploaded

bp = Blueprint("jd14_return", __name__, url_prefix="/jd14-return")


def _find_session(token):
    if not token:
        return None
    return db.query(
        """SELECT cs.*, c.title AS course_title FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id WHERE cs.jd14_return_token = ?""",
        (token,), one=True,
    )


@bp.route("/<token>")
def details(token):
    session_row = _find_session(token)
    if session_row is None:
        return render_template("jd14_return/not_found.html")
    return render_template("jd14_return/details.html", s=session_row, token=token)


@bp.route("/<token>/submit", methods=("POST",))
def submit(token):
    session_row = _find_session(token)
    if session_row is None:
        return render_template("jd14_return/not_found.html")

    file_storage = request.files.get("jd14_file")
    if not file_storage or not file_storage.filename:
        flash("Choose the signed JD14 file first.", "danger")
        return redirect(url_for("jd14_return.details", token=token))

    if not _handle_jd14_upload(session_row["id"]):
        # _handle_jd14_upload already flashed the specific reason (bad file type, etc).
        return redirect(url_for("jd14_return.details", token=token))

    db.execute(
        "UPDATE course_sessions SET jd14_received_at = datetime('now'), "
        "jd14_received_via = 'client_upload' WHERE id = ?",
        (session_row["id"],),
    )
    _notify_document_uploaded(session_row["id"], "JD14 Form")

    return render_template("jd14_return/success.html", s=session_row)
