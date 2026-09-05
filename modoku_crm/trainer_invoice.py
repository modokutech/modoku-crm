"""Public "Submit Your Invoice" flow for trainers — no login required.

Once a class's status changes to Completed (see sessions._notify_trainers_invoice_due,
called both from the manual Edit Class form and from the automatic date-based
status advance in sessions._auto_advance_statuses), every trainer assigned to
that class is emailed a unique, unguessable link to this page. They can
upload one or more invoice/claim documents (PDF, Word, or Excel — trainers
sometimes have a separate invoice and expense claim, so multiple files are
allowed) at any time; nothing here ever locks, so they can come back and add
more later. Every uploaded document is listed on the trainer's Purchase
Order page in Modoku Hub (purchase_orders/view.html) for staff to review —
even though the upload link itself is still per-Class (shared by every
trainer assigned to it), that's where staff naturally look to check on a
trainer's invoice.
"""
import os
import secrets
import uuid

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from . import db, doc_sanity, mailer, notifications, uploadutil
from . import settings as settings_module

bp = Blueprint("trainer_invoice", __name__, url_prefix="/trainer-invoice")


def _upload_dir(session_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "trainer_invoices", str(session_id))
    os.makedirs(path, exist_ok=True)
    return path


def ensure_token(session_id):
    """Every class gets a long, unguessable token the first time it's
    needed — generated lazily (when the Completed-status email is about to
    go out) rather than at class-creation time."""
    row = db.query("SELECT trainer_invoice_token FROM course_sessions WHERE id = ?", (session_id,), one=True)
    if row and row["trainer_invoice_token"]:
        return row["trainer_invoice_token"]
    token = secrets.token_urlsafe(24)
    db.execute("UPDATE course_sessions SET trainer_invoice_token = ? WHERE id = ?", (token, session_id))
    return token


def _find_session(token):
    if not token:
        return None
    return db.query(
        """SELECT cs.*, c.title AS course_title FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id WHERE cs.trainer_invoice_token = ?""",
        (token,), one=True,
    )


@bp.route("/<token>")
def form(token):
    session_row = _find_session(token)
    if session_row is None:
        return render_template("trainer_invoice/not_found.html")
    documents = db.query(
        "SELECT * FROM trainer_invoice_documents WHERE session_id = ? ORDER BY id",
        (session_row["id"],),
    )
    return render_template("trainer_invoice/form.html", s=session_row, documents=documents, token=token)


@bp.route("/<token>/submit", methods=("POST",))
def submit(token):
    session_row = _find_session(token)
    if session_row is None:
        return render_template("trainer_invoice/not_found.html")

    files = request.files.getlist("invoice_files")
    saved = 0
    sanity_warnings = []
    for file_storage in files:
        if not file_storage or not file_storage.filename:
            continue
        error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.DOCUMENT_EXTENSIONS)
        if error:
            flash(error, "danger")
            return redirect(url_for("trainer_invoice.form", token=token))
        safe_name = secure_filename(file_storage.filename)
        stored_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
        saved_path = os.path.join(_upload_dir(session_row["id"]), stored_name)
        file_storage.save(saved_path)
        db.execute(
            "INSERT INTO trainer_invoice_documents (session_id, filename, original_name) VALUES (?,?,?)",
            (session_row["id"], stored_name, file_storage.filename),
        )
        saved += 1
        warning = doc_sanity.check_document(saved_path, "financial_document")
        if warning:
            sanity_warnings.append((file_storage.filename, warning))

    if saved:
        flash(f"Uploaded {saved} document(s) — thank you!", "success")
        # Every submission (the first one, and any later change/addition)
        # tells the office — by email and in the notifications inbox — so
        # nobody has to keep checking the class page to notice new
        # documents. The link is shared by every trainer on the class (it
        # isn't trainer-specific), so the notification names the class
        # rather than a particular trainer.
        session_url = url_for("sessions.view", session_id=session_row["id"], _external=True)
        sanity_line = ""
        if sanity_warnings:
            sanity_line = "\n\nNote (AI sanity-check):\n" + "\n".join(
                f"- {name}: {warning}" for name, warning in sanity_warnings
            )
        try:
            notify_to = ", ".join(settings_module.get_notification_emails())
            if notify_to:
                mailer.send_email(
                    notify_to,
                    f"Trainer invoice documents submitted — {session_row['course_title']}",
                    f"A trainer has submitted {saved} invoice/claim document(s) for "
                    f"{session_row['course_title']}.\n\nReview them here:\n{session_url}" + sanity_line,
                    related_type="course_session", related_id=session_row["id"],
                )
        except Exception:  # noqa: BLE001 - notification must never break the trainer's upload
            current_app.logger.exception(
                "Failed to send office email for trainer invoice upload on session %s", session_row["id"])
        notifications.notify_admins(
            "trainer_invoice_submitted",
            f"Invoice documents submitted — {session_row['course_title']}",
            body=f"{saved} document(s) uploaded." + (" AI sanity-check flagged a possible issue — see email." if sanity_warnings else ""),
            link=url_for("sessions.view", session_id=session_row["id"]),
        )
    elif not files or not files[0].filename:
        flash("Choose at least one file first.", "danger")
    return redirect(url_for("trainer_invoice.form", token=token))
