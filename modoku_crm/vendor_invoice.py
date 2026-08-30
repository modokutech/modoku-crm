"""Public "Submit Your Invoice" flow for vendors — no login required.

Mirrors trainer_invoice.py's flow exactly, but for Vendor Purchase Orders:
once a vendor PO's job end date ("When does this job finish?") has passed
(see vendor_purchase_orders._notify_vendor_invoice_due, checked opportunistically
on every request from vendor_purchase_orders._sync_vendor_po_statuses), the
vendor is emailed a unique, unguessable link to this page to upload their
invoice/claim documents. Nothing here ever locks, so they can come back and
add more later. Every uploaded document is listed on the PO's own page in
Modoku Hub for staff to review.
"""
import os
import secrets
import uuid

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from . import db, mailer, notifications, uploadutil
from . import settings as settings_module

bp = Blueprint("vendor_invoice", __name__, url_prefix="/vendor-invoice")


def _upload_dir(po_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "vendor_invoices", str(po_id))
    os.makedirs(path, exist_ok=True)
    return path


def ensure_token(po_id):
    """Every vendor PO gets a long, unguessable token the first time it's
    needed — generated lazily (when the job-end-date email is about to go
    out) rather than at PO-creation time."""
    row = db.query("SELECT vendor_invoice_token FROM vendor_purchase_orders WHERE id = ?", (po_id,), one=True)
    if row and row["vendor_invoice_token"]:
        return row["vendor_invoice_token"]
    token = secrets.token_urlsafe(24)
    db.execute("UPDATE vendor_purchase_orders SET vendor_invoice_token = ? WHERE id = ?", (token, po_id))
    return token


def _find_po(token):
    if not token:
        return None
    return db.query(
        """SELECT vpo.*, v.name AS vendor_name FROM vendor_purchase_orders vpo
           JOIN vendors v ON v.id = vpo.vendor_id WHERE vpo.vendor_invoice_token = ?""",
        (token,), one=True,
    )


@bp.route("/<token>")
def form(token):
    po = _find_po(token)
    if po is None:
        return render_template("vendor_invoice/not_found.html")
    documents = db.query(
        "SELECT * FROM vendor_invoice_documents WHERE po_id = ? ORDER BY id",
        (po["id"],),
    )
    return render_template("vendor_invoice/form.html", po=po, documents=documents, token=token)


@bp.route("/<token>/submit", methods=("POST",))
def submit(token):
    po = _find_po(token)
    if po is None:
        return render_template("vendor_invoice/not_found.html")

    files = request.files.getlist("invoice_files")
    saved = 0
    for file_storage in files:
        if not file_storage or not file_storage.filename:
            continue
        error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.DOCUMENT_EXTENSIONS)
        if error:
            flash(error, "danger")
            return redirect(url_for("vendor_invoice.form", token=token))
        safe_name = secure_filename(file_storage.filename)
        stored_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
        file_storage.save(os.path.join(_upload_dir(po["id"]), stored_name))
        db.execute(
            "INSERT INTO vendor_invoice_documents (po_id, filename, original_name) VALUES (?,?,?)",
            (po["id"], stored_name, file_storage.filename),
        )
        saved += 1

    if saved:
        flash(f"Uploaded {saved} document(s) — thank you!", "success")
        # Every submission (the first one, and any later change/addition)
        # tells the office — by email and in the notifications inbox — so
        # nobody has to keep checking the PO page to notice new documents.
        po_url = url_for("vendor_purchase_orders.view", po_id=po["id"], _external=True)
        try:
            notify_to = ", ".join(settings_module.get_notification_emails())
            if notify_to:
                mailer.send_email(
                    notify_to,
                    f"Vendor invoice documents submitted — {po['po_no']}",
                    f"{po['vendor_name']} has submitted {saved} invoice/claim document(s) for "
                    f"{po['po_no']}.\n\nReview them here:\n{po_url}",
                    related_type="vendor_purchase_order", related_id=po["id"],
                )
        except Exception:  # noqa: BLE001 - notification must never break the vendor's upload
            current_app.logger.exception(
                "Failed to send office email for vendor invoice upload on PO %s", po["id"])
        notifications.notify_admins(
            "vendor_invoice_submitted",
            f"{po['vendor_name']} submitted invoice documents — {po['po_no']}",
            body=f"{saved} document(s) uploaded for {po['po_no']}.",
            link=url_for("vendor_purchase_orders.view", po_id=po["id"]),
        )
    elif not files or not files[0].filename:
        flash("Choose at least one file first.", "danger")
    return redirect(url_for("vendor_invoice.form", token=token))
