"""Public "Confirm or Reject this Vendor Purchase Order" flow — no login
required. Mirrors po_confirm.py exactly, but for Vendor POs, which may or
may not be linked to a class (LEFT JOINs throughout, unlike po_confirm.py's
inner joins, since a trainer PO always has a session but a vendor PO
doesn't have to).
"""
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from . import activity, db, fmtdaterange, mailer, notifications
from . import settings as settings_module

bp = Blueprint("vendor_po_confirm", __name__, url_prefix="/vendor-po-confirm")


def _find_po(token):
    if not token:
        return None
    return db.query(
        """SELECT vpo.*, v.name AS vendor_name, c.title AS course_title,
                  cs.start_date, cs.end_date, cs.venue
           FROM vendor_purchase_orders vpo
           JOIN vendors v ON v.id = vpo.vendor_id
           LEFT JOIN course_sessions cs ON cs.id = vpo.session_id
           LEFT JOIN courses c ON c.id = cs.course_id
           WHERE vpo.confirm_token = ?""",
        (token,), one=True,
    )


@bp.route("/<token>")
def details(token):
    po = _find_po(token)
    if po is None:
        return render_template("vendor_po_confirm/not_found.html")
    return render_template("vendor_po_confirm/details.html", po=po, token=token)


@bp.route("/<token>/respond", methods=("POST",))
def respond(token):
    po = _find_po(token)
    if po is None:
        return render_template("vendor_po_confirm/not_found.html")

    decision = request.form.get("decision")
    if decision not in ("confirm", "reject"):
        flash("Invalid response — please try again.", "danger")
        return redirect(url_for("vendor_po_confirm.details", token=token))

    new_status = "Confirmed" if decision == "confirm" else "Cancelled"
    db.execute(
        "UPDATE vendor_purchase_orders SET status = ?, confirm_responded_at = datetime('now') WHERE id = ?",
        (new_status, po["id"]),
    )
    activity.log(
        "update", "vendor_purchase_order", po["id"],
        f"Vendor {'confirmed' if decision == 'confirm' else 'rejected'} {po['po_no']} via public "
        f"link — status set to {new_status}",
    )

    try:
        notify_to = ", ".join(settings_module.get_notification_emails())
        verb = "confirmed" if decision == "confirm" else "rejected"
        subject = f"{po['po_no']} {verb} by vendor"
        what = po["course_title"] or "(standalone order)"
        body_lines = [
            f"{po['vendor_name']} has {verb} {po['po_no']} ({what}) via the confirmation link.",
            "",
            f"Status is now: {new_status}.",
        ]
        if po["start_date"]:
            date_range = fmtdaterange(po["start_date"], po["end_date"])
            body_lines += ["", f"Date: {date_range}", f"Venue: {po['venue'] or 'To be confirmed'}"]
        body = "\n".join(body_lines)
        if notify_to:
            mailer.send_email(notify_to, subject, body, related_type="vendor_purchase_order", related_id=po["id"])
    except Exception:  # noqa: BLE001 - notification must never break the vendor's response
        current_app.logger.exception("Failed to send vendor PO response notification for %s", po["id"])

    notifications.notify_admins(
        "vendor_po_confirmed" if decision == "confirm" else "vendor_po_rejected",
        f"{po['vendor_name']} {'confirmed' if decision == 'confirm' else 'rejected'} {po['po_no']}",
        body=po["course_title"] or po["description"] or "",
        link=url_for("vendor_purchase_orders.view", po_id=po["id"]),
    )

    return render_template("vendor_po_confirm/success.html", po=po, decision=decision, status=new_status)
