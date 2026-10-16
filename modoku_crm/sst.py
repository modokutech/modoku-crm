"""SST (Fix186): the two-monthly service tax return, worked out from the money
actually received.

Taxable periods run Jan-Feb, Mar-Apr, May-Jun, Jul-Aug, Sep-Oct, Nov-Dec. Each
payment recorded against an invoice (invoice_payments.py) carries its share of
that invoice's SST and counts in the period of the date it was received; the
return and payment are due by the last day of the month after the period.

Reminders go to every admin (in-app) and the Settings notification emails:
when a period closes (with the amount), a week before the due date, on the due
date, and once if it's overdue - until the period is marked as paid here.
Periods whose due date was before this module started tracking
(settings 'sst_tracking_since') are shown but never chased.
"""
import calendar
import os
import time
import uuid
from datetime import date, timedelta

from flask import (Blueprint, current_app, flash, g, redirect, render_template, request, send_from_directory,
                   url_for)
from werkzeug.utils import secure_filename

from . import activity, db, fmtdate, invoice_payments, mailer, notifications, parse_money, uploadutil
from . import settings as settings_module
from .auth import admin_required
from .csvutil import table_response

bp = Blueprint("sst", __name__, url_prefix="/sst")

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
REMINDER_DAYS_BEFORE = 7


# --- Periods -------------------------------------------------------------

def _last_day(year, month):
    return date(year, month, calendar.monthrange(year, month)[1])


def period_start(d):
    return date(d.year, d.month if d.month % 2 == 1 else d.month - 1, 1)


def period_end(start):
    return _last_day(start.year, start.month + 1)


def due_date(start):
    end = period_end(start)
    nxt = end + timedelta(days=1)
    return _last_day(nxt.year, nxt.month)


def period_key(start):
    return start.strftime("%Y-%m")


def period_label(start):
    return f"{_MONTHS[start.month - 1]}–{_MONTHS[start.month]} {start.year}"


def _from_key(key):
    try:
        y, m = (int(x) for x in key.split("-"))
        d = date(y, m, 1)
    except (ValueError, AttributeError):
        return None
    return d if d.month % 2 == 1 else None


def tracking_since():
    value = db.get_setting("sst_tracking_since")
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return date.today()


# --- Data ----------------------------------------------------------------

def payment_rows():
    """Every payment against a (non-cancelled) invoice that carries SST, with
    its SST share and the value excluding SST."""
    invoices = db.query(
        """SELECT i.*, co.name AS company_name FROM invoices i
           LEFT JOIN companies co ON co.id = i.company_id
           WHERE i.status != 'Cancelled' AND i.sst_amount > 0
             AND EXISTS (SELECT 1 FROM invoice_payments ip WHERE ip.invoice_id = i.id)"""
    )
    rows = []
    for inv in invoices:
        s = invoice_payments.summary(inv)
        for p in s["payments"]:
            rows.append({
                "payment_id": p["id"], "invoice_id": inv["id"], "invoice_no": inv["invoice_no"],
                "client": inv["company_name"] or inv["bill_to_name"], "invoice_date": inv["invoice_date"],
                "paid_on": p["paid_on"], "amount": p["amount"], "sst": p["sst_share"],
                "taxable": round(p["amount"] - p["sst_share"], 2), "note": p["note"], "kind": p["kind"],
                "sst_rate": inv["sst_rate"],
            })
    rows.sort(key=lambda r: (r["paid_on"] or "", r["invoice_no"]))
    return rows


def _returns():
    return {r["period_start"]: r for r in db.query("SELECT * FROM sst_returns")}


def _status(start, ret, today, since):
    if ret is not None:
        return "paid"
    if today <= period_end(start):
        return "open"
    if due_date(start) < since:
        return "before"
    if today <= due_date(start):
        return "due"
    return "overdue"


def overview():
    """All periods from the first payment (or the current one) up to now,
    newest first, plus the payments still waiting for a date."""
    today = date.today()
    since = tracking_since()
    rows = payment_rows()
    undated = [r for r in rows if not r["paid_on"]]
    by_key = {}
    for r in rows:
        if r["paid_on"]:
            by_key.setdefault(period_key(period_start(date.fromisoformat(r["paid_on"]))), []).append(r)
    returns = _returns()
    first = period_start(today)
    for k in list(by_key) + list(returns):
        d = _from_key(k)
        if d and d < first:
            first = d
    periods = []
    start = first
    current = period_start(today)
    while start <= current:
        k = period_key(start)
        items = by_key.get(k, [])
        ret = returns.get(k)
        sst = round(sum(r["sst"] for r in items), 2)
        periods.append({
            "key": k, "start": start, "end": period_end(start), "due": due_date(start),
            "label": period_label(start), "payments": items, "count": len(items),
            "invoices": len({r["invoice_id"] for r in items}),
            "taxable": round(sum(r["taxable"] for r in items), 2), "sst": sst,
            "ret": ret, "status": _status(start, ret, today, since),
            "days_left": (period_end(start) - today).days, "days_to_due": (due_date(start) - today).days,
        })
        start = date(start.year + (start.month + 2 > 12), (start.month + 1) % 12 + 1, 1)
    periods.reverse()
    return periods, undated


def unpaid_with_sst():
    """Invoices with SST that still have money outstanding - they'll count in
    whichever period they're paid."""
    out = []
    for inv in db.query("SELECT * FROM invoices WHERE status NOT IN ('Cancelled', 'Draft') AND sst_amount > 0"):
        s = invoice_payments.summary(inv)
        if s["outstanding"] > invoice_payments.EPS:
            ratio = (inv["sst_amount"] / s["gross"]) if s["gross"] else 0
            out.append({"invoice_id": inv["id"], "invoice_no": inv["invoice_no"], "client": inv["bill_to_name"],
                        "outstanding": s["outstanding"], "sst": round(s["outstanding"] * ratio, 2),
                        "status": inv["status"]})
    return out


def _period(key):
    for p in overview()[0]:
        if p["key"] == key:
            return p
    return None


def attention_entries():
    """For the dashboard's Needs Your Attention panel."""
    periods, undated = overview()
    out = []
    for p in periods:
        if p["status"] in ("due", "overdue"):
            when = "overdue" if p["status"] == "overdue" else f"due by {fmtdate(p['due'].isoformat())}"
            out.append({"text": f"SST {p['label']}: RM {p['sst']:,.2f}, {when}",
                        "link": url_for("sst.period", key=p["key"])})
    if undated:
        out.append({"text": f"{len(undated)} invoice payment(s) need a date for SST",
                    "link": url_for("sst.index")})
    return out


# --- Reminders -------------------------------------------------------------

_last_check = {"t": 0.0}


def _send_reminder(p, kind, title, body):
    dedupe = f"sst:{p['key']}:{kind}"
    if db.query("SELECT 1 FROM notifications WHERE dedupe_key = ? LIMIT 1", (dedupe,), one=True):
        return
    link = url_for("sst.period", key=p["key"])
    notifications.notify_admins("sst_reminder", title, body=body, link=link, dedupe_key=dedupe)
    to = ", ".join(settings_module.get_notification_emails())
    if to:
        try:
            mailer.send_email(to, title, body + f"\n\nDetails and the payment list:\n{url_for('sst.period', key=p['key'], _external=True)}")
        except Exception:  # noqa: BLE001 - the in-app reminder is already saved
            current_app.logger.exception("SST reminder email failed for %s", p["key"])


def check_reminders(force=False):
    """Runs at most every 10 minutes per worker (on any request)."""
    now = time.time()
    if not force and now - _last_check["t"] < 600:
        return
    _last_check["t"] = now
    today = date.today()
    periods, _ = overview()
    for p in periods:
        if p["status"] not in ("due", "overdue"):
            continue
        amount = f"RM {p['sst']:,.2f}"
        due = fmtdate(p["due"].isoformat())
        summary = (f"{p['count']} payment(s) received in {p['label']}, SST {amount}. Pay and submit the SST return "
                   f"by {due}, then mark it as paid in Modoku Hub (SST page) to stop these reminders.")
        _send_reminder(p, "closed", f"SST to pay for {p['label']}: {amount} (due {due})", summary)
        if p["status"] == "due" and p["days_to_due"] <= REMINDER_DAYS_BEFORE:
            _send_reminder(p, "week", f"SST due in {p['days_to_due']} day(s): {p['label']}, {amount}", summary)
        if p["status"] == "due" and p["due"] == today:
            _send_reminder(p, "today", f"SST due today: {p['label']}, {amount}", summary)
        if p["status"] == "overdue":
            _send_reminder(p, "overdue", f"SST overdue: {p['label']}, {amount} (was due {due})", summary)


@bp.before_app_request
def _sst_reminders():
    try:
        check_reminders()
    except Exception:  # noqa: BLE001 - never let this housekeeping break a request
        current_app.logger.exception("SST reminder check failed")


# --- Routes ----------------------------------------------------------------

def _receipt_dir():
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "sst")
    os.makedirs(path, exist_ok=True)
    return path


@bp.route("/")
@admin_required
def index():
    periods, undated = overview()
    current = next((p for p in periods if p["status"] == "open"), None)
    action = [p for p in periods if p["status"] in ("due", "overdue")]
    return render_template("sst/index.html", periods=periods, undated=undated, current=current, action=action,
                           upcoming=unpaid_with_sst(), today=date.today())


@bp.route("/<key>")
@admin_required
def period(key):
    p = _period(key)
    if p is None:
        flash("That SST period wasn't found.", "warning")
        return redirect(url_for("sst.index"))
    return render_template("sst/period.html", p=p, today=date.today(), undated=overview()[1])


@bp.route("/<key>/export.xlsx")
@admin_required
def export(key):
    p = _period(key)
    if p is None:
        return redirect(url_for("sst.index"))
    header = ["Invoice No", "Client", "Invoice Date", "Date Received", "Amount Received (RM)",
              "Value excl. SST (RM)", "SST (RM)", "SST Rate %", "Note"]
    rows = [[r["invoice_no"], r["client"], r["invoice_date"], r["paid_on"], float(r["amount"]),
             float(r["taxable"]), float(r["sst"]), r["sst_rate"], r["note"] or ""] for r in p["payments"]]
    rows.append(["TOTAL", "", "", "", float(round(sum(r["amount"] for r in p["payments"]), 2)),
                 float(p["taxable"]), float(p["sst"]), "", ""])
    return table_response(f"SST {p['label'].replace('–', '-')}", header, rows, fmt="xlsx")


@bp.route("/<key>/paid", methods=("POST",))
@admin_required
def mark_paid(key):
    p = _period(key)
    if p is None:
        return redirect(url_for("sst.index"))
    if p["ret"] is not None:
        flash("This period is already marked as paid.", "info")
        return redirect(url_for("sst.period", key=key))
    paid_on = invoice_payments.parse_date(request.form.get("paid_on"))
    try:
        amount = parse_money(request.form.get("amount"), default=None)
    except ValueError:
        amount = None
    if not paid_on or amount is None or amount < 0:
        flash("Enter the amount paid and the date (today or earlier).", "danger")
        return redirect(url_for("sst.period", key=key))
    receipt = None
    file_storage = request.files.get("receipt")
    if file_storage and file_storage.filename:
        error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.DEFAULT_EXTENSIONS)
        if error:
            flash(error, "danger")
            return redirect(url_for("sst.period", key=key))
        receipt = f"sst_{key}_{uuid.uuid4().hex[:8]}_{secure_filename(file_storage.filename)}"
        file_storage.save(os.path.join(_receipt_dir(), receipt))
    db.execute(
        """INSERT INTO sst_returns (period_start, amount_paid, paid_on, reference, receipt_file, notes, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (key, round(amount, 2), paid_on, (request.form.get("reference") or "").strip() or None, receipt,
         (request.form.get("notes") or "").strip() or None, g.user["id"] if g.user else None),
    )
    activity.log("update", "sst_return", None, f"Marked SST {p['label']} as paid: RM {amount:,.2f} on {paid_on}")
    flash(f"SST for {p['label']} marked as paid. Reminders for it have stopped.", "success")
    return redirect(url_for("sst.period", key=key))


@bp.route("/<key>/unpaid", methods=("POST",))
@admin_required
def mark_unpaid(key):
    p = _period(key)
    if p is None or p["ret"] is None:
        return redirect(url_for("sst.index"))
    db.execute("DELETE FROM sst_returns WHERE period_start = ?", (key,))
    activity.log("update", "sst_return", None, f"Undid 'paid' on SST {p['label']}")
    flash(f"SST for {p['label']} is no longer marked as paid.", "info")
    return redirect(url_for("sst.period", key=key))


@bp.route("/<key>/receipt")
@admin_required
def receipt(key):
    p = _period(key)
    if p is None or p["ret"] is None or not p["ret"]["receipt_file"]:
        return redirect(url_for("sst.index"))
    return send_from_directory(_receipt_dir(), p["ret"]["receipt_file"])
