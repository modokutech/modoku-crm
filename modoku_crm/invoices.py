import json
import re
from datetime import date, timedelta

from flask import Blueprint, Response, current_app, flash, g, redirect, render_template, request, url_for

from . import activity, db, fmtdate, mailer, notifications, parse_money
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
            body="Payment is past its due date, chase the client for payment.",
            link=url_for("invoices.view", invoice_id=row["id"]),
            dedupe_key=f"invoice:{row['id']}:overdue",
        )


@bp.before_app_request
def _sync_invoice_statuses():
    try:
        _auto_mark_overdue_invoices()
    except Exception:  # noqa: BLE001 - never let this housekeeping break a request
        current_app.logger.exception("Failed to auto-mark overdue invoices")


def _next_invoice_no(consume=True):
    """INV-<yy>-<00001> by default, e.g. INV-26-00297. Prefix/suffix are
    admin-configurable under Settings, as is a one-time 'reset next number
    to' override.

    The <yy> segment is the CURRENT calendar year, two digits. The running
    sequence keeps counting up across the year boundary rather than
    resetting in January, so INV-26-00057 is followed by INV-27-00058, not
    INV-27-00001. The lookup below matches on prefix only, across all
    years, to find that running total.

    Numbers issued before this format change (INV-2026-0297, four-digit
    sequence and a four-digit year) still parse correctly here: the
    sequence is read from the last dash-separated segment either way, so
    counting continues from wherever it had reached rather than restarting.
    Existing invoice records keep the number they were issued with."""
    prefix = settings_module.get_invoice_number_prefix()
    suffix = settings_module.get_invoice_number_suffix()
    year = date.today().strftime("%y")
    # consume=False is for previews (the New Invoice form shows the number it
    # is *about* to use). Consuming there would clear the admin's one-time
    # override before the invoice is ever saved, so the save would fall back
    # to the running sequence — see settings._consume_override.
    override = (settings_module.consume_invoice_number_override() if consume
                else settings_module.peek_invoice_number_override())
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
    return f"{prefix}-{year}-{last_seq + 1:05d}{suffix}"


def _invoice_pic(invoice):
    """Fix100: the PIC (leads row: name, email) of the class this invoice is
    for, or None. The class is the one it was created from (session_id);
    older invoices, created before that was stored, are matched on the
    HRDCorp Grant ID when exactly one class carries it."""
    session_id = invoice["session_id"] if "session_id" in invoice.keys() else None
    if not session_id and invoice["grant_id"]:
        matches = db.query("SELECT id FROM course_sessions WHERE hrdcorp_grant_id = ?", (invoice["grant_id"],))
        if len(matches) == 1:
            session_id = matches[0]["id"]
    if not session_id:
        return None
    pic = db.query(
        """SELECT l.name, l.email FROM course_sessions cs JOIN leads l ON l.id = cs.pic_lead_id
           WHERE cs.id = ?""", (session_id,), one=True)
    return pic if pic and pic["email"] else None


def _invoice_pdf_filename(invoice, items):
    """Fix100: INV-26-00297_Hong_Leong_Microsoft_Excel_Basic_modoku_invoice.pdf -
    the invoice number, the first two words of the client (the Employer,
    falling back to the linked company, then the Bill To name), the first
    three words of the course (the Project, falling back to the first line
    item) - same name for the download and the emailed attachment."""
    def words(text, n):
        found = re.findall(r"[A-Za-z0-9]+", text or "")
        return "_".join(found[:n])
    company = invoice["company_name"] if "company_name" in invoice.keys() else None
    client = invoice["employer"] or company or invoice["bill_to_name"]
    course = invoice["project_title"] or (items[0]["description"] if items else "")
    parts = [invoice["invoice_no"], words(client, 2), words(course, 3), "modoku_invoice"]
    return "_".join(p for p in parts if p) + ".pdf"


def _upfront_from_form(form, gross_total):
    """Fix114: the "Less upfront payment" option on New Invoice. Returns
    (upfront_type, upfront_value, upfront_amount, error). The deduction
    comes off the total after SST, and has to leave something to pay."""
    if not form.get("upfront_enabled"):
        return None, None, 0.0, None
    kind = form.get("upfront_type")
    try:
        value = parse_money(form.get("upfront_value"), default=None)
    except ValueError:
        value = None
    if kind not in ("percent", "fixed") or value is None or value <= 0:
        return None, None, 0.0, "Enter the upfront payment as a percentage or an amount above 0."
    amount = round(gross_total * value / 100, 2) if kind == "percent" else round(value, 2)
    if (kind == "percent" and value >= 100) or amount >= gross_total:
        return None, None, 0.0, "The upfront payment has to be less than the invoice total."
    return kind, value, amount, None


def _default_invoice_email_subject(invoice):
    return f"Invoice {invoice['invoice_no']} from Modoku Tech Sdn Bhd"


def _default_invoice_email_body(invoice, pic=None):
    attention = invoice["attention_to"] if "attention_to" in invoice.keys() else None
    greeting_name = attention or (pic["name"] if pic else None) or invoice["bill_to_name"] or "there"
    project_line = f"Project: {invoice['project_title']}\n" if invoice["project_title"] else ""
    return (
        f"Hi {greeting_name},\n\n"
        f"Please find attached invoice {invoice['invoice_no']} for your reference.\n\n"
        f"{project_line}"
        f"Amount due: {invoice['currency']} {invoice['total']:,.2f}\n"
        f"Due date: {fmtdate(invoice['due_date'])}\n\n"
        "We would appreciate payment by the due date. If you have any questions, please feel free to "
        "contact us at hello@modoku.tech.\n\n"
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


def _form_choices():
    """What the New/Edit Invoice form's dropdowns need."""
    companies = db.query("SELECT * FROM companies ORDER BY name")
    open_enrollments = db.query(
        """SELECT e.id, e.participant_name, e.amount, e.company_id, c.title AS course_title
           FROM enrollments e
           JOIN course_sessions cs ON cs.id = e.session_id
           JOIN courses c ON c.id = cs.course_id
           WHERE e.status != 'Cancelled'
           ORDER BY e.created_at DESC"""
    )
    # Optional "from a Class" prefill - lets an invoice be started straight
    # from a class's own page (Description/Date/Venue/Client auto-pulled)
    # instead of always typed in manually.
    classes_for_invoice = db.query(
        """SELECT cs.id, cs.start_date, cs.end_date, cs.venue, cs.client_company_id, cs.training_type,
                  cs.hrdcorp_grant_id,
                  c.title AS course_title,
                  CASE WHEN cs.training_type = 'Public Training' THEN c.price_public ELSE c.price_inhouse END
                      AS course_price,
                  cl.name AS client_name, pic.name AS pic_name
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN companies cl ON cl.id = cs.client_company_id
           LEFT JOIN leads pic ON pic.id = cs.pic_lead_id
           WHERE cs.status != 'Cancelled'
           ORDER BY cs.start_date DESC LIMIT 200"""
    )
    # Fix134: each client's PICs, offered as suggestions in Attention To.
    leads = db.query("SELECT name, company_id FROM leads WHERE name IS NOT NULL AND name != '' ORDER BY name")
    return dict(companies=companies, open_enrollments=open_enrollments,
                classes_for_invoice=classes_for_invoice,
                leads=[{"name": l["name"], "company_id": l["company_id"]} for l in leads])


def _invoice_from_form(form):
    """Fix134: reads the New/Edit Invoice form. Returns (fields, items, error):
    fields is the invoices-table values, items the line items as tuples for
    invoice_items. Totals are always worked out here, never trusted from
    the browser."""
    bill_to_name = form.get("bill_to_name", "").strip()
    descriptions = form.getlist("item_description")
    if not bill_to_name:
        return None, None, "Bill-to name is required."
    if not any(d.strip() for d in descriptions):
        return None, None, "Add at least one invoice line item."
    subtotal = 0.0
    items = []
    for desc, qty, price, eid, duration, venue, item_date, item_date_end in zip(
        descriptions, form.getlist("item_quantity"), form.getlist("item_unit_price"),
        form.getlist("item_enrollment_id"), form.getlist("item_duration"), form.getlist("item_venue"),
        form.getlist("item_date"), form.getlist("item_date_end"),
    ):
        if not desc.strip():
            continue
        qty_f = float(qty or 1)
        price_f = parse_money(price)
        # Duration is a number of days - Amount is Unit Price x Duration only
        # (No. of Pax is a headcount for the record, not part of the money math).
        duration_f = float(duration or 1) or 1
        amount = round(duration_f * price_f, 2)
        subtotal += amount
        # An end date only makes sense if it's a distinct, later day than
        # the start date - same-day/blank end dates are ignored.
        date_end = item_date_end or None
        if not item_date or not date_end or date_end <= item_date:
            date_end = None
        items.append((desc.strip(), qty_f, price_f, amount, eid or None,
                      f"{duration_f:g} day(s)", venue.strip() or None, item_date or None, date_end))

    sst_rate = float(form.get("sst_rate") or 0)
    sst_inclusive = 1 if form.get("sst_inclusive") else 0
    if sst_inclusive and sst_rate:
        # The typed prices already include SST - the total stays exactly
        # what was entered, and subtotal/SST are backed out of it.
        total = round(subtotal, 2)
        subtotal = round(total / (1 + sst_rate / 100), 2)
        sst_amount = round(total - subtotal, 2)
    else:
        sst_amount = round(subtotal * sst_rate / 100, 2)
        total = round(subtotal + sst_amount, 2)

    upfront_type, upfront_value, upfront_amount, upfront_error = _upfront_from_form(form, total)
    if upfront_error:
        return None, None, upfront_error
    total = round(total - upfront_amount, 2)  # Fix114: the balance due

    invoice_date_value = form.get("invoice_date") or date.today().isoformat()
    # Due date is always 30 days after the invoice date.
    try:
        due_date_value = (date.fromisoformat(invoice_date_value) + timedelta(days=30)).isoformat()
    except ValueError:
        due_date_value = (date.today() + timedelta(days=30)).isoformat()
    fields = {
        "company_id": form.get("company_id") or None,
        "bill_to_name": bill_to_name,
        "attention_to": (form.get("attention_to") or "").strip() or None,
        "bill_to_address": form.get("bill_to_address") or None,
        "project_title": form.get("project_title") or None,
        "employer": form.get("employer") or None,
        "grant_id": form.get("grant_id") or None,
        "sst_reg_no": form.get("sst_reg_no") or None,
        "buyer_tin": form.get("buyer_tin") or None,
        "invoice_date": invoice_date_value,
        "due_date": due_date_value,
        "currency": form.get("currency") or "RM",
        "subtotal": subtotal,
        "sst_rate": sst_rate,
        "sst_inclusive": sst_inclusive,
        "sst_amount": sst_amount,
        "total": total,
        "status": form.get("status") or "Draft",
        "notes": form.get("notes") or None,
        "session_id": form.get("session_id", type=int),
        "upfront_type": upfront_type,
        "upfront_value": upfront_value,
        "upfront_amount": upfront_amount,
    }
    return fields, items, None


def _save_items(invoice_id, items):
    for desc, qty_f, price_f, amount, eid, duration, venue, item_date, date_end in items:
        db.execute(
            """INSERT INTO invoice_items (invoice_id, enrollment_id, description, quantity,
                   unit_price, amount, duration, venue, item_date, item_date_end)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (invoice_id, eid, desc, qty_f, price_f, amount, duration, venue, item_date, date_end),
        )


def _class_pic_name(invoice):
    """Fix134: the PIC name an invoice is addressed to - what was typed in
    Attention To, else (older invoices) the linked class's PIC."""
    if "attention_to" in invoice.keys() and invoice["attention_to"]:
        return invoice["attention_to"]
    session_id = invoice["session_id"] if "session_id" in invoice.keys() else None
    if not session_id and invoice["grant_id"]:
        matches = db.query("SELECT id FROM course_sessions WHERE hrdcorp_grant_id = ?", (invoice["grant_id"],))
        if len(matches) == 1:
            session_id = matches[0]["id"]
    if not session_id:
        return None
    row = db.query("""SELECT l.name FROM course_sessions cs JOIN leads l ON l.id = cs.pic_lead_id
                      WHERE cs.id = ?""", (session_id,), one=True)
    return row["name"] if row and row["name"] else None


def _with_attention(invoice):
    """The invoice row as a dict with attention_to filled in (see
    _class_pic_name), for the page and the PDF."""
    d = dict(invoice)
    d["attention_to"] = _class_pic_name(invoice)
    return d


@bp.route("/new", methods=("GET", "POST"))
@login_required
def new():
    preselect_session_id = request.args.get("session_id", type=int)

    def _render_new_form():
        return render_template("invoices/form.html", invoice=None, items=[], statuses=STATUSES,
                                preselect_session_id=preselect_session_id,
                                next_invoice_no=_next_invoice_no(consume=False),
                                today=date.today().isoformat(), **_form_choices())

    if request.method == "POST":
        fields, items, error = _invoice_from_form(request.form)
        if error:
            flash(error, "danger")
            return _render_new_form()
        invoice_no = _next_invoice_no()
        cols = ["invoice_no", *fields.keys(), "created_by"]
        invoice_id = db.execute(
            f"INSERT INTO invoices ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            (invoice_no, *fields.values(), g.user["id"]),
        )
        _save_items(invoice_id, items)
        activity.log("create", "invoice", invoice_id, f"Created invoice {invoice_no}")
        flash("Invoice created.", "success")
        return redirect(url_for("invoices.view", invoice_id=invoice_id))

    return _render_new_form()


@bp.route("/<int:invoice_id>/edit", methods=("GET", "POST"))
@login_required
def edit(invoice_id):
    """Fix134: edit an existing invoice. Same form and maths as New; the
    invoice number, who created it and its email history stay as they are."""
    invoice = db.query("SELECT * FROM invoices WHERE id = ?", (invoice_id,), one=True)
    if invoice is None:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.index"))
    items = db.query("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY id", (invoice_id,))

    if request.method == "POST":
        fields, new_items, error = _invoice_from_form(request.form)
        if not error:
            old_gross = round((invoice["subtotal"] or 0) + (invoice["sst_amount"] or 0), 2)
            db.execute(
                f"UPDATE invoices SET {', '.join(k + '=?' for k in fields)} WHERE id = ?",
                (*fields.values(), invoice_id),
            )
            db.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
            _save_items(invoice_id, new_items)
            new_gross = round(fields["subtotal"] + fields["sst_amount"], 2)
            detail = ""
            if old_gross != new_gross:
                detail = f" - total changed from RM {old_gross:,.2f} to RM {new_gross:,.2f}"
            activity.log("update", "invoice", invoice_id, f"Updated invoice {invoice['invoice_no']}{detail}")
            flash("Invoice updated.", "success")
            if invoice["sent_at"]:
                flash(f"This invoice was already emailed to {invoice['sent_to_email']}. Resend it so the client "
                      "has the updated copy.", "warning")
            return redirect(url_for("invoices.view", invoice_id=invoice_id))
        flash(error, "danger")

    form_invoice = _with_attention(invoice)
    form_items = []
    for it in items:
        d = dict(it)
        m = re.match(r"\s*([\d.]+)", it["duration"] or "")
        d["duration_days"] = float(m.group(1)) if m else 1
        form_items.append(d)
    return render_template("invoices/form.html", invoice=form_invoice, items=form_items, statuses=STATUSES,
                            preselect_session_id=invoice["session_id"], next_invoice_no=invoice["invoice_no"],
                            today=invoice["invoice_date"], **_form_choices())


@bp.route("/<int:invoice_id>")
@login_required
def view(invoice_id):
    invoice = db.query(
        """SELECT i.*, co.name AS company_name, co.email AS company_email,
                  co.address AS company_address, co.city AS company_city,
                  co.postcode AS company_postcode, co.state AS company_state,
                  u.name AS created_by_name
           FROM invoices i
           LEFT JOIN companies co ON co.id = i.company_id
           LEFT JOIN users u ON u.id = i.created_by WHERE i.id = ?""",
        (invoice_id,), one=True,
    )
    if invoice is None:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.index"))
    items = db.query("SELECT * FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
    pic = _invoice_pic(invoice)
    invoice = _with_attention(invoice)
    return render_template("invoices/view.html", invoice=invoice, items=items, statuses=STATUSES,
                            mail_configured=mailer.is_configured(), pic=pic,
                            default_to_email=(pic["email"] if pic else None) or invoice["company_email"] or "",
                            pdf_filename=_invoice_pdf_filename(invoice, items),
                            default_email_subject=_default_invoice_email_subject(invoice),
                            default_email_body=_default_invoice_email_body(invoice, pic))


@bp.route("/<int:invoice_id>/send-email", methods=("POST",))
@login_required
def send_email(invoice_id):
    invoice = db.query(
        """SELECT i.*, co.name AS company_name, co.email AS company_email FROM invoices i
           LEFT JOIN companies co ON co.id = i.company_id WHERE i.id = ?""",
        (invoice_id,), one=True,
    )
    if invoice is None:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.index"))

    pic = _invoice_pic(invoice)
    to_email = (request.form.get("to_email") or (pic["email"] if pic else None)
                or invoice["company_email"] or "").strip()
    if not to_email:
        flash("No client email on file for this invoice. Add one, or type an address to send to.", "danger")
        return redirect(url_for("invoices.view", invoice_id=invoice_id))

    subject = (request.form.get("subject") or "").strip() or _default_invoice_email_subject(invoice)
    body = (request.form.get("body") or "").strip() or _default_invoice_email_body(invoice, pic)
    cc_email = (request.form.get("cc_email") or "").strip() or None

    items = db.query("SELECT * FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
    try:
        from . import pdfgen
        pdf_bytes = pdfgen.generate_invoice_pdf(_with_attention(invoice), items)
        attachments = [(_invoice_pdf_filename(invoice, items), pdf_bytes, "application/pdf")]
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
        """SELECT i.*, co.name AS company_name,
                  co.address AS company_address, co.city AS company_city,
                  co.postcode AS company_postcode, co.state AS company_state
           FROM invoices i
           LEFT JOIN companies co ON co.id = i.company_id WHERE i.id = ?""",
        (invoice_id,), one=True,
    )
    if invoice is None:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.index"))
    items = db.query("SELECT * FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
    try:
        from . import pdfgen
        pdf_bytes = pdfgen.generate_invoice_pdf(_with_attention(invoice), items)
    except Exception:  # noqa: BLE001 - surface a clean message rather than a 500
        current_app.logger.exception("Failed to generate invoice PDF for %s", invoice["invoice_no"])
        flash("Couldn't generate the PDF. Is wkhtmltopdf installed on the server?", "danger")
        return redirect(url_for("invoices.view", invoice_id=invoice_id))
    return Response(
        pdf_bytes, mimetype="application/pdf",
        headers={"Content-Disposition": content_disposition(_invoice_pdf_filename(invoice, items))},
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
