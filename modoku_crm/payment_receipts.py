"""Payment Receipts — a unified, read-only view across trainer Purchase
Orders and Vendor Purchase Orders that have had invoices submitted (via
the public trainer/vendor invoice-upload links), so finance can see
everything awaiting payment in one place.

This module doesn't duplicate the upload-receipt / send-email logic —
that lives on each PO's own view page (built in purchase_orders.py and
vendor_purchase_orders.py as the "Payment Receipt" section), which stays
the single source of truth for that document. Clicking a row here just
takes you straight to it.
"""
from flask import Blueprint, redirect, render_template, url_for

from . import db
from .auth import login_required

bp = Blueprint("payment_receipts", __name__, url_prefix="/payment-receipts")


@bp.route("/")
@login_required
def index():
    trainer_rows = db.query(
        """SELECT po.id AS po_id, po.po_no AS ref, t.name AS name, c.title AS course_title,
                  po.currency,
                  po.fee_amount + COALESCE((SELECT SUM(amount) FROM po_items WHERE po_id = po.id), 0) AS amount,
                  po.payment_status AS status,
                  MAX(tid.uploaded_at) AS date_submitted, 'trainer' AS kind
           FROM purchase_orders po
           JOIN trainers t ON t.id = po.trainer_id
           JOIN course_sessions cs ON cs.id = po.session_id
           JOIN courses c ON c.id = cs.course_id
           JOIN trainer_invoice_documents tid ON tid.session_id = po.session_id
           GROUP BY po.id"""
    )
    vendor_rows = db.query(
        """SELECT vpo.id AS po_id, vpo.po_no AS ref, v.name AS name,
                  COALESCE(c.title, vpo.description, '-') AS course_title,
                  vpo.currency,
                  vpo.fee_amount + COALESCE((SELECT SUM(amount) FROM vendor_po_items WHERE po_id = vpo.id), 0) AS amount,
                  vpo.payment_status AS status,
                  MAX(vid.uploaded_at) AS date_submitted, 'vendor' AS kind
           FROM vendor_purchase_orders vpo
           JOIN vendors v ON v.id = vpo.vendor_id
           LEFT JOIN course_sessions cs ON cs.id = vpo.session_id
           LEFT JOIN courses c ON c.id = cs.course_id
           JOIN vendor_invoice_documents vid ON vid.po_id = vpo.id
           GROUP BY vpo.id"""
    )
    rows = [dict(r) for r in trainer_rows] + [dict(r) for r in vendor_rows]
    rows.sort(key=lambda r: r["date_submitted"] or "", reverse=True)
    return render_template("payment_receipts/index.html", rows=rows)


@bp.route("/<kind>/<int:po_id>")
@login_required
def go(kind, po_id):
    """Convenience redirect so the list's rows can share one URL-building
    helper in the template — sends the visitor to the actual PO page
    (trainer or vendor) where the Payment Receipt section lives."""
    if kind == "vendor":
        return redirect(url_for("vendor_purchase_orders.view", po_id=po_id))
    return redirect(url_for("purchase_orders.view", po_id=po_id))
