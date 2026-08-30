"""Hotels — a simple venue directory (name, location, contact person, meeting
package rates, minimum pax, and room-by-room pax capacities) so staff can
look up a suitable venue without digging through emails/spreadsheets. Not
linked to Classes/Purchase Orders yet — this is the data-entry/lookup layer
the rest of the app can build on later (e.g. picking a hotel as a Class
venue).
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

from . import activity, db
from .auth import admin_required, login_required
from .csvutil import csv_response

bp = Blueprint("hotels", __name__, url_prefix="/hotels")


def _form_fields(form):
    return (
        form.get("name", "").strip(),
        form.get("location") or None,
        form.get("contact_name") or None,
        form.get("contact_position") or None,
        form.get("contact_phone") or None,
        form.get("contact_email") or None,
        form.get("rate_full_day") or None,
        form.get("rate_half_day") or None,
        form.get("rate_others_label") or None,
        form.get("rate_others_amount") or None,
        form.get("minimum_pax") or None,
        form.get("notes") or None,
    )


def _save_capacities(hotel_id, form):
    """Replaces the full set of room/capacity rows for a hotel — the form
    always resubmits the complete current list (like Quotation/PO line
    items), so the simplest correct approach is delete-then-reinsert rather
    than trying to diff old vs. new rows."""
    db.execute("DELETE FROM hotel_capacities WHERE hotel_id = ?", (hotel_id,))
    room_names = form.getlist("room_name")
    pax_values = form.getlist("room_pax")
    for room_name, pax in zip(room_names, pax_values):
        room_name = room_name.strip()
        if not room_name:
            continue
        db.execute(
            "INSERT INTO hotel_capacities (hotel_id, room_name, pax_capacity) VALUES (?,?,?)",
            (hotel_id, room_name, pax or None),
        )


def _filtered_hotels():
    q = request.args.get("q", "").strip()
    sql = """SELECT h.*, (SELECT COUNT(*) FROM hotel_capacities hc WHERE hc.hotel_id = h.id) AS room_count,
                    (SELECT MAX(pax_capacity) FROM hotel_capacities hc WHERE hc.hotel_id = h.id) AS max_capacity
             FROM hotels h WHERE 1=1"""
    args = []
    if q:
        sql += " AND (h.name LIKE ? OR h.location LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY h.name COLLATE NOCASE"
    return db.query(sql, args), q


@bp.route("/")
@login_required
def index():
    hotels, q = _filtered_hotels()
    return render_template("hotels/list.html", hotels=hotels, q=q)


@bp.route("/export")
@admin_required
def export():
    hotels, _q = _filtered_hotels()
    rows = (
        (h["name"], h["location"] or "", h["contact_name"] or "", h["contact_phone"] or "",
         h["contact_email"] or "", h["rate_full_day"] or "", h["rate_half_day"] or "",
         h["minimum_pax"] or "", h["room_count"], h["max_capacity"] or "")
        for h in hotels
    )
    return csv_response(
        "hotels.csv",
        ["Name", "Location", "Contact Name", "Contact Phone", "Contact Email", "Rate Full Day",
         "Rate Half Day", "Minimum Pax", "Rooms", "Max Capacity"],
        rows,
    )


@bp.route("/new", methods=("GET", "POST"))
@login_required
def new():
    if request.method == "POST":
        fields = _form_fields(request.form)
        if not fields[0]:
            flash("Hotel name is required.", "danger")
        else:
            hotel_id = db.execute(
                """INSERT INTO hotels (name, location, contact_name, contact_position, contact_phone,
                       contact_email, rate_full_day, rate_half_day, rate_others_label, rate_others_amount,
                       minimum_pax, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                fields,
            )
            _save_capacities(hotel_id, request.form)
            activity.log("create", "hotel", hotel_id, f"Added hotel {fields[0]}")
            flash("Hotel added.", "success")
            return redirect(url_for("hotels.view", hotel_id=hotel_id))
    return render_template("hotels/form.html", hotel=None, capacities=[])


@bp.route("/<int:hotel_id>")
@login_required
def view(hotel_id):
    hotel = db.query("SELECT * FROM hotels WHERE id = ?", (hotel_id,), one=True)
    if hotel is None:
        flash("Hotel not found.", "danger")
        return redirect(url_for("hotels.index"))
    capacities = db.query(
        "SELECT * FROM hotel_capacities WHERE hotel_id = ? ORDER BY id", (hotel_id,)
    )
    return render_template("hotels/view.html", hotel=hotel, capacities=capacities)


@bp.route("/<int:hotel_id>/edit", methods=("GET", "POST"))
@login_required
def edit(hotel_id):
    hotel = db.query("SELECT * FROM hotels WHERE id = ?", (hotel_id,), one=True)
    if hotel is None:
        flash("Hotel not found.", "danger")
        return redirect(url_for("hotels.index"))
    if request.method == "POST":
        fields = _form_fields(request.form)
        if not fields[0]:
            flash("Hotel name is required.", "danger")
        else:
            db.execute(
                """UPDATE hotels SET name=?, location=?, contact_name=?, contact_position=?, contact_phone=?,
                       contact_email=?, rate_full_day=?, rate_half_day=?, rate_others_label=?,
                       rate_others_amount=?, minimum_pax=?, notes=?
                   WHERE id=?""",
                fields + (hotel_id,),
            )
            _save_capacities(hotel_id, request.form)
            activity.log("update", "hotel", hotel_id, f"Updated hotel {fields[0]}")
            flash("Hotel updated.", "success")
            return redirect(url_for("hotels.view", hotel_id=hotel_id))
    capacities = db.query(
        "SELECT * FROM hotel_capacities WHERE hotel_id = ? ORDER BY id", (hotel_id,)
    )
    return render_template("hotels/form.html", hotel=hotel, capacities=capacities)


@bp.route("/<int:hotel_id>/delete", methods=("POST",))
@login_required
def delete(hotel_id):
    hotel = db.query("SELECT name FROM hotels WHERE id = ?", (hotel_id,), one=True)
    db.execute("DELETE FROM hotels WHERE id = ?", (hotel_id,))
    activity.log("delete", "hotel", hotel_id, f"Deleted hotel {hotel['name'] if hotel else hotel_id}")
    flash("Hotel deleted.", "success")
    return redirect(url_for("hotels.index"))
