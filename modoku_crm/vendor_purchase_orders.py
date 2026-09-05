"""Vendor Purchase Orders — same lifecycle (Draft/Sent/Confirmed/Cancelled)
and document/email handling as the trainer-facing purchase_orders.py, but
issued to a Vendor instead of a Trainer, and optionally NOT tied to any one
class at all (e.g. a standalone printing or transport order).

IMPORTANT: PO numbers are drawn from the exact same running sequence as
trainer POs — see purchase_orders._next_po_no(), which already scans both
this table and purchase_orders when computing the next number. Do not add
a separate numbering scheme here.
"""
import mimetypes
import os
import uuid
from datetime import date

from flask import (Blueprint, Response, current_app, flash, g, redirect, render_template,
                    request, send_from_directory, url_for)
from werkzeug.utils import secure_filename

from . import activity, db, doc_sanity, mailer, uploadutil
from .auth import admin_required, login_required
from .csvutil import csv_response
from .docutil import content_disposition
from .purchase_orders import _next_po_no

bp = Blueprint("vendor_purchase_orders", __name__, url_prefix="/vendor-purchase-orders")

STATUSES = ["Draft", "Sent", "Confirmed", "Cancelled"]

DEFAULT_TERMS = "\n".join([
    "Payment term within 30 days from the date of invoice, unless otherwise agreed.",
    "Modoku reserves the right to evaluate the vendor's service quality against the agreed requirements.",
    "Any changes to scope, quantity, or schedule must be agreed with Modoku in writing beforehand.",
])


@bp.before_request
def _require_module_enabled():
    if not g.modules.get("purchase_orders", True):
        flash("The Purchase Orders module is currently disabled. Ask an admin to re-enable it under Settings.", "warning")
        return redirect(url_for("dashboard.index"))


def _po_upload_dir(po_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "vendor_purchase_orders", str(po_id))
    os.makedirs(path, exist_ok=True)
    return path


def _payment_receipt_upload_dir(po_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "vendor_po_payment_receipts", str(po_id))
    os.makedirs(path, exist_ok=True)
    return path


def _handle_document_uploads(po_id):
    files = request.files.getlist("documents")
    for file_storage in files:
        if not file_storage or not file_storage.filename:
            continue
        error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.DEFAULT_EXTENSIONS)
        if error:
            flash(error, "danger")
            continue
        safe_name = secure_filename(file_storage.filename)
        stored_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
        saved_path = os.path.join(_po_upload_dir(po_id), stored_name)
        file_storage.save(saved_path)
        db.execute(
            "INSERT INTO vendor_po_documents (po_id, filename, original_name) VALUES (?,?,?)",
            (po_id, stored_name, file_storage.filename),
        )
        warning = doc_sanity.check_document(saved_path, "financial_document")
        if warning:
            flash(f"{file_storage.filename}: {warning}", "warning")


def _save_items(po_id, form):
    descriptions = form.getlist("item_description")
    quantities = form.getlist("item_quantity")
    prices = form.getlist("item_unit_price")
    for desc, qty, price in zip(descriptions, quantities, prices):
        if not desc.strip():
            continue
        qty_f = float(qty or 1)
        price_f = float(price or 0)
        amount = round(qty_f * price_f, 2)
        db.execute(
            "INSERT INTO vendor_po_items (po_id, description, quantity, unit_price, amount) VALUES (?,?,?,?,?)",
            (po_id, desc.strip(), qty_f, price_f, amount),
        )


def _resolve_job_end_date(session_id, submitted_value):
    """When the PO is linked to a Class, the job end date follows that
    class automatically (its end date, or start date for a one-day class)
    rather than being typed in separately — so it can't drift out of sync
    with the class it's actually for. Only a standalone PO (no linked
    class) takes the manually-entered date."""
    if session_id:
        session_row = db.query(
            "SELECT start_date, end_date FROM course_sessions WHERE id = ?",
            (session_id,), one=True,
        )
        if session_row:
            return session_row["end_date"] or session_row["start_date"]
    return submitted_value or None


def _ensure_confirm_token(po_id):
    import secrets
    row = db.query("SELECT confirm_token FROM vendor_purchase_orders WHERE id = ?", (po_id,), one=True)
    if row and row["confirm_token"]:
        return row["confirm_token"]
    token = secrets.token_urlsafe(24)
    db.execute("UPDATE vendor_purchase_orders SET confirm_token = ? WHERE id = ?", (token, po_id))
    return token


def _default_email_subject(po):
    return f"{po['po_no']} — {po['description'] or po['course_title'] or 'Purchase Order'} — {po['vendor_name']}"


def _default_email_body(po, confirm_url=None):
    what = po["course_title"] or po["description"] or "the services below"
    confirm_para = ""
    if confirm_url:
        confirm_para = f"Please confirm or reject this PO here:\n{confirm_url}\n\n"
    return (
        f"Dear {po['vendor_name']},\n\n"
        "Please find attached the Purchase Order (PO) for the following:\n\n"
        f"For: {what}\n"
        f"Amount: {po['currency']} {po['fee_amount']:.2f}\n\n"
        "Kindly review the attached PO and acknowledge receipt.\n\n"
        f"{confirm_para}"
        "Should you have any questions, please feel free to contact us.\n\n"
        "Cheers!"
    )


def _default_payment_receipt_email_subject(po):
    return f"Payment Receipt — {po['po_no']} — {po['description'] or po['course_title'] or 'Purchase Order'}"


def _default_payment_receipt_email_body(po):
    what = po["course_title"] or po["description"] or "the services rendered"
    return (
        f"Dear {po['vendor_name']},\n\n"
        f"Your invoice for {po['po_no']} ({what}) has been processed and payment has been made. "
        "Please find the payment receipt attached for your records.\n\n"
        "Should you have any questions, please feel free to contact us.\n\n"
        "Cheers!"
    )


def _notify_vendor_invoice_due(po_id):
    """Fired once, the moment a vendor PO's job end date ("When does this
    job finish?") has passed — emails the vendor a unique link to the
    public invoice-submission page (vendor_invoice.py) where they can
    upload their invoice/claim documents. Guarded by
    vendor_invoice_email_sent_at so it only ever fires once per PO.

    This runs from a before_app_request hook, so it's re-checked on EVERY
    request — including the several static-asset requests one page load
    triggers (css/js/images) — which used to let more than one of those
    near-simultaneous requests see "not sent yet" before the first one's
    UPDATE landed, firing the email multiple times. Fixed by claiming the
    row (the UPDATE ... WHERE ... IS NULL below) BEFORE sending anything:
    whichever request's UPDATE actually affects a row is the only one that
    proceeds to email the vendor, so a losing concurrent request just
    returns. Best-effort throughout: a mail hiccup must never block the
    request that triggered this check."""
    try:
        po = db.query(
            """SELECT vpo.*, v.name AS vendor_name, v.contact_email AS vendor_email,
                      c.title AS course_title
               FROM vendor_purchase_orders vpo
               JOIN vendors v ON v.id = vpo.vendor_id
               LEFT JOIN course_sessions cs ON cs.id = vpo.session_id
               LEFT JOIN courses c ON c.id = cs.course_id
               WHERE vpo.id = ?""",
            (po_id,), one=True,
        )
        if po is None or po["vendor_invoice_email_sent_at"] or not po["vendor_email"]:
            return

        # Claim first — only the request whose UPDATE actually changes a row
        # goes on to send the email.
        cur = db.get_db().execute(
            "UPDATE vendor_purchase_orders SET vendor_invoice_email_sent_at = datetime('now') "
            "WHERE id = ? AND vendor_invoice_email_sent_at IS NULL",
            (po_id,),
        )
        db.get_db().commit()
        if cur.rowcount == 0:
            return  # another concurrent request already claimed this one

        from .vendor_invoice import ensure_token
        token = ensure_token(po_id)
        link = url_for("vendor_invoice.form", token=token, _external=True)
        what = po["course_title"] or po["description"] or "your job"
        subject = f"Please submit your invoice — {po['po_no']}"
        body = (
            f"Dear {po['vendor_name']},\n\n"
            f"The job below has now finished — please submit your invoice (and any claims) using the "
            f"link below. You can upload PDF, Word, or Excel files, and more than one file if you have "
            f"separate invoice/claim documents.\n\n"
            f"PO: {po['po_no']}\n"
            f"For: {what}\n\n"
            f"Submit your invoice here:\n{link}\n\n"
            f"Thank you,\nModoku Tech"
        )
        try:
            mailer.send_email(po["vendor_email"], subject, body,
                               related_type="vendor_purchase_order", related_id=po_id)
        except (mailer.MailNotConfigured, mailer.MailSendError):
            current_app.logger.exception(
                "Failed to send invoice-request email to vendor for PO %s", po_id)
    except Exception:  # noqa: BLE001 - must never block the request that triggered this
        current_app.logger.exception("Failed to notify vendor to submit invoice for PO %s", po_id)


def _sync_vendor_po_job_completion():
    """Runs once per request (cheap once a day's POs have already been
    checked): finds every Confirmed vendor PO whose job end date has passed
    and no invoice-request email has gone out yet, and fires one. Only
    Confirmed POs qualify — a Draft/Sent/Cancelled PO was never actually
    committed to, so there's nothing to invoice for."""
    today = date.today().isoformat()
    due_rows = db.query(
        """SELECT id FROM vendor_purchase_orders
           WHERE status = 'Confirmed' AND job_end_date IS NOT NULL AND job_end_date <= ?
             AND vendor_invoice_email_sent_at IS NULL""",
        (today,),
    )
    for row in due_rows:
        _notify_vendor_invoice_due(row["id"])


@bp.before_app_request
def _sync_vendor_po_statuses():
    try:
        _sync_vendor_po_job_completion()
    except Exception:  # noqa: BLE001 - never let this housekeeping break a request
        current_app.logger.exception("Failed to check for vendor POs due for an invoice request")


def _filtered_pos():
    return db.query(
        """SELECT vpo.*, v.name AS vendor_name, c.title AS course_title, cs.start_date,
                  u.name AS created_by_name
           FROM vendor_purchase_orders vpo
           JOIN vendors v ON v.id = vpo.vendor_id
           LEFT JOIN course_sessions cs ON cs.id = vpo.session_id
           LEFT JOIN courses c ON c.id = cs.course_id
           LEFT JOIN users u ON u.id = vpo.created_by
           ORDER BY vpo.issue_date DESC, vpo.id DESC"""
    )


@bp.route("/")
@login_required
def index():
    pos = _filtered_pos()
    return render_template("vendor_purchase_orders/list.html", pos=pos, statuses=STATUSES)


@bp.route("/export")
@admin_required
def export():
    pos = _filtered_pos()
    rows = (
        (p["po_no"], p["issue_date"], p["vendor_name"], p["course_title"] or p["description"] or "",
         p["fee_amount"], p["currency"], p["status"], p["created_by_name"] or "")
        for p in pos
    )
    return csv_response(
        "vendor_purchase_orders.csv",
        ["PO No", "Issue Date", "Vendor", "For", "Fee Amount", "Currency", "Status", "Created By"],
        rows,
    )


@bp.route("/new", methods=("GET", "POST"))
@login_required
def new():
    vendors = db.query("SELECT * FROM vendors ORDER BY name COLLATE NOCASE")
    sessions = db.query(
        """SELECT cs.id, cs.start_date, cs.end_date, c.title FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           WHERE cs.status != 'Cancelled' ORDER BY cs.start_date DESC"""
    )
    preselect_vendor = request.args.get("vendor_id", type=int)
    preselect_session = request.args.get("session_id", type=int)

    if request.method == "POST":
        vendor_id = request.form.get("vendor_id")
        if not vendor_id:
            flash("Vendor is required.", "danger")
        else:
            po_no = _next_po_no()
            session_id = request.form.get("session_id") or None
            job_end_date = _resolve_job_end_date(session_id, request.form.get("job_end_date"))
            po_id = db.execute(
                """INSERT INTO vendor_purchase_orders (po_no, vendor_id, session_id, description,
                       fee_amount, currency, status, terms, issue_date, job_end_date, notes, created_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    po_no, vendor_id, session_id,
                    request.form.get("description") or None,
                    request.form.get("fee_amount") or 0,
                    request.form.get("currency") or "RM",
                    request.form.get("status") or "Draft",
                    request.form.get("terms") or None,
                    request.form.get("issue_date") or date.today().isoformat(),
                    job_end_date,
                    request.form.get("notes") or None,
                    g.user["id"],
                ),
            )
            _save_items(po_id, request.form)
            _handle_document_uploads(po_id)
            activity.log("create", "vendor_purchase_order", po_id, f"Created vendor purchase order {po_no}")
            flash("Vendor purchase order created.", "success")
            return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))

    return render_template(
        "vendor_purchase_orders/form.html", po=None, vendors=vendors, sessions=sessions,
        statuses=STATUSES, preselect_vendor=preselect_vendor, preselect_session=preselect_session,
        today=date.today().isoformat(), default_terms=DEFAULT_TERMS,
    )


def _get_po_full(po_id):
    return db.query(
        """SELECT vpo.*, v.name AS vendor_name, v.contact_email AS vendor_email,
                  v.contact_phone AS vendor_phone,
                  c.title AS course_title, cs.start_date, cs.end_date, cs.venue
           FROM vendor_purchase_orders vpo
           JOIN vendors v ON v.id = vpo.vendor_id
           LEFT JOIN course_sessions cs ON cs.id = vpo.session_id
           LEFT JOIN courses c ON c.id = cs.course_id
           WHERE vpo.id = ?""",
        (po_id,), one=True,
    )


@bp.route("/<int:po_id>")
@login_required
def view(po_id):
    po = _get_po_full(po_id)
    if po is None:
        flash("Vendor purchase order not found.", "danger")
        return redirect(url_for("vendor_purchase_orders.index"))
    items = db.query("SELECT * FROM vendor_po_items WHERE po_id = ? ORDER BY id", (po_id,))
    items_total = sum(item["amount"] for item in items)
    grand_total = round(po["fee_amount"] + items_total, 2)
    documents = db.query("SELECT * FROM vendor_po_documents WHERE po_id = ? ORDER BY id", (po_id,))
    invoice_documents = db.query(
        "SELECT * FROM vendor_invoice_documents WHERE po_id = ? ORDER BY id", (po_id,)
    )
    confirm_token = _ensure_confirm_token(po_id)
    confirm_url = url_for("vendor_po_confirm.details", token=confirm_token, _external=True)
    payment_receipts = db.query(
        "SELECT * FROM vendor_po_payment_receipts WHERE po_id = ? ORDER BY id", (po_id,)
    )
    return render_template(
        "vendor_purchase_orders/view.html", po=po, statuses=STATUSES, items=items,
        grand_total=grand_total, mail_configured=mailer.is_configured(), documents=documents,
        invoice_documents=invoice_documents,
        default_email_subject=_default_email_subject(po),
        default_email_body=_default_email_body(po, confirm_url), confirm_url=confirm_url,
        payment_receipts=payment_receipts,
        default_payment_receipt_email_subject=_default_payment_receipt_email_subject(po),
        default_payment_receipt_email_body=_default_payment_receipt_email_body(po),
    )


@bp.route("/<int:po_id>/edit", methods=("GET", "POST"))
@login_required
def edit(po_id):
    po = _get_po_full(po_id)
    if po is None:
        flash("Vendor purchase order not found.", "danger")
        return redirect(url_for("vendor_purchase_orders.index"))
    if request.method == "POST":
        vendor_id = request.form.get("vendor_id")
        if not vendor_id:
            flash("Vendor is required.", "danger")
        else:
            session_id = request.form.get("session_id") or None
            job_end_date = _resolve_job_end_date(session_id, request.form.get("job_end_date"))
            db.execute(
                """UPDATE vendor_purchase_orders SET vendor_id=?, session_id=?, description=?,
                       fee_amount=?, currency=?, status=?, terms=?, issue_date=?, job_end_date=?, notes=?
                   WHERE id=?""",
                (
                    vendor_id, session_id,
                    request.form.get("description") or None,
                    request.form.get("fee_amount") or 0,
                    request.form.get("currency") or "RM",
                    request.form.get("status") or "Draft",
                    request.form.get("terms") or None,
                    request.form.get("issue_date") or date.today().isoformat(),
                    job_end_date,
                    request.form.get("notes") or None,
                    po_id,
                ),
            )
            db.execute("DELETE FROM vendor_po_items WHERE po_id = ?", (po_id,))
            _save_items(po_id, request.form)
            _handle_document_uploads(po_id)
            activity.log("update", "vendor_purchase_order", po_id, f"Updated vendor purchase order {po['po_no']}")
            flash("Vendor purchase order updated.", "success")
            return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))
    vendors = db.query("SELECT * FROM vendors ORDER BY name COLLATE NOCASE")
    sessions = db.query(
        """SELECT cs.id, cs.start_date, cs.end_date, c.title FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           WHERE cs.status != 'Cancelled' ORDER BY cs.start_date DESC"""
    )
    items = db.query("SELECT * FROM vendor_po_items WHERE po_id = ? ORDER BY id", (po_id,))
    return render_template(
        "vendor_purchase_orders/form.html", po=po, vendors=vendors, sessions=sessions,
        statuses=STATUSES, preselect_vendor=po["vendor_id"], preselect_session=po["session_id"],
        today=date.today().isoformat(), default_terms=DEFAULT_TERMS, items=items,
    )


@bp.route("/<int:po_id>/download")
@login_required
def download(po_id):
    po = _get_po_full(po_id)
    if po is None:
        flash("Vendor purchase order not found.", "danger")
        return redirect(url_for("vendor_purchase_orders.index"))
    items = db.query("SELECT * FROM vendor_po_items WHERE po_id = ? ORDER BY id", (po_id,))
    items_total = sum(item["amount"] for item in items)
    grand_total = round(po["fee_amount"] + items_total, 2)
    try:
        from . import pdfgen
        pdf_bytes = pdfgen.generate_vendor_po_pdf(po, items, grand_total)
    except Exception:  # noqa: BLE001 - surface a clean message rather than a 500
        current_app.logger.exception("Failed to generate vendor PO PDF for %s", po["po_no"])
        flash("Couldn't generate the PDF — is wkhtmltopdf installed on the server?", "danger")
        return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))
    return Response(
        pdf_bytes, mimetype="application/pdf",
        headers={"Content-Disposition": content_disposition(f"{po['po_no']}.pdf")},
    )


@bp.route("/<int:po_id>/documents", methods=("POST",))
@login_required
def upload_documents(po_id):
    po = db.query("SELECT id FROM vendor_purchase_orders WHERE id = ?", (po_id,), one=True)
    if po is None:
        flash("Vendor purchase order not found.", "danger")
        return redirect(url_for("vendor_purchase_orders.index"))
    _handle_document_uploads(po_id)
    flash("Document(s) uploaded.", "success")
    return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))


@bp.route("/<int:po_id>/documents/<int:doc_id>/download")
@login_required
def download_document(po_id, doc_id):
    doc = db.query("SELECT * FROM vendor_po_documents WHERE id = ? AND po_id = ?", (doc_id, po_id), one=True)
    if doc is None:
        flash("Document not found.", "danger")
        return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))
    return send_from_directory(_po_upload_dir(po_id), doc["filename"], as_attachment=False,
                                download_name=doc["original_name"])


@bp.route("/<int:po_id>/invoice-documents/<int:doc_id>/download")
@login_required
def download_invoice_document(po_id, doc_id):
    from .vendor_invoice import _upload_dir as _vendor_invoice_upload_dir
    doc = db.query("SELECT * FROM vendor_invoice_documents WHERE id = ? AND po_id = ?",
                    (doc_id, po_id), one=True)
    if doc is None:
        flash("Document not found.", "danger")
        return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))
    return send_from_directory(_vendor_invoice_upload_dir(po_id), doc["filename"], as_attachment=False,
                                download_name=doc["original_name"])


@bp.route("/<int:po_id>/documents/<int:doc_id>/delete", methods=("POST",))
@login_required
def delete_document(po_id, doc_id):
    db.execute("DELETE FROM vendor_po_documents WHERE id = ? AND po_id = ?", (doc_id, po_id))
    flash("Document removed.", "success")
    return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))


@bp.route("/<int:po_id>/payment-receipts", methods=("POST",))
@login_required
def upload_payment_receipts(po_id):
    po = db.query("SELECT id FROM vendor_purchase_orders WHERE id = ?", (po_id,), one=True)
    if po is None:
        flash("Vendor purchase order not found.", "danger")
        return redirect(url_for("vendor_purchase_orders.index"))
    files = request.files.getlist("payment_receipts")
    saved = 0
    for file_storage in files:
        if not file_storage or not file_storage.filename:
            continue
        error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.DEFAULT_EXTENSIONS)
        if error:
            flash(error, "danger")
            continue
        safe_name = secure_filename(file_storage.filename)
        stored_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
        saved_path = os.path.join(_payment_receipt_upload_dir(po_id), stored_name)
        file_storage.save(saved_path)
        db.execute(
            "INSERT INTO vendor_po_payment_receipts (po_id, filename, original_name) VALUES (?,?,?)",
            (po_id, stored_name, file_storage.filename),
        )
        saved += 1
        warning = doc_sanity.check_document(saved_path, "financial_document")
        if warning:
            flash(f"{file_storage.filename}: {warning}", "warning")
    if saved:
        flash(f"Uploaded {saved} payment receipt file(s).", "success")
    else:
        flash("Choose at least one file first.", "danger")
    return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))


@bp.route("/<int:po_id>/payment-receipts/<int:doc_id>/download")
@login_required
def download_payment_receipt(po_id, doc_id):
    doc = db.query("SELECT * FROM vendor_po_payment_receipts WHERE id = ? AND po_id = ?", (doc_id, po_id), one=True)
    if doc is None:
        flash("File not found.", "danger")
        return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))
    return send_from_directory(_payment_receipt_upload_dir(po_id), doc["filename"], as_attachment=False,
                                download_name=doc["original_name"])


@bp.route("/<int:po_id>/payment-receipts/<int:doc_id>/delete", methods=("POST",))
@login_required
def delete_payment_receipt(po_id, doc_id):
    db.execute("DELETE FROM vendor_po_payment_receipts WHERE id = ? AND po_id = ?", (doc_id, po_id))
    flash("File removed.", "success")
    return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))


@bp.route("/<int:po_id>/payment-receipts/send-email", methods=("POST",))
@login_required
def send_payment_receipt_email(po_id):
    po = _get_po_full(po_id)
    if po is None:
        flash("Vendor purchase order not found.", "danger")
        return redirect(url_for("vendor_purchase_orders.index"))

    receipts = db.query("SELECT * FROM vendor_po_payment_receipts WHERE po_id = ? ORDER BY id", (po_id,))
    if not receipts:
        flash("Upload at least one payment receipt file before sending.", "danger")
        return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))

    to_email = (request.form.get("to_email") or po["vendor_email"] or "").strip()
    if not to_email:
        flash("No vendor email on file — add one, or type an address to send to.", "danger")
        return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))

    subject = (request.form.get("subject") or "").strip() or _default_payment_receipt_email_subject(po)
    body = (request.form.get("body") or "").strip() or _default_payment_receipt_email_body(po)
    cc_email = (request.form.get("cc_email") or "").strip() or None

    attachments = []
    for doc in receipts:
        try:
            with open(os.path.join(_payment_receipt_upload_dir(po_id), doc["filename"]), "rb") as f:
                mimetype, _ = mimetypes.guess_type(doc["original_name"])
                attachments.append((doc["original_name"], f.read(), mimetype or "application/octet-stream"))
        except OSError:
            pass

    try:
        mailer.send_email(to_email, subject, body, attachments=attachments,
                           related_type="vendor_purchase_order", related_id=po_id, cc_email=cc_email)
    except mailer.MailNotConfigured as exc:
        flash(str(exc), "danger")
        return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))
    except mailer.MailSendError as exc:
        flash(f"Email failed to send: {exc}", "danger")
        return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))

    db.execute(
        "UPDATE vendor_purchase_orders SET payment_status = 'Paid', payment_receipt_sent_at = datetime('now'), "
        "payment_receipt_sent_to = ? WHERE id = ?",
        (to_email, po_id),
    )
    activity.log("send_email", "vendor_purchase_order", po_id, f"Emailed payment receipt for {po['po_no']} to {to_email}")
    flash(f"Payment receipt emailed to {to_email} — marked as Paid.", "success")
    return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))


@bp.route("/<int:po_id>/status", methods=("POST",))
@login_required
def update_status(po_id):
    status = request.form.get("status")
    if status in STATUSES:
        db.execute("UPDATE vendor_purchase_orders SET status = ? WHERE id = ?", (status, po_id))
        flash(f"Vendor purchase order marked as {status}.", "success")
    return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))


@bp.route("/<int:po_id>/send-email", methods=("POST",))
@login_required
def send_email(po_id):
    po = _get_po_full(po_id)
    if po is None:
        flash("Vendor purchase order not found.", "danger")
        return redirect(url_for("vendor_purchase_orders.index"))

    to_email = request.form.get("to_email") or po["vendor_email"]
    if not to_email:
        flash("This vendor has no email on file — add one on their profile, or type an address to send to.", "danger")
        return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))

    items = db.query("SELECT * FROM vendor_po_items WHERE po_id = ? ORDER BY id", (po_id,))
    items_total = sum(item["amount"] for item in items)
    grand_total = round(po["fee_amount"] + items_total, 2)

    confirm_token = _ensure_confirm_token(po_id)
    confirm_url = url_for("vendor_po_confirm.details", token=confirm_token, _external=True)

    subject = (request.form.get("subject") or "").strip() or _default_email_subject(po)
    body = (request.form.get("body") or "").strip() or _default_email_body(po, confirm_url)
    cc_email = (request.form.get("cc_email") or "").strip() or None

    attachments = []
    try:
        from . import pdfgen
        pdf_bytes = pdfgen.generate_vendor_po_pdf(po, items, grand_total)
        attachments.append((f"{po['po_no']}.pdf", pdf_bytes, "application/pdf"))
    except Exception:  # noqa: BLE001 - PDF generation is a nice-to-have, never block the email
        current_app.logger.exception("Failed to generate vendor PO PDF for %s", po["po_no"])

    documents = db.query("SELECT * FROM vendor_po_documents WHERE po_id = ? ORDER BY id", (po_id,))
    for doc in documents:
        try:
            with open(os.path.join(_po_upload_dir(po_id), doc["filename"]), "rb") as f:
                mimetype, _ = mimetypes.guess_type(doc["original_name"])
                attachments.append((doc["original_name"], f.read(), mimetype or "application/octet-stream"))
        except OSError:
            pass

    try:
        mailer.send_email(to_email, subject, body, attachments=attachments,
                           related_type="vendor_purchase_order", related_id=po_id, cc_email=cc_email)
    except mailer.MailNotConfigured as exc:
        flash(str(exc), "danger")
        return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))
    except mailer.MailSendError as exc:
        flash(f"Email failed to send: {exc}", "danger")
        return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))

    db.execute(
        "UPDATE vendor_purchase_orders SET sent_at = datetime('now'), sent_to_email = ?, "
        "status = CASE WHEN status = 'Draft' THEN 'Sent' ELSE status END WHERE id = ?",
        (to_email, po_id),
    )
    activity.log("send_email", "vendor_purchase_order", po_id, f"Emailed vendor PO {po['po_no']} to {to_email}")
    flash(f"Vendor purchase order emailed to {to_email}.", "success")
    return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))


@bp.route("/<int:po_id>/delete", methods=("POST",))
@login_required
def delete(po_id):
    po = db.query("SELECT po_no FROM vendor_purchase_orders WHERE id = ?", (po_id,), one=True)
    db.execute("DELETE FROM vendor_purchase_orders WHERE id = ?", (po_id,))
    activity.log("delete", "vendor_purchase_order", po_id,
                  f"Deleted vendor purchase order {po['po_no'] if po else po_id}")
    flash("Vendor purchase order deleted.", "success")
    return redirect(url_for("vendor_purchase_orders.index"))
