import mimetypes
import os
import secrets
import uuid
from datetime import date

from flask import (Blueprint, Response, current_app, flash, g, redirect, render_template,
                    request, send_from_directory, url_for)
from werkzeug.utils import secure_filename

from . import activity, db, mailer, uploadutil
from . import fmtdaterange
from . import sessions as _sessions
from . import settings as settings_module
from .csvutil import csv_response
from .docutil import content_disposition
from .auth import admin_required, login_required

bp = Blueprint("purchase_orders", __name__, url_prefix="/purchase-orders")

STATUSES = ["Draft", "Sent", "Confirmed", "Cancelled"]

DEFAULT_TERMS = "\n".join([
    "Training will run upon confirmation of this order.",
    "Payment term within 45 days from the date of invoice.",
    "Modoku reserves the right to evaluate the vendor's training delivery and performance against "
    "the agreed training requirements, deliverables, and service standards.",
    "In the event that the assigned trainer is unable to conduct the training, the trainer shall "
    "provide a suitable replacement trainer with equivalent or higher qualifications, subject to our approval.",
])

DEFAULT_TRAINER_RESPONSIBILITIES = "\n".join([
    "You are required to arrive at the training venue at least forty-five (45) minutes prior to the "
    "scheduled training commencement time to allow sufficient time for setup and preparation. In the event "
    "you'll be late, please contact our PIC.",
    "You shall comply with all venue, safety, and operational requirements.",
    "You shall display and use the official training banner provided by Modoku at the start of the "
    "training session and/or during breaks, including as a screen saver where applicable.",
    "Please allow adequate break time after each module to allow everyone to refresh, if needed.",
    "Please ensure that the training handouts/course manuals/exercise files(if any) are shared with "
    "the participants at the start of the training.",
    "Before ending the training, kindly share the post-test (if any) and evaluation form (attached).",
    "Please ensure that all participants complete and sign the attendance list and submit it back to "
    "Modoku.",
    "You could take a photo of the attendance list to prevent any loss of information.",
])

def _po_upload_dir(po_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "purchase_orders", str(po_id))
    os.makedirs(path, exist_ok=True)
    return path


def _payment_receipt_upload_dir(po_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "po_payment_receipts", str(po_id))
    os.makedirs(path, exist_ok=True)
    return path


def _default_payment_receipt_email_subject(po):
    return f"Payment Receipt — {po['po_no']} — {po['course_title']}"


def _default_payment_receipt_email_body(po):
    return (
        f"Hi {po['trainer_name'] or 'there'},\n\n"
        f"Your invoice for {po['po_no']} ({po['course_title']}) has been processed and payment has been "
        "made. Please find the payment receipt attached for your records.\n\n"
        "Should you have any questions, please feel free to contact us.\n\n"
        "Cheers!"
    )


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
        file_storage.save(os.path.join(_po_upload_dir(po_id), stored_name))
        db.execute(
            "INSERT INTO po_documents (po_id, filename, original_name) VALUES (?,?,?)",
            (po_id, stored_name, file_storage.filename),
        )


def _build_ics_invite(po, uid_suffix=""):
    """Builds a minimal .ics calendar invite for the training session dates
    — always carrying a real time-of-day (see sessions.ics_datetime_lines),
    not just a bare date."""
    training_time = po["training_time"] if "training_time" in po.keys() else None
    dtstart_line, dtend_line = _sessions.ics_datetime_lines(po["start_date"], po["end_date"], training_time)
    now_stamp = date.today().strftime("%Y%m%d") + "T000000Z"
    summary = f"{po['course_title']} — Training".replace("\n", " ")
    location = (po["venue"] or "").replace("\n", " ")
    uid = f"po-{po['id']}{uid_suffix}@modoku.tech"
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Modoku Hub//Purchase Order//EN\r\n"
        "CALSCALE:GREGORIAN\r\n"
        "METHOD:PUBLISH\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{now_stamp}\r\n"
        f"{dtstart_line}"
        f"{dtend_line}"
        f"SUMMARY:{summary}\r\n"
        f"LOCATION:{location}\r\n"
        f"DESCRIPTION:Training: {po['course_title']}\\nPO No.: {po['po_no']}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    ).encode("utf-8")


def _email_attachment_names(po, documents):
    """The exact list of filenames send_email() will attach for this PO —
    kept in one place so the staff-facing preview (view()) always matches
    what actually goes out, rather than a generic static description."""
    names = [f"{po['po_no']}.pdf", f"{po['po_no']}.ics"]
    if po["training_banner_file"]:
        names.append(po["training_banner_file"])
    if po["evaluation_qr_poster_file"]:
        names.append(po["evaluation_qr_poster_file"])
    names.extend(doc["original_name"] for doc in documents)
    return names


def _default_po_email_subject(po):
    client_name = po["client_name"] or "Client"
    return f"{po['po_no']} — {po['course_title']} — {client_name}"


def _ensure_confirm_token(po_id):
    """Every PO gets a long, unguessable token the first time it's needed —
    the public 'Confirm or Reject this PO' link included in the trainer
    email. Generated lazily so old POs pick one up transparently too."""
    row = db.query("SELECT confirm_token FROM purchase_orders WHERE id = ?", (po_id,), one=True)
    if row and row["confirm_token"]:
        return row["confirm_token"]
    token = secrets.token_urlsafe(24)
    db.execute("UPDATE purchase_orders SET confirm_token = ? WHERE id = ?", (token, po_id))
    return token


def _default_po_email_body(po, confirm_url=None):
    dates = fmtdaterange(po["start_date"], po["end_date"])
    venue = po["venue"] or "To be confirmed"
    meeting_link_line = ""
    if po["training_mode"] in ("Virtual", "Hybrid") and po["meeting_link"]:
        meeting_link_line = f"Meeting Link: {po['meeting_link']}\n"

    attachment_notes = []
    if po["training_banner_file"]:
        attachment_notes.append("the training banner")
    if po["evaluation_qr_poster_file"]:
        attachment_notes.append("the Training Evaluation QR poster")
    attachments_line = ""
    if attachment_notes:
        attachments_line = f"Also attached: {' and '.join(attachment_notes)}.\n\n"

    confirm_para = ""
    if confirm_url:
        confirm_para = f"Please confirm or reject this PO here:\n{confirm_url}\n\n"

    tutorial_url = url_for("tutorial.index", _external=True)
    tutorial_para = (
        "At the end of the training, please share this page with participants so they know how to "
        f"claim their e-Certificate:\n{tutorial_url}\n\n"
    )

    return (
        f"Dear {po['trainer_name']},\n\n"
        "Please find attached the Purchase Order (PO) for the training services as follows:\n\n"
        f"Training: {po['course_title']}\n"
        f"Date: {dates}\n"
        f"Time: {po['training_time'] or 'To be confirmed'}\n"
        f"Venue: {venue}\n"
        f"{meeting_link_line}\n"
        f"{attachments_line}"
        "Kindly review the attached PO — please read through the terms & conditions and your "
        "responsibilities as the trainer, set out in the PO document, and acknowledge receipt.\n\n"
        f"{confirm_para}"
        f"{tutorial_para}"
        "Should you have any questions, please feel free to contact us.\n\n"
        "Cheers!"
    )


@bp.before_request
def _require_module_enabled():
    if not g.modules.get("purchase_orders", True):
        flash("The Purchase Orders module is currently disabled. Ask an admin to re-enable it under Settings.", "warning")
        return redirect(url_for("dashboard.index"))


def _next_po_no():
    """PO-<year>-<0001> by default — prefix/suffix are admin-configurable
    under Settings, as is a one-time 'reset next number to' override. The
    <year> segment always reflects the CURRENT calendar year, but the
    running sequence number keeps counting up across the year boundary —
    it does NOT reset to 0001 in January. (So PO-2026-0057 is followed by
    PO-2027-0058, not PO-2027-0001.)

    Trainer POs (purchase_orders) and Vendor POs (vendor_purchase_orders)
    draw from this SAME running sequence, per Erik — so both tables are
    scanned here and the true numeric maximum across both wins, rather
    than trusting "last row inserted" in just one table (which would be
    wrong the moment a vendor PO and a trainer PO are created back to
    back)."""
    prefix = settings_module.get_po_number_prefix()
    suffix = settings_module.get_po_number_suffix()
    year = date.today().year
    override = settings_module.consume_po_number_override()
    if override is not None:
        last_seq = override - 1
    else:
        pattern = f"{prefix}-%"
        rows = list(db.query("SELECT po_no FROM purchase_orders WHERE po_no LIKE ?", (pattern,)))
        rows += db.query("SELECT po_no FROM vendor_purchase_orders WHERE po_no LIKE ?", (pattern,))
        last_seq = 0
        for row in rows:
            core = row["po_no"]
            if suffix and core.endswith(suffix):
                core = core[: -len(suffix)]
            try:
                seq = int(core.split("-")[-1])
            except ValueError:
                continue
            last_seq = max(last_seq, seq)
    return f"{prefix}-{year}-{last_seq + 1:04d}{suffix}"


def _save_extra_items(po_id, form):
    """Optional custom itemized rows on top of the trainer fee (e.g. materials,
    transport, per-diem) — description/quantity/unit price triples."""
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
            "INSERT INTO po_items (po_id, description, quantity, unit_price, amount) VALUES (?,?,?,?,?)",
            (po_id, desc.strip(), qty_f, price_f, amount),
        )


def _conflicts_for(trainer_id, session_id):
    """Other confirmed/sent POs for this trainer whose session dates overlap
    the given session — i.e. the trainer may already be booked that day."""
    session_row = db.query("SELECT start_date, end_date FROM course_sessions WHERE id = ?",
                            (session_id,), one=True)
    if session_row is None:
        return []
    start = session_row["start_date"]
    end = session_row["end_date"] or start
    return db.query(
        """SELECT po.po_no, po.status, cs.start_date, cs.end_date, c.title AS course_title
           FROM purchase_orders po
           JOIN course_sessions cs ON cs.id = po.session_id
           JOIN courses c ON c.id = cs.course_id
           WHERE po.trainer_id = ? AND po.session_id != ? AND po.status IN ('Sent','Confirmed')
             AND date(cs.start_date) <= date(?) AND date(COALESCE(cs.end_date, cs.start_date)) >= date(?)""",
        (trainer_id, session_id, end, start),
    )


def _filtered_pos():
    pos = db.query(
        """SELECT po.*, t.name AS trainer_name, c.title AS course_title, cs.start_date, cs.end_date,
                  u.name AS created_by_name
           FROM purchase_orders po
           LEFT JOIN trainers t ON t.id = po.trainer_id
           JOIN course_sessions cs ON cs.id = po.session_id
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN users u ON u.id = po.created_by
           ORDER BY po.issue_date DESC, po.id DESC"""
    )
    return pos


@bp.route("/")
@login_required
def index():
    pos = _filtered_pos()
    return render_template("purchase_orders/list.html", pos=pos, statuses=STATUSES)


@bp.route("/export")
@admin_required
def export():
    pos = _filtered_pos()
    rows = (
        (p["po_no"], p["issue_date"], p["trainer_name"] or "", p["course_title"], p["start_date"] or "",
         p["fee_amount"], p["currency"], p["status"], p["created_by_name"] or "")
        for p in pos
    )
    return csv_response(
        "purchase_orders.csv",
        ["PO No", "Issue Date", "Trainer", "Course", "Class Start Date", "Fee Amount", "Currency",
         "Status", "Created By"],
        rows,
    )


@bp.route("/new", methods=("GET", "POST"))
@login_required
def new():
    trainers = db.query("SELECT * FROM trainers ORDER BY name")
    sessions = db.query(
        """SELECT cs.id, cs.start_date, cs.trainer_id, c.title, c.duration_days FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           WHERE cs.status != 'Cancelled' ORDER BY cs.start_date DESC"""
    )
    preselect_trainer = request.args.get("trainer_id", type=int)
    preselect_session = request.args.get("session_id", type=int)
    conflicts = []

    if request.method == "POST":
        trainer_id = request.form.get("trainer_id")
        session_id = request.form.get("session_id")
        if not trainer_id or not session_id:
            flash("Trainer and session are required.", "danger")
        else:
            conflicts = _conflicts_for(int(trainer_id), int(session_id))
            if conflicts and not request.form.get("confirm_despite_conflict"):
                flash("This trainer already has a confirmed/sent PO overlapping these dates — "
                      "review below, then tick the box to proceed anyway if that's intended.", "danger")
            else:
                po_no = _next_po_no()
                po_id = db.execute(
                    """INSERT INTO purchase_orders (po_no, session_id, trainer_id, fee_amount, currency,
                           status, terms, trainer_responsibilities, issue_date, notes, created_by)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        po_no,
                        session_id,
                        trainer_id,
                        request.form.get("fee_amount") or 0,
                        request.form.get("currency") or "RM",
                        request.form.get("status") or "Draft",
                        request.form.get("terms") or None,
                        request.form.get("trainer_responsibilities") or None,
                        request.form.get("issue_date") or date.today().isoformat(),
                        request.form.get("notes") or None,
                        g.user["id"],
                    ),
                )
                _save_extra_items(po_id, request.form)
                _handle_document_uploads(po_id)
                activity.log("create", "purchase_order", po_id, f"Created purchase order {po_no}")
                flash("Purchase order created.", "success")
                return redirect(url_for("purchase_orders.view", po_id=po_id))

    return render_template("purchase_orders/form.html", po=None, trainers=trainers, sessions=sessions,
                            statuses=STATUSES, preselect_trainer=preselect_trainer,
                            preselect_session=preselect_session, conflicts=conflicts,
                            today=date.today().isoformat(), default_terms=DEFAULT_TERMS,
                            default_responsibilities=DEFAULT_TRAINER_RESPONSIBILITIES)


@bp.route("/<int:po_id>")
@login_required
def view(po_id):
    po = db.query(
        """SELECT po.*, t.name AS trainer_name, t.email AS trainer_email, t.phone AS trainer_phone,
                  c.title AS course_title, cs.start_date, cs.end_date, cs.venue, cs.training_time,
                  cs.training_mode, cs.meeting_link, cs.training_banner_file, cs.evaluation_qr_poster_file,
                  cl.name AS client_name,
                  u.name AS authoriser_name, u.position AS authoriser_position,
                  u.signature_file AS authoriser_signature
           FROM purchase_orders po
           JOIN trainers t ON t.id = po.trainer_id
           JOIN course_sessions cs ON cs.id = po.session_id
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN companies cl ON cl.id = cs.client_company_id
           LEFT JOIN users u ON u.id = po.created_by
           WHERE po.id = ?""",
        (po_id,), one=True,
    )
    if po is None:
        flash("Purchase order not found.", "danger")
        return redirect(url_for("purchase_orders.index"))
    items = db.query("SELECT * FROM po_items WHERE po_id = ? ORDER BY id", (po_id,))
    items_total = sum(item["amount"] for item in items)
    grand_total = round(po["fee_amount"] + items_total, 2)
    documents = db.query("SELECT * FROM po_documents WHERE po_id = ? ORDER BY id", (po_id,))
    confirm_token = _ensure_confirm_token(po_id)
    confirm_url = url_for("po_confirm.details", token=confirm_token, _external=True)
    # Trainer Invoice Documents live here now (moved off the Class page —
    # this is where staff naturally look to review a trainer's invoice).
    # The upload link itself is still per-Class, not per-PO (see
    # trainer_invoice.py — one link is shared by every trainer assigned to
    # that class), so if more than one trainer is on the same class, each
    # of their POs will show the same shared document list.
    trainer_invoice_documents = db.query(
        "SELECT * FROM trainer_invoice_documents WHERE session_id = ? ORDER BY id", (po["session_id"],)
    )
    payment_receipts = db.query(
        "SELECT * FROM po_payment_receipts WHERE po_id = ? ORDER BY id", (po_id,)
    )
    return render_template("purchase_orders/view.html", po=po, statuses=STATUSES, items=items,
                            grand_total=grand_total, mail_configured=mailer.is_configured(),
                            documents=documents, default_email_subject=_default_po_email_subject(po),
                            email_attachment_names=_email_attachment_names(po, documents),
                            default_email_body=_default_po_email_body(po, confirm_url),
                            confirm_url=confirm_url,
                            trainer_invoice_documents=trainer_invoice_documents,
                            payment_receipts=payment_receipts,
                            default_payment_receipt_email_subject=_default_payment_receipt_email_subject(po),
                            default_payment_receipt_email_body=_default_payment_receipt_email_body(po))


@bp.route("/<int:po_id>/download")
@login_required
def download(po_id):
    po = db.query(
        """SELECT po.*, t.name AS trainer_name, t.email AS trainer_email, t.phone AS trainer_phone,
                  c.title AS course_title, cs.start_date, cs.end_date, cs.venue,
                  u.name AS authoriser_name, u.position AS authoriser_position,
                  u.signature_file AS authoriser_signature
           FROM purchase_orders po
           JOIN trainers t ON t.id = po.trainer_id
           JOIN course_sessions cs ON cs.id = po.session_id
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN users u ON u.id = po.created_by
           WHERE po.id = ?""",
        (po_id,), one=True,
    )
    if po is None:
        flash("Purchase order not found.", "danger")
        return redirect(url_for("purchase_orders.index"))
    items = db.query("SELECT * FROM po_items WHERE po_id = ? ORDER BY id", (po_id,))
    items_total = sum(item["amount"] for item in items)
    grand_total = round(po["fee_amount"] + items_total, 2)
    try:
        from . import pdfgen
        pdf_bytes = pdfgen.generate_po_pdf(po, items, grand_total)
    except Exception:  # noqa: BLE001 - surface a clean message rather than a 500
        current_app.logger.exception("Failed to generate PO PDF for %s", po["po_no"])
        flash("Couldn't generate the PDF — is wkhtmltopdf installed on the server?", "danger")
        return redirect(url_for("purchase_orders.view", po_id=po_id))
    return Response(
        pdf_bytes, mimetype="application/pdf",
        headers={"Content-Disposition": content_disposition(f"{po['po_no']}.pdf")},
    )


@bp.route("/<int:po_id>/documents", methods=("POST",))
@login_required
def upload_documents(po_id):
    po = db.query("SELECT id FROM purchase_orders WHERE id = ?", (po_id,), one=True)
    if po is None:
        flash("Purchase order not found.", "danger")
        return redirect(url_for("purchase_orders.index"))
    _handle_document_uploads(po_id)
    flash("Document(s) uploaded.", "success")
    return redirect(url_for("purchase_orders.view", po_id=po_id))


@bp.route("/<int:po_id>/documents/<int:doc_id>/download")
@login_required
def download_document(po_id, doc_id):
    doc = db.query("SELECT * FROM po_documents WHERE id = ? AND po_id = ?", (doc_id, po_id), one=True)
    if doc is None:
        flash("Document not found.", "danger")
        return redirect(url_for("purchase_orders.view", po_id=po_id))
    return send_from_directory(_po_upload_dir(po_id), doc["filename"], as_attachment=False,
                                download_name=doc["original_name"])


@bp.route("/<int:po_id>/documents/<int:doc_id>/delete", methods=("POST",))
@login_required
def delete_document(po_id, doc_id):
    db.execute("DELETE FROM po_documents WHERE id = ? AND po_id = ?", (doc_id, po_id))
    flash("Document removed.", "success")
    return redirect(url_for("purchase_orders.view", po_id=po_id))


@bp.route("/<int:po_id>/payment-receipts", methods=("POST",))
@login_required
def upload_payment_receipts(po_id):
    po = db.query("SELECT id FROM purchase_orders WHERE id = ?", (po_id,), one=True)
    if po is None:
        flash("Purchase order not found.", "danger")
        return redirect(url_for("purchase_orders.index"))
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
        file_storage.save(os.path.join(_payment_receipt_upload_dir(po_id), stored_name))
        db.execute(
            "INSERT INTO po_payment_receipts (po_id, filename, original_name) VALUES (?,?,?)",
            (po_id, stored_name, file_storage.filename),
        )
        saved += 1
    if saved:
        flash(f"Uploaded {saved} payment receipt file(s).", "success")
    else:
        flash("Choose at least one file first.", "danger")
    return redirect(url_for("purchase_orders.view", po_id=po_id))


@bp.route("/<int:po_id>/payment-receipts/<int:doc_id>/download")
@login_required
def download_payment_receipt(po_id, doc_id):
    doc = db.query("SELECT * FROM po_payment_receipts WHERE id = ? AND po_id = ?", (doc_id, po_id), one=True)
    if doc is None:
        flash("File not found.", "danger")
        return redirect(url_for("purchase_orders.view", po_id=po_id))
    return send_from_directory(_payment_receipt_upload_dir(po_id), doc["filename"], as_attachment=False,
                                download_name=doc["original_name"])


@bp.route("/<int:po_id>/payment-receipts/<int:doc_id>/delete", methods=("POST",))
@login_required
def delete_payment_receipt(po_id, doc_id):
    db.execute("DELETE FROM po_payment_receipts WHERE id = ? AND po_id = ?", (doc_id, po_id))
    flash("File removed.", "success")
    return redirect(url_for("purchase_orders.view", po_id=po_id))


@bp.route("/<int:po_id>/payment-receipts/send-email", methods=("POST",))
@login_required
def send_payment_receipt_email(po_id):
    po = db.query(
        """SELECT po.*, t.name AS trainer_name, t.email AS trainer_email, c.title AS course_title
           FROM purchase_orders po
           JOIN trainers t ON t.id = po.trainer_id
           JOIN course_sessions cs ON cs.id = po.session_id
           JOIN courses c ON c.id = cs.course_id
           WHERE po.id = ?""",
        (po_id,), one=True,
    )
    if po is None:
        flash("Purchase order not found.", "danger")
        return redirect(url_for("purchase_orders.index"))

    receipts = db.query("SELECT * FROM po_payment_receipts WHERE po_id = ? ORDER BY id", (po_id,))
    if not receipts:
        flash("Upload at least one payment receipt file before sending.", "danger")
        return redirect(url_for("purchase_orders.view", po_id=po_id))

    to_email = (request.form.get("to_email") or po["trainer_email"] or "").strip()
    if not to_email:
        flash("No trainer email on file — add one, or type an address to send to.", "danger")
        return redirect(url_for("purchase_orders.view", po_id=po_id))

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
                           related_type="purchase_order", related_id=po_id, cc_email=cc_email)
    except mailer.MailNotConfigured as exc:
        flash(str(exc), "danger")
        return redirect(url_for("purchase_orders.view", po_id=po_id))
    except mailer.MailSendError as exc:
        flash(f"Email failed to send: {exc}", "danger")
        return redirect(url_for("purchase_orders.view", po_id=po_id))

    db.execute(
        "UPDATE purchase_orders SET payment_status = 'Paid', payment_receipt_sent_at = datetime('now'), "
        "payment_receipt_sent_to = ? WHERE id = ?",
        (to_email, po_id),
    )
    activity.log("send_email", "purchase_order", po_id, f"Emailed payment receipt for {po['po_no']} to {to_email}")
    flash(f"Payment receipt emailed to {to_email} — marked as Paid.", "success")
    return redirect(url_for("purchase_orders.view", po_id=po_id))


@bp.route("/<int:po_id>/status", methods=("POST",))
@login_required
def update_status(po_id):
    status = request.form.get("status")
    if status in STATUSES:
        db.execute("UPDATE purchase_orders SET status = ? WHERE id = ?", (status, po_id))
        flash(f"Purchase order marked as {status}.", "success")
        if status == "Confirmed":
            flash("Date blocked — this trainer will now show a conflict warning if another PO "
                  "is created for overlapping dates.", "success")
    return redirect(url_for("purchase_orders.view", po_id=po_id))


@bp.route("/<int:po_id>/send-email", methods=("POST",))
@login_required
def send_email(po_id):
    po = db.query(
        """SELECT po.*, t.name AS trainer_name, t.email AS trainer_email, t.phone AS trainer_phone,
                  c.title AS course_title, cs.start_date, cs.end_date, cs.venue, cs.training_time,
                  cs.training_mode, cs.meeting_link, cs.training_banner_file, cs.evaluation_qr_poster_file,
                  cl.name AS client_name,
                  u.name AS authoriser_name, u.position AS authoriser_position,
                  u.signature_file AS authoriser_signature
           FROM purchase_orders po
           JOIN trainers t ON t.id = po.trainer_id
           JOIN course_sessions cs ON cs.id = po.session_id
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN companies cl ON cl.id = cs.client_company_id
           LEFT JOIN users u ON u.id = po.created_by
           WHERE po.id = ?""",
        (po_id,), one=True,
    )
    if po is None:
        flash("Purchase order not found.", "danger")
        return redirect(url_for("purchase_orders.index"))

    to_email = request.form.get("to_email") or po["trainer_email"]
    if not to_email:
        flash("This trainer has no email on file — add one on their profile, or type an address to send to.", "danger")
        return redirect(url_for("purchase_orders.view", po_id=po_id))

    if not po["evaluation_qr_poster_file"] and not request.form.get("skip_evaluation_reminder"):
        flash(
            "This class doesn't have a Training Evaluation QR poster generated yet — generate one from the "
            "class page first so it can go out with the PO, or send anyway if you'll handle it separately.",
            "warning",
        )
        return redirect(url_for("sessions.view", session_id=po["session_id"]))

    if not po["training_banner_file"] and not request.form.get("skip_banner_reminder"):
        flash(
            "This class doesn't have a Training Banner generated yet — generate one from the "
            "class page first so it can go out with the PO, or send anyway if you'll handle it separately.",
            "warning",
        )
        return redirect(url_for("sessions.view", session_id=po["session_id"]))

    items = db.query("SELECT * FROM po_items WHERE po_id = ? ORDER BY id", (po_id,))
    items_total = sum(item["amount"] for item in items)
    grand_total = round(po["fee_amount"] + items_total, 2)

    confirm_token = _ensure_confirm_token(po_id)
    confirm_url = url_for("po_confirm.details", token=confirm_token, _external=True)

    subject = (request.form.get("subject") or "").strip() or _default_po_email_subject(po)
    body = (request.form.get("body") or "").strip() or _default_po_email_body(po, confirm_url)
    cc_email = (request.form.get("cc_email") or "").strip() or None

    attachments = []
    try:
        from . import pdfgen
        pdf_bytes = pdfgen.generate_po_pdf(po, items, grand_total)
        attachments.append((f"{po['po_no']}.pdf", pdf_bytes, "application/pdf"))
    except Exception:  # noqa: BLE001 - PDF generation is a nice-to-have, never block the email
        current_app.logger.exception("Failed to generate PO PDF for %s", po["po_no"])

    try:
        attachments.append((f"{po['po_no']}.ics", _build_ics_invite(po), "text/calendar"))
    except Exception:  # noqa: BLE001
        current_app.logger.exception("Failed to build calendar invite for %s", po["po_no"])

    session_upload_dir = os.path.join(current_app.config["UPLOAD_FOLDER"], "sessions", str(po["session_id"]))
    if po["training_banner_file"]:
        try:
            with open(os.path.join(session_upload_dir, po["training_banner_file"]), "rb") as f:
                mimetype, _ = mimetypes.guess_type(po["training_banner_file"])
                attachments.append((po["training_banner_file"], f.read(), mimetype or "application/octet-stream"))
        except OSError:
            pass
    if po["evaluation_qr_poster_file"]:
        try:
            with open(os.path.join(session_upload_dir, po["evaluation_qr_poster_file"]), "rb") as f:
                attachments.append((po["evaluation_qr_poster_file"], f.read(), "image/jpeg"))
        except OSError:
            pass

    documents = db.query("SELECT * FROM po_documents WHERE po_id = ? ORDER BY id", (po_id,))
    for doc in documents:
        try:
            with open(os.path.join(_po_upload_dir(po_id), doc["filename"]), "rb") as f:
                mimetype, _ = mimetypes.guess_type(doc["original_name"])
                attachments.append((doc["original_name"], f.read(), mimetype or "application/octet-stream"))
        except OSError:
            pass

    try:
        mailer.send_email(to_email, subject, body, attachments=attachments,
                           related_type="purchase_order", related_id=po_id, cc_email=cc_email)
    except mailer.MailNotConfigured as exc:
        flash(str(exc), "danger")
        return redirect(url_for("purchase_orders.view", po_id=po_id))
    except mailer.MailSendError as exc:
        flash(f"Email failed to send: {exc}", "danger")
        return redirect(url_for("purchase_orders.view", po_id=po_id))

    db.execute(
        "UPDATE purchase_orders SET sent_at = datetime('now'), sent_to_email = ?, "
        "status = CASE WHEN status = 'Draft' THEN 'Sent' ELSE status END WHERE id = ?",
        (to_email, po_id),
    )
    activity.log("send_email", "purchase_order", po_id, f"Emailed PO {po['po_no']} to {to_email}")
    flash(f"Purchase order emailed to {to_email}, with a calendar invite attached.", "success")
    return redirect(url_for("purchase_orders.view", po_id=po_id))


@bp.route("/<int:po_id>/delete", methods=("POST",))
@login_required
def delete(po_id):
    po = db.query("SELECT po_no FROM purchase_orders WHERE id = ?", (po_id,), one=True)
    db.execute("DELETE FROM purchase_orders WHERE id = ?", (po_id,))
    activity.log("delete", "purchase_order", po_id, f"Deleted purchase order {po['po_no'] if po else po_id}")
    flash("Purchase order deleted.", "success")
    return redirect(url_for("purchase_orders.index"))
