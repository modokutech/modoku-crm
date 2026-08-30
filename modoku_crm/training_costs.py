"""Training Costs — a simple per-class P&L worksheet, one row per class,
reachable via the "Training Costs" button on the class's own page.

Modeled on the client's existing (legacy, spreadsheet-like) costing tool —
see the screenshot referenced when this module was requested — trimmed down
to just the fields that matter for Modoku's current flow. Deliberately
dropped: Event Allowance, Total Marketing/Allowance/Promotion (those track a
separate marketing budget this app has no data for), the 10% DNNS Charge,
and the Training Voucher Fee section. "Need PC" is renamed "Laptop Rental"
to match the rest of the app's terminology (see course_sessions.requires_laptop_rental),
and "GP" is spelled out as "Gross Profit".

Lunch, Tea Break, Meeting Package, and Laptop Rental are billed per
participant PER DAY (as labelled in the reference tool), so their totals
multiply by the class's own training-day count; every other line item is a
flat per-participant rate.
Gross Profit needs a revenue figure to compare costs against — the legacy
tool computes it from other spreadsheet tabs this app doesn't have, so here
it's simply a manually-entered "Training Revenue" field (e.g. copied from
the accepted Quotation or Invoice for this class).
"""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from . import db
from .auth import login_required
from .trainer_utilization import _session_days

bp = Blueprint("training_costs", __name__, url_prefix="/training-costs")

CERT_TYPES = ["E-Cert", "HC-Cert"]
EXAM_TYPES = ["Not Bundle", "Bundle"]

_FIELDS = [
    ("pax_count", int, 0), ("lunch_rate", float, 0),
    ("tea_break_rate", float, 0), ("meeting_package_rate", float, 0),
    ("laptop_rental_qty", int, 0), ("laptop_rental_rate", float, 0),
    ("courseware_qty", int, 0), ("courseware_rate", float, 0), ("manual_qty", int, 0),
    ("manual_rate", float, 0), ("book_qty", int, 0), ("book_rate", float, 0),
    ("certificate_qty", int, 0), ("certificate_type", str, "E-Cert"), ("certificate_rate", float, 0),
    ("exam_qty", int, 0), ("exam_type", str, "Not Bundle"), ("exam_rate", float, 0),
    ("others_remarks", str, ""),
    ("trainer_fee_per_day", float, 0), ("trainer_allowance_per_day", float, 0),
    ("bus_air_fee", float, 0), ("venue_fee", float, 0), ("hotel_fee", float, 0),
    ("training_revenue", float, 0),
]


def _session_or_none(session_id):
    return db.query(
        """SELECT cs.*, c.title AS course_title FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?""",
        (session_id,), one=True,
    )


def _compute(costs, training_days, custom_items_total=0):
    """Returns a dict of computed line totals plus the overall costing/GP
    figures, given a training_costs row (or the zeroed defaults below), the
    class's training-day count, and the sum of any custom fee line items
    (training_cost_items) — kept as a separate parameter since those live in
    their own table rather than as columns on the training_costs row."""
    c = dict(costs)
    total_lunch = c["lunch_rate"] * c["pax_count"] * training_days
    total_tea_break = c["tea_break_rate"] * c["pax_count"] * training_days
    total_meeting_package = c["meeting_package_rate"] * c["pax_count"] * training_days
    total_laptop_rental = c["laptop_rental_rate"] * c["laptop_rental_qty"] * training_days
    total_courseware = c["courseware_rate"] * c["courseware_qty"]
    total_manual = c["manual_rate"] * c["manual_qty"]
    total_book = c["book_rate"] * c["book_qty"]
    total_certificate = c["certificate_rate"] * c["certificate_qty"]
    total_exam = c["exam_rate"] * c["exam_qty"]
    total_trainer_fee = c["trainer_fee_per_day"] * training_days
    total_trainer_allowance = c["trainer_allowance_per_day"] * training_days

    total_costing = (total_lunch + total_tea_break + total_meeting_package + total_laptop_rental
                      + total_courseware + total_manual + total_book + total_certificate + total_exam
                      + custom_items_total + total_trainer_fee + total_trainer_allowance
                      + c["bus_air_fee"] + c["venue_fee"] + c["hotel_fee"])
    revenue = c["training_revenue"]
    gross_profit = revenue - total_costing
    gross_profit_pct = round((gross_profit / revenue * 100), 1) if revenue else 0

    return {
        "total_lunch": total_lunch, "total_tea_break": total_tea_break,
        "total_meeting_package": total_meeting_package,
        "total_laptop_rental": total_laptop_rental, "total_courseware": total_courseware,
        "total_manual": total_manual, "total_book": total_book,
        "total_certificate": total_certificate, "total_exam": total_exam,
        "total_trainer_fee": total_trainer_fee, "total_trainer_allowance": total_trainer_allowance,
        "total_costing": total_costing, "gross_profit": gross_profit,
        "gross_profit_pct": gross_profit_pct,
    }


_DEFAULTS = {name: default for name, _type, default in _FIELDS}


@bp.route("/<int:session_id>", methods=("GET", "POST"))
@login_required
def view(session_id):
    session_row = _session_or_none(session_id)
    if session_row is None:
        flash("Class not found.", "danger")
        return redirect(url_for("sessions.index"))

    training_days = _session_days(session_row["start_date"], session_row["end_date"])

    if request.method == "POST":
        values = {}
        for name, type_, default in _FIELDS:
            raw = request.form.get(name, "")
            if type_ is str:
                values[name] = raw.strip() or default
            else:
                try:
                    values[name] = type_(raw) if raw not in (None, "") else default
                except ValueError:
                    values[name] = default
        existing = db.query("SELECT id FROM training_costs WHERE session_id = ?", (session_id,), one=True)
        columns = [name for name, _t, _d in _FIELDS]
        if existing:
            set_clause = ", ".join(f"{c}=?" for c in columns)
            db.execute(
                f"UPDATE training_costs SET {set_clause}, updated_at=datetime('now'), updated_by=? WHERE session_id=?",
                [values[c] for c in columns] + [g.user["id"], session_id],
            )
        else:
            col_list = ", ".join(columns)
            placeholders = ",".join("?" * len(columns))
            db.execute(
                f"INSERT INTO training_costs (session_id, {col_list}, updated_by) "
                f"VALUES (?, {placeholders}, ?)",
                [session_id] + [values[c] for c in columns] + [g.user["id"]],
            )

        # Custom fee line items — always replaced wholesale (delete then
        # re-insert whatever the form submitted), same pattern used for a
        # class's trainer list, so removed rows on the page are actually
        # removed rather than lingering in the database.
        db.execute("DELETE FROM training_cost_items WHERE session_id = ?", (session_id,))
        descriptions = request.form.getlist("item_description")
        amounts = request.form.getlist("item_amount")
        for desc, amount_raw in zip(descriptions, amounts):
            if not desc.strip() and not (amount_raw or "").strip():
                continue
            try:
                amount = float(amount_raw) if amount_raw not in (None, "") else 0
            except ValueError:
                amount = 0
            db.execute(
                "INSERT INTO training_cost_items (session_id, description, amount) VALUES (?,?,?)",
                (session_id, desc.strip(), amount),
            )

        flash("Training costs saved.", "success")
        return redirect(url_for("training_costs.view", session_id=session_id))

    costs_row = db.query("SELECT * FROM training_costs WHERE session_id = ?", (session_id,), one=True)
    costs = dict(_DEFAULTS)
    if costs_row:
        costs.update({k: costs_row[k] for k in _DEFAULTS if k in costs_row.keys()})
    items = db.query("SELECT * FROM training_cost_items WHERE session_id = ? ORDER BY id", (session_id,))
    custom_items_total = sum(item["amount"] for item in items)
    computed = _compute(costs, training_days, custom_items_total)

    return render_template("training_costs/view.html", s=session_row, costs=costs, computed=computed,
                            training_days=training_days, cert_types=CERT_TYPES, exam_types=EXAM_TYPES,
                            items=items)


@bp.route("/<int:session_id>/reset", methods=("POST",))
@login_required
def reset(session_id):
    session_row = _session_or_none(session_id)
    if session_row is None:
        flash("Class not found.", "danger")
        return redirect(url_for("sessions.index"))
    db.execute("DELETE FROM training_costs WHERE session_id = ?", (session_id,))
    db.execute("DELETE FROM training_cost_items WHERE session_id = ?", (session_id,))
    flash("Training costs cleared.", "success")
    return redirect(url_for("training_costs.view", session_id=session_id))
