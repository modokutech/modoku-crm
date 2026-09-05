"""Public "Return Signed Quotation" flow — no login required.

Each quotation gets a unique, unguessable link (see quotations._ensure_return_token)
included in the email sent to the client. The client opens it, sees a summary
of the quotation, and uploads a photo/scan of the signed copy directly —
landing in Modoku Hub against that quotation. This is the self-service
counterpart to the staff-side manual upload on the quotation's own page
(quotations.upload_signed); both paths trigger the same downstream
automation (quotations._handle_quotation_signed): the linked class advances
out of 'Proposed' if needed, and the client is emailed their T3 Attendance
Form link plus a calendar invite.
"""
import os
import uuid

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from . import db, doc_sanity, uploadutil

bp = Blueprint("quotation_return", __name__, url_prefix="/quotation-return")


def _upload_dir(quotation_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "quotations", str(quotation_id))
    os.makedirs(path, exist_ok=True)
    return path


def _find_quotation(token):
    if not token:
        return None
    return db.query(
        """SELECT q.*, co.name AS client_company_name,
                  cs.start_date AS training_start_date, cs.end_date AS training_end_date
           FROM quotations q
           LEFT JOIN companies co ON co.id = q.client_company_id
           LEFT JOIN course_sessions cs ON cs.id = q.session_id
           WHERE q.return_token = ?""",
        (token,), one=True,
    )


@bp.route("/<token>")
def details(token):
    q = _find_quotation(token)
    if q is None:
        flash("That link isn't valid — please check the email again, or contact us for a new one.", "danger")
        return render_template("quotation_return/not_found.html")
    return render_template("quotation_return/details.html", q=q, token=token)


@bp.route("/<token>/submit", methods=("POST",))
def submit(token):
    q = _find_quotation(token)
    if q is None:
        flash("That link isn't valid — please check the email again, or contact us for a new one.", "danger")
        return render_template("quotation_return/not_found.html")

    file_storage = request.files.get("signed_file")
    if not file_storage or not file_storage.filename:
        flash("Choose the signed quotation file first.", "danger")
        return redirect(url_for("quotation_return.details", token=token))
    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.DEFAULT_EXTENSIONS)
    if error:
        flash(error, "danger")
        return redirect(url_for("quotation_return.details", token=token))

    safe_name = secure_filename(file_storage.filename)
    stored_name = f"signed_{uuid.uuid4().hex[:8]}_{safe_name}"
    saved_path = os.path.join(_upload_dir(q["id"]), stored_name)
    file_storage.save(saved_path)

    client_email = (request.form.get("client_email") or "").strip() or None
    db.execute(
        "UPDATE quotations SET signed_file = ?, signed_received_at = datetime('now'), "
        "signed_received_via = 'client_upload', status = 'Accepted' WHERE id = ?",
        (stored_name, q["id"]),
    )

    ai_warning = doc_sanity.check_document(saved_path, "signed_quotation")
    from .quotations import _handle_quotation_signed
    _handle_quotation_signed(q["id"], client_email, ai_warning=ai_warning)

    return render_template("quotation_return/success.html", q=q)
