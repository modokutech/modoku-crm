"""Fix242: how an invoice line is charged.

Every line has a "charge by" basis (invoice_items.charge_by):

  day      rate x days          (the default, and every line before Fix242)
  pax      rate x pax
  pax_day  rate x pax x days
  unit     rate x quantity      (with its own unit word, e.g. "room", "bus")
  lump     the amount as typed  (no quantity, no days)

The quantity is stored in invoice_items.quantity (pax, or units for "unit"),
the days in invoice_items.duration as "N day(s)" (only for day / pax_day).
"""

CHARGE_BASES = [
    ("day", "Per day"),
    ("pax", "Per pax"),
    ("pax_day", "Per pax per day"),
    ("unit", "Per unit"),
    ("lump", "Lump sum"),
]
CHARGE_KEYS = {key for key, _label in CHARGE_BASES}
DEFAULT_UNIT = "unit"


def basis(item):
    try:
        value = item["charge_by"]
    except (KeyError, IndexError):
        value = None
    return value if value in CHARGE_KEYS else "day"


def uses_days(charge_by):
    return charge_by in ("day", "pax_day")


def uses_quantity(charge_by):
    return charge_by in ("pax", "pax_day", "unit")


def line_amount(charge_by, rate, quantity, days):
    if charge_by == "pax":
        return round(rate * quantity, 2)
    if charge_by == "pax_day":
        return round(rate * quantity * days, 2)
    if charge_by == "unit":
        return round(rate * quantity, 2)
    if charge_by == "lump":
        return round(rate, 2)
    return round(rate * days, 2)


def _num(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{value:g}" if value != int(value) else str(int(value))


def _unit(item):
    try:
        label = (item["unit_label"] or "").strip()
    except (KeyError, IndexError):
        label = ""
    return label or DEFAULT_UNIT


def qty_text(item, training_row=False):
    """What the No. of Pax / Qty column shows."""
    cb = basis(item)
    if cb == "lump":
        return ""
    if cb == "unit":
        return f"{_num(item['quantity'])} {_unit(item)}"
    if cb in ("pax", "pax_day") or training_row:
        return f"{_num(item['quantity'])} pax"
    return _num(item["quantity"])  # a plain "per day" line, as before Fix242


def rate_suffix(item):
    """' / pax', ' / pax / day', ' / room' - shown after the rate."""
    cb = basis(item)
    return {"pax": " / pax", "pax_day": " / pax / day", "unit": f" / {_unit(item)}"}.get(cb, "")


def detail_qty(item):
    """The sub-detail line under a dated/venue line: ('Pax', '81 pax'),
    ('Quantity', '40 room'), or None for a lump sum."""
    cb = basis(item)
    if cb == "lump":
        return None
    if cb == "unit":
        return ("Quantity", f"{_num(item['quantity'])} {_unit(item)}")
    return ("Pax", f"{_num(item['quantity'])} pax")


def qty_header(items):
    """'No. of Pax', or 'Qty' when any line is charged per unit."""
    return "Qty" if any(basis(i) == "unit" for i in items) else "No. of Pax"
