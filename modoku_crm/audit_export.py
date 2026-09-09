"""Audit Export (/audit-export, admin-only) — once a year, Erik compiles
every Purchase Order, Quotation, Invoice, payment receipt, staff Claim,
Petty Cash Voucher and trainer/vendor-submitted invoice into one package
for the external accountant/auditor. This module builds that package on
demand: a single zip, organized into one numbered folder per document
type, plus a plain-text summary and a CSV rollup with per-category totals
and a grand total.

Only FINALIZED records count toward the totals — Confirmed POs, Accepted
quotations, Paid invoices, Paid claims, Approved petty cash vouchers.
Anything still Draft/Pending or Cancelled/Rejected is left out, since the
point of this pack is "what actually happened financially", not
everything that was ever drafted.

Quotations aren't actual money received (a client agreeing to a quote
isn't the same as being paid), so their total is shown for reference only
and excluded from Income/Expenses. Payment-receipt files and
trainer/vendor-submitted invoice documents are supporting evidence for
amounts already counted under Purchase Orders — they're included as files
but never given their own dollar total, to avoid double-counting.
"""
import csv
import io
import os
import zipfile
from datetime import date

from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from . import activity, db
from .auth import admin_required, login_required
from .claims import _receipt_dir as _claim_receipt_dir
from .claims import _upload_dir as _claim_upload_dir
from .docutil import content_disposition
from .petty_cash import _receipt_dir as _petty_cash_receipt_dir
from .purchase_orders import _payment_receipt_upload_dir as _trainer_po_receipt_dir
from .purchase_orders import _po_upload_dir as _trainer_po_upload_dir
from .quotations import _document_title as _quotation_title
from .quotations import _totals as _quotation_totals
from .trainer_invoice import _upload_dir as _trainer_invoice_upload_dir
from .vendor_invoice import _upload_dir as _vendor_invoice_upload_dir
from .vendor_purchase_orders import _payment_receipt_upload_dir as _vendor_po_receipt_dir
from .vendor_purchase_orders import _po_upload_dir as _vendor_po_upload_dir

bp = Blueprint("audit_export", __name__, url_prefix="/audit-export")


def _available_years():
    """Every year that has at least one finalized record in any category,
    plus the current year (so the page always has something to select even
    for a brand-new install)."""
    queries = [
        "SELECT DISTINCT substr(issue_date,1,4) AS yr FROM purchase_orders WHERE issue_date IS NOT NULL",
        "SELECT DISTINCT substr(issue_date,1,4) AS yr FROM vendor_purchase_orders WHERE issue_date IS NOT NULL",
        "SELECT DISTINCT substr(quote_date,1,4) AS yr FROM quotations WHERE quote_date IS NOT NULL",
        "SELECT DISTINCT substr(invoice_date,1,4) AS yr FROM invoices WHERE invoice_date IS NOT NULL",
        "SELECT DISTINCT substr(paid_at,1,4) AS yr FROM staff_claims WHERE paid_at IS NOT NULL",
        "SELECT DISTINCT substr(voucher_date,1,4) AS yr FROM petty_cash_vouchers WHERE voucher_date IS NOT NULL",
    ]
    years = set()
    for sql in queries:
        for row in db.query(sql):
            if row["yr"] and row["yr"].isdigit():
                years.add(int(row["yr"]))
    years.add(date.today().year)
    return sorted(years, reverse=True)


def _resolve_range(args):
    """Either an explicit start_date/end_date override, or Jan 1 - Dec 31
    of the selected (or current) year. Returns (start, end, year_label)."""
    start = (args.get("start_date") or "").strip()
    end = (args.get("end_date") or "").strip()
    year = args.get("year", type=int) or date.today().year
    if start and end:
        return start, end, f"{start}_to_{end}"
    return f"{year}-01-01", f"{year}-12-31", str(year)


def _gather(start, end):
    """Every finalized record in [start, end] (inclusive), grouped by
    category. Each PO row carries its own grand_total (fee_amount + line
    items) via a correlated subquery, matching how each module's own PDF
    download route computes it."""
    # Same column shape as purchase_orders.download()'s own query (trainer
    # name/email/phone, class title/dates/venue, optional authoriser
    # signature block) — pdfgen.generate_po_pdf expects exactly these
    # fields to exist as keys, so we mirror that query rather than a
    # slimmer one, plus a correlated subquery for the PO's line-item total.
    trainer_pos = db.query(
        """SELECT po.*, t.name AS trainer_name, t.email AS trainer_email, t.phone AS trainer_phone,
                  c.title AS course_title, cs.start_date, cs.end_date, cs.venue,
                  u.name AS authoriser_name, u.position AS authoriser_position,
                  u.signature_file AS authoriser_signature,
                  (SELECT COALESCE(SUM(amount), 0) FROM po_items WHERE po_id = po.id) AS items_total
           FROM purchase_orders po
           JOIN trainers t ON t.id = po.trainer_id
           JOIN course_sessions cs ON cs.id = po.session_id
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN users u ON u.id = po.created_by
           WHERE po.status = 'Confirmed' AND po.issue_date BETWEEN ? AND ?
           ORDER BY po.issue_date""",
        (start, end),
    )
    # Same column shape as vendor_purchase_orders._get_po_full() — class
    # link is optional for a vendor PO, hence the LEFT JOINs.
    vendor_pos = db.query(
        """SELECT vpo.*, v.name AS vendor_name, v.contact_email AS vendor_email, v.contact_phone AS vendor_phone,
                  c.title AS course_title, cs.start_date, cs.end_date, cs.venue,
                  (SELECT COALESCE(SUM(amount), 0) FROM vendor_po_items WHERE po_id = vpo.id) AS items_total
           FROM vendor_purchase_orders vpo
           JOIN vendors v ON v.id = vpo.vendor_id
           LEFT JOIN course_sessions cs ON cs.id = vpo.session_id
           LEFT JOIN courses c ON c.id = cs.course_id
           WHERE vpo.status = 'Confirmed' AND vpo.issue_date BETWEEN ? AND ?
           ORDER BY vpo.issue_date""",
        (start, end),
    )
    quotes = db.query(
        """SELECT q.*, co.name AS client_company_name
           FROM quotations q LEFT JOIN companies co ON co.id = q.client_company_id
           WHERE q.status = 'Accepted' AND q.quote_date BETWEEN ? AND ?
           ORDER BY q.quote_date""",
        (start, end),
    )
    invoices = db.query(
        """SELECT i.*, co.name AS company_name FROM invoices i
           LEFT JOIN companies co ON co.id = i.company_id
           WHERE i.status = 'Paid' AND i.invoice_date BETWEEN ? AND ?
           ORDER BY i.invoice_date""",
        (start, end),
    )
    claims = db.query(
        """SELECT sc.*, c.title AS course_title FROM staff_claims sc
           JOIN course_sessions cs ON cs.id = sc.session_id
           JOIN courses c ON c.id = cs.course_id
           WHERE sc.status = 'Paid' AND sc.paid_at IS NOT NULL AND date(sc.paid_at) BETWEEN ? AND ?
           ORDER BY sc.paid_at""",
        (start, end),
    )
    vouchers = db.query(
        """SELECT * FROM petty_cash_vouchers
           WHERE status = 'Approved' AND voucher_date BETWEEN ? AND ?
           ORDER BY voucher_date""",
        (start, end),
    )
    return {
        "trainer_pos": trainer_pos, "vendor_pos": vendor_pos, "quotes": quotes,
        "invoices": invoices, "claims": claims, "vouchers": vouchers,
    }


def _quote_grand_total(q):
    items = db.query("SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY id", (q["id"],))
    _, _, grand_total = _quotation_totals(items, q["sst_rate"], q["sst_inclusive"])
    return grand_total


def _claim_amount(c):
    return c["approved_amount"] if c["approved_amount"] is not None else c["total_amount"]


def _summarize(data):
    trainer_po_total = round(sum(po["fee_amount"] + po["items_total"] for po in data["trainer_pos"]), 2)
    vendor_po_total = round(sum(po["fee_amount"] + po["items_total"] for po in data["vendor_pos"]), 2)
    quote_total = round(sum(_quote_grand_total(q) for q in data["quotes"]), 2)
    invoice_total = round(sum(i["total"] for i in data["invoices"]), 2)
    claim_total = round(sum(_claim_amount(c) for c in data["claims"]), 2)
    voucher_total = round(sum(v["amount"] for v in data["vouchers"]), 2)

    total_expenses = round(trainer_po_total + vendor_po_total + claim_total + voucher_total, 2)
    total_income = invoice_total
    net = round(total_income - total_expenses, 2)
    grand_total_all = round(
        trainer_po_total + vendor_po_total + quote_total + invoice_total + claim_total + voucher_total, 2
    )
    return {
        "trainer_po_count": len(data["trainer_pos"]), "trainer_po_total": trainer_po_total,
        "vendor_po_count": len(data["vendor_pos"]), "vendor_po_total": vendor_po_total,
        "quote_count": len(data["quotes"]), "quote_total": quote_total,
        "invoice_count": len(data["invoices"]), "invoice_total": invoice_total,
        "claim_count": len(data["claims"]), "claim_total": claim_total,
        "voucher_count": len(data["vouchers"]), "voucher_total": voucher_total,
        "total_income": total_income, "total_expenses": total_expenses, "net": net,
        "grand_total_all": grand_total_all,
    }


@bp.route("/")
@login_required
@admin_required
def index():
    start, end, year_label = _resolve_range(request.args)
    data = _gather(start, end)
    summary = _summarize(data)
    return render_template(
        "audit_export/index.html", years=_available_years(),
        selected_year=request.args.get("year", type=int) or date.today().year,
        start_date=request.args.get("start_date", ""), end_date=request.args.get("end_date", ""),
        range_start=start, range_end=end, year_label=year_label, summary=summary, data=data,
    )


def _zip_write(zf, arcname, path, missing):
    if path and os.path.isfile(path):
        zf.write(path, arcname)
    elif path:
        missing.append(arcname)


def _zip_add_dir(zf, source_dir, arc_prefix, missing):
    if not source_dir or not os.path.isdir(source_dir):
        return 0
    added = 0
    for fname in sorted(os.listdir(source_dir)):
        fpath = os.path.join(source_dir, fname)
        if os.path.isfile(fpath):
            zf.write(fpath, f"{arc_prefix}/{fname}")
            added += 1
    return added


def _build_summary_text(start, end, year_label, summary):
    lines = [
        "MODOKU TECH SDN BHD (1390352-H) — AUDIT EXPORT",
        f"Period: {start} to {end}",
        f"Generated: {date.today().isoformat()}",
        "",
        "Only finalized records are counted below — Confirmed Purchase Orders, Accepted",
        "Quotations, Paid Invoices, Paid Claims, and Approved Petty Cash Vouchers. Draft,",
        "Pending, Cancelled and Rejected records are excluded.",
        "",
        "=" * 60,
        "CATEGORY TOTALS",
        "=" * 60,
        f"01. Purchase Orders — Trainer (Confirmed): RM {summary['trainer_po_total']:,.2f}  ({summary['trainer_po_count']} PO(s))",
        f"01. Purchase Orders — Vendor  (Confirmed): RM {summary['vendor_po_total']:,.2f}  ({summary['vendor_po_count']} PO(s))",
        f"02. Quotations (Accepted) [reference only]: RM {summary['quote_total']:,.2f}  ({summary['quote_count']} quotation(s))",
        f"03. Invoices (Paid):                        RM {summary['invoice_total']:,.2f}  ({summary['invoice_count']} invoice(s))",
        f"05. Claims (Paid):                           RM {summary['claim_total']:,.2f}  ({summary['claim_count']} claim(s))",
        f"06. Petty Cash Vouchers (Approved):           RM {summary['voucher_total']:,.2f}  ({summary['voucher_count']} voucher(s))",
        "-" * 60,
        f"GRAND TOTAL (all categories above, incl. Quotations): RM {summary['grand_total_all']:,.2f}",
        "",
        "Note: the Grand Total above simply adds every category together as a quick",
        "reference figure. It mixes income (Invoices) with expenses (Purchase Orders,",
        "Claims, Petty Cash) and a non-cash reference figure (Quotations) — it is NOT a",
        "profit/net-revenue figure. See the breakdown below for that.",
        "",
        "=" * 60,
        "INCOME vs. EXPENSES",
        "=" * 60,
        f"Total Income  (Invoices, Paid):                          RM {summary['total_income']:,.2f}",
        f"Total Expenses (Purchase Orders + Claims + Petty Cash):  RM {summary['total_expenses']:,.2f}",
        f"Net (Income - Expenses):                                 RM {summary['net']:,.2f}",
        "",
        "=" * 60,
        "SUPPORTING DOCUMENTS (no separate dollar total — amounts already counted",
        "under Purchase Orders above)",
        "=" * 60,
        "04. Payment Receipts — proof Modoku paid the trainer/vendor for each",
        "    Confirmed PO in this period (see folder 04_Receipts).",
        "07. Trainer/Vendor Invoice Documents — the invoice/claim files the",
        "    trainer or vendor themselves submitted for each Confirmed PO in this",
        "    period (see folder 07_Trainer_Vendor_Invoices).",
        "",
        "Folder guide:",
        "  01_Purchase_Orders       — Trainer & Vendor PO PDFs + any staff-uploaded documents",
        "  02_Quotations            — Accepted quotation PDFs",
        "  03_Invoices              — Paid client invoice PDFs",
        "  04_Receipts              — Payment receipt files (proof of payment to trainer/vendor)",
        "  05_Claims                — Paid staff claim files and payment receipts",
        "  06_Petty_Cash_Vouchers   — Approved voucher PDFs + any attached receipt",
        "  07_Trainer_Vendor_Invoices — Invoice documents submitted by trainers/vendors",
    ]
    return "\n".join(lines) + "\n"


def _build_summary_csv(data):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Category", "Reference No.", "Date", "Party", "Amount (RM)", "Status", "Notes"])
    for po in data["trainer_pos"]:
        writer.writerow(["Purchase Order (Trainer)", po["po_no"], po["issue_date"], po["trainer_name"],
                          f"{po['fee_amount'] + po['items_total']:.2f}", po["status"], po["course_title"]])
    for po in data["vendor_pos"]:
        writer.writerow(["Purchase Order (Vendor)", po["po_no"], po["issue_date"], po["vendor_name"],
                          f"{po['fee_amount'] + po['items_total']:.2f}", po["status"], po["description"] or ""])
    for q in data["quotes"]:
        writer.writerow(["Quotation (reference only)", q["quote_no"], q["quote_date"],
                          q["client_company_name"] or q["company_name_override"] or "",
                          f"{_quote_grand_total(q):.2f}", q["status"], q["course_title"] or ""])
    for i in data["invoices"]:
        writer.writerow(["Invoice", i["invoice_no"], i["invoice_date"], i["company_name"] or i["bill_to_name"],
                          f"{i['total']:.2f}", i["status"], ""])
    for c in data["claims"]:
        writer.writerow(["Claim", f"#{c['id']}", c["paid_at"], c["claimant_name"],
                          f"{_claim_amount(c):.2f}", c["status"], c["course_title"]])
    for v in data["vouchers"]:
        writer.writerow(["Petty Cash Voucher", v["voucher_no"], v["voucher_date"], v["payee_name"],
                          f"{v['amount']:.2f}", v["status"], v["purpose"]])
    return "﻿" + buf.getvalue()


@bp.route("/download")
@login_required
@admin_required
def download():
    start, end, year_label = _resolve_range(request.args)
    data = _gather(start, end)
    summary = _summarize(data)
    missing = []
    root = f"Audit Export FY{year_label}"

    from . import pdfgen

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 01 — Purchase Orders
        for po in data["trainer_pos"]:
            items = db.query("SELECT * FROM po_items WHERE po_id = ? ORDER BY id", (po["id"],))
            grand_total = round(po["fee_amount"] + po["items_total"], 2)
            try:
                pdf_bytes = pdfgen.generate_po_pdf(po, items, grand_total)
                zf.writestr(f"{root}/01_Purchase_Orders/Trainer/{po['po_no']}.pdf", pdf_bytes)
            except Exception:  # noqa: BLE001 - one bad PDF shouldn't sink the whole export
                current_app.logger.exception("Audit export: failed to build PDF for trainer PO %s", po["po_no"])
                missing.append(f"01_Purchase_Orders/Trainer/{po['po_no']}.pdf (PDF generation failed)")
            _zip_add_dir(zf, _trainer_po_upload_dir(po["id"]),
                         f"{root}/01_Purchase_Orders/Trainer/{po['po_no']}_documents", missing)

        for po in data["vendor_pos"]:
            items = db.query("SELECT * FROM vendor_po_items WHERE po_id = ? ORDER BY id", (po["id"],))
            grand_total = round(po["fee_amount"] + po["items_total"], 2)
            try:
                pdf_bytes = pdfgen.generate_vendor_po_pdf(po, items, grand_total)
                zf.writestr(f"{root}/01_Purchase_Orders/Vendor/{po['po_no']}.pdf", pdf_bytes)
            except Exception:  # noqa: BLE001
                current_app.logger.exception("Audit export: failed to build PDF for vendor PO %s", po["po_no"])
                missing.append(f"01_Purchase_Orders/Vendor/{po['po_no']}.pdf (PDF generation failed)")
            _zip_add_dir(zf, _vendor_po_upload_dir(po["id"]),
                         f"{root}/01_Purchase_Orders/Vendor/{po['po_no']}_documents", missing)

        # 02 — Quotations
        for q in data["quotes"]:
            items = db.query("SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY id", (q["id"],))
            subtotal, _, _ = _quotation_totals(items, q["sst_rate"], q["sst_inclusive"])
            title = _quotation_title({**dict(q), "client_company_name": q["client_company_name"]})
            try:
                pdf_bytes = pdfgen.generate_quotation_pdf(q, items, subtotal, title)
                zf.writestr(f"{root}/02_Quotations/{q['quote_no']}.pdf", pdf_bytes)
            except Exception:  # noqa: BLE001
                current_app.logger.exception("Audit export: failed to build PDF for quotation %s", q["quote_no"])
                missing.append(f"02_Quotations/{q['quote_no']}.pdf (PDF generation failed)")

        # 03 — Invoices
        for i in data["invoices"]:
            items = db.query("SELECT * FROM invoice_items WHERE invoice_id = ?", (i["id"],))
            try:
                pdf_bytes = pdfgen.generate_invoice_pdf(i, items)
                zf.writestr(f"{root}/03_Invoices/{i['invoice_no']}.pdf", pdf_bytes)
            except Exception:  # noqa: BLE001
                current_app.logger.exception("Audit export: failed to build PDF for invoice %s", i["invoice_no"])
                missing.append(f"03_Invoices/{i['invoice_no']}.pdf (PDF generation failed)")

        # 04 — Payment Receipts (proof of payment for the same Confirmed POs above)
        for po in data["trainer_pos"]:
            _zip_add_dir(zf, _trainer_po_receipt_dir(po["id"]),
                         f"{root}/04_Receipts/Trainer_PO/{po['po_no']}", missing)
        for po in data["vendor_pos"]:
            _zip_add_dir(zf, _vendor_po_receipt_dir(po["id"]),
                         f"{root}/04_Receipts/Vendor_PO/{po['po_no']}", missing)

        # 05 — Claims (uploaded files + payment receipts, no separate PDF exists for a claim)
        for c in data["claims"]:
            claim_folder = f"{c['id']}_{c['claimant_name']}".replace("/", "-")
            _zip_add_dir(zf, _claim_upload_dir(c["id"]), f"{root}/05_Claims/{claim_folder}/files", missing)
            _zip_add_dir(zf, _claim_receipt_dir(c["id"]), f"{root}/05_Claims/{claim_folder}/payment_receipts", missing)

        # 06 — Petty Cash Vouchers
        for v in data["vouchers"]:
            try:
                voucher_row = db.query(
                    """SELECT pcv.*, c.title AS course_title, cs.start_date, cs.end_date, cs.venue,
                              req.name AS requested_by_name, app.name AS approved_by_name
                       FROM petty_cash_vouchers pcv
                       LEFT JOIN course_sessions cs ON cs.id = pcv.session_id
                       LEFT JOIN courses c ON c.id = cs.course_id
                       LEFT JOIN users req ON req.id = pcv.requested_by
                       LEFT JOIN users app ON app.id = pcv.approved_by
                       WHERE pcv.id = ?""",
                    (v["id"],), one=True,
                )
                pdf_bytes = pdfgen.generate_petty_cash_pdf(voucher_row)
                zf.writestr(f"{root}/06_Petty_Cash_Vouchers/{v['voucher_no']}.pdf", pdf_bytes)
            except Exception:  # noqa: BLE001
                current_app.logger.exception("Audit export: failed to build PDF for voucher %s", v["voucher_no"])
                missing.append(f"06_Petty_Cash_Vouchers/{v['voucher_no']}.pdf (PDF generation failed)")
            if v["receipt_filename"]:
                # receipt_original_name is the client-supplied upload filename,
                # stored as-is (see petty_cash._handle_receipt_upload) — unlike
                # every other category above, which names its zip entries from
                # the safe uuid-prefixed on-disk filename via _zip_add_dir, this
                # is the one spot that builds a zip arcname from a raw
                # user-supplied string, so it has to be sanitized here rather
                # than trusted (a filename like "../../evil.pdf" would
                # otherwise let an uploaded receipt's name escape this
                # voucher's folder in the exported zip).
                safe_receipt_name = secure_filename(v["receipt_original_name"] or v["receipt_filename"])
                _zip_write(
                    zf, f"{root}/06_Petty_Cash_Vouchers/{v['voucher_no']}_receipt_{safe_receipt_name}",
                    os.path.join(_petty_cash_receipt_dir(v["id"]), v["receipt_filename"]), missing,
                )

        # 07 — Trainer/Vendor Invoice Documents (submitted for the same Confirmed POs above)
        for po in data["trainer_pos"]:
            _zip_add_dir(zf, _trainer_invoice_upload_dir(po["session_id"]),
                         f"{root}/07_Trainer_Vendor_Invoices/Trainer/{po['po_no']}", missing)
        for po in data["vendor_pos"]:
            _zip_add_dir(zf, _vendor_invoice_upload_dir(po["id"]),
                         f"{root}/07_Trainer_Vendor_Invoices/Vendor/{po['po_no']}", missing)

        # Summary — plain text + CSV rollup at the root of the pack
        summary_text = _build_summary_text(start, end, year_label, summary)
        if missing:
            summary_text += (
                "\n" + "=" * 60 + "\nFILES REFERENCED BUT NOT FOUND ON DISK (skipped)\n" + "=" * 60 + "\n"
                + "\n".join(missing) + "\n"
            )
        zf.writestr(f"{root}/Audit_Summary.txt", summary_text)
        zf.writestr(f"{root}/Audit_Summary.csv", _build_summary_csv(data))

    activity.log("export", "audit_export", None, f"Generated Audit Export for {start} to {end}")
    return Response(
        buf.getvalue(), mimetype="application/zip",
        headers={"Content-Disposition": content_disposition(f"{root}.zip")},
    )
