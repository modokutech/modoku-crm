import json
from datetime import date, timedelta

from flask import Blueprint, Response, current_app, flash, g, redirect, render_template, request, url_for

from . import activity, db, fmtdate, mailer, notifications
from . import settings as settings_module
from .auth import admin_required, login_required
from .csvutil import csv_response
from .docutil import content_disposition

bp = Blueprint("invoices", __name__, url_prefix="/invoices")

STATUSES = ["Draft", "Sent", "Paid", "Overdue", "Cancelled"]


@bp.before_request
def _require_module_enabled():
    if not g.modules.get("invoices", True):
        flash("The Invoices module is currently disabled. Ask an admin to re-enable it under Settings.", "warning")
        return redirect(url_for("dashboard.index"))


def _auto_mark_overdue_invoices():
    """A 'Sent' (unpaid) invoice whose due date (invoice_date + 30 days) has
    passed flips to 'Overdue' automatically, and its creator gets a
    Notification reminding them to chase the client. Never touches
    Draft/Paid/Cancelled, and never moves an invoice backwards. Runs once
    per request, same pattern as classes'/quotations' own auto-advance."""
    today = date.today().isoformat()
    due_rows = db.query(
        "SELECT id, invoice_no, created_by FROM invoices "
        "WHERE status = 'Sent' AND due_date IS NOT NULL AND due_date < ?",
        (today,),
    )
    if not due_rows:
        return
    ids = [row["id"] for row in due_rows]
    placeholders = ",".join("?" * len(ids))
    db.execute(f"UPDATE invoices SET status = 'Overdue' WHERE id IN ({placeholders})", ids)
    for row in due_rows:
        notifications.notify(
            row["created_by"], "invoice_overdue",
            f"Invoice {row['invoice_no']} is now overdue",
            body="Payment is past its due date — chase the client for payment.",
            link=url_for("invoices.view", invoice_id=row["id"]),
            dedupe_key=f"invoice:{row['id']}:overdue",
        )


@bp.before_app_request
def _sync_invoice_statuses():
    try:
        _auto_mark_overdue_invoices()
    except Exception:  # noqa: BLE001 - never let this housekeeping break a request
        current_app.logger.exception("Failed to auto-mark overdue invoices")


def _next_invoice_no():
    """INV-<year>-<0001> by default — prefix/suffix are admin-configurable
    under Settings, as is a one-time 'reset next number to' override. The
    <year> segment always reflects the CURRENT calendar year, but the
    running sequence number keeps counting up across the year boundary —
    it does NOT reset to 0001 in January. (So INV-2026-0057 is followed by
    INV-2027-0058, not INV-2027-0001.) The lookup below deliberately
    matches on prefix only, across all years, to find that running total."""
    prefix = settings_module.get_invoice_number_prefix()
    suffix = settings_module.get_invoice_number_suffix()
    year = date.today().year
    override = settings_module.consume_invoice_number_override()
    if override is not None:
        last_seq = override - 1
    else:
        row = db.query(
            "SELECT invoice_no FROM invoices WHERE invoice_no LIKE ? ORDER BY id DESC LIMIT 1",
            (f"{prefix}-%",), one=True,
        )
        last_seq = 0
        if row:
            core = row["invoice_no"]
            if suffix and core.endswith(suffix):
                core = core[: -len(suffix)]
            try:
                last_seq = int(core.split("-")[-1])
            except ValueError:
                last_seq = 0
    return f"{prefix}-{year}-{last_seq + 1:04d}{suffix}"


def _default_invoice_email_subject(invoice):
    return f"Invoice {invoice['invoice_no']} from Modoku Tech Sdn Bhd"


def _default_invoice_email_body(invoice):
    greeting_name = invoice["bill_to_name"] or "there"
    project_line = f"Project: {invoice['project_title']}\n" if invoice["project_title"] else ""
    return (
        f"Hi {greeting_name},\n\n"
        f"Please find attached invoice {invoice['invoice_no']} for your reference.\n\n"
        f"{project_line}"
        f"Amount due: {invoice['currency']} {invoice['total']:,.2f}\n"
        f"Due date: {fmtdate(invoice['due_date'])}\n\n"
        "Kindly arrange payment by the due date above. Do let us know if you have any questions.\n\n"
        "Thank you."
    )


def _filtered_invoices():
    status = request.args.get("status", "")
    sql = """SELECT i.*, co.name AS company_name, u.name AS created_by_name FROM invoices i
              LEFT JOIN companies co ON co.id = i.company_id
              LEFT JOIN users u ON u.id = i.created_by WHERE 1=1"""
    args = []
    if status:
        sql += " AND i.status = ?"
        args.append(status)
    sql += " ORDER BY i.invoice_date DESC, i.id DESC"
    return db.query(sql, args), status


@bp.route("/")
@login_required
def index():
    invoices, status = _filtered_invoices()
    return render_template("invoices/list.html", invoices=invoices, statuses=STATUSES, current_status=status)


@bp.route("/export")
@admin_required
def export():
    invoices, _status = _filtered_invoices()
    rows = (
        (i["invoice_no"], i["invoice_date"], i["due_date"] or "", i["bill_to_name"], i["company_name"] or "",
         i["status"], i["currency"], i["subtotal"], i["sst_amount"], i["total"], i["created_by_name"] or "")
        for i in invoices
    )
    return csv_response(
        "invoices.csv",
        ["Invoice No", "Invoice Date", "Due Date", "Bill To", "Company", "Status", "Currency",
         "Subtotal", "SST Amount", "Total", "Created By"],
        rows,
    )


@bp.route("/new", methods=("GET", "POST"))
@login_required
def new():
    companies = db.query("SELECT * FROM companies ORDER BY name")
    open_enrollments = db.query(
        """SELECT e.id, e.participant_name, e.amount, e.company_id, c.title AS course_title
           FROM enrollments e
           JOIN course_sessions cs ON cs.id = e.session_id
           JOIN courses c ON c.id = cs.course_id
           WHERE e.status != 'Cancelled'
           ORDER BY e.created_at DESC"""
    )
    # Optional "from a Class" prefill — lets an invoice be started straight
    # from a class's own page (Description/Date/Venue/Client auto-pulled)
    # instead of always typed in manually. Purely a convenience: nothing
    # here is a hard link, so a class can still be deleted/changed later
    # without affecting an invoice already created from it.
    classes_for_invoice = db.query(
        """SELECT cs.id, cs.start_date, cs.end_date, cs.venue, cs.client_company_id, cs.training_type,
                  c.title AS course_title,
                  CASE WHEN cs.training_type = 'Public Training' THEN c.price_public ELSE c.price_inhouse END
                      AS course_price,
                  cl.name AS client_name
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN companies cl ON cl.id = cs.client_company_id
           WHERE cs.status != 'Cancelled'
           ORDER BY cs.start_date DESC LIMIT 200"""
    )
    preselect_session_id = request.args.get("session_id", type=int)

    if request.method == "POST":
        company_id = request.form.get("company_id") or None
        bill_to_name = request.form.get("bill_to_name", "").strip()
        descriptions = request.form.getlist("item_description")
        quantities = request.form.getlist("item_quantity")
        prices = request.form.getlist("item_unit_price")
        enrollment_ids = request.form.getlist("item_enrollment_id")
        durations = request.form.getlist("item_duration")
        venues = request.form.getlist("item_venue")
        dates_ = request.form.getlist("item_date")
        dates_end = request.form.getlist("item_date_end")

        if not bill_to_name:
            flash("Bill-to name is required.", "danger")
        elif not any(d.strip() for d in descriptions):
            flash("Add at least one invoice line item.", "danger")
        else:
            subtotal = 0.0
            items = []
            for desc, qty, price, eid, duration, venue, item_date, item_date_end in zip(
                descriptions, quantities, prices, enrollment_ids, durations, venues, dates_, dates_end
            ):
                if not desc.strip():
                    continue
                qty_f = float(qty or 1)
                price_f = float(price or 0)
                # Duration is now a number of days, not a free-text field —
                # Amount is Unit Price x Duration only (No. of Pax is a
                # headcount for the record, not part of the money math).
                duration_f = float(duration or 1) or 1
                amount = round(duration_f * price_f, 2)
                subtotal += amount
                # An end date only makes sense if it's a distinct, later day
                # than the start date — same-day/blank end dates are ignored.
                date_end = item_date_end or None
                if not item_date or not date_end or date_end <= item_date:
                    date_end = None
                duration_display = f"{duration_f:g} day(s)"
                items.append((desc.strip(), qty_f, price_f, amount, eid or None,
                              duration_display, venue.strip() or None,
                              item_date or None, date_end))

            sst_rate = float(request.form.get("sst_rate") or 0)
            sst_inclusive = 1 if request.form.get("sst_inclusive") else 0
            if sst_inclusive and sst_rate:
                # The typed unit prices/amounts already include SST (e.g. a
                # client-quoted "RM21,000 all-in" price) — the total stays
                # exactly what was entered, and subtotal/SST are backed out
                # of it instead of SST being added on top.
                total = round(subtotal, 2)
                subtotal = round(total / (1 + sst_rate / 100), 2)
                sst_amount = round(total - subtotal, 2)
            else:
                sst_amount = round(subtotal * sst_rate / 100, 2)
                total = round(subtotal + sst_amount, 2)

            invoice_no = _next_invoice_no()
            invoice_date_value = request.form.get("invoice_date") or date.today().isoformat()
            # Due date is no longer a manual field — always 30 days after the
            # invoice date, computed here rather than left for staff to set.
            try:
                due_date_value = (date.fromisoformat(invoice_date_value) + timedelta(days=30)).isoformat()
            except ValueError:
                due_date_value = (date.today() + timedelta(days=30)).isoformat()
            invoice_id = db.execute(
                """INSERT INTO invoices (invoice_no, company_id, bill_to_name, bill_to_address,
                       project_title, employer, grant_id, sst_reg_no, buyer_tin, invoice_date, due_date,
                       currency, subtotal, sst_rate, sst_inclusive, sst_amount, total, status, notes, created_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    invoice_no,
                    company_id,
                    bill_to_name,
                    request.form.get("bill_to_address") or None,
                    request.form.get("project_title") or None,
                    request.form.get("employer") or None,
                    request.form.get("grant_id") or None,
                    request.form.get("sst_reg_no") or None,
                    request.form.get("buyer_tin") or None,
                    invoice_date_value,
                    due_date_value,
                    request.form.get("currency") or "RM",
                    subtotal,
                    sst_rate,
                    sst_inclusive,
                    sst_amount,
                    total,
                    request.form.get("status") or "Draft",
                    request.form.get("notes") or None,
                    g.user["id"],
                ),
            )
            for desc, qty_f, price_f, amount, eid, duration, venue, item_date, date_end in items:
                db.execute(
                    """INSERT INTO invoice_items (invoice_id, enrollment_id, description, quantity,
                           unit_price, amount, duration, venue, item_date, item_date_end)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (invoice_id, eid, desc, qty_f, price_f, amount, duration, venue, item_date, date_end),
                )
            activity.log("create", "invoice", invoice_id, f"Created invoice {invoice_no}")
            flash("Invoice created.", "success")
            return redirect(url_for("invoices.view", invoice_id=invoice_id))

    return render_template("invoices/form.html", invoice=None, items=[], companies=companies,
                            open_enrollments=open_enrollments, statuses=STATUSES,
                            classes_for_invoice=classes_for_invoice, preselect_session_id=preselect_session_id,
                            next_invoice_no=_next_invoice_no(), today=date.today().isoformat())


@bp.route("/<int:invoice_id>")
@login_required
def view(invoice_id):
    invoice = db.query(
        """SELECT i.*, co.name AS company_name, co.email AS company_email, u.name AS created_by_name
           FROM invoices i
           LEFT JOIN companies co ON co.id = i.company_id
           LEFT JOIN users u ON u.id = i.created_by WHERE i.id = ?""",
        (invoice_id,), one=True,
    )
    if invoice is None:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.index"))
    items = db.query("SELECT * FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
    return render_template("invoices/view.html", invoice=invoice, items=items, statuses=STATUSES,
                            mail_configured=mailer.is_configured(),
                            default_email_subject=_default_invoice_email_subject(invoice),
                            default_email_body=_default_invoice_email_body(invoice))


@bp.route("/<int:invoice_id>/send-email", methods=("POST",))
@login_required
def send_email(invoice_id):
    invoice = db.query(
        """SELECT i.*, co.email AS company_email FROM invoices i
           LEFT JOIN companies co ON co.id = i.company_id WHERE i.id = ?""",
        (invoice_id,), one=True,
    )
    if invoice is None:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.index"))

    to_email = (request.form.get("to_email") or invoice["company_email"] or "").strip()
    if not to_email:
        flash("No client email on file for this invoice — add one, or type an address to send to.", "danger")
        return redirect(url_for("invoices.view", invoice_id=invoice_id))

    subject = (request.form.get("subject") or "").strip() or _default_invoice_email_subject(invoice)
    body = (request.form.get("body") or "").strip() or _default_invoice_email_body(invoice)
    cc_email = (request.form.get("cc_email") or "").strip() or None

    items = db.query("SELECT * FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
    try:
        from . import pdfgen
        pdf_bytes = pdfgen.generate_invoice_pdf(invoice, items)
        attachments = [(f"{invoice['invoice_no']}.pdf", pdf_bytes, "application/pdf")]
        mailer.send_email(to_email, subject, body, attachments=attachments,
                           related_type="invoice", related_id=invoice_id, cc_email=cc_email)
    except mailer.MailNotConfigured as exc:
        flash(str(exc), "danger")
        return redirect(url_for("invoices.view", invoice_id=invoice_id))
    except mailer.MailSendError as exc:
        flash(f"Email failed to send: {exc}", "danger")
        return redirect(url_for("invoices.view", invoice_id=invoice_id))
    except Exception:  # noqa: BLE001 - surface a clean message rather than a 500
        current_app.logger.exception("Failed to generate/send invoice PDF for %s", invoice["invoice_no"])
        flash("Couldn't generate the invoice PDF.", "danger")
        return redirect(url_for("invoices.view", invoice_id=invoice_id))

    if invoice["status"] == "Draft":
        db.execute("UPDATE invoices SET status = 'Sent', sent_at = datetime('now'), sent_to_email = ? WHERE id = ?",
                   (to_email, invoice_id))
    else:
        db.execute("UPDATE invoices SET sent_at = datetime('now'), sent_to_email = ? WHERE id = ?",
                   (to_email, invoice_id))
    activity.log("send_email", "invoice", invoice_id, f"Emailed invoice {invoice['invoice_no']} to {to_email}")
    flash(f"Invoice emailed to {to_email}.", "success")
    return redirect(url_for("invoices.view", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/download")
@login_required
def download(invoice_id):
    invoice = db.query(
        """SELECT i.*, co.name AS company_name FROM invoices i
           LEFT JOIN companies co ON co.id = i.company_id WHERE i.id = ?""",
        (invoice_id,), one=True,
    )
    if invoice is None:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.index"))
    items = db.query("SELECT * FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
    try:
        from . import pdfgen
        pdf_bytes = pdfgen.generate_invoice_pdf(invoice, items)
    except Exception:  # noqa: BLE001 - surface a clean message rather than a 500
        current_app.logger.exception("Failed to generate invoice PDF for %s", invoice["invoice_no"])
        flash("Couldn't generate the PDF — is wkhtmltopdf installed on the server?", "danger")
        return redirect(url_for("invoices.view", invoice_id=invoice_id))
    return Response(
        pdf_bytes, mimetype="application/pdf",
        headers={"Content-Disposition": content_disposition(f"{invoice['invoice_no']}.pdf")},
    )


@bp.route("/<int:invoice_id>/status", methods=("POST",))
@login_required
def update_status(invoice_id):
    status = request.form.get("status")
    if status in STATUSES:
        db.execute("UPDATE invoices SET status = ? WHERE id = ?", (status, invoice_id))
        invoice = db.query("SELECT invoice_no FROM invoices WHERE id = ?", (invoice_id,), one=True)
        activity.log("update", "invoice", invoice_id,
                      f"Marked invoice {invoice['invoice_no'] if invoice else invoice_id} as {status}")
        flash(f"Invoice marked as {status}.", "success")
    return redirect(url_for("invoices.view", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/delete", methods=("POST",))
@login_required
def delete(invoice_id):
    invoice = db.query("SELECT invoice_no FROM invoices WHERE id = ?", (invoice_id,), one=True)
    db.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    activity.log("delete", "invoice", invoice_id,
                  f"Deleted invoice {invoice['invoice_no'] if invoice else invoice_id}")
    flash("Invoice deleted.", "success")
    return redirect(url_for("invoices.index"))
