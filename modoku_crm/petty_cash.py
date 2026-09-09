"""Petty Cash Vouchers (/petty-cash) — a simple running log of small cash-out
payments. Deliberately NOT a float/imprest system: there's no running
balance to reconcile against, just one voucher per cash-out with a date,
payee, purpose, category and amount.

Any logged-in staff member can raise a voucher; it starts life Pending and
only counts as final once a designated approver (admin) marks it Approved
(or Rejected, with a reason). A voucher can optionally be linked to a class
and/or carry one attached receipt/proof image or PDF — both are entirely
optional, so it also works standalone (free-text payee/purpose, no class
required).

A printable/downloadable PDF slip (voucher no., date, paid to, purpose,
amount, signature lines) is generated on demand, matching how the PO /
Invoice / Quotation modules already produce their documents.
"""
import os
import uuid
from datetime import date

from flask import (Blueprint, Response, current_app, flash, g, redirect, render_template,
                    request, send_from_directory, url_for)
from werkzeug.utils import secure_filename

from . import activity, db, doc_sanity, uploadutil
from .auth import admin_required, login_required
from .docutil import content_disposition

bp = Blueprint("petty_cash", __name__, url_prefix="/petty-cash")

STATUSES = ("Pending", "Approved", "Rejected")


def _receipt_dir(voucher_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "petty_cash", str(voucher_id))
    os.makedirs(path, exist_ok=True)
    return path


def _next_voucher_no():
    """PCV-<year>-<0001>, its own simple sequence — not shared with the
    admin-configurable PO/Invoice numbering, to match the 'simple log'
    spirit of this module. Never resets across year boundaries; scans
    existing rows for the true numeric max, same approach as
    purchase_orders._next_po_no()."""
    year = date.today().year
    prefix = f"PCV-{year}-"
    rows = db.query("SELECT voucher_no FROM petty_cash_vouchers WHERE voucher_no LIKE ?", (f"{prefix}%",))
    last_seq = 0
    for row in rows:
        try:
            seq = int(row["voucher_no"].split("-")[-1])
        except ValueError:
            continue
        last_seq = max(last_seq, seq)
    return f"{prefix}{last_seq + 1:04d}"


def _get_voucher(voucher_id):
    return db.query(
        """SELECT pcv.*, c.title AS course_title, cs.start_date, cs.end_date, cs.venue,
                  req.name AS requested_by_name, app.name AS approved_by_name
           FROM petty_cash_vouchers pcv
           LEFT JOIN course_sessions cs ON cs.id = pcv.session_id
           LEFT JOIN courses c ON c.id = cs.course_id
           LEFT JOIN users req ON req.id = pcv.requested_by
           LEFT JOIN users app ON app.id = pcv.approved_by
           WHERE pcv.id = ?""",
        (voucher_id,), one=True,
    )


def _sessions_for_link():
    return db.query(
        """SELECT cs.id, cs.start_date, cs.end_date, c.title
           FROM course_sessions cs JOIN courses c ON c.id = cs.course_id
           WHERE cs.status != 'Cancelled' ORDER BY cs.start_date DESC"""
    )


@bp.route("/")
@login_required
def index():
    status_filter = request.args.get("status") or ""
    sql = (
        """SELECT pcv.*, c.title AS course_title, req.name AS requested_by_name
           FROM petty_cash_vouchers pcv
           LEFT JOIN course_sessions cs ON cs.id = pcv.session_id
           LEFT JOIN courses c ON c.id = cs.course_id
           LEFT JOIN users req ON req.id = pcv.requested_by"""
    )
    params = ()
    if status_filter in STATUSES:
        sql += " WHERE pcv.status = ?"
        params = (status_filter,)
    sql += " ORDER BY pcv.created_at DESC"
    vouchers = db.query(sql, params)
    return render_template(
        "petty_cash/index.html", vouchers=vouchers, statuses=STATUSES, status_filter=status_filter,
    )


@bp.route("/new", methods=("GET", "POST"))
@login_required
def new():
    if request.method == "POST":
        payee_name = (request.form.get("payee_name") or "").strip()
        purpose = (request.form.get("purpose") or "").strip()
        category = (request.form.get("category") or "").strip() or None
        voucher_date = request.form.get("voucher_date") or date.today().isoformat()
        amount = request.form.get("amount") or "0"
        session_id = request.form.get("session_id") or None

        if not payee_name or not purpose:
            flash("Paid To and Purpose are required.", "danger")
        else:
            try:
                amount_f = round(float(amount), 2)
            except ValueError:
                amount_f = 0
            if amount_f <= 0:
                flash("Enter an amount greater than zero.", "danger")
            else:
                voucher_no = _next_voucher_no()
                voucher_id = db.execute(
                    """INSERT INTO petty_cash_vouchers
                           (voucher_no, voucher_date, payee_name, purpose, category, amount,
                            session_id, requested_by)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (voucher_no, voucher_date, payee_name, purpose, category, amount_f,
                     session_id or None, g.user["id"]),
                )
                _handle_receipt_upload(voucher_id)
                activity.log("create", "petty_cash_voucher", voucher_id,
                             f"Raised petty cash voucher {voucher_no} for {payee_name}")
                flash(f"Voucher {voucher_no} submitted — pending approval.", "success")
                return redirect(url_for("petty_cash.view", voucher_id=voucher_id))

    return render_template(
        "petty_cash/form.html", voucher=None, sessions=_sessions_for_link(),
        today=date.today().isoformat(),
    )


def _handle_receipt_upload(voucher_id):
    file_storage = request.files.get("receipt")
    if not file_storage or not file_storage.filename:
        return
    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.DEFAULT_EXTENSIONS)
    if error:
        flash(error, "danger")
        return
    safe_name = secure_filename(file_storage.filename)
    stored_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
    saved_path = os.path.join(_receipt_dir(voucher_id), stored_name)
    file_storage.save(saved_path)
    db.execute(
        "UPDATE petty_cash_vouchers SET receipt_filename = ?, receipt_original_name = ? WHERE id = ?",
        (stored_name, file_storage.filename, voucher_id),
    )
    warning = doc_sanity.check_document(saved_path, "financial_document")
    if warning:
        flash(f"{file_storage.filename}: {warning}", "warning")


@bp.route("/<int:voucher_id>")
@login_required
def view(voucher_id):
    voucher = _get_voucher(voucher_id)
    if voucher is None:
        flash("Voucher not found.", "danger")
        return redirect(url_for("petty_cash.index"))
    return render_template("petty_cash/view.html", voucher=voucher)


@bp.route("/<int:voucher_id>/edit", methods=("GET", "POST"))
@login_required
def edit(voucher_id):
    voucher = _get_voucher(voucher_id)
    if voucher is None:
        flash("Voucher not found.", "danger")
        return redirect(url_for("petty_cash.index"))
    if voucher["status"] != "Pending":
        flash("Only a Pending voucher can be edited — it's already been processed.", "danger")
        return redirect(url_for("petty_cash.view", voucher_id=voucher_id))

    if request.method == "POST":
        payee_name = (request.form.get("payee_name") or "").strip()
        purpose = (request.form.get("purpose") or "").strip()
        category = (request.form.get("category") or "").strip() or None
        voucher_date = request.form.get("voucher_date") or voucher["voucher_date"]
        amount = request.form.get("amount") or "0"
        session_id = request.form.get("session_id") or None

        if not payee_name or not purpose:
            flash("Paid To and Purpose are required.", "danger")
        else:
            try:
                amount_f = round(float(amount), 2)
            except ValueError:
                amount_f = 0
            if amount_f <= 0:
                flash("Enter an amount greater than zero.", "danger")
            else:
                db.execute(
                    """UPDATE petty_cash_vouchers
                       SET payee_name = ?, purpose = ?, category = ?, voucher_date = ?,
                           amount = ?, session_id = ?
                       WHERE id = ?""",
                    (payee_name, purpose, category, voucher_date, amount_f, session_id or None, voucher_id),
                )
                _handle_receipt_upload(voucher_id)
                activity.log("update", "petty_cash_voucher", voucher_id,
                             f"Edited petty cash voucher {voucher['voucher_no']}")
                flash("Voucher updated.", "success")
                return redirect(url_for("petty_cash.view", voucher_id=voucher_id))

    return render_template(
        "petty_cash/form.html", voucher=voucher, sessions=_sessions_for_link(),
        today=date.today().isoformat(),
    )


@bp.route("/<int:voucher_id>/receipt/download")
@login_required
def download_receipt(voucher_id):
    voucher = db.query("SELECT * FROM petty_cash_vouchers WHERE id = ?", (voucher_id,), one=True)
    if voucher is None or not voucher["receipt_filename"]:
        flash("No receipt attached.", "danger")
        return redirect(url_for("petty_cash.view", voucher_id=voucher_id))
    return send_from_directory(
        _receipt_dir(voucher_id), voucher["receipt_filename"], as_attachment=False,
        download_name=voucher["receipt_original_name"] or voucher["receipt_filename"],
    )


@bp.route("/<int:voucher_id>/receipt/delete", methods=("POST",))
@login_required
def delete_receipt(voucher_id):
    voucher = db.query("SELECT * FROM petty_cash_vouchers WHERE id = ?", (voucher_id,), one=True)
    if voucher is None:
        flash("Voucher not found.", "danger")
        return redirect(url_for("petty_cash.index"))
    if voucher["status"] != "Pending":
        flash("Only a Pending voucher can be edited — it's already been processed.", "danger")
        return redirect(url_for("petty_cash.view", voucher_id=voucher_id))
    db.execute(
        "UPDATE petty_cash_vouchers SET receipt_filename = NULL, receipt_original_name = NULL WHERE id = ?",
        (voucher_id,),
    )
    flash("Receipt removed.", "success")
    return redirect(url_for("petty_cash.view", voucher_id=voucher_id))


@bp.route("/<int:voucher_id>/approve", methods=("POST",))
@login_required
@admin_required
def approve(voucher_id):
    voucher = db.query("SELECT * FROM petty_cash_vouchers WHERE id = ?", (voucher_id,), one=True)
    if voucher is None:
        flash("Voucher not found.", "danger")
        return redirect(url_for("petty_cash.index"))
    if voucher["status"] != "Pending":
        flash("This voucher has already been processed.", "danger")
        return redirect(url_for("petty_cash.view", voucher_id=voucher_id))
    db.execute(
        """UPDATE petty_cash_vouchers
           SET status = 'Approved', approved_by = ?, approved_at = datetime('now'), rejection_reason = NULL
           WHERE id = ?""",
        (g.user["id"], voucher_id),
    )
    activity.log("approve", "petty_cash_voucher", voucher_id,
                 f"Approved petty cash voucher {voucher['voucher_no']}")
    flash(f"Voucher {voucher['voucher_no']} approved.", "success")
    return redirect(url_for("petty_cash.view", voucher_id=voucher_id))


@bp.route("/<int:voucher_id>/reject", methods=("POST",))
@login_required
@admin_required
def reject(voucher_id):
    voucher = db.query("SELECT * FROM petty_cash_vouchers WHERE id = ?", (voucher_id,), one=True)
    if voucher is None:
        flash("Voucher not found.", "danger")
        return redirect(url_for("petty_cash.index"))
    if voucher["status"] != "Pending":
        flash("This voucher has already been processed.", "danger")
        return redirect(url_for("petty_cash.view", voucher_id=voucher_id))
    reason = (request.form.get("rejection_reason") or "").strip() or None
    db.execute(
        """UPDATE petty_cash_vouchers
           SET status = 'Rejected', approved_by = ?, approved_at = datetime('now'), rejection_reason = ?
           WHERE id = ?""",
        (g.user["id"], reason, voucher_id),
    )
    activity.log("reject", "petty_cash_voucher", voucher_id,
                 f"Rejected petty cash voucher {voucher['voucher_no']}")
    flash(f"Voucher {voucher['voucher_no']} rejected.", "success")
    return redirect(url_for("petty_cash.view", voucher_id=voucher_id))


@bp.route("/<int:voucher_id>/download")
@login_required
def download(voucher_id):
    voucher = _get_voucher(voucher_id)
    if voucher is None:
        flash("Voucher not found.", "danger")
        return redirect(url_for("petty_cash.index"))
    try:
        from . import pdfgen
        pdf_bytes = pdfgen.generate_petty_cash_pdf(voucher)
    except Exception:  # noqa: BLE001 - surface a clean message rather than a 500
        current_app.logger.exception("Failed to generate Petty Cash Voucher PDF for %s", voucher["voucher_no"])
        flash("Couldn't generate the PDF — is wkhtmltopdf installed on the server?", "danger")
        return redirect(url_for("petty_cash.view", voucher_id=voucher_id))
    return Response(
        pdf_bytes, mimetype="application/pdf",
        headers={"Content-Disposition": content_disposition(f"{voucher['voucher_no']}.pdf")},
    )


@bp.route("/<int:voucher_id>/delete", methods=("POST",))
@login_required
@admin_required
def delete(voucher_id):
    voucher = db.query("SELECT voucher_no FROM petty_cash_vouchers WHERE id = ?", (voucher_id,), one=True)
    db.execute("DELETE FROM petty_cash_vouchers WHERE id = ?", (voucher_id,))
    activity.log("delete", "petty_cash_voucher", voucher_id,
                 f"Deleted petty cash voucher {voucher['voucher_no'] if voucher else voucher_id}")
    flash("Voucher deleted.", "success")
    return redirect(url_for("petty_cash.index"))
