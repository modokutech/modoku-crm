"""Staff expense claims — anyone with the link submits a claim tied to a
class they worked on (trainer allowance, transport, materials, etc.),
uploading supporting photos/files. Finance later reviews it, uploads the
payment receipt plus the approved amount and a remark, then emails the
receipt to the claimant — which flips the claim to Paid.

Public / no-login by design, at Erik's explicit request — a claim carries
a bank account number, and he's confirmed that being visible without a
login isn't a concern for this internal tool. Both /claims (the list,
including the finance processing step) and /claim (the submission entry
point) work without a staff account, same as attendance_return, the T3
public form, or the trainer/vendor invoice-submission pages.

If a staff member IS logged in when they visit, they still get the normal
sidebar layout (see the templates' guest_content fallback, which reuses
the same content block) — only an anonymous visitor sees the bare page.

Routes are hand-written at both "/claims..." and "/claim..." (rather than
using a Blueprint url_prefix) because the request explicitly asked for
both spellings to work — people forget the trailing 's'.
"""
import mimetypes
import os
import uuid
from datetime import date

from flask import (Blueprint, current_app, flash, g, redirect, render_template,
                    request, send_from_directory, url_for)
from werkzeug.utils import secure_filename

from . import activity, db, mailer, uploadutil

bp = Blueprint("claims", __name__)

# Classes in these statuses are eligible to claim against. Proposed classes
# aren't confirmed yet, so they're excluded on purpose (per the request).
ELIGIBLE_SESSION_STATUSES = ("Scheduled", "Ongoing", "Completed")


def _upload_dir(claim_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "claims", str(claim_id))
    os.makedirs(path, exist_ok=True)
    return path


def _receipt_dir(claim_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "claim_receipts", str(claim_id))
    os.makedirs(path, exist_ok=True)
    return path


def _default_claim_email_subject(claim):
    return f"Claim Payment Receipt — {claim['course_title']} — {claim['claimant_name']}"


def _default_claim_email_body(claim):
    approved_line = f"Approved amount: RM {claim['approved_amount']:.2f}\n" if claim["approved_amount"] is not None else ""
    remark_line = f"Remark: {claim['remark']}\n" if claim["remark"] else ""
    return (
        f"Hi {claim['claimant_name']},\n\n"
        f"Your claim for {claim['course_title']} has been processed. "
        "Please find the payment receipt attached for your records.\n\n"
        f"{approved_line}"
        f"{remark_line}"
        "\nShould you have any questions, please feel free to contact us.\n\n"
        "Cheers!"
    )


def _get_claim_full(claim_id):
    return db.query(
        """SELECT sc.*, c.title AS course_title, cs.start_date, cs.end_date, cs.venue,
                  u.name AS submitted_by_name, u.email AS submitted_by_email
           FROM staff_claims sc
           JOIN course_sessions cs ON cs.id = sc.session_id
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN users u ON u.id = sc.user_id
           WHERE sc.id = ?""",
        (claim_id,), one=True,
    )


@bp.route("/claims")
def index():
    if not g.user:
        # Anonymous visitors don't need to see who submitted what (or anyone
        # else's claim at all) — send them straight to the "Submit a Claim"
        # flow instead of the staff processing list. Logged-in staff still
        # land on the normal list below, since that's where they process claims.
        return redirect(url_for("claims.new"))
    claims = db.query(
        """SELECT sc.*, c.title AS course_title
           FROM staff_claims sc
           JOIN course_sessions cs ON cs.id = sc.session_id
           JOIN courses c ON c.id = cs.course_id
           ORDER BY sc.created_at DESC"""
    )
    return render_template("claims/index.html", claims=claims)


@bp.route("/claim")
def index_alias():
    return redirect(url_for("claims.index"))


@bp.route("/claims/new", methods=("GET", "POST"))
def new():
    selected_date = request.values.get("claim_date") or ""
    sessions_on_date = []
    if selected_date:
        placeholders = ",".join("?" * len(ELIGIBLE_SESSION_STATUSES))
        sessions_on_date = db.query(
            f"""SELECT cs.id, cs.start_date, cs.end_date, cs.venue, c.title
                FROM course_sessions cs
                JOIN courses c ON c.id = cs.course_id
                WHERE cs.status IN ({placeholders})
                  AND date(?) BETWEEN date(cs.start_date) AND date(cs.end_date)
                ORDER BY c.title""",
            (*ELIGIBLE_SESSION_STATUSES, selected_date),
        )

    if request.method == "POST":
        session_id = request.form.get("session_id")
        claimant_name = (request.form.get("claimant_name") or "").strip()
        claimant_email = (request.form.get("claimant_email") or "").strip()
        bank_name = (request.form.get("bank_name") or "").strip()
        bank_account_no = (request.form.get("bank_account_no") or "").strip()
        total_amount = request.form.get("total_amount") or "0"
        claim_note = (request.form.get("claim_note") or "").strip() or None

        if not selected_date:
            flash("Select a date first.", "danger")
        elif not session_id:
            flash("No class was selected — check the date and try again.", "danger")
        elif not claimant_name or not claimant_email or not bank_name or not bank_account_no:
            flash("Name, Email, Bank Name, and Bank Account are all required.", "danger")
        else:
            # Re-validate the session is really eligible for that date server-side
            # (never trust the submitted session_id alone).
            placeholders = ",".join("?" * len(ELIGIBLE_SESSION_STATUSES))
            session_row = db.query(
                f"""SELECT cs.id FROM course_sessions cs
                    WHERE cs.id = ? AND cs.status IN ({placeholders})
                      AND date(?) BETWEEN date(cs.start_date) AND date(cs.end_date)""",
                (session_id, *ELIGIBLE_SESSION_STATUSES, selected_date),
                one=True,
            )
            if session_row is None:
                flash("That class isn't available on the selected date — check the date and try again.", "danger")
            else:
                try:
                    total_amount_f = round(float(total_amount or 0), 2)
                except ValueError:
                    total_amount_f = 0
                claim_id = db.execute(
                    """INSERT INTO staff_claims (user_id, session_id, claimant_name, claimant_email,
                           bank_name, bank_account_no, total_amount, claim_note)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (g.user["id"] if g.user else None, session_id, claimant_name, claimant_email,
                     bank_name, bank_account_no, total_amount_f, claim_note),
                )
                saved = 0
                for file_storage in request.files.getlist("files"):
                    if not file_storage or not file_storage.filename:
                        continue
                    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.DEFAULT_EXTENSIONS)
                    if error:
                        flash(error, "danger")
                        continue
                    safe_name = secure_filename(file_storage.filename)
                    stored_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
                    file_storage.save(os.path.join(_upload_dir(claim_id), stored_name))
                    db.execute(
                        "INSERT INTO staff_claim_files (claim_id, filename, original_name) VALUES (?,?,?)",
                        (claim_id, stored_name, file_storage.filename),
                    )
                    saved += 1
                activity.log("create", "staff_claim", claim_id, f"Submitted claim for {claimant_name}")
                flash("Claim submitted." + ("" if saved else " (No files were attached.)"), "success")
                return redirect(url_for("claims.index"))

    return render_template(
        "claims/new.html", selected_date=selected_date, sessions_on_date=sessions_on_date,
        today=date.today().isoformat(),
        default_name=g.user["name"] if g.user else "",
        default_email=g.user["email"] if g.user else "",
    )


@bp.route("/claim/new", methods=("GET", "POST"))
def new_alias():
    return new()


@bp.route("/claims/<int:claim_id>")
def view(claim_id):
    claim = _get_claim_full(claim_id)
    if claim is None:
        flash("Claim not found.", "danger")
        return redirect(url_for("claims.index"))
    files = db.query("SELECT * FROM staff_claim_files WHERE claim_id = ? ORDER BY id", (claim_id,))
    receipts = db.query("SELECT * FROM staff_claim_receipts WHERE claim_id = ? ORDER BY id", (claim_id,))
    return render_template(
        "claims/view.html", claim=claim, files=files, receipts=receipts,
        mail_configured=mailer.is_configured(),
        default_email_subject=_default_claim_email_subject(claim),
        default_email_body=_default_claim_email_body(claim),
    )


@bp.route("/claim/<int:claim_id>")
def view_alias(claim_id):
    return redirect(url_for("claims.view", claim_id=claim_id))


@bp.route("/claims/<int:claim_id>/files/<int:doc_id>/download")
def download_file(claim_id, doc_id):
    doc = db.query("SELECT * FROM staff_claim_files WHERE id = ? AND claim_id = ?", (doc_id, claim_id), one=True)
    if doc is None:
        flash("File not found.", "danger")
        return redirect(url_for("claims.view", claim_id=claim_id))
    return send_from_directory(_upload_dir(claim_id), doc["filename"], as_attachment=False,
                                download_name=doc["original_name"])


@bp.route("/claims/<int:claim_id>/process", methods=("POST",))
def process(claim_id):
    """Finance step: record the approved amount + remark, and (optionally,
    at the same time) upload the payment receipt file(s). Doesn't send
    anything or change status by itself — that's the separate send-email
    step below, once the approved amount is on file."""
    claim = db.query("SELECT id FROM staff_claims WHERE id = ?", (claim_id,), one=True)
    if claim is None:
        flash("Claim not found.", "danger")
        return redirect(url_for("claims.index"))

    approved_amount = request.form.get("approved_amount")
    try:
        approved_amount_f = round(float(approved_amount), 2) if approved_amount not in (None, "") else None
    except ValueError:
        approved_amount_f = None
    remark = (request.form.get("remark") or "").strip() or None

    if approved_amount_f is None:
        flash("Enter the approved amount.", "danger")
        return redirect(url_for("claims.view", claim_id=claim_id))

    db.execute(
        "UPDATE staff_claims SET approved_amount = ?, remark = ? WHERE id = ?",
        (approved_amount_f, remark, claim_id),
    )

    saved = 0
    for file_storage in request.files.getlist("receipts"):
        if not file_storage or not file_storage.filename:
            continue
        error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.DEFAULT_EXTENSIONS)
        if error:
            flash(error, "danger")
            continue
        safe_name = secure_filename(file_storage.filename)
        stored_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
        file_storage.save(os.path.join(_receipt_dir(claim_id), stored_name))
        db.execute(
            "INSERT INTO staff_claim_receipts (claim_id, filename, original_name) VALUES (?,?,?)",
            (claim_id, stored_name, file_storage.filename),
        )
        saved += 1

    activity.log("update", "staff_claim", claim_id, "Recorded approved amount / remark for claim")
    flash("Claim processed — the email box is ready below." if saved else
          "Approved amount saved. Upload a payment receipt file, then send the email.", "success")
    return redirect(url_for("claims.view", claim_id=claim_id))


@bp.route("/claims/<int:claim_id>/receipts/<int:doc_id>/download")
def download_receipt(claim_id, doc_id):
    doc = db.query("SELECT * FROM staff_claim_receipts WHERE id = ? AND claim_id = ?", (doc_id, claim_id), one=True)
    if doc is None:
        flash("File not found.", "danger")
        return redirect(url_for("claims.view", claim_id=claim_id))
    return send_from_directory(_receipt_dir(claim_id), doc["filename"], as_attachment=False,
                                download_name=doc["original_name"])


@bp.route("/claims/<int:claim_id>/receipts/<int:doc_id>/delete", methods=("POST",))
def delete_receipt(claim_id, doc_id):
    db.execute("DELETE FROM staff_claim_receipts WHERE id = ? AND claim_id = ?", (doc_id, claim_id))
    flash("File removed.", "success")
    return redirect(url_for("claims.view", claim_id=claim_id))


@bp.route("/claims/<int:claim_id>/send-email", methods=("POST",))
def send_email(claim_id):
    claim = _get_claim_full(claim_id)
    if claim is None:
        flash("Claim not found.", "danger")
        return redirect(url_for("claims.index"))
    if claim["approved_amount"] is None:
        flash("Record the approved amount first, then send the email.", "danger")
        return redirect(url_for("claims.view", claim_id=claim_id))

    receipts = db.query("SELECT * FROM staff_claim_receipts WHERE claim_id = ? ORDER BY id", (claim_id,))
    if not receipts:
        flash("Upload at least one payment receipt file before sending.", "danger")
        return redirect(url_for("claims.view", claim_id=claim_id))

    to_email = (request.form.get("to_email") or claim["claimant_email"] or claim["submitted_by_email"] or "").strip()
    if not to_email:
        flash("No email on file for this claimant — type an address to send to.", "danger")
        return redirect(url_for("claims.view", claim_id=claim_id))

    subject = (request.form.get("subject") or "").strip() or _default_claim_email_subject(claim)
    body = (request.form.get("body") or "").strip() or _default_claim_email_body(claim)
    cc_email = (request.form.get("cc_email") or "").strip() or None

    attachments = []
    for doc in receipts:
        try:
            with open(os.path.join(_receipt_dir(claim_id), doc["filename"]), "rb") as f:
                mimetype, _ = mimetypes.guess_type(doc["original_name"])
                attachments.append((doc["original_name"], f.read(), mimetype or "application/octet-stream"))
        except OSError:
            pass

    try:
        mailer.send_email(to_email, subject, body, attachments=attachments,
                           related_type="staff_claim", related_id=claim_id, cc_email=cc_email)
    except mailer.MailNotConfigured as exc:
        flash(str(exc), "danger")
        return redirect(url_for("claims.view", claim_id=claim_id))
    except mailer.MailSendError as exc:
        flash(f"Email failed to send: {exc}", "danger")
        return redirect(url_for("claims.view", claim_id=claim_id))

    db.execute(
        "UPDATE staff_claims SET status = 'Paid', paid_at = datetime('now'), paid_to_email = ? WHERE id = ?",
        (to_email, claim_id),
    )
    activity.log("send_email", "staff_claim", claim_id, f"Emailed payment receipt for claim to {to_email}")
    flash(f"Payment receipt emailed to {to_email} — marked as Paid.", "success")
    return redirect(url_for("claims.view", claim_id=claim_id))
