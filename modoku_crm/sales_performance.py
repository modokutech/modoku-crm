"""Sales Performance — for each salesperson, how many invoices they've
raised and how much revenue those invoices total, computed live from
Invoices (excluding Cancelled ones), bucketed by the invoice's own date.

Mirrors trainer_utilization.py's architecture on purpose (by-salesperson and
by-month breakdowns, a sortable index, a yearly Jan-Dec pivot) so the two
modules feel consistent, but sources from Invoices.created_by/invoice_date
instead of Purchase Orders — invoices are the actual billed revenue, the
closest equivalent here to a Confirmed PO's "amount paid".
"""
from datetime import date, datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for

from . import db
from .auth import admin_required, login_required
from .csvutil import csv_response

bp = Blueprint("sales_performance", __name__, url_prefix="/sales-performance")

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _month_label(ym):
    try:
        return datetime.strptime(ym + "-01", "%Y-%m-%d").strftime("%B %Y")
    except (ValueError, TypeError):
        return ym


def _billed_invoice_rows():
    """Every non-Cancelled invoice with its creator (salesperson) info, one
    row per invoice, newest first."""
    return db.query(
        """SELECT i.id, i.invoice_no, i.invoice_date, i.total, i.status, i.created_by,
                  u.name AS salesperson_name
           FROM invoices i
           LEFT JOIN users u ON u.id = i.created_by
           WHERE i.status != 'Cancelled'
           ORDER BY i.invoice_date DESC"""
    )


def _aggregate_by_salesperson(rows, sort="amount", direction="desc"):
    by_person = {}
    for r in rows:
        key = r["created_by"] or 0
        p = by_person.setdefault(key, {
            "salesperson_id": r["created_by"],
            "salesperson_name": r["salesperson_name"] or "Unassigned",
            "invoice_count": 0,
            "total_amount": 0.0,
        })
        p["invoice_count"] += 1
        p["total_amount"] += r["total"] or 0
    key_func = {"invoices": lambda p: p["invoice_count"], "amount": lambda p: p["total_amount"]}.get(
        sort, lambda p: p["total_amount"])
    return sorted(by_person.values(), key=key_func, reverse=(direction != "asc"))


def _aggregate_by_month(rows, sort="month", direction="desc"):
    by_month = {}
    for r in rows:
        ym = (r["invoice_date"] or "")[:7] or "Unknown"
        m = by_month.setdefault(ym, {"ym": ym, "invoice_count": 0, "total_amount": 0.0})
        m["invoice_count"] += 1
        m["total_amount"] += r["total"] or 0
    key_func = {
        "invoices": lambda m: m["invoice_count"],
        "amount": lambda m: m["total_amount"],
        "month": lambda m: m["ym"],
    }.get(sort, lambda m: m["ym"])
    return sorted(by_month.values(), key=key_func, reverse=(direction != "asc"))


def _available_years():
    rows = db.query(
        "SELECT DISTINCT substr(invoice_date, 1, 4) AS yr FROM invoices WHERE status != 'Cancelled'"
    )
    years = {int(r["yr"]) for r in rows if r["yr"] and r["yr"].isdigit()}
    years.add(date.today().year)
    return sorted(years, reverse=True)


def _yearly_pivot(year):
    salespeople = db.query("SELECT id, name FROM users WHERE active = 1 ORDER BY name COLLATE NOCASE")
    rows = [r for r in _billed_invoice_rows() if (r["invoice_date"] or "")[:4] == str(year)]

    by_person_month = {}
    for r in rows:
        if not r["invoice_date"]:
            continue
        month = int(r["invoice_date"][5:7])
        cell = by_person_month.setdefault(r["created_by"], {}).setdefault(
            month, {"invoices": 0, "amount": 0.0})
        cell["invoices"] += 1
        cell["amount"] += r["total"] or 0

    pivot = []
    for p in salespeople:
        months = []
        ytd_invoices = 0
        ytd_amount = 0.0
        for m in range(1, 13):
            cell = by_person_month.get(p["id"], {}).get(m, {"invoices": 0, "amount": 0.0})
            months.append(cell)
            ytd_invoices += cell["invoices"]
            ytd_amount += cell["amount"]
        pivot.append({
            "salesperson_id": p["id"],
            "salesperson_name": p["name"],
            "months": months,
            "ytd_invoices": ytd_invoices,
            "ytd_amount": ytd_amount,
        })
    return pivot


def _sort_params(default_sort):
    sort = request.args.get("sort", default_sort)
    direction = request.args.get("dir", "desc")
    if sort not in ("amount", "invoices", "month"):
        sort = default_sort
    if direction not in ("asc", "desc"):
        direction = "desc"
    return sort, direction


@bp.route("/")
@login_required
def index():
    psort, pdir = _sort_params("amount")
    msort = request.args.get("msort", "month")
    mdir = request.args.get("mdir", "desc")
    if msort not in ("amount", "invoices", "month"):
        msort = "month"
    if mdir not in ("asc", "desc"):
        mdir = "desc"

    rows = _billed_invoice_rows()
    salespeople = _aggregate_by_salesperson(rows, psort, pdir)
    by_month = _aggregate_by_month(rows, msort, mdir)
    for m in by_month:
        m["label"] = _month_label(m["ym"])
    grand_invoices = sum(p["invoice_count"] for p in salespeople)
    grand_amount = sum(p["total_amount"] for p in salespeople)
    return render_template(
        "sales_performance/index.html", salespeople=salespeople, by_month=by_month,
        grand_invoices=grand_invoices, grand_amount=grand_amount,
        psort=psort, pdir=pdir, msort=msort, mdir=mdir,
    )


@bp.route("/export")
@admin_required
def export():
    psort, pdir = _sort_params("amount")
    salespeople = _aggregate_by_salesperson(_billed_invoice_rows(), psort, pdir)
    rows = (
        (p["salesperson_name"], p["invoice_count"], p["total_amount"])
        for p in salespeople
    )
    return csv_response(
        "sales_performance.csv",
        ["Salesperson", "Invoices Raised", "Total Revenue (RM)"],
        rows,
    )


@bp.route("/yearly")
@login_required
def yearly():
    year = request.args.get("year", type=int) or date.today().year
    pivot = _yearly_pivot(year)
    return render_template(
        "sales_performance/yearly.html", pivot=pivot, year=year,
        month_names=MONTH_NAMES, available_years=_available_years(),
    )


@bp.route("/yearly/export")
@admin_required
def yearly_export():
    year = request.args.get("year", type=int) or date.today().year
    pivot = _yearly_pivot(year)
    header = ["Salesperson"]
    for name in MONTH_NAMES:
        header += [f"{name} Invoices", f"{name} Amt"]
    header += ["YTD Invoices", "YTD Amt"]

    def _rows():
        for p in pivot:
            row = [p["salesperson_name"]]
            for cell in p["months"]:
                row += [cell["invoices"], cell["amount"]]
            row += [p["ytd_invoices"], p["ytd_amount"]]
            yield row

    return csv_response(f"sales_performance_{year}.csv", header, _rows())


@bp.route("/<int:salesperson_id>")
@login_required
def detail(salesperson_id):
    person = db.query("SELECT * FROM users WHERE id = ?", (salesperson_id,), one=True)
    if person is None:
        flash("Staff member not found.", "danger")
        return redirect(url_for("sales_performance.index"))
    rows = [r for r in _billed_invoice_rows() if r["created_by"] == salesperson_id]
    total_amount = sum(r["total"] or 0 for r in rows)
    return render_template(
        "sales_performance/detail.html", person=person, rows=rows, total_amount=total_amount,
    )
