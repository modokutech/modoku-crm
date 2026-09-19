"""JD14 Form - OUR side of the HRDCorp Joint Declaration Form
(PSMB/SBL-KHAS/JD/14): generating it, filling in our part, and getting it
signed/stamped by an admin so it can be sent to the client to fill in their
part, sign, stamp and return.

Deliberately the "front half" of the existing JD14 workflow. sessions.py /
jd14_return.py / hrdcorp_grant.py already handle the "receiving" half - the
CLIENT'S SIGNED SCAN once it comes back (course_sessions.jd14_file, via the
public jd14_return_token upload link or a manual staff upload) - and none of
that is touched here. This module only produces the outgoing blank/signed
form; once the client returns their signed copy, it lands through that
existing flow exactly as it always has, since jd14_file/jd14_return_token
just accept "a signed JD14 file" and don't care who generated the outgoing
one.

NOTE - the on-screen live preview in templates/jd14/edit.html and
pdfgen._build_jd14_html render the SAME table-based layout by hand, kept in
sync manually (the same discipline the T3 Attendance Form already uses
between its on-screen and PDF renderings). Changing the layout means
changing both.
"""
from flask import Blueprint, Response, current_app, flash, g, redirect, render_template, request, url_for

from . import db, fmtaddress, mailer, settings
from .auth import admin_required, login_required
from .pdfgen import generate_jd14_pdf
from .sessions import ensure_jd14_return_token

bp = Blueprint("jd14", __name__, url_prefix="/jd14")


def _session_or_none(session_id):
    return db.query(
        """SELECT cs.*, c.title AS course_title, co.name AS company_name, co.address AS company_address,
                  co.city AS company_city, co.state AS company_state, co.postcode AS company_postcode,
                  co.email AS company_email
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN companies co ON co.id = cs.client_company_id
           WHERE cs.id = ?""",
        (session_id,), one=True,
    )


def _derive_prefill(session_row):
    """Computes the prefill values described in jd14.py's module docstring
    (B4) fresh from the class's own data - the client/company, the linked
    HRDCorp grant ID, the course/dates/venue, attendance, and the quoted
    price. Returns a dict matching jd14_forms' own editable columns (minus
    id/session_id/signed_*/updated_*). group_approved/group_claimed are
    always blank - never derived, always left for the admin to type."""
    grant_id = session_row["hrdcorp_grant_id"] or ""
    employer_code = grant_id.split("_", 1)[0] if grant_id else ""

    address = fmtaddress(
        session_row["company_address"], session_row["company_city"],
        session_row["company_postcode"], session_row["company_state"],
    ) if "company_address" in session_row.keys() else ""

    attended_row = db.query(
        "SELECT COUNT(*) AS n FROM t3_participants WHERE session_id = ? AND attended = 1",
        (session_row["id"],), one=True,
    )
    num_trainees = str(attended_row["n"] if attended_row else 0)

    from . import quotations as quotations_module
    quoted = quotations_module.quoted_price_for_session(session_row["id"])
    fee = f"{quoted['subtotal']:,.2f}" if quoted and quoted["subtotal"] is not None else ""

    return {
        "employer_name": session_row["company_name"] or "",
        "employer_address": address,
        "employer_code": employer_code,
        "approval_no": grant_id,
        "group_approved": "",
        "group_claimed": "",
        "course_title": session_row["course_title"] or "",
        "training_date_commenced": session_row["start_date"] or "",
        "training_date_ended": session_row["end_date"] or session_row["start_date"] or "",
        "training_venue": session_row["venue"] or "",
        "num_trainees": num_trainees,
        "total_fee_approved": fee,
        "total_fee_claimed": fee,
    }


_FORM_FIELDS = (
    "employer_name", "employer_address", "employer_code", "approval_no",
    "group_approved", "group_claimed", "course_title",
    "training_date_commenced", "training_date_ended", "training_venue",
    "num_trainees", "total_fee_approved", "total_fee_claimed",
)


def _jd14_row_or_derived(session_row):
    """The stored jd14_forms row for this class if one exists, or a
    dict of freshly-derived prefill values (with the same field names,
    plus session_id) if it doesn't - so the edit page always has something
    to show without ever inserting a row on a bare GET."""
    row = db.query("SELECT * FROM jd14_forms WHERE session_id = ?", (session_row["id"],), one=True)
    if row is not None:
        return row
    derived = _derive_prefill(session_row)
    derived["session_id"] = session_row["id"]
    derived["signed_by_user_id"] = None
    derived["signed_at"] = None
    return derived


def _save_fields(session_id, form, derived=False):
    """Upserts the jd14_forms row for this class from `form` (a dict-like
    with the _FORM_FIELDS keys, e.g. request.form or a derived dict).
    Never touches signed_by_user_id/signed_at - only sign() does that."""
    values = {field: (form.get(field) or "").strip() if hasattr(form.get(field), "strip")
              else (form.get(field) or "") for field in _FORM_FIELDS}
    existing = db.query("SELECT id FROM jd14_forms WHERE session_id = ?", (session_id,), one=True)
    if existing:
        set_clause = ", ".join(f"{f} = ?" for f in _FORM_FIELDS)
        db.execute(
            f"UPDATE jd14_forms SET {set_clause}, updated_at = datetime('now'), updated_by = ? WHERE session_id = ?",
            (*[values[f] for f in _FORM_FIELDS], g.user["id"] if g.user else None, session_id),
        )
    else:
        columns = ", ".join(_FORM_FIELDS)
        placeholders = ", ".join("?" for _ in _FORM_FIELDS)
        db.execute(
            f"""INSERT INTO jd14_forms (session_id, {columns}, updated_by)
                VALUES (?, {placeholders}, ?)""",
            (session_id, *[values[f] for f in _FORM_FIELDS], g.user["id"] if g.user else None),
        )


def _pdf_row_and_signer(session_id):
    """Builds the (jd14_row, signed_by_user) pair generate_jd14_pdf needs,
    from whatever is currently on file - the saved jd14_forms row if one
    exists (falling back to fresh derived values otherwise), and the
    recorded signer's user row if the form has been signed."""
    session_row = _session_or_none(session_id)
    if session_row is None:
        return None, None, None
    jd14_row = _jd14_row_or_derived(session_row)
    signed_by_user = None
    if jd14_row["signed_by_user_id"]:
        signed_by_user = db.query("SELECT * FROM users WHERE id = ?", (jd14_row["signed_by_user_id"],), one=True)
    return session_row, jd14_row, signed_by_user


@bp.route("/")
@login_required
def index():
    rows = db.query(
        """SELECT cs.id, cs.start_date, cs.end_date, cs.status, c.title AS course_title,
                  jf.signed_at, jf.id AS jd14_forms_id
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN jd14_forms jf ON jf.session_id = cs.id
           ORDER BY cs.start_date DESC"""
    )
    return render_template("jd14/index.html", sessions=rows)


@bp.route("/sessions/<int:session_id>")
@login_required
def edit(session_id):
    session_row = _session_or_none(session_id)
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("jd14.index"))
    jd14_row = _jd14_row_or_derived(session_row)
    signed_by_user = None
    if jd14_row["signed_by_user_id"]:
        signed_by_user = db.query("SELECT * FROM users WHERE id = ?", (jd14_row["signed_by_user_id"],), one=True)
    # For the live "who will sign" preview before anyone has signed yet,
    # show the logged-in user's own signature/mykad/position if they have
    # one on file - purely a preview convenience, sign() is what actually
    # records who signed.
    preview_user = signed_by_user or g.user
    return render_template(
        "jd14/edit.html", s=session_row, f=jd14_row, signed_by_user=signed_by_user,
        preview_user=preview_user, company_stamp_file=settings.get_company_stamp_file(),
        is_admin=(g.user and g.user["role"] == "admin"),
    )


@bp.route("/sessions/<int:session_id>/save", methods=("POST",))
@login_required
def save(session_id):
    session_row = _session_or_none(session_id)
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("jd14.index"))
    _save_fields(session_id, request.form)
    flash("JD14 Form details saved.", "success")
    return redirect(url_for("jd14.edit", session_id=session_id))


@bp.route("/sessions/<int:session_id>/reset-prefill", methods=("POST",))
@login_required
def reset_prefill(session_id):
    session_row = _session_or_none(session_id)
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("jd14.index"))
    derived = _derive_prefill(session_row)
    _save_fields(session_id, derived, derived=True)
    flash("JD14 Form fields reset to the derived values.", "success")
    return redirect(url_for("jd14.edit", session_id=session_id))


@bp.route("/sessions/<int:session_id>/preview.pdf")
@login_required
def preview_pdf(session_id):
    session_row, jd14_row, signed_by_user = _pdf_row_and_signer(session_id)
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("jd14.index"))
    try:
        pdf_bytes = generate_jd14_pdf(session_row, jd14_row, signed_by_user)
    except Exception:  # noqa: BLE001 - surface a clean message rather than a 500
        current_app.logger.exception("JD14 preview PDF generation failed for session %s", session_id)
        flash("Could not generate the JD14 Form PDF. Is wkhtmltopdf installed on the server?", "danger")
        return redirect(url_for("jd14.edit", session_id=session_id))
    return Response(
        pdf_bytes, mimetype="application/pdf",
        headers={"Content-Disposition": 'inline; filename="JD14_Form_preview.pdf"'},
    )


@bp.route("/sessions/<int:session_id>/download")
@login_required
def download(session_id):
    session_row, jd14_row, signed_by_user = _pdf_row_and_signer(session_id)
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("jd14.index"))
    try:
        pdf_bytes = generate_jd14_pdf(session_row, jd14_row, signed_by_user)
    except Exception:  # noqa: BLE001
        current_app.logger.exception("JD14 download PDF generation failed for session %s", session_id)
        flash("Could not generate the JD14 Form PDF. Is wkhtmltopdf installed on the server?", "danger")
        return redirect(url_for("jd14.edit", session_id=session_id))
    filename = f"JD14_Form_{session_row['course_title']}_{session_row['start_date']}.pdf".replace(" ", "_")
    return Response(
        pdf_bytes, mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/sessions/<int:session_id>/sign", methods=("POST",))
@login_required
@admin_required
def sign(session_id):
    session_row = _session_or_none(session_id)
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("jd14.index"))

    if not g.user["signature_file"]:
        flash("Upload your signature on your Profile first, then come back to sign the JD14 Form.", "danger")
        return redirect(url_for("profile.edit"))

    # Make sure a jd14_forms row exists (with whatever is currently shown -
    # saved edits, or freshly derived values if nothing was ever saved) so
    # signing never happens against a row that doesn't exist yet.
    if db.query("SELECT id FROM jd14_forms WHERE session_id = ?", (session_id,), one=True) is None:
        _save_fields(session_id, _derive_prefill(session_row))

    db.execute(
        "UPDATE jd14_forms SET signed_by_user_id = ?, signed_at = datetime('now'), "
        "updated_at = datetime('now'), updated_by = ? WHERE session_id = ?",
        (g.user["id"], g.user["id"], session_id),
    )
    if not settings.get_company_stamp_file():
        flash("Signed. Note: no company stamp is on file yet (Settings -> Company Stamp) - the "
              "stamp box will render empty until one is uploaded.", "warning")
    else:
        flash("JD14 Form signed.", "success")
    return redirect(url_for("jd14.edit", session_id=session_id))


@bp.route("/sessions/<int:session_id>/send", methods=("POST",))
@login_required
@admin_required
def send(session_id):
    session_row = _session_or_none(session_id)
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("jd14.index"))

    jd14_row = db.query("SELECT * FROM jd14_forms WHERE session_id = ?", (session_id,), one=True)
    if jd14_row is None or not jd14_row["signed_at"]:
        flash("Sign the JD14 Form first, then send it.", "danger")
        return redirect(url_for("jd14.edit", session_id=session_id))

    to_email = (session_row["company_email"] or "").strip()
    if not to_email:
        flash("This class's client company has no email on file to send the JD14 Form to.", "danger")
        return redirect(url_for("jd14.edit", session_id=session_id))

    signed_by_user = db.query("SELECT * FROM users WHERE id = ?", (jd14_row["signed_by_user_id"],), one=True)
    try:
        pdf_bytes = generate_jd14_pdf(session_row, jd14_row, signed_by_user)
    except Exception:  # noqa: BLE001
        current_app.logger.exception("JD14 send PDF generation failed for session %s", session_id)
        flash("Could not generate the JD14 Form PDF. Is wkhtmltopdf installed on the server?", "danger")
        return redirect(url_for("jd14.edit", session_id=session_id))

    return_token = ensure_jd14_return_token(session_id)
    return_url = url_for("jd14_return.details", token=return_token, _external=True)
    subject = f"JD14 Form - {session_row['course_title']}"
    body = (
        f"Hi,\n\nPlease find attached the JD14 Joint Declaration Form (PSMB/SBL-KHAS/JD/14) for "
        f"{session_row['course_title']}, with our part completed and signed.\n\n"
        "Please fill in your part, sign and stamp it, then upload the signed copy directly here "
        f"for our reference - no need to email it separately:\n{return_url}\n\n"
        "Thank you."
    )
    attachments = [("JD14_Form.pdf", pdf_bytes, "application/pdf")]
    try:
        mailer.send_email(to_email, subject, body, attachments=attachments,
                           related_type="course_session", related_id=session_id)
    except mailer.MailNotConfigured as exc:
        flash(str(exc), "danger")
        return redirect(url_for("jd14.edit", session_id=session_id))
    except mailer.MailSendError as exc:
        flash(f"Email failed to send: {exc}", "danger")
        return redirect(url_for("jd14.edit", session_id=session_id))

    flash(f"JD14 Form emailed to {to_email}.", "success")
    return redirect(url_for("jd14.edit", session_id=session_id))
