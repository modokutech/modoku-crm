"""Vendors — a directory of non-training suppliers (photographers,
caterers, printers, transport, etc.), separate from Trainers (who deliver
training) and Hotels (venues). Mirrors hotels.py's shape: simple CRUD plus
a delete-and-reinsert sub-table, here for priced services instead of room
capacities.
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

from . import activity, db
from .auth import admin_required, login_required
from .csvutil import csv_response

bp = Blueprint("vendors", __name__, url_prefix="/vendors")

VENDOR_TYPES = ["Company", "Freelancer"]


def _form_fields(form):
    rating = form.get("rating") or None
    return (
        form.get("name", "").strip(),
        form.get("vendor_type") if form.get("vendor_type") in VENDOR_TYPES else "Company",
        form.get("service") or None,
        form.get("contact_name") or None,
        form.get("contact_phone") or None,
        form.get("contact_email") or None,
        rating,
        form.get("notes") or None,
    )


def _save_rates(vendor_id, form):
    """Replaces the full set of rate rows for a vendor — same
    delete-then-reinsert pattern as hotels' room capacities, since the form
    always resubmits the complete current list.

    per_day is a <select> (Flat/Per Day) rather than a checkbox on
    purpose — a <select> always submits a value for every row, so it zips
    up 1:1 with the description/price arrays; a checkbox only submits when
    checked, which would silently shift every row after the first
    unchecked one out of alignment."""
    db.execute("DELETE FROM vendor_rates WHERE vendor_id = ?", (vendor_id,))
    services = form.getlist("rate_service")
    prices = form.getlist("rate_price")
    per_day_values = form.getlist("rate_per_day")
    for service, price, per_day in zip(services, prices, per_day_values):
        service = service.strip()
        if not service:
            continue
        db.execute(
            "INSERT INTO vendor_rates (vendor_id, service, price, per_day) VALUES (?,?,?,?)",
            (vendor_id, service, price or None, 1 if per_day == "1" else 0),
        )


def _filtered_vendors():
    q = request.args.get("q", "").strip()
    sql = """SELECT v.*, (SELECT COUNT(*) FROM vendor_rates vr WHERE vr.vendor_id = v.id) AS rate_count
             FROM vendors v WHERE 1=1"""
    args = []
    if q:
        sql += " AND (v.name LIKE ? OR v.service LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY v.name COLLATE NOCASE"
    return db.query(sql, args), q


@bp.route("/")
@login_required
def index():
    vendors, q = _filtered_vendors()
    return render_template("vendors/list.html", vendors=vendors, q=q)


@bp.route("/export")
@admin_required
def export():
    vendors, _q = _filtered_vendors()
    rows = (
        (v["name"], v["vendor_type"], v["service"] or "", v["contact_name"] or "", v["contact_phone"] or "",
         v["contact_email"] or "", v["rating"] if v["rating"] is not None else "", v["rate_count"])
        for v in vendors
    )
    return csv_response(
        "vendors.csv",
        ["Name", "Type", "Service", "Contact Name", "Contact Phone", "Contact Email", "Rating", "Rates on file"],
        rows,
    )


@bp.route("/new", methods=("GET", "POST"))
@login_required
def new():
    if request.method == "POST":
        fields = _form_fields(request.form)
        if not fields[0]:
            flash("Vendor name is required.", "danger")
        else:
            vendor_id = db.execute(
                """INSERT INTO vendors (name, vendor_type, service, contact_name, contact_phone,
                       contact_email, rating, notes)
                   VALUES (?,?,?,?,?,?,?,?)""",
                fields,
            )
            _save_rates(vendor_id, request.form)
            activity.log("create", "vendor", vendor_id, f"Added vendor {fields[0]}")
            flash("Vendor added.", "success")
            return redirect(url_for("vendors.view", vendor_id=vendor_id))
    return render_template("vendors/form.html", vendor=None, rates=[], vendor_types=VENDOR_TYPES)


@bp.route("/<int:vendor_id>")
@login_required
def view(vendor_id):
    vendor = db.query("SELECT * FROM vendors WHERE id = ?", (vendor_id,), one=True)
    if vendor is None:
        flash("Vendor not found.", "danger")
        return redirect(url_for("vendors.index"))
    rates = db.query("SELECT * FROM vendor_rates WHERE vendor_id = ? ORDER BY id", (vendor_id,))
    purchase_orders = db.query(
        """SELECT vpo.*, c.title AS course_title FROM vendor_purchase_orders vpo
           LEFT JOIN course_sessions cs ON cs.id = vpo.session_id
           LEFT JOIN courses c ON c.id = cs.course_id
           WHERE vpo.vendor_id = ? ORDER BY vpo.issue_date DESC""",
        (vendor_id,),
    )
    return render_template("vendors/view.html", vendor=vendor, rates=rates, purchase_orders=purchase_orders)


@bp.route("/<int:vendor_id>/edit", methods=("GET", "POST"))
@login_required
def edit(vendor_id):
    vendor = db.query("SELECT * FROM vendors WHERE id = ?", (vendor_id,), one=True)
    if vendor is None:
        flash("Vendor not found.", "danger")
        return redirect(url_for("vendors.index"))
    if request.method == "POST":
        fields = _form_fields(request.form)
        if not fields[0]:
            flash("Vendor name is required.", "danger")
        else:
            db.execute(
                """UPDATE vendors SET name=?, vendor_type=?, service=?, contact_name=?, contact_phone=?,
                       contact_email=?, rating=?, notes=?
                   WHERE id=?""",
                fields + (vendor_id,),
            )
            _save_rates(vendor_id, request.form)
            activity.log("update", "vendor", vendor_id, f"Updated vendor {fields[0]}")
            flash("Vendor updated.", "success")
            return redirect(url_for("vendors.view", vendor_id=vendor_id))
    rates = db.query("SELECT * FROM vendor_rates WHERE vendor_id = ? ORDER BY id", (vendor_id,))
    return render_template("vendors/form.html", vendor=vendor, rates=rates, vendor_types=VENDOR_TYPES)


@bp.route("/<int:vendor_id>/delete", methods=("POST",))
@login_required
def delete(vendor_id):
    vendor = db.query("SELECT name FROM vendors WHERE id = ?", (vendor_id,), one=True)
    db.execute("DELETE FROM vendors WHERE id = ?", (vendor_id,))
    activity.log("delete", "vendor", vendor_id, f"Deleted vendor {vendor['name'] if vendor else vendor_id}")
    flash("Vendor deleted.", "success")
    return redirect(url_for("vendors.index"))
