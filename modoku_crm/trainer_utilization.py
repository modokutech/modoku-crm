"""Trainer Utilization — for each trainer, how many training days they've
delivered and how much they've been paid, computed live from Purchase Orders
that have reached 'Confirmed' status (the class is booked and the trainer has
agreed to the PO), joined to that PO's class session for the training dates.

Nothing is separately logged into its own table — a Confirmed PO already
carries everything needed (fee + extra items = amount paid, the session's
date range = training days delivered), so aggregating on the fly here means
these numbers can never drift out of sync with the POs themselves. If a PO is
later un-confirmed/cancelled, or its fee is edited, this page reflects that
automatically the next time it's viewed — there's no separate record that
could go stale.

"Day" (rather than "Hours") matches how Modoku actually counts training
delivery — a man-day count, not a hours-worked total — and the By Month
breakdown buckets on the session's actual training start date rather than
the PO's issue date, since that's the date the utilization actually happened.
"""
from datetime import date, datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for

from . import db, fmtdaterange
from .auth import admin_required, login_required
from .csvutil import csv_response

bp = Blueprint("trainer_utilization", __name__, url_prefix="/trainer-utilization")

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _month_label(ym):
    """'2026-08' -> 'August 2026'."""
    try:
        return datetime.strptime(ym + "-01", "%Y-%m-%d").strftime("%B %Y")
    except (ValueError, TypeError):
        return ym


def _session_days(start_date, end_date):
    """Training-day count for one session — the number of calendar days from
    start_date to end_date inclusive (Modoku counts utilization in man-days,
    not hours). Returns 1 if dates are missing/unparseable rather than 0, so
    a session with an incomplete date still counts as at least one day."""
    if not start_date:
        return 1
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else start
        if end < start:
            end = start
        return (end - start).days + 1
    except (ValueError, TypeError):
        return 1


def _confirmed_po_rows():
    """Every Confirmed PO with its trainer + session info and total amount
    (fee + itemized extras), one row per PO, newest class first."""
    pos = db.query(
        """SELECT po.id, po.po_no, po.trainer_id, po.fee_amount, po.currency, po.issue_date,
                  t.name AS trainer_name, t.email AS trainer_email,
                  cs.id AS session_id, cs.start_date, cs.end_date,
                  c.title AS course_title
           FROM purchase_orders po
           JOIN trainers t ON t.id = po.trainer_id
           JOIN course_sessions cs ON cs.id = po.session_id
           JOIN courses c ON c.id = cs.course_id
           WHERE po.status = 'Confirmed'
           ORDER BY cs.start_date DESC"""
    )
    item_totals = {
        row["po_id"]: (row["total"] or 0)
        for row in db.query("SELECT po_id, SUM(amount) AS total FROM po_items GROUP BY po_id")
    }

    result = []
    for po in pos:
        amount = round((po["fee_amount"] or 0) + item_totals.get(po["id"], 0), 2)
        days = _session_days(po["start_date"], po["end_date"])
        row = dict(po)
        row["amount"] = amount
        row["days"] = days
        result.append(row)
    return result


def _aggregate_by_trainer(rows, sort="amount", direction="desc"):
    by_trainer = {}
    for r in rows:
        t = by_trainer.setdefault(r["trainer_id"], {
            "trainer_id": r["trainer_id"],
            "trainer_name": r["trainer_name"],
            "trainer_email": r["trainer_email"],
            "class_count": 0,
            "total_days": 0,
            "total_amount": 0.0,
        })
        t["class_count"] += 1
        t["total_days"] += r["days"]
        t["total_amount"] += r["amount"]
    key_func = {"days": lambda t: t["total_days"], "amount": lambda t: t["total_amount"]}.get(
        sort, lambda t: t["total_amount"])
    return sorted(by_trainer.values(), key=key_func, reverse=(direction != "asc"))


def _aggregate_by_month(rows, sort="month", direction="desc"):
    by_month = {}
    for r in rows:
        ym = (r["start_date"] or "")[:7] or "Unknown"
        m = by_month.setdefault(ym, {"ym": ym, "class_count": 0, "total_days": 0, "total_amount": 0.0})
        m["class_count"] += 1
        m["total_days"] += r["days"]
        m["total_amount"] += r["amount"]
    key_func = {
        "days": lambda m: m["total_days"],
        "amount": lambda m: m["total_amount"],
        "month": lambda m: m["ym"],
    }.get(sort, lambda m: m["ym"])
    return sorted(by_month.values(), key=key_func, reverse=(direction != "asc"))


def _available_years():
    rows = db.query(
        """SELECT DISTINCT substr(cs.start_date, 1, 4) AS yr
           FROM purchase_orders po JOIN course_sessions cs ON cs.id = po.session_id
           WHERE po.status = 'Confirmed' AND cs.start_date IS NOT NULL"""
    )
    years = {int(r["yr"]) for r in rows if r["yr"] and r["yr"].isdigit()}
    years.add(date.today().year)
    return sorted(years, reverse=True)


def _yearly_pivot(year):
    """All trainers (even ones with no activity this year) on the left, with
    a Day/Amount pair for each of Jan-Dec plus a YTD Day/Amount total on the
    right — mirrors the layout of the reference spreadsheet, scoped to just
    Day/Amount/YTD (the spreadsheet's extra MUPM/Utilize/Pending/+- columns
    track separate internal targets this app has no data source for, so
    they're intentionally left out here)."""
    all_trainers = db.query("SELECT id, name FROM trainers ORDER BY name COLLATE NOCASE")
    rows = [r for r in _confirmed_po_rows() if (r["start_date"] or "")[:4] == str(year)]

    by_trainer_month = {}
    for r in rows:
        month = int(r["start_date"][5:7])
        cell = by_trainer_month.setdefault(r["trainer_id"], {}).setdefault(month, {"days": 0, "amount": 0.0})
        cell["days"] += r["days"]
        cell["amount"] += r["amount"]

    pivot = []
    for t in all_trainers:
        months = []
        ytd_days = 0
        ytd_amount = 0.0
        for m in range(1, 13):
            cell = by_trainer_month.get(t["id"], {}).get(m, {"days": 0, "amount": 0.0})
            months.append(cell)
            ytd_days += cell["days"]
            ytd_amount += cell["amount"]
        pivot.append({
            "trainer_id": t["id"],
            "trainer_name": t["name"],
            "months": months,
            "ytd_days": ytd_days,
            "ytd_amount": ytd_amount,
        })
    return pivot


def _sort_params(default_sort):
    sort = request.args.get("sort", default_sort)
    direction = request.args.get("dir", "desc")
    if sort not in ("amount", "days", "month"):
        sort = default_sort
    if direction not in ("asc", "desc"):
        direction = "desc"
    return sort, direction


@bp.route("/")
@login_required
def index():
    tsort, tdir = _sort_params("amount")
    msort = request.args.get("msort", "month")
    mdir = request.args.get("mdir", "desc")
    if msort not in ("amount", "days", "month"):
        msort = "month"
    if mdir not in ("asc", "desc"):
        mdir = "desc"

    rows = _confirmed_po_rows()
    trainers = _aggregate_by_trainer(rows, tsort, tdir)
    by_month = _aggregate_by_month(rows, msort, mdir)
    for m in by_month:
        m["label"] = _month_label(m["ym"])
    grand_days = sum(t["total_days"] for t in trainers)
    grand_amount = sum(t["total_amount"] for t in trainers)
    return render_template(
        "trainer_utilization/index.html", trainers=trainers, by_month=by_month,
        grand_days=grand_days, grand_amount=grand_amount, grand_classes=len(rows),
        tsort=tsort, tdir=tdir, msort=msort, mdir=mdir,
    )


@bp.route("/export")
@admin_required
def export():
    tsort, tdir = _sort_params("amount")
    trainers = _aggregate_by_trainer(_confirmed_po_rows(), tsort, tdir)
    rows = (
        (t["trainer_name"], t["class_count"], t["total_days"], t["total_amount"])
        for t in trainers
    )
    return csv_response(
        "trainer_utilization.csv",
        ["Trainer", "Confirmed Classes", "Total Days", "Total Paid (RM)"],
        rows,
    )


@bp.route("/yearly")
@login_required
def yearly():
    year = request.args.get("year", type=int) or date.today().year
    pivot = _yearly_pivot(year)
    return render_template(
        "trainer_utilization/yearly.html", pivot=pivot, year=year,
        month_names=MONTH_NAMES, available_years=_available_years(),
    )


@bp.route("/yearly/export")
@admin_required
def yearly_export():
    year = request.args.get("year", type=int) or date.today().year
    pivot = _yearly_pivot(year)
    header = ["Trainer"]
    for name in MONTH_NAMES:
        header += [f"{name} Day", f"{name} Amt"]
    header += ["YTD Day", "YTD Amt"]

    def _rows():
        for t in pivot:
            row = [t["trainer_name"]]
            for cell in t["months"]:
                row += [cell["days"], cell["amount"]]
            row += [t["ytd_days"], t["ytd_amount"]]
            yield row

    return csv_response(f"trainer_utilization_{year}.csv", header, _rows())


def _class_trainer_matrix(ym):
    """Classes scheduled this month (with at least one Confirmed Purchase
    Order — same 'Confirmed' meaning used everywhere else on this page)
    down the rows, every trainer across the columns, cell = the date(s) of
    that class if this trainer is assigned to it, blank otherwise."""
    sessions = db.query(
        """SELECT DISTINCT cs.id, cs.session_code, cs.start_date, cs.end_date, c.title AS course_title
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           JOIN purchase_orders po ON po.session_id = cs.id AND po.status = 'Confirmed'
           WHERE substr(cs.start_date, 1, 7) = ?
           ORDER BY cs.start_date, cs.id""",
        (ym,),
    )
    trainers = db.query("SELECT id, name FROM trainers ORDER BY name COLLATE NOCASE")

    assignments = db.query(
        """SELECT st.session_id, st.trainer_id FROM session_trainers st
           JOIN course_sessions cs ON cs.id = st.session_id
           WHERE substr(cs.start_date, 1, 7) = ?""",
        (ym,),
    )
    by_session = {}
    for row in assignments:
        by_session.setdefault(row["session_id"], set()).add(row["trainer_id"])

    rows = []
    for s in sessions:
        assigned_ids = by_session.get(s["id"], set())
        date_label = fmtdaterange(s["start_date"], s["end_date"])
        cells = [date_label if t["id"] in assigned_ids else None for t in trainers]
        rows.append({"session": s, "cells": cells})
    return trainers, rows


@bp.route("/matrix")
@login_required
def matrix():
    today = date.today()
    ym = request.args.get("month") or today.strftime("%Y-%m")
    trainers, rows = _class_trainer_matrix(ym)
    month_label = _month_label(ym)
    return render_template(
        "trainer_utilization/matrix.html", trainers=trainers, rows=rows,
        ym=ym, month_label=month_label,
    )


@bp.route("/<int:trainer_id>")
@login_required
def detail(trainer_id):
    trainer = db.query("SELECT * FROM trainers WHERE id = ?", (trainer_id,), one=True)
    if trainer is None:
        flash("Trainer not found.", "danger")
        return redirect(url_for("trainer_utilization.index"))
    rows = [r for r in _confirmed_po_rows() if r["trainer_id"] == trainer_id]
    total_days = sum(r["days"] for r in rows)
    total_amount = sum(r["amount"] for r in rows)
    return render_template(
        "trainer_utilization/detail.html", trainer=trainer, rows=rows,
        total_days=total_days, total_amount=total_amount,
    )
