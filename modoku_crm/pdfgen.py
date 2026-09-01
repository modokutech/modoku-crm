"""Renders a Purchase Order as a real PDF (attached to the trainer's PO
email) using wkhtmltopdf, which is available in this environment even
though no Python PDF/HTML-to-PDF library is installed. The HTML is fully
self-contained (logo embedded as a base64 data URI) so rendering needs no
network access.
"""
import base64
import os
import re
import subprocess
import tempfile
from io import BytesIO

from flask import current_app
from markupsafe import escape
from PIL import Image, ImageDraw, ImageFont


# The certificate's signee — a fixed company signatory (not the logged-in
# staff member who marked attendance), matching the reference design.
CERT_SIGNEE_NAME = "Abd Fariq Mohd Tajudin"
CERT_SIGNEE_POSITION = "Director"


def _data_uri(path):
    try:
        with open(path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        ext = path.rsplit(".", 1)[-1].lower()
        mimetype = {"jpg": "jpeg"}.get(ext, ext)
        return f"data:image/{mimetype};base64,{encoded}"
    except OSError:
        return ""


def _logo_data_uri():
    return _data_uri(os.path.join(current_app.root_path, "static", "img", "logo.png"))


def _user_signature_data_uri(signature_file, user_id):
    if not signature_file:
        return ""
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "users", str(user_id), signature_file)
    return _data_uri(path)


def _linelist_html(text, ordered=False):
    """Python equivalent of the `linelist` Jinja filter, for use outside a
    Jinja render (wkhtmltopdf gets raw HTML strings, not a template)."""
    if not text:
        return ""
    raw_lines = text.split("\n") if "\n" in text else re.split(r"<br\s*/?>", text)
    lines = []
    for line in raw_lines:
        line = line.strip()
        line = re.sub(r"^[•\-\*]\s*", "", line)
        line = re.sub(r"^\d+[\.\)]\s*", "", line)
        if line:
            lines.append(line)
    if not lines:
        return ""
    tag = "ol" if ordered else "ul"
    items = "".join(f"<li>{l}</li>" for l in lines)
    return f"<{tag} style='margin:4px 0 0;padding-left:18px'>{items}</{tag}>"


def _fmtdate(value):
    if not value:
        return ""
    from datetime import datetime
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%-d %b %Y")
    except ValueError:
        return value


def _fmtdaterange(start_value, end_value):
    """Renders a PO's class date(s) the same way the rest of the app does
    (see fmtdaterange in __init__.py, used on the Quotations list etc.) —
    a back-to-back 2-day class reads '1 & 2 Oct 2026' rather than the more
    cluttered '1 Oct 2026 – 2 Oct 2026', and a longer span drops the
    repeated month/year on the start date ('1 – 15 Oct 2026')."""
    from datetime import datetime
    if not start_value:
        return ""
    try:
        start = datetime.strptime(start_value, "%Y-%m-%d")
    except ValueError:
        return start_value
    if not end_value or end_value == start_value:
        return _fmtdate(start_value)
    try:
        end = datetime.strptime(end_value, "%Y-%m-%d")
    except ValueError:
        return _fmtdate(start_value)

    same_year = start.year == end.year
    same_month = same_year and start.month == end.month
    num_days = (end - start).days + 1

    if same_month:
        start_part = f"{start.day}"
    elif same_year:
        start_part = start.strftime("%-d %b")
    else:
        start_part = start.strftime("%-d %b %Y")
    end_part = _fmtdate(end_value)

    joiner = " &amp; " if num_days == 2 else " &ndash; "
    return f"{start_part}{joiner}{end_part}"


def _build_html(po, items, grand_total):
    logo_uri = _logo_data_uri()
    dates = _fmtdaterange(po["start_date"], po["end_date"])

    item_row_parts = []
    for item in items:
        qty_note = ""
        if item["quantity"] != 1:
            qty_note = (f" <span style='color:#666'>&times; {item['quantity']} @ "
                        f"{po['currency']} {item['unit_price']:,.2f}</span>")
        item_row_parts.append(
            f"<tr><td colspan='3'>{item['description']}{qty_note}</td>"
            f"<td style='text-align:right'>{po['currency']} {item['amount']:,.2f}</td></tr>"
        )
    item_rows = "".join(item_row_parts)
    items_total_row = ""
    if items:
        items_total_row = (
            f"<tr style='font-weight:700'><td colspan='3' style='text-align:right'>Total</td>"
            f"<td style='text-align:right'>{po['currency']} {grand_total:,.2f}</td></tr>"
        )

    small_style = "font-size:9px;line-height:1.5"
    terms_html = (
        f"<div style='margin-top:16px'><div style='font-size:11px;color:#666;text-transform:uppercase'>"
        f"Terms &amp; Conditions</div><div style='{small_style}'>{_linelist_html(po['terms'])}</div></div>"
    ) if po["terms"] else ""
    resp_html = (
        f"<div style='margin-top:16px'><div style='font-size:11px;color:#666;text-transform:uppercase'>"
        f"Trainer's Responsibilities</div><div style='{small_style}'>{_linelist_html(po['trainer_responsibilities'])}</div></div>"
    ) if po["trainer_responsibilities"] else ""

    signature_uri = ""
    authoriser_name = ""
    authoriser_position = ""
    if "authoriser_name" in po.keys():
        authoriser_name = po["authoriser_name"] or ""
        authoriser_position = po["authoriser_position"] or ""
        if po["authoriser_signature"] and "created_by" in po.keys() and po["created_by"]:
            signature_uri = _user_signature_data_uri(po["authoriser_signature"], po["created_by"])
    signature_html = ""
    if authoriser_name:
        sig_img = f"<img src='{signature_uri}' style='max-height:55px;display:block;margin-bottom:4px'>" if signature_uri else "<div style='height:55px'></div>"
        position_html = (
            f"<div style='font-size:11px;font-style:italic;color:#666'>{authoriser_position}</div>"
            if authoriser_position else ""
        )
        signature_html = (
            f"<div style='margin-top:36px'>"
            f"<div style='font-size:11px;color:#666;text-transform:uppercase;margin-bottom:8px'>Authorised by</div>"
            f"{sig_img}"
            f"<div style='font-weight:700'>{authoriser_name}</div>"
            f"{position_html}"
            f"</div>"
        )

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
  body {{ font-family: Arial, Helvetica, sans-serif; font-size: 13px; color: #1a1a1a; margin: 32px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
  th, td {{ padding: 8px 6px; border-bottom: 1px solid #eee; text-align: left; }}
  th {{ font-size: 11px; text-transform: uppercase; color: #666; }}
  .header {{ display: flex; justify-content: space-between; align-items: flex-start; }}
  .muted {{ color: #666; }}
  img.logo {{ width: 76px; height: auto; display: block; margin-bottom: 10px; }}
</style></head>
<body>
  <table style="border:none;margin-top:0"><tr style="border:none">
    <td style="border:none;width:60%">
      <img class="logo" src="{logo_uri}">
      <strong>Modoku Tech Sdn Bhd (1390352-H)</strong><br>
      <span class="muted">Level 30, Menara Prestige<br>1, Jalan Pinang<br>50450 Kuala Lumpur</span><br>
      <span class="muted">hello@modoku.tech</span>
    </td>
    <td style="border:none;text-align:right;vertical-align:top">
      <h2 style="margin:0">PURCHASE ORDER</h2>
      <div class="muted">{po['po_no']}</div>
    </td>
  </tr></table>

  <table style="border:none">
    <tr style="border:none">
      <td style="border:none;width:50%">
        <div class="muted" style="font-size:11px;text-transform:uppercase">Issued To (Trainer)</div>
        <strong>{po['trainer_name'] or 'Trainer removed'}</strong><br>
        {po['trainer_email'] or ''}<br>{(po['trainer_phone'] if 'trainer_phone' in po.keys() else None) or ''}
      </td>
      <td style="border:none;text-align:right">
        <span class="muted">Issue date:</span> {_fmtdate(po['issue_date'])}<br>
        <span class="muted">Status:</span> {po['status']}
      </td>
    </tr>
  </table>

  <table>
    <thead><tr><th>Description</th><th>Date(s)</th><th>Venue</th><th style="text-align:right">Fee</th></tr></thead>
    <tbody>
      <tr>
        <td>{po['course_title']}</td>
        <td>{dates}</td>
        <td>{po['venue'] or '-'}</td>
        <td style="text-align:right">{po['currency']} {po['fee_amount']:,.2f}</td>
      </tr>
      {item_rows}
      {items_total_row}
    </tbody>
  </table>

  {terms_html}
  {resp_html}
  {signature_html}
</body></html>"""


def _build_quotation_html(q, items, subtotal, title):
    logo_uri = _logo_data_uri()
    sst_rate = q["sst_rate"] if "sst_rate" in q.keys() else 0
    sst_amount = round(subtotal * (sst_rate or 0) / 100, 2)
    grand_total = subtotal + sst_amount

    company_name = q["company_name_override"] or q["client_company_name"] or ""
    address = q["address"] or (q["client_company_address"] if "client_company_address" in q.keys() else "") or ""
    tel = q["tel"] or (q["client_company_phone"] if "client_company_phone" in q.keys() else "") or ""

    item_rows = "".join(
        f"<tr><td>{i + 1}</td><td>{item['programme']}</td><td>{item['no_of_pax'] or ''}</td>"
        f"<td>{item['training_type'] or ''}</td><td>{item['duration'] or ''}</td>"
        f"<td>{_fmtdate(item['item_date'])}"
        f"{' &ndash; ' + _fmtdate(item['item_date_end']) if item['item_date_end'] else ''}</td>"
        f"<td>{(item['item_time'] if 'item_time' in item.keys() else '') or ''}</td>"
        f"<td style='text-align:right'>RM {item['investment_fee']:,.2f}</td></tr>"
        for i, item in enumerate(items)
    )
    total_row = ""
    if items:
        subtotal_row = (
            f"<tr><td colspan='7' style='text-align:right;border:none' class='muted'>Sub-total</td>"
            f"<td style='text-align:right;border:none'>RM {subtotal:,.2f}</td></tr>"
        )
        sst_row = ""
        if sst_rate:
            sst_row = (
                f"<tr><td colspan='7' style='text-align:right;border:none' class='muted'>SST ({sst_rate}%)</td>"
                f"<td style='text-align:right;border:none'>RM {sst_amount:,.2f}</td></tr>"
            )
        total_row = (
            f"{subtotal_row}{sst_row}"
            f"<tr style='font-weight:700'><td colspan='7' style='text-align:right;border-top:1px solid #999'>Total Investment</td>"
            f"<td style='text-align:right;border-top:1px solid #999'>RM {grand_total:,.2f}</td></tr>"
        )

    small_style = "font-size:9px;line-height:1.5"
    terms_html = (
        f"<div style='margin-top:16px'><div style='font-size:11px;color:#666;text-transform:uppercase'>"
        f"Terms &amp; Conditions</div><div style='{small_style}'>{_linelist_html(q['terms'], ordered=True)}</div></div>"
    ) if q["terms"] else ""

    contact_phone = q["created_by_phone"] if "created_by_phone" in q.keys() else ""
    sales_name = q["created_by_name"] if "created_by_name" in q.keys() else ""
    sales_position = q["created_by_position"] if "created_by_position" in q.keys() else ""
    signature_uri = ""
    if "created_by_signature" in q.keys() and q["created_by_signature"] and q["created_by"]:
        signature_uri = _user_signature_data_uri(q["created_by_signature"], q["created_by"])
    sig_img = f"<img src='{signature_uri}' style='max-height:55px;display:block;margin-bottom:4px'>" if signature_uri else "<div style='height:55px'></div>"
    position_html = (
        f"<div style='font-size:11px;font-style:italic;color:#666'>{sales_position}</div>"
        if sales_position else ""
    )

    closing_html = (
        f"<div style='margin-top:16px;{small_style}'>Trusting that the above quotation will meet your "
        f"requirement and we look forward to your favorable reply. Please do not hesitate to contact me at "
        f"{contact_phone or '-'}.<br>We hereby accept &amp; confirm the terms and conditions above.</div>"
    )

    signature_block = f"""
      <table style="border:none;margin-top:32px"><tr style="border:none">
        <td style="border:none;width:50%;vertical-align:top">
          {sig_img}
          <div style="font-weight:700">{sales_name}</div>
          {position_html}
        </td>
        <td style="border:none;width:50%;vertical-align:top">
          <div style="height:55px"></div>
          <div style="border-top:1px solid #999;padding-top:4px;font-size:11px;color:#666">
            On Behalf of Name, Company Stamp &amp; Date
          </div>
        </td>
      </tr></table>
    """

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
  body {{ font-family: Arial, Helvetica, sans-serif; font-size: 13px; color: #1a1a1a; margin: 32px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
  th, td {{ padding: 8px 6px; border-bottom: 1px solid #eee; text-align: left; }}
  th {{ font-size: 11px; text-transform: uppercase; color: #666; }}
  .muted {{ color: #666; }}
  img.logo {{ width: 76px; height: auto; display: block; margin-bottom: 10px; }}
</style></head>
<body>
  <table style="border:none;margin-top:0"><tr style="border:none">
    <td style="border:none;width:60%">
      <img class="logo" src="{logo_uri}">
      <strong>Modoku Tech Sdn Bhd (1390352-H)</strong><br>
      <span class="muted">Level 30, Menara Prestige<br>1, Jalan Pinang<br>50450 Kuala Lumpur</span><br>
      <span class="muted">hello@modoku.tech</span>
    </td>
    <td style="border:none;text-align:right;vertical-align:top">
      <h2 style="margin:0">QUOTATION</h2>
      <div class="muted">{q['quote_no']}</div>
    </td>
  </tr></table>

  <table style="border:none">
    <tr style="border:none">
      <td style="border:none;width:50%">
        <div class="muted" style="font-size:11px;text-transform:uppercase">Attention To</div>
        <strong>{q['attention_to'] or ''}</strong><br>
        {company_name}<br>{address}<br>{tel}
      </td>
      <td style="border:none;text-align:right">
        <span class="muted">Date:</span> {_fmtdate(q['quote_date'])}<br>
        <span class="muted">Ref.:</span> {q['ref_no'] or '-'}
      </td>
    </tr>
  </table>

  <p style="margin-top:20px">Thank you for giving us the opportunity to propose the below program to your
  organization. The following are the information pertaining to your request.</p>

  <table>
    <thead><tr><th>No</th><th>Programme</th><th>No of Pax</th><th>Training Type</th><th>Duration</th>
      <th>Date</th><th>Time</th><th style="text-align:right">Investment Fees</th></tr></thead>
    <tbody>
      {item_rows}
      {total_row}
    </tbody>
  </table>

  {terms_html}
  {closing_html}
  {signature_block}
</body></html>"""


def generate_quotation_pdf(q, items, subtotal, title):
    """Returns PDF bytes for the given quotation (sqlite Row), items, items subtotal
    (SST is applied on top using q['sst_rate']), and document title."""
    html = _build_quotation_html(q, items, subtotal, title)
    with tempfile.NamedTemporaryFile(suffix=".html", mode="w", encoding="utf-8", delete=False) as html_file:
        html_file.write(html)
        html_path = html_file.name
    pdf_path = html_path.replace(".html", ".pdf")
    try:
        subprocess.run(
            ["wkhtmltopdf", "--quiet", "--page-size", "A4", "--margin-top", "10mm",
             "--margin-bottom", "10mm", html_path, pdf_path],
            check=True, timeout=30, capture_output=True,
        )
        with open(pdf_path, "rb") as f:
            return f.read()
    finally:
        for path in (html_path, pdf_path):
            try:
                os.remove(path)
            except OSError:
                pass


def _fmt_qty(qty):
    return str(int(qty)) if float(qty) == int(qty) else str(qty)


def _build_invoice_html(invoice, items):
    logo_uri = _logo_data_uri()
    brand = "#0c45a6"
    accent = "#fbaf17"

    item_rows = []
    for idx, item in enumerate(items, start=1):
        # Duration is now always populated (it's part of the Amount math for
        # every line item), so it's no longer a useful signal for "is this a
        # training row with extra scheduling detail" — venue/date still are.
        is_training_row = bool(item["venue"] or item["item_date"])
        qty_display = _fmt_qty(item["quantity"]) + (" pax" if is_training_row else "")
        sub_detail = ""
        if is_training_row:
            date_line = ""
            if item["item_date"]:
                date_line = f"<div>Date: {_fmtdate(item['item_date'])}"
                if item["item_date_end"]:
                    date_line += f" &ndash; {_fmtdate(item['item_date_end'])}"
                date_line += "</div>"
            venue_line = f"<div>Venue: {item['venue']}</div>" if item["venue"] else ""
            sub_detail = (
                "<div style='padding-top:6px;font-size:12px;color:#444'>"
                f"<div>Programme: {item['description']}</div>"
                f"<div>Pax: {_fmt_qty(item['quantity'])} pax</div>"
                f"{date_line}{venue_line}</div>"
            )
        item_rows.append(
            "<tr style='page-break-inside:avoid'>"
            f"<td style='vertical-align:top'>{idx:02d}</td>"
            f"<td style='font-weight:600;vertical-align:top'>{item['description']}{sub_detail}</td>"
            f"<td style='vertical-align:top'>{item['duration'] or ''}</td>"
            f"<td style='text-align:right;vertical-align:top'>{qty_display}</td>"
            f"<td style='text-align:right;vertical-align:top'>{item['unit_price']:,.2f}</td>"
            f"<td style='text-align:right;vertical-align:top'>{item['amount']:,.2f}</td></tr>"
        )
    item_rows_html = "".join(item_rows)

    meta_middle = ""
    if invoice["project_title"]:
        meta_middle += (
            f"<div class='muted' style='font-size:11px;text-transform:uppercase'>Project</div>"
            f"<strong>{invoice['project_title']}</strong><br><br>"
        )
    if invoice["grant_id"]:
        meta_middle += f"<div><strong>Grant ID</strong> &nbsp;{invoice['grant_id']}</div>"
    if invoice["employer"]:
        meta_middle += f"<div><strong>Employer</strong> &nbsp;{invoice['employer']}</div>"

    sst_row = (
        f"<tr><td class='muted' style='border:none'>SST ({invoice['sst_rate']}%)</td>"
        f"<td style='border:none;text-align:right'>{invoice['sst_amount']:,.2f}</td></tr>"
    ) if invoice["sst_rate"] else ""

    notes_html = (
        f"<div style='margin-top:16px'><div style='font-size:11px;color:#666;text-transform:uppercase'>"
        f"Notes</div><p>{invoice['notes']}</p></div>"
    ) if invoice["notes"] else ""

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
  html, body {{ height: 100%; margin: 0; }}
  body {{ font-family: Arial, Helvetica, sans-serif; font-size: 13px; color: #1a1a1a; }}
  /* min-height fills the full A4 content area (297mm page minus the 10mm
     top/bottom/left/right margins passed to wkhtmltopdf below) so the gold
     frame reaches the bottom of the page like a real letterhead, instead of
     shrink-wrapping to just the content and leaving the rest of the page a
     blank void underneath a floating box — that mismatch was the "distorted"
     look reported against the print/browser version, which doesn't have
     this visible border to expose the same shrink-wrapped whitespace. */
  .frame {{ border: 8px solid {accent}; padding: 20px 30px 16px; min-height: 277mm;
            box-sizing: border-box; display: flex; flex-direction: column; }}
  .frame-body {{ flex: 1 1 auto; }}
  .frame-foot {{ margin-top: auto; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
  th, td {{ padding: 8px 6px; border-bottom: 1px solid #eee; text-align: left; }}
  th {{ font-size: 11px; text-transform: uppercase; color: {brand}; font-weight: 700;
       border-bottom: 2px solid {brand}; }}
  .muted {{ color: #666; }}
  img.logo {{ width: 90px; height: auto; }}
  .brand {{ color: {brand}; }}
  tr {{ page-break-inside: avoid; }}
</style></head>
<body>
<div class="frame">
<div class="frame-body">
  <table style="border:none;margin-top:0"><tr style="border:none">
    <td style="border:none;width:60%;vertical-align:top">
      <img class="logo" src="{logo_uri}"><br>
      <strong class="brand">Modoku Tech Sdn Bhd</strong><br>
      <span class="brand" style="font-size:11px">(1390352-H)</span>
    </td>
    <td style="border:none;text-align:right;vertical-align:top">
      <h2 class="brand" style="margin:0;letter-spacing:3px">INVOICE</h2>
      <div style="font-weight:600">{invoice['invoice_no']}</div>
    </td>
  </tr></table>

  <hr style="border:none;border-top:1px solid #d8dce3;margin:14px 0">
  <table style="border:none">
    <tr style="border:none">
      <td style="border:none;width:34%;vertical-align:top">
        <div class="brand" style="font-size:11px;font-weight:700;text-transform:uppercase">Billed To</div>
        <strong>{invoice['bill_to_name']}</strong><br>
        {invoice['bill_to_address'] or ''}
        {'<br>SST Reg. No: ' + invoice['sst_reg_no'] if invoice['sst_reg_no'] else ''}
        {'<br>TIN: ' + invoice['buyer_tin'] if invoice['buyer_tin'] else ''}
      </td>
      <td style="border:none;width:38%;vertical-align:top;font-size:12px">
        {meta_middle}
      </td>
      <td style="border:none;width:28%;text-align:right;vertical-align:top">
        <div class="brand" style="font-size:11px;font-weight:700;text-transform:uppercase">Date</div>
        <strong>{_fmtdate(invoice['invoice_date'])}</strong>
        {'<div class="muted" style="font-size:11px">Due ' + _fmtdate(invoice['due_date']) + '</div>' if invoice['due_date'] else ''}
      </td>
    </tr>
  </table>
  <hr style="border:none;border-top:1px solid #d8dce3;margin:14px 0">

  <table>
    <thead><tr><th style="width:5%">No.</th><th>Description</th><th>Duration</th>
      <th style="text-align:right">No. of Pax</th><th style="text-align:right">Rate ({invoice['currency']})</th>
      <th style="text-align:right">Amount ({invoice['currency']})</th></tr></thead>
    <tbody>
      {item_rows_html}
    </tbody>
  </table>

  <table style="border:none;margin-top:0"><tr style="border:none"><td style="border:none;width:60%"></td>
    <td style="border:none">
      <table>
        <tr><td class="muted" style="border:none">Sub-total</td><td style="border:none;text-align:right">{invoice['subtotal']:,.2f}</td></tr>
        {sst_row}
        <tr style="font-weight:700" class="brand"><td style="border-top:1px solid #d8dce3">Total Due</td>
          <td style="border-top:1px solid #d8dce3;text-align:right">{invoice['currency']} {invoice['total']:,.2f}</td></tr>
      </table>
    </td>
  </tr></table>

  <hr style="border:none;border-top:1px solid #d8dce3;margin:18px 0 12px">
  <table style="border:none">
    <tr style="border:none">
      <td style="border:none;width:45%;vertical-align:top">
        <div style="font-weight:700;font-size:12px;margin-bottom:4px">Payable to:</div>
        <table style="border:none;margin-top:0;width:auto">
          <tr style="border:none"><td style="border:none;padding:1px 0;font-size:12px">Name</td><td style="border:none;padding:1px 8px;font-size:12px">:</td><td style="border:none;padding:1px 0;font-size:12px">Modoku Tech Sdn Bhd</td></tr>
          <tr style="border:none"><td style="border:none;padding:1px 0;font-size:12px">Account No</td><td style="border:none;padding:1px 8px;font-size:12px">:</td><td style="border:none;padding:1px 0;font-size:12px">564490459176</td></tr>
          <tr style="border:none"><td style="border:none;padding:1px 0;font-size:12px">Bank</td><td style="border:none;padding:1px 8px;font-size:12px">:</td><td style="border:none;padding:1px 0;font-size:12px">Maybank (MBB)</td></tr>
          <tr style="border:none"><td style="border:none;padding:1px 0;font-size:12px">SWIFT</td><td style="border:none;padding:1px 8px;font-size:12px">:</td><td style="border:none;padding:1px 0;font-size:12px">MBBEMYKL</td></tr>
        </table>
      </td>
      <td style="border:none;width:55%;vertical-align:top">
        <div style="font-weight:700;font-size:12px">Payment terms:</div>
        <div style="font-size:12px">Payment is due within 30 calendar days from the training completion date.</div>
      </td>
    </tr>
  </table>

  {notes_html}
  </div>

  <div class="frame-foot">
    <div style="color:#888;font-size:10px;margin-top:20px">
      This is a computer generated and no signature is required.
    </div>
    <hr style="border:none;border-top:1px solid #d8dce3;margin:14px 0">
    <table style="border:none"><tr style="border:none" class="brand" style="font-size:11px">
      <td style="border:none;font-size:11px">Level 30, Menara Prestige<br>1, Jalan Pinang<br>50450 Kuala Lumpur</td>
      <td style="border:none;font-size:11px;text-align:center">+60 3 2728 1035<br>hello@modoku.tech</td>
      <td style="border:none;font-size:11px;text-align:right;font-weight:700">modoku.tech</td>
    </tr></table>
  </div>
</div>
</body></html>"""


def generate_invoice_pdf(invoice, items):
    """Returns PDF bytes for the given invoice (sqlite Row) and items."""
    html = _build_invoice_html(invoice, items)
    with tempfile.NamedTemporaryFile(suffix=".html", mode="w", encoding="utf-8", delete=False) as html_file:
        html_file.write(html)
        html_path = html_file.name
    pdf_path = html_path.replace(".html", ".pdf")
    try:
        subprocess.run(
            ["wkhtmltopdf", "--quiet", "--page-size", "A4",
             "--margin-top", "10mm", "--margin-bottom", "10mm",
             "--margin-left", "10mm", "--margin-right", "10mm",
             html_path, pdf_path],
            check=True, timeout=30, capture_output=True,
        )
        with open(pdf_path, "rb") as f:
            return f.read()
    finally:
        for path in (html_path, pdf_path):
            try:
                os.remove(path)
            except OSError:
                pass


# --- Certificate rendering ---------------------------------------------
#
# Certificates are composited as an image with Pillow and saved straight to
# PDF, rather than built as HTML/CSS and rendered through wkhtmltopdf like
# every other document in this file. That's a deliberate exception, for the
# same reason poster.py avoids wkhtmltopdf entirely (see that module's
# docstring): the certificate is the one landscape, heavily
# absolutely-positioned layout in this app, and this environment's
# wkhtmltopdf (0.12.6, unpatched Qt) has a documented width-mis-scaling bug
# on exactly that combination. An earlier fix worked around it with
# percentage-based CSS positioning and still produced blank pages for some
# inputs — so rather than continue chasing wkhtmltopdf/Qt quirks, generation
# is moved to the same reliable, dependency-free rendering path poster.py
# already uses: Pillow draws the design (border frame, corner accents in
# Modoku's brand colours, the logo, the Poppins-font text, and the real
# director signature image) onto a 300dpi canvas sized to an A4-landscape
# page, which Pillow can then save directly as a one-page PDF. No HTML, no
# subprocess, no page-size flags to get wrong.
CERT_PAGE_W_MM = 297.0
CERT_PAGE_H_MM = 210.0
CERT_DPI = 300
_CERT_PX_PER_MM = CERT_DPI / 25.4
_CERT_PX_PER_PT = CERT_DPI / 72
CERT_W_PX = round(CERT_PAGE_W_MM * _CERT_PX_PER_MM)
CERT_H_PX = round(CERT_PAGE_H_MM * _CERT_PX_PER_MM)

_CERT_NAVY = (16, 41, 142)       # #10298e
_CERT_ORANGE = (251, 175, 23)    # #fbaf17
_CERT_DARK = (26, 26, 26)        # #1a1a1a
_CERT_MID = (51, 51, 51)         # #333333
_CERT_GRAY = (102, 102, 102)     # #666666

_CERT_BG_TEXTURE_PATH = os.path.join("static", "img", "cert_bg_texture.png")

# Bundled inside the repo (not a system font package) so rendering doesn't
# depend on fonts happening to be installed on whatever server runs this —
# same self-contained-asset approach as the logo/background texture above.
_CERT_FONT_DIR = os.path.join(os.path.dirname(__file__), "static", "fonts")
_CERT_FONT_LIGHT = os.path.join(_CERT_FONT_DIR, "Poppins-Light.ttf")
_CERT_FONT_REGULAR = os.path.join(_CERT_FONT_DIR, "Poppins-Regular.ttf")
_CERT_FONT_ITALIC = os.path.join(_CERT_FONT_DIR, "Poppins-Italic.ttf")
_CERT_FONT_BOLD = os.path.join(_CERT_FONT_DIR, "Poppins-Bold.ttf")


def _cert_mm(value):
    return value * _CERT_PX_PER_MM


def _cert_pt(value):
    return round(value * _CERT_PX_PER_PT)


def _cert_font(path, size_pt):
    try:
        return ImageFont.truetype(path, _cert_pt(size_pt))
    except OSError:
        return ImageFont.load_default()


def _cert_wrap_text(draw, text, font, max_width):
    words = (text or "").split()
    if not words:
        return [""]
    lines, line = [], ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines or [""]


def _cert_fit_wrapped(draw, text, font_path, base_size_pt, max_width, scale, min_size_pt, max_lines=2):
    """Wraps text to max_width at base_size_pt * scale, shrinking further
    (down to min_size_pt) if it still doesn't fit within max_lines. Used for
    the two variable-length pieces of a certificate (participant name,
    course title) so a long value degrades gracefully — first by wrapping,
    then by shrinking — rather than overflowing into the signature block
    below."""
    size = max(min_size_pt, round(base_size_pt * scale))
    font = _cert_font(font_path, size)
    lines = _cert_wrap_text(draw, text, font, max_width)
    while len(lines) > max_lines and size > min_size_pt:
        size -= 1
        font = _cert_font(font_path, size)
        lines = _cert_wrap_text(draw, text, font, max_width)
    return font, lines


def _cert_draw_centered(draw, y, text, font, fill, max_width=None):
    """Draws text (wrapped to max_width if given) centered horizontally on
    the page, returning the y position just below the last line drawn."""
    lines = _cert_wrap_text(draw, text, font, max_width) if max_width else [text]
    ascent, descent = font.getmetrics()
    line_height = ascent + descent
    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text(((CERT_W_PX - w) / 2, y), line, font=font, fill=fill)
        y += line_height
    return y


def _cert_shape(canvas, top=None, left=None, right=None, bottom=None, size=90, color=_CERT_ORANGE):
    """Paints one 45°-rotated square ('diamond') corner accent — mirrors the
    rotated, position:absolute divs of the previous CSS design. Only two of
    top/bottom and left/right are given (matching CSS semantics: 'top'
    measures from the page's top edge to the box's un-rotated top edge,
    'bottom' from the page's bottom edge to the box's bottom edge, etc.);
    most of these boxes are intentionally positioned mostly off-page so only
    a triangular sliver shows in a page corner."""
    size_px = round(_cert_mm(size))
    top_px = _cert_mm(top) if top is not None else CERT_H_PX - _cert_mm(bottom) - size_px
    left_px = _cert_mm(left) if left is not None else CERT_W_PX - _cert_mm(right) - size_px
    center_x = left_px + size_px / 2
    center_y = top_px + size_px / 2

    square = Image.new("RGBA", (size_px, size_px), color + (255,))
    rotated = square.rotate(45, expand=True, resample=Image.BICUBIC)
    paste_x = round(center_x - rotated.width / 2)
    paste_y = round(center_y - rotated.height / 2)
    canvas.paste(rotated, (paste_x, paste_y), rotated)


_CERT_BG_TEXTURE_OPACITY = 0.5  # how strongly the guilloché pattern shows against white (+10%)


def _cert_tile_background(canvas):
    """Fills the canvas with the repeating mint/teal guilloché security-mesh
    texture used by the reference design, tiling a small text-free patch
    (extracted from the reference certificate) across the full page. Faded
    toward white (40% strength) so it reads as a subtle security texture
    rather than competing with the certificate's text."""
    try:
        tile_path = os.path.join(current_app.root_path, "static", "img", "cert_bg_texture.png")
        tile = Image.open(tile_path).convert("RGB")
        white = Image.new("RGB", tile.size, (255, 255, 255))
        tile = Image.blend(white, tile, _CERT_BG_TEXTURE_OPACITY)
    except Exception:  # noqa: BLE001 - fall back to a plain white background
        current_app.logger.exception("Failed to load certificate background texture")
        return
    tile_w, tile_h = tile.size
    for x in range(0, CERT_W_PX, tile_w):
        for y in range(0, CERT_H_PX, tile_h):
            canvas.paste(tile, (x, y))


def _build_certificate_image(fullname, course_title, date_range):
    """Returns a Pillow RGB Image of the certificate at CERT_W_PX x CERT_H_PX
    (A4 landscape, 300dpi) — see the module-level comment above this
    function for why this replaced an HTML/wkhtmltopdf approach."""
    canvas = Image.new("RGB", (CERT_W_PX, CERT_H_PX), (255, 255, 255))
    _cert_tile_background(canvas)

    draw = ImageDraw.Draw(canvas)

    # Decorative shapes are drawn FIRST — the border frame is drawn AFTER
    # (below), on top of them, so the frame's orange line stays a clean,
    # unbroken rectangle that occludes/notches every shape it crosses. This
    # was verified pixel-by-pixel against the reference certificate: sampling
    # straight across a corner wedge shows the frame's orange line sitting
    # solid and continuous over the navy fill on both sides, not the other
    # way around.
    # Simplified to four clean, symmetric corner wedges (dropped the small
    # mid-left "chevron" notches and the two-piece bottom-right chevron
    # approximation from the original reverse-engineered layout — on review
    # those read as truncated/odd rather than crisp, so a plainer,
    # more conservative set of accents is used instead).
    for shape_kwargs in [
        dict(top=-86, left=-86, size=115, color=_CERT_NAVY),        # top-left navy wedge (pushed outward another 10%)
        dict(top=-86, right=-86, size=130, color=_CERT_NAVY),       # top-right navy wedge (pushed outward another 10%)
        dict(bottom=-62, left=-62, size=90, color=_CERT_ORANGE),    # bottom-left orange wedge (pushed outward another 10%)
        dict(bottom=-62, right=-62, size=90, color=_CERT_ORANGE),   # bottom-right orange wedge (mirrors bottom-left)
    ]:
        _cert_shape(canvas, **shape_kwargs)

    # Orange "flag" accent — a right triangle with its sharp apex touching
    # the very top edge of the page, positioned right-of-center (not in a
    # corner). Vertices measured from the reference design (mm, on the
    # 297x210 page).
    draw.polygon(
        [
            (_cert_mm(196), 0),
            (_cert_mm(196), _cert_mm(26.6)),
            (_cert_mm(169), _cert_mm(26.6)),
        ],
        fill=_CERT_ORANGE,
    )

    # Border frame — orange rectangle, uniformly inset from the page edges —
    # drawn last so it stays crisp and unbroken over every shape above.
    frame_inset = _cert_mm(14)
    frame_box = [frame_inset, frame_inset, CERT_W_PX - frame_inset, CERT_H_PX - frame_inset]
    draw.rectangle(frame_box, outline=_CERT_ORANGE, width=8)

    # Heading, top-left.
    heading_x = _cert_mm(32)
    y = _cert_mm(30)
    font_heading_light = _cert_font(_CERT_FONT_LIGHT, 26)
    font_heading_bold = _cert_font(_CERT_FONT_BOLD, 26)
    draw.text((heading_x, y), "CERTIFICATE", font=font_heading_light, fill=_CERT_DARK)
    ascent, descent = font_heading_light.getmetrics()
    y += ascent + descent - _cert_pt(3)
    draw.text((heading_x, y), "OF PARTICIPATION", font=font_heading_bold, fill=_CERT_DARK)

    # Logo, top-right — sized and positioned to sit entirely in white space,
    # clear of the navy top-right wedge (shrunk ~30% and moved further from
    # the corner than the original placement, which let the wordmark's
    # right side cross into the navy shape).
    try:
        logo = Image.open(os.path.join(current_app.root_path, "static", "img", "logo.png")).convert("RGBA")
        logo_w = round(_cert_mm(46 * 0.7 * 1.2))  # +20% size
        logo_h = round(logo_w * logo.height / logo.width)
        logo = logo.resize((logo_w, logo_h), Image.LANCZOS)
        logo_x = round(CERT_W_PX - _cert_mm(60) - logo_w + logo_w * 0.60)  # +25%, +20%, then another +15% to the right
        logo_y = round(_cert_mm(34 * 0.9 * 1.05))  # moved up 10%, then down 5%
        canvas.paste(logo, (logo_x, logo_y), logo)
    except Exception:  # noqa: BLE001 - a missing/unreadable logo shouldn't break the whole certificate
        current_app.logger.exception("Failed to composite logo onto certificate")

    # Signature block metrics are computed first (without drawing) — its
    # bottom edge is pinned 26mm above the page bottom, so its total height
    # determines where its top (and therefore the bottom of the available
    # space for the body block above it) falls. Computing this before laying
    # out the body block lets the body block size itself to fit whatever
    # room is actually left, instead of the two blocks being laid out
    # independently and risking an overlap when fullname/course_title are
    # long enough to wrap onto extra lines.
    font_sig_name = _cert_font(_CERT_FONT_BOLD, 12)
    font_sig_position = _cert_font(_CERT_FONT_ITALIC, 10)
    font_sig_company = _cert_font(_CERT_FONT_BOLD, 11)
    font_sig_regno = _cert_font(_CERT_FONT_REGULAR, 9)

    sig_img_h = round(_cert_mm(20))
    sig_line_gap = _cert_mm(2)
    sig_name_lh = sum(font_sig_name.getmetrics())
    sig_pos_gap = _cert_mm(1)
    sig_pos_lh = sum(font_sig_position.getmetrics())
    sig_company_gap = _cert_mm(3)
    sig_company_lh = sum(font_sig_company.getmetrics())
    sig_regno_lh = sum(font_sig_regno.getmetrics())
    sig_block_height = (sig_img_h + sig_line_gap + sig_name_lh + sig_pos_gap + sig_pos_lh
                         + sig_company_gap + sig_company_lh + sig_regno_lh)
    sig_top = CERT_H_PX - _cert_mm(26) - sig_block_height

    # Body block, centered — auto-shrinks (font size, then line spacing) if
    # a long fullname/course_title would otherwise wrap onto extra lines and
    # run into the signature block above. Each attempt re-wraps fullname and
    # course_title (capped at 2 lines each, shrinking further within that
    # cap if still too wide) at a smaller scale until everything fits in the
    # space between the fixed body top (88mm) and the signature block, or
    # the scale floor is hit — in which case it's drawn as small as the
    # floor allows rather than looping forever.
    body_top = _cert_mm(88)
    available = sig_top - _cert_mm(6) - body_top
    body_max_width = CERT_W_PX - _cert_mm(80)

    scale = 1.0
    while True:
        font_small = _cert_font(_CERT_FONT_REGULAR, max(9, round(13 * scale)))
        font_name, name_lines = _cert_fit_wrapped(
            draw, fullname, _CERT_FONT_BOLD, 27, body_max_width, scale, min_size_pt=14)
        font_course, course_lines = _cert_fit_wrapped(
            draw, course_title, _CERT_FONT_BOLD, 17, body_max_width, scale, min_size_pt=11)
        margin = _cert_mm(max(1.2, 3 * scale))
        small_lh = sum(font_small.getmetrics())
        name_lh = sum(font_name.getmetrics()) * len(name_lines)
        course_lh = sum(font_course.getmetrics()) * len(course_lines)
        total_height = small_lh + margin + name_lh + margin + small_lh + margin + course_lh + margin + small_lh
        if total_height <= available or scale <= 0.6:
            break
        scale -= 0.05

    y = body_top
    y = _cert_draw_centered(draw, y, "This is to certify that", font_small, _CERT_MID) + margin
    y = _cert_draw_centered(draw, y, fullname, font_name, _CERT_DARK, max_width=body_max_width) + margin
    y = _cert_draw_centered(draw, y, "has successfully completed the", font_small, _CERT_MID) + margin
    y = _cert_draw_centered(draw, y, course_title, font_course, _CERT_DARK, max_width=body_max_width) + margin
    _cert_draw_centered(draw, y, f"training program conducted on {date_range}", font_small, _CERT_MID)

    # Now draw the signature block at its precomputed position.
    y = sig_top
    signature_path = os.path.join(current_app.root_path, "static", "img", "director_signature.png")
    try:
        signature = Image.open(signature_path).convert("RGBA")
        sig_w = round(sig_img_h * signature.width / signature.height)
        signature = signature.resize((sig_w, sig_img_h), Image.LANCZOS)
        canvas.paste(signature, (round((CERT_W_PX - sig_w) / 2), round(y)), signature)
    except Exception:  # noqa: BLE001 - render the rest of the certificate even if the signature is missing
        current_app.logger.exception("Failed to composite signature onto certificate")
    y += sig_img_h
    y += sig_line_gap

    y = _cert_draw_centered(draw, y, CERT_SIGNEE_NAME, font_sig_name, _CERT_DARK) + sig_pos_gap
    y = _cert_draw_centered(draw, y, CERT_SIGNEE_POSITION, font_sig_position, _CERT_GRAY) + sig_company_gap
    y = _cert_draw_centered(draw, y, "Modoku Tech Sdn Bhd", font_sig_company, _CERT_NAVY)
    _cert_draw_centered(draw, y, "(1390352-H)", font_sig_regno, _CERT_GRAY)

    return canvas


def generate_certificate_pdf(fullname, course_title, date_range):
    """Returns landscape PDF bytes for an e-Certificate — fullname/course_title/
    date_range are plain strings the caller has already formatted (date_range
    via the fmtdaterange filter, e.g. '25 & 26 June 2026'). See the
    module-level comment above _build_certificate_image for why this is a
    Pillow-composited image saved to PDF rather than an HTML/wkhtmltopdf
    render like the rest of this file."""
    image = _build_certificate_image(fullname, course_title, date_range)
    buf = BytesIO()
    image.save(buf, format="PDF", resolution=CERT_DPI)
    return buf.getvalue()


def _build_t3_form_html(session_row, participants, training_days, extra_blank_rows=0):
    """Self-contained HTML for the printable T3 (PSMB/SBL-KHAS/T3/01)
    Attendance List — same layout as templates/sessions/t3_attendance_form.html,
    reimplemented here with inline CSS instead of reusing that Jinja
    template (which pulls Bootstrap from a CDN and links a static
    stylesheet by URL) since wkhtmltopdf renders a bare local HTML file
    with no Flask request context and no guaranteed network access. The
    CSS below is hand-translated from static/css/style.css's .t3-* rules
    (and the Bootstrap classes the real template layers on top of them —
    card/table-sm/small/etc.) pixel-for-pixel, including the site's
    non-standard 15px root font-size that its rem values are based on, so
    this should look like a straight screenshot of the on-screen/print
    version rather than an approximation. One page per training day,
    matching the on-screen version's per-day sheets for multi-day
    trainings. Participant-supplied fields (name, employer, IC no. —
    entered via the public T3 form) are HTML-escaped since they're the one
    part of this document that isn't staff-typed."""
    course_title = escape(session_row["course_title"] or "")
    session_code = escape(session_row["session_code"] or "")

    participant_rows = "".join(
        f"<tr><td style='text-align:center'>{i}</td><td>{escape(p['name'] or '')}</td>"
        f"<td>{escape(p['employer_name'] or '')}</td><td>{escape(p['ic_no'] or '')}</td>"
        f"<td>{escape(p['citizenship'] or 'Malaysian')}</td>"
        f"<td style='text-align:center'>{escape((p['gender'] or '')[:1])}</td><td></td></tr>"
        for i, p in enumerate(participants, start=1)
    )
    # At least 6 rows total, plus any extra blank rows the user asked for
    # (e.g. for last-minute walk-in participants to fill in by hand).
    blank_row_count = max(0, 6 - len(participants)) + max(0, extra_blank_rows)
    blank_rows = "".join(
        "<tr><td style='text-align:center'>&nbsp;</td><td></td><td></td><td></td><td></td><td></td><td></td></tr>"
        for _ in range(blank_row_count)
    )

    pages = []
    for idx, day in enumerate(training_days):
        day_label = _fmtdate(day.isoformat())
        if len(training_days) > 1:
            day_label += f" (Day {idx + 1})"
        page_style = "page-break-before: always;" if idx > 0 else ""
        pages.append(f"""
        <div class="t3-page" style="{page_style}">
          <div class="code-stamp">{session_code}</div>
          <p style="text-align:center;font-weight:700;margin:0 0 16px">FOR SBL-KHAS SCHEME ONLY</p>
          <div class="t3-header-row">
            <div class="t3-box t3-box-first">PSMB/SBL-KHAS/T3/01</div>
            <div class="t3-header-title"><span>ATTENDANCE LIST</span></div>
            <div class="t3-box t3-box-last">This attendance list must be enclosed when submitting the claim form PSMB/SBL-KHAS/JD/14</div>
          </div>
          <table class="info-table">
            <tr><td class="label">Course Title</td><td class="colon">:</td><td class="t3-fill">{course_title}</td></tr>
            <tr><td class="label">Dates of Training</td><td class="colon">:</td><td class="t3-fill">{day_label}</td></tr>
          </table>
          <table class="attendance-table">
            <thead>
              <tr>
                <th style="width:4%">No.</th><th>Name of Trainee(s)</th><th>Name of Employer(s)</th>
                <th style="width:14%">NRIC</th><th style="width:11%">Citizenship</th>
                <th style="width:7%">Sex</th><th style="width:16%">Signature*</th>
              </tr>
            </thead>
            <tbody>{participant_rows}{blank_rows}</tbody>
          </table>
          <p style="font-weight:700;margin:0 0 16px">I certify that all trainees listed above had fully attended the training.</p>
          <table class="cert-block">
            <tr>
              <td style="width:48%;vertical-align:top;padding:0">
                <table class="cert-table">
                  <tr><td class="label">NAME</td><td class="colon">:</td><td class="t3-fill">&nbsp;</td></tr>
                  <tr><td class="label">DESIGNATION</td><td class="colon">:</td><td class="t3-fill">&nbsp;</td></tr>
                  <tr>
                    <td class="label" style="vertical-align:top">TRAINING<br>PROVIDER'S<br>STAMP</td>
                    <td class="colon" style="vertical-align:top">:</td>
                    <td><div class="stamp-box"></div></td>
                  </tr>
                </table>
              </td>
              <td style="width:4%;padding:0"></td>
              <td style="width:48%;vertical-align:top;padding:0">
                <table class="cert-table">
                  <tr><td class="label">SIGNATURE</td><td class="colon">:</td><td class="t3-fill">&nbsp;</td></tr>
                  <tr><td class="label">DATE</td><td class="colon">:</td><td class="t3-fill">&nbsp;</td></tr>
                </table>
              </td>
            </tr>
          </table>
          <p class="note">* Note: 1. Please make a separate attachment if more space is required</p>
          <p class="note">&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;2. This attendance list must be prepared on daily basis and signed by the trainee in each column of the relevant date of training if he/she had attended the programme on that day</p>
        </div>
        """)
    pages_html = "".join(pages)

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  /* Root font-size mirrors html {{ font-size: 15px }} in static/css/style.css —
     every rem value below is translated from that site's rem values, not the
     browser-default 16px, so spacing lines up with the real screen/print version. */
  body {{ font-family: Arial, Helvetica, sans-serif; font-size: 13.8px; color: #1a1a1a; margin: 0; padding: 0; }}
  table {{ border-collapse: collapse; width: 100%; }}
  .t3-page {{ position: relative; padding: 16px 14px; }}

  /* .t3-header-row: flexbox row of three boxes, matching the on-screen
     d-flex .t3-header-row markup (gap-3 = 15px, mb-4 = 22px). */
  .t3-header-row {{ display: flex; justify-content: space-between; align-items: stretch;
                     gap: 15px; margin-bottom: 22px; }}
  .t3-box {{ border: 1px solid #333; padding: 9px 13px; display: flex; align-items: center;
             justify-content: center; text-align: center; font-weight: 700; }}
  .t3-box-first {{ min-width: 170px; }}
  .t3-box-last {{ max-width: 280px; font-weight: 400; font-size: 12px; }}
  .t3-header-title {{ flex-grow: 1; display: flex; align-items: center; justify-content: center; text-align: center; }}
  .t3-header-title span {{ font-size: 18.75px; font-weight: 700; }}

  /* Course Title / Dates of Training and NAME/SIGNATURE blocks: borderless
     info tables with a single underline (.t3-fill) on the value cell. */
  .info-table {{ margin-bottom: 22px; }}
  .info-table td {{ padding: 4px 6px; vertical-align: middle; }}
  .info-table td.label {{ white-space: nowrap; font-weight: 600; width: 140px; }}
  .info-table td.colon {{ white-space: nowrap; width: 16px; }}
  .info-table td.align-top {{ vertical-align: top; padding-top: 8px; }}
  .info-table .sub {{ font-size: 11px; color: #666; }}
  .t3-fill {{ border-bottom: 1px solid #333; }}

  /* Certification block (NAME/DESIGNATION/STAMP + SIGNATURE/DATE): two
     side-by-side tables with a real gap between them, rather than one wide
     table, so the two column groups read as clearly separate — and each
     row gets extra top padding so consecutive underlines don't crowd into
     what looks like one merged line. */
  .cert-block {{ table-layout: fixed; margin-bottom: 20px; }}
  .cert-table {{ width: 100%; table-layout: fixed; }}
  .cert-table td {{ padding: 6px 6px; vertical-align: middle; }}
  .cert-table tr + tr td {{ padding-top: 18px; }}
  .cert-table td.label {{ white-space: nowrap; font-weight: 600; width: 130px; padding-right: 4px; }}
  .cert-table td.colon {{ white-space: nowrap; width: 14px; }}
  .stamp-box {{ border: 1px dashed #999; border-radius: 3px; height: 60px; width: 100%; }}

  /* Attendance table: bordered #333 throughout (not the lighter #999 the
     earlier version used), uppercase muted header text with no shaded
     background (the real .table thead th rule has none), 33px-tall rows. */
  .attendance-table {{ margin-bottom: 22px; }}
  .attendance-table th, .attendance-table td {{ border: 1px solid #333; padding: 4px 6px;
                                                  text-align: center; vertical-align: middle; }}
  .attendance-table th {{ font-size: 11.7px; font-weight: 700; text-transform: uppercase;
                           letter-spacing: 0.03em; color: #6b7280; }}
  .attendance-table td {{ height: 33px; text-align: left; }}
  /* No./Sex columns override to centered via their own inline style, which
     wins over this class rule — matching the two text-center cells in the
     real template. */

  .note {{ font-size: 11px; font-style: italic; margin: 0 0 3px; }}

  .code-stamp {{ position: absolute; top: 6px; right: 9px; font-size: 10px; letter-spacing: 0.08em;
                  color: #9aa1ab; font-family: "Courier New", monospace; }}
</style></head>
<body>{pages_html}</body></html>"""


def generate_t3_form_pdf(session_row, participants, training_days, extra_blank_rows=0):
    """Returns portrait A4 PDF bytes for the printable T3 (PSMB/SBL-KHAS/T3/01)
    Attendance List — used to email the current form straight to the
    trainer when the client hasn't filled the online version, so they can
    print it and get it signed manually (see sessions.email_t3_form).
    extra_blank_rows adds extra empty rows on top of the usual minimum, for
    last-minute walk-in participants to fill in by hand."""
    html = _build_t3_form_html(session_row, participants, training_days, extra_blank_rows=extra_blank_rows)
    with tempfile.NamedTemporaryFile(suffix=".html", mode="w", encoding="utf-8", delete=False) as html_file:
        html_file.write(html)
        html_path = html_file.name
    pdf_path = html_path.replace(".html", ".pdf")
    try:
        result = subprocess.run(
            ["wkhtmltopdf", "--page-size", "A4",
             "--margin-top", "10mm", "--margin-bottom", "10mm",
             "--margin-left", "10mm", "--margin-right", "10mm",
             html_path, pdf_path],
            check=True, timeout=30, capture_output=True, text=True,
        )
        if result.stderr:
            current_app.logger.info("wkhtmltopdf T3 form render stderr: %s", result.stderr.strip())
        with open(pdf_path, "rb") as f:
            return f.read()
    finally:
        for path in (html_path, pdf_path):
            try:
                os.remove(path)
            except OSError:
                pass


def generate_po_pdf(po, items, grand_total):
    """Returns PDF bytes for the given purchase order (sqlite Row), items, and grand_total."""
    html = _build_html(po, items, grand_total)
    with tempfile.NamedTemporaryFile(suffix=".html", mode="w", encoding="utf-8", delete=False) as html_file:
        html_file.write(html)
        html_path = html_file.name
    pdf_path = html_path.replace(".html", ".pdf")
    try:
        subprocess.run(
            ["wkhtmltopdf", "--quiet", "--page-size", "A4", "--margin-top", "10mm",
             "--margin-bottom", "10mm", html_path, pdf_path],
            check=True, timeout=30, capture_output=True,
        )
        with open(pdf_path, "rb") as f:
            return f.read()
    finally:
        for path in (html_path, pdf_path):
            try:
                os.remove(path)
            except OSError:
                pass


def _build_vendor_po_html(po, items, grand_total):
    """Vendor Purchase Orders have their own (simpler) layout — 'Issued To'
    is a Vendor rather than a Trainer, and the class/date/venue block is
    optional since a vendor PO doesn't have to be tied to any one class."""
    logo_uri = _logo_data_uri()

    class_block = ""
    if po["course_title"]:
        dates = _fmtdaterange(po["start_date"], po["end_date"]) if po["start_date"] else "-"
        class_block = f"""
        <table>
          <thead><tr><th>For Class</th><th>Date(s)</th><th>Venue</th></tr></thead>
          <tbody><tr>
            <td>{po['course_title']}</td><td>{dates}</td><td>{po['venue'] or '-'}</td>
          </tr></tbody>
        </table>"""

    item_row_parts = []
    for item in items:
        qty_note = ""
        if item["quantity"] != 1:
            qty_note = (f" <span style='color:#666'>&times; {item['quantity']} @ "
                        f"{po['currency']} {item['unit_price']:,.2f}</span>")
        item_row_parts.append(
            f"<tr><td colspan='2'>{item['description']}{qty_note}</td>"
            f"<td style='text-align:right'>{po['currency']} {item['amount']:,.2f}</td></tr>"
        )
    item_rows = "".join(item_row_parts)

    small_style = "font-size:9px;line-height:1.5"
    terms_html = (
        f"<div style='margin-top:16px'><div style='font-size:11px;color:#666;text-transform:uppercase'>"
        f"Terms &amp; Conditions</div><div style='{small_style}'>{_linelist_html(po['terms'])}</div></div>"
    ) if po["terms"] else ""

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
  body {{ font-family: Arial, Helvetica, sans-serif; font-size: 13px; color: #1a1a1a; margin: 32px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
  th, td {{ padding: 8px 6px; border-bottom: 1px solid #eee; text-align: left; }}
  th {{ font-size: 11px; text-transform: uppercase; color: #666; }}
  .muted {{ color: #666; }}
  img.logo {{ width: 76px; height: auto; display: block; margin-bottom: 10px; }}
</style></head>
<body>
  <table style="border:none;margin-top:0"><tr style="border:none">
    <td style="border:none;width:60%">
      <img class="logo" src="{logo_uri}">
      <strong>Modoku Tech Sdn Bhd (1390352-H)</strong><br>
      <span class="muted">Level 30, Menara Prestige<br>1, Jalan Pinang<br>50450 Kuala Lumpur</span><br>
      <span class="muted">hello@modoku.tech</span>
    </td>
    <td style="border:none;text-align:right;vertical-align:top">
      <h2 style="margin:0">PURCHASE ORDER</h2>
      <div class="muted">{po['po_no']}</div>
    </td>
  </tr></table>

  <table style="border:none">
    <tr style="border:none">
      <td style="border:none;width:50%">
        <div class="muted" style="font-size:11px;text-transform:uppercase">Issued To (Vendor)</div>
        <strong>{po['vendor_name'] or 'Vendor removed'}</strong><br>
        {po['vendor_email'] or ''}<br>{po['vendor_phone'] or ''}
      </td>
      <td style="border:none;text-align:right">
        <span class="muted">Issue date:</span> {_fmtdate(po['issue_date'])}<br>
        <span class="muted">Status:</span> {po['status']}
      </td>
    </tr>
  </table>

  {class_block}

  <table>
    <thead><tr><th colspan="2">Description</th><th style="text-align:right">Amount</th></tr></thead>
    <tbody>
      <tr><td colspan="2">{po['description'] or '-'}</td><td style="text-align:right">{po['currency']} {po['fee_amount']:,.2f}</td></tr>
      {item_rows}
      <tr style="font-weight:700"><td colspan="2" style="text-align:right">Total</td>
          <td style="text-align:right">{po['currency']} {grand_total:,.2f}</td></tr>
    </tbody>
  </table>

  {terms_html}
</body></html>"""


def generate_vendor_po_pdf(po, items, grand_total):
    """Returns PDF bytes for the given Vendor Purchase Order (sqlite Row), items, and grand_total."""
    html = _build_vendor_po_html(po, items, grand_total)
    with tempfile.NamedTemporaryFile(suffix=".html", mode="w", encoding="utf-8", delete=False) as html_file:
        html_file.write(html)
        html_path = html_file.name
    pdf_path = html_path.replace(".html", ".pdf")
    try:
        subprocess.run(
            ["wkhtmltopdf", "--quiet", "--page-size", "A4", "--margin-top", "10mm",
             "--margin-bottom", "10mm", html_path, pdf_path],
            check=True, timeout=30, capture_output=True,
        )
        with open(pdf_path, "rb") as f:
            return f.read()
    finally:
        for path in (html_path, pdf_path):
            try:
                os.remove(path)
            except OSError:
                pass
