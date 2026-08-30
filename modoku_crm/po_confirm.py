"""Public "Confirm or Reject this Purchase Order" flow — no login required.

The trainer gets a link (sent alongside the PO email) letting them confirm
or reject the assignment without needing an account. Confirming sets the
PO status to 'Confirmed' (the same status the staff-side dropdown already
uses to flag a date as blocked against other overlapping POs for that
trainer); rejecting sets it to 'Cancelled'. Mirrors the token-gated public
blueprint pattern already used by quotation_return.py / t3_public.py.
"""
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from . import activity, db, fmtdaterange, mailer, notifications
from . import settings as settings_module

bp = Blueprint("po_confirm", __name__, url_prefix="/po-confirm")


def _find_po(token):
    if not token:
        return None
    return db.query(
        """SELECT po.*, t.name AS trainer_name, c.title AS course_title,
                  cs.start_date, cs.end_date, cs.training_time, cs.venue
           FROM purchase_orders po
           JOIN trainers t ON t.id = po.trainer_id
           JOIN course_sessions cs ON cs.id = po.session_id
           JOIN courses c ON c.id = cs.course_id
           WHERE po.confirm_token = ?""",
        (token,), one=True,
    )


@bp.route("/<token>")
def details(token):
    po = _find_po(token)
    if po is None:
        return render_template("po_confirm/not_found.html")
    return render_template("po_confirm/details.html", po=po, token=token)


@bp.route("/<token>/respond", methods=("POST",))
def respond(token):
    po = _find_po(token)
    if po is None:
        return render_template("po_confirm/not_found.html")

    decision = request.form.get("decision")
    if decision not in ("confirm", "reject"):
        flash("Invalid response — please try again.", "danger")
        return redirect(url_for("po_confirm.details", token=token))

    new_status = "Confirmed" if decision == "confirm" else "Cancelled"
    db.execute(
        "UPDATE purchase_orders SET status = ?, confirm_responded_at = datetime('now') WHERE id = ?",
        (new_status, po["id"]),
    )
    activity.log(
        "update", "purchase_order", po["id"],
        f"Trainer {'confirmed' if decision == 'confirm' else 'rejected'} {po['po_no']} via public "
        f"link — status set to {new_status}",
    )

    try:
        notify_to = ", ".join(settings_module.get_notification_emails())
        verb = "confirmed" if decision == "confirm" else "rejected"
        subject = f"{po['po_no']} {verb} by trainer"
        date_range = fmtdaterange(po["start_date"], po["end_date"])
        body = (
            f"{po['trainer_name']} has {verb} {po['po_no']} ({po['course_title']}) via the "
            f"confirmation link.\n\nStatus is now: {new_status}.\n\n"
            f"Date: {date_range}\n"
            f"Time: {po['training_time'] or 'To be confirmed'}\n"
            f"Venue: {po['venue'] or 'To be confirmed'}"
        )
        if notify_to:
            mailer.send_email(notify_to, subject, body, related_type="purchase_order", related_id=po["id"])
    except Exception:  # noqa: BLE001 - notification must never break the trainer's response
        current_app.logger.exception("Failed to send PO response notification for %s", po["id"])

    notifications.notify_admins(
        "po_confirmed" if decision == "confirm" else "po_rejected",
        f"{po['trainer_name']} {'confirmed' if decision == 'confirm' else 'rejected'} {po['po_no']}",
        body=po["course_title"] or "",
        link=url_for("purchase_orders.view", po_id=po["id"]),
    )

    return render_template("po_confirm/success.html", po=po, decision=decision, status=new_status)
