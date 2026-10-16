"""Payments received against an invoice (Fix185) - the money-in record the
SST module (step 2) is built on: service tax is due on payments actually
received, so each payment carries its share of the invoice's SST and counts
in the two-month SST period of the date it came in.

An invoice can be paid in one go (Mark as Paid records the balance with the
date received) or in parts (HRDCorp's 30% upfront, a client deposit, then the
balance). The Fix114 upfront deduction on an invoice is mirrored as an
'upfront' payment row whose date is filled in by hand (it often arrives
before the invoice is even issued).

kind: 'upfront' (mirrors invoices.upfront_amount - change its amount through
Edit Invoice), 'balance' (made by Mark as Paid - undone if the invoice is set
back from Paid), 'payment' (recorded by hand). paid_on is NULL only for rows
that still need a date (upfront rows, and paid invoices from before this
existed whose payment date couldn't be recovered).
"""
from datetime import date

from . import db

EPS = 0.005  # half a sen: amounts within this count as equal


def gross_total(invoice):
    """The invoice value incl. SST, before any upfront deduction - what the
    client pays in total across all payments."""
    return round((invoice["subtotal"] or 0) + (invoice["sst_amount"] or 0), 2)


def payments(invoice_id):
    return db.query(
        """SELECT * FROM invoice_payments WHERE invoice_id = ?
           ORDER BY paid_on IS NULL, paid_on, id""",
        (invoice_id,),
    )


def summary(invoice):
    """Payments with each one's SST share, plus totals. The SST share is the
    payment's proportion of the invoice (SST / gross); once the invoice is
    fully paid the last payment takes the exact remainder, so the shares
    always add up to the invoice's SST to the sen."""
    rows = [dict(p) for p in payments(invoice["id"])]
    gross = gross_total(invoice)
    sst = round(invoice["sst_amount"] or 0, 2)
    ratio = (sst / gross) if gross else 0
    paid = round(sum(p["amount"] for p in rows), 2)
    outstanding = round(gross - paid, 2)
    fully_paid = bool(rows) and outstanding <= EPS
    running = 0.0
    for i, p in enumerate(rows):
        if fully_paid and i == len(rows) - 1:
            p["sst_share"] = round(sst - running, 2)
        else:
            p["sst_share"] = round(p["amount"] * ratio, 2)
        running = round(running + p["sst_share"], 2)
    return {
        "payments": rows, "gross": gross, "paid": paid, "outstanding": max(outstanding, 0.0),
        "overpaid": max(-outstanding, 0.0), "fully_paid": fully_paid,
        "partly_paid": bool(rows) and paid > EPS and not fully_paid,
        "needs_date": sum(1 for p in rows if not p["paid_on"]),
        "pct": min(100, round(paid / gross * 100)) if gross else 0,
    }


def sync_upfront(invoice_id):
    """Keeps the 'upfront' payment row in step with the invoice's upfront
    deduction (Fix114): created (date to be filled in) when one is added,
    its amount updated when it changes, removed when it's taken off."""
    invoice = db.query("SELECT upfront_amount FROM invoices WHERE id = ?", (invoice_id,), one=True)
    amount = round((invoice["upfront_amount"] or 0) if invoice else 0, 2)
    row = db.query("SELECT id, amount FROM invoice_payments WHERE invoice_id = ? AND kind = 'upfront'",
                   (invoice_id,), one=True)
    if amount > EPS:
        if row is None:
            db.execute(
                "INSERT INTO invoice_payments (invoice_id, kind, amount, note) VALUES (?, 'upfront', ?, ?)",
                (invoice_id, amount, "Upfront payment"),
            )
        elif abs(row["amount"] - amount) > EPS:
            db.execute("UPDATE invoice_payments SET amount = ? WHERE id = ?", (amount, row["id"]))
    elif row is not None:
        db.execute("DELETE FROM invoice_payments WHERE id = ?", (row["id"],))


def record_balance(invoice, paid_on, user_id=None, note="Balance"):
    """Mark as Paid: records whatever is still outstanding as one payment on
    paid_on. Returns the amount recorded (0 if nothing was outstanding)."""
    s = summary(invoice)
    if s["outstanding"] <= EPS:
        return 0.0
    db.execute(
        """INSERT INTO invoice_payments (invoice_id, kind, paid_on, amount, note, created_by)
           VALUES (?, 'balance', ?, ?, ?, ?)""",
        (invoice["id"], paid_on, s["outstanding"], note, user_id),
    )
    return s["outstanding"]


def sync_status(invoice_id):
    """Paid follows the payments: fully paid -> Paid; a Paid invoice that's
    no longer fully covered (a payment removed, the total raised) -> Sent.
    Cancelled is left alone. Returns the new status if it changed."""
    invoice = db.query("SELECT * FROM invoices WHERE id = ?", (invoice_id,), one=True)
    if invoice is None or invoice["status"] == "Cancelled":
        return None
    s = summary(invoice)
    if s["fully_paid"] and invoice["status"] != "Paid":
        db.execute("UPDATE invoices SET status = 'Paid' WHERE id = ?", (invoice_id,))
        return "Paid"
    if not s["fully_paid"] and invoice["status"] == "Paid":
        db.execute("UPDATE invoices SET status = 'Sent' WHERE id = ?", (invoice_id,))
        return "Sent"
    return None


def parse_date(value):
    """A YYYY-MM-DD string from a date input, or None. Future dates aren't
    allowed (money can't have come in yet)."""
    try:
        d = date.fromisoformat((value or "").strip())
    except ValueError:
        return None
    return d.isoformat() if d <= date.today() else None
