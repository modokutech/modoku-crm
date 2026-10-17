"""Hotels — a simple venue directory (name, location, contact person, meeting
packages (Fix190), accommodation rates, the hotel's quotation, minimum pax,
and room-by-room pax capacities) so staff can
look up a suitable venue without digging through emails/spreadsheets. Not
linked to Classes/Purchase Orders yet — this is the data-entry/lookup layer
the rest of the app can build on later (e.g. picking a hotel as a Class
venue).
"""
import math
import os
import uuid
from datetime import date

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

from . import activity, db, uploadutil
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
        form.get("minimum_pax") or None,
        form.get("notes") or None,
        8.0 if form.get("plus_sst_rate") != "6" else 6.0,
        _parse_date(form.get("quoted_on")),
    )


def _parse_date(value):
    try:
        return date.fromisoformat((value or "").strip()).isoformat()
    except ValueError:
        return None


def _parse_price(value):
    raw = (value or "").replace(",", "").strip()
    try:
        return round(float(raw), 2) if raw else None
    except ValueError:
        return None


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


OCCUPANCIES = ["Single", "Twin-sharing", "Double", "Triple"]


def _save_rooms(hotel_id, form):
    """Fix189: the Accommodation rows - same delete-then-reinsert approach as
    the meeting room capacities."""
    db.execute("DELETE FROM hotel_rooms WHERE hotel_id = ?", (hotel_id,))
    types = form.getlist("acc_room_type")
    occs = form.getlist("acc_occupancy")
    prices = form.getlist("acc_price")
    breakfasts = form.getlist("acc_breakfast")  # one "0"/"1" per row (hidden input kept in step by JS)
    pluses = form.getlist("acc_plus")  # Fix190: same, for the "++" tick
    for i, room_type in enumerate(types):
        room_type = room_type.strip()
        if not room_type:
            continue
        occ = occs[i] if i < len(occs) and occs[i] in OCCUPANCIES else None
        price = _parse_price(prices[i] if i < len(prices) else "")
        breakfast = 1 if i < len(breakfasts) and breakfasts[i] == "1" else 0
        plus = 1 if i < len(pluses) and pluses[i] == "1" else 0
        db.execute("INSERT INTO hotel_rooms (hotel_id, room_type, occupancy, price, breakfast, plus_plus) "
                   "VALUES (?,?,?,?,?,?)", (hotel_id, room_type, occ, price, breakfast, plus))


def _rooms(hotel_id, hotel=None):
    rows = db.query("SELECT * FROM hotel_rooms WHERE hotel_id = ? ORDER BY id", (hotel_id,))
    sst = hotel["plus_sst_rate"] if hotel is not None else 8
    return [dict(r, nett=nett_price(r["price"], r["plus_plus"], sst)) for r in rows]


# Fix190: meeting packages. Hotels quote in packages (meeting room plus some
# meals, sometimes up to dinner, sometimes meals only), so each package lists
# what it includes, and the compare tool matches on that.
INCLUSIONS = [
    ("meeting_room", "Meeting room"),
    ("breakfast", "Breakfast"),
    ("morning_tea", "Morning tea"),
    ("lunch", "Lunch"),
    ("afternoon_tea", "Afternoon tea"),
    ("dinner", "Dinner"),
    ("projector", "Projector & screen"),
    ("pa_system", "PA system"),
    ("stationery", "Stationery"),
    ("water", "Mineral water"),
]
INCLUSION_LABELS = dict(INCLUSIONS)
DEFAULT_NEEDS = ["meeting_room", "morning_tea", "lunch", "afternoon_tea"]

# "++" = service charge, then SST on the price plus service charge (SST is
# charged on the full value of the service, service charge included).
SERVICE_CHARGE_PCT = 10
STALE_MONTHS = 6  # a quote older than this gets flagged


def nett_price(price, plus_plus, sst_rate=8):
    if price is None:
        return None
    if not plus_plus:
        return round(price, 2)
    return round(price * (1 + SERVICE_CHARGE_PCT / 100) * (1 + (sst_rate or 0) / 100), 2)


def _split(inclusions):
    return [k for k in (inclusions or "").split(",") if k in INCLUSION_LABELS]


def _packages(hotel_id, hotel=None):
    rows = db.query("SELECT * FROM hotel_packages WHERE hotel_id = ? ORDER BY id", (hotel_id,))
    sst = hotel["plus_sst_rate"] if hotel is not None else 8
    return [dict(p, nett=nett_price(p["price"], p["plus_plus"], sst), included=_split(p["inclusions"]))
            for p in rows]


def _save_packages(hotel_id, form):
    """Same delete-then-reinsert approach as the other row lists. The "++"
    tick and the inclusion chips each post one hidden value per row (kept in
    step by JS) so the lists stay aligned."""
    db.execute("DELETE FROM hotel_packages WHERE hotel_id = ?", (hotel_id,))
    cols = {k: form.getlist(f"pkg_{k}") for k in ("name", "price", "plus", "inclusions", "min_pax", "notes")}

    def at(key, i):
        values = cols[key]
        return values[i].strip() if i < len(values) else ""

    for i in range(len(cols["name"])):
        name, price = at("name", i), _parse_price(at("price", i))
        if not name and price is None:
            continue
        min_pax = at("min_pax", i)
        db.execute(
            "INSERT INTO hotel_packages (hotel_id, name, price, plus_plus, inclusions, min_pax, notes) "
            "VALUES (?,?,?,?,?,?,?)",
            (hotel_id, name or "Package", price, 1 if at("plus", i) == "1" else 0,
             ",".join(_split(at("inclusions", i))), int(min_pax) if min_pax.isdigit() else None,
             at("notes", i) or None),
        )


def quote_age(quoted_on):
    """{"text": "3 months ago", "stale": False}, or None when no date."""
    try:
        d = date.fromisoformat(quoted_on or "")
    except ValueError:
        return None
    today = date.today()
    months = (today.year - d.year) * 12 + today.month - d.month - (1 if today.day < d.day else 0)
    if months < 1:
        days = (today - d).days
        text = "today" if days <= 0 else ("yesterday" if days == 1 else f"{days} days ago")
    elif months < 12:
        text = f"{months} month{'s' if months != 1 else ''} ago"
    else:
        y, m = divmod(months, 12)
        text = f"{y} year{'s' if y != 1 else ''}" + (f" {m} month{'s' if m != 1 else ''}" if m else "") + " ago"
    return {"text": text, "stale": months >= STALE_MONTHS}


def _quote_dir():
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "hotel_quotes")
    os.makedirs(path, exist_ok=True)
    return path


def _remove_quote_file(stored):
    if stored:
        try:
            os.remove(os.path.join(_quote_dir(), stored))
        except OSError:
            pass


def _save_quotation(hotel_id, hotel=None):
    """The hotel's own quotation (PDF/scan): replaces any earlier one, or
    removes it when "Remove" is ticked."""
    old = hotel["quotation_file"] if hotel is not None else None
    file_storage = request.files.get("quotation_file")
    if file_storage and file_storage.filename:
        error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.DEFAULT_EXTENSIONS)
        if error:
            flash(f"Quotation file not saved: {error}", "warning")
            return
        safe_name = secure_filename(file_storage.filename) or "quotation"
        stored = f"{uuid.uuid4().hex[:8]}_{safe_name}"
        file_storage.save(os.path.join(_quote_dir(), stored))
        db.execute("UPDATE hotels SET quotation_file = ?, quotation_name = ? WHERE id = ?",
                   (stored, file_storage.filename, hotel_id))
        _remove_quote_file(old)
    elif old and request.form.get("remove_quotation") == "1":
        db.execute("UPDATE hotels SET quotation_file = NULL, quotation_name = NULL WHERE id = ?", (hotel_id,))
        _remove_quote_file(old)


def _compare_params():
    """The compare tool's inputs from the query string, or None when it
    wasn't run."""
    if not request.args.get("compare"):
        return None

    def num(key, default, lo=1, hi=100000):
        try:
            return max(lo, min(hi, int(request.args.get(key) or default)))
        except ValueError:
            return default

    needs = [k for k in request.args.getlist("need") if k in INCLUSION_LABELS]
    occupancy = request.args.get("occupancy")
    return {
        "pax": num("pax", 20), "days": num("days", 1),
        "needs": needs, "rooms": request.args.get("rooms") == "1",
        "nights": num("nights", 1, lo=1, hi=365),
        "occupancy": occupancy if occupancy in OCCUPANCIES else "Twin-sharing",
    }


ROOM_SHARE = {"Single": 1, "Twin-sharing": 2, "Double": 2, "Triple": 3}


def _compare(hotels, params):
    """Each hotel's estimated total for pax x days: the cheapest package that
    includes everything needed (charged for at least its minimum pax), plus
    rooms if needed (cheapest rate for the chosen occupancy). Prices are
    nett, i.e. with "++" added. Hotels with no match are listed after, with
    the reason."""
    pax, days, needs = params["pax"], params["days"], set(params["needs"])
    out = []
    for h in hotels:
        r = {"hotel": h, "notes": [], "package": None, "meeting_total": None, "room_total": None, "total": None}
        best = None
        closest = None
        for p in _packages(h["id"], h):
            if p["nett"] is None:
                continue
            missing = needs - set(p["included"])
            if missing:
                if closest is None or len(missing) < len(closest):
                    closest = missing
                continue
            charged = max(pax, p["min_pax"] or h["minimum_pax"] or 0)
            total = round(p["nett"] * charged * days, 2)
            if best is None or total < best["total"]:
                best = {"p": p, "charged": charged, "total": total}
        if best:
            r["package"], r["charged"], r["meeting_total"] = best["p"], best["charged"], best["total"]
        elif closest:
            labels = [INCLUSION_LABELS[k] for k, _ in INCLUSIONS if k in closest]
            r["missing"] = f"No package includes {', '.join(labels)}"
        else:
            r["missing"] = "No package prices recorded"
        if params["rooms"]:
            rates = [x for x in _rooms(h["id"], h) if x["occupancy"] == params["occupancy"] and x["nett"] is not None]
            if rates:
                room = min(rates, key=lambda x: x["nett"])
                count = math.ceil(pax / ROOM_SHARE[params["occupancy"]])
                r["room"], r["room_count"] = room, count
                r["room_total"] = round(room["nett"] * count * params["nights"], 2)
            else:
                r["missing_rooms"] = f"No {params['occupancy'].lower()} room rate"
        if h["max_capacity"] and h["max_capacity"] < pax:
            r["notes"].append(f"Largest meeting room fits {h['max_capacity']} pax")
        age = quote_age(h["quoted_on"])
        if age and age["stale"]:
            r["notes"].append(f"Quoted {age['text']}")
        complete = r["meeting_total"] is not None and (not params["rooms"] or r["room_total"] is not None)
        if complete:
            r["total"] = round(r["meeting_total"] + (r["room_total"] or 0), 2)
        out.append(r)
    out.sort(key=lambda r: (r["total"] is None, r["total"] or 0, r["hotel"]["name"].lower()))
    return out


def _filtered_hotels():
    q = request.args.get("q", "").strip()
    sql = """SELECT h.*, (SELECT COUNT(*) FROM hotel_capacities hc WHERE hc.hotel_id = h.id) AS room_count,
                    (SELECT MAX(pax_capacity) FROM hotel_capacities hc WHERE hc.hotel_id = h.id) AS max_capacity,
                    (SELECT COUNT(*) FROM hotel_packages hp WHERE hp.hotel_id = h.id) AS package_count
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
    hotels = [_with_summary(h) for h in hotels]
    params = _compare_params()
    results = _compare(hotels, params) if params else None
    return render_template("hotels/list.html", hotels=hotels, q=q, params=params, results=results,
                           inclusions=INCLUSIONS, default_needs=DEFAULT_NEEDS, occupancies=OCCUPANCIES)


def _with_summary(h):
    """Adds the cheapest package (nett per pax per day) and the quote's age."""
    nets = [p["nett"] for p in _packages(h["id"], h) if p["nett"] is not None]
    return dict(h, from_price=min(nets) if nets else None, age=quote_age(h["quoted_on"]))


@bp.route("/export")
@admin_required
def export():
    hotels, _q = _filtered_hotels()
    rows = []
    for h in hotels:
        pkgs = "; ".join(f"{p['name']}: RM {p['price']:,.2f}{'++' if p['plus_plus'] else ''}"
                         for p in _packages(h["id"], h) if p["price"] is not None)
        cheapest = _with_summary(h)["from_price"]
        rows.append((h["name"], h["location"] or "", h["contact_name"] or "", h["contact_phone"] or "",
                     h["contact_email"] or "", pkgs, cheapest if cheapest is not None else "",
                     h["minimum_pax"] or "", h["room_count"], h["max_capacity"] or "", h["quoted_on"] or ""))
    return csv_response(
        "hotels.csv",
        ["Name", "Location", "Contact Name", "Contact Phone", "Contact Email", "Meeting Packages",
         "Cheapest Package (nett per pax/day)", "Minimum Pax", "Meeting Rooms", "Max Capacity", "Quoted On"],
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
                       contact_email, minimum_pax, notes, plus_sst_rate, quoted_on)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                fields,
            )
            _save_packages(hotel_id, request.form)
            _save_capacities(hotel_id, request.form)
            _save_rooms(hotel_id, request.form)
            _save_quotation(hotel_id)
            activity.log("create", "hotel", hotel_id, f"Added hotel {fields[0]}")
            flash("Hotel added.", "success")
            return redirect(url_for("hotels.view", hotel_id=hotel_id))
    return render_template("hotels/form.html", hotel=None, capacities=[], rooms=[], packages=[],
                           **_form_context())


def _form_context():
    return {"occupancies": OCCUPANCIES, "inclusions": INCLUSIONS, "service_pct": SERVICE_CHARGE_PCT}


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
    return render_template("hotels/view.html", hotel=hotel, capacities=capacities, rooms=_rooms(hotel_id, hotel),
                           packages=_packages(hotel_id, hotel), inclusion_labels=INCLUSION_LABELS,
                           age=quote_age(hotel["quoted_on"]), service_pct=SERVICE_CHARGE_PCT)


@bp.route("/<int:hotel_id>/quotation")
@login_required
def quotation(hotel_id):
    hotel = db.query("SELECT quotation_file, quotation_name FROM hotels WHERE id = ?", (hotel_id,), one=True)
    if hotel is None or not hotel["quotation_file"]:
        abort(404)
    return send_from_directory(_quote_dir(), hotel["quotation_file"],
                               download_name=hotel["quotation_name"] or hotel["quotation_file"],
                               as_attachment=bool(request.args.get("download")))


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
                       contact_email=?, minimum_pax=?, notes=?, plus_sst_rate=?, quoted_on=?
                   WHERE id=?""",
                fields + (hotel_id,),
            )
            _save_packages(hotel_id, request.form)
            _save_capacities(hotel_id, request.form)
            _save_rooms(hotel_id, request.form)
            _save_quotation(hotel_id, hotel)
            activity.log("update", "hotel", hotel_id, f"Updated hotel {fields[0]}")
            flash("Hotel updated.", "success")
            return redirect(url_for("hotels.view", hotel_id=hotel_id))
    capacities = db.query(
        "SELECT * FROM hotel_capacities WHERE hotel_id = ? ORDER BY id", (hotel_id,)
    )
    return render_template("hotels/form.html", hotel=hotel, capacities=capacities, rooms=_rooms(hotel_id, hotel),
                           packages=_packages(hotel_id, hotel), **_form_context())


@bp.route("/<int:hotel_id>/delete", methods=("POST",))
@login_required
def delete(hotel_id):
    hotel = db.query("SELECT name, quotation_file FROM hotels WHERE id = ?", (hotel_id,), one=True)
    db.execute("DELETE FROM hotels WHERE id = ?", (hotel_id,))
    if hotel:
        _remove_quote_file(hotel["quotation_file"])
    activity.log("delete", "hotel", hotel_id, f"Deleted hotel {hotel['name'] if hotel else hotel_id}")
    flash("Hotel deleted.", "success")
    return redirect(url_for("hotels.index"))
