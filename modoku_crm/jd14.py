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

NOTE (Fix93) - the on-screen live preview in templates/jd14/edit.html is
rendered by pdfgen._build_jd14_html itself (see live_preview()), so there
is ONE layout to maintain; the preview and the PDF can't drift apart.
"""
from flask import Blueprint, Response, current_app, flash, g, redirect, render_template, request, url_for

from . import activity, db, fmtaddress, mailer, settings
from .auth import admin_required, login_required
from .pdfgen import _build_jd14_html, generate_jd14_pdf
from .sessions import ensure_jd14_return_token

bp = Blueprint("jd14", __name__, url_prefix="/jd14")


def _session_or_none(session_id):
    return db.query(
        """SELECT cs.*, c.title AS course_title, co.name AS company_name, co.address AS company_address,
                  co.city AS company_city, co.state AS company_state, co.postcode AS company_postcode,
                  co.email AS company_email, pic.name AS pic_name, pic.email AS pic_email
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN companies co ON co.id = cs.client_company_id
           LEFT JOIN leads pic ON pic.id = cs.pic_lead_id
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
    derived["sent_at"] = None
    derived["sent_to"] = None
    return derived


def jd14_stage(session_row, jd14_row):
    """The JD14 pipeline stage for a class, driven entirely by existing or
    derived state - this helper never writes anything itself. One of:
      - "not_signed": our half hasn't been signed yet - the pipeline
        (Sent -> Awaiting Return -> Received) hasn't started, so nothing
        should be shown for it.
      - "preparing": signed, but not yet emailed to the client - "Sent"
        hasn't been reached yet.
      - "awaiting_return": emailed to the client (jd14_forms.sent_at is
        set), but their countersigned copy hasn't come back yet.
      - "received": the client's signed copy is on file - this reads
        course_sessions.jd14_file, which the EXISTING receive flow
        (jd14_return.py / sessions.upload_jd14) already maintains; this
        function never writes it, only reads it.
    session_row and jd14_row can be the SAME row object (e.g. one query
    joining course_sessions and jd14_forms together, as index() does) as
    long as it carries jd14_file, signed_at and sent_at - this function
    only ever reads those three fields by name."""
    if jd14_row is None or not jd14_row["signed_at"]:
        return "not_signed"
    if not jd14_row["sent_at"]:
        return "preparing"
    if not session_row["jd14_file"]:
        return "awaiting_return"
    return "received"


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


# Status is a derived label (no jd14_forms row at all / drafted-not-signed /
# signed / sent-awaiting-return / received), not a stored column, so sorting
# by it needs its own rank expression rather than a plain column name - kept
# in the same order the badges progress in on the page.
_STATUS_RANK_SQL = """CASE
    WHEN cs.jd14_file IS NOT NULL THEN 4
    WHEN jf.sent_at IS NOT NULL THEN 3
    WHEN jf.signed_at IS NOT NULL THEN 2
    WHEN jf.id IS NOT NULL THEN 1
    ELSE 0
END"""

SORTABLE_COLUMNS = {
    "class": "c.title",
    "date": "cs.start_date",
    "status": _STATUS_RANK_SQL,
}


@bp.route("/")
@login_required
def index():
    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "date")
    direction = request.args.get("dir", "desc")
    if sort not in SORTABLE_COLUMNS:
        sort = "date"
    if direction not in ("asc", "desc"):
        direction = "desc"

    sql = """SELECT cs.id, cs.start_date, cs.end_date, cs.status, cs.jd14_file, c.title AS course_title,
                    co.name AS client_name, jf.signed_at, jf.sent_at, jf.id AS jd14_forms_id
             FROM course_sessions cs
             JOIN courses c ON c.id = cs.course_id
             LEFT JOIN companies co ON co.id = cs.client_company_id
             LEFT JOIN jd14_forms jf ON jf.session_id = cs.id
             WHERE 1=1"""
    args = []
    if q:
        sql += " AND (c.title LIKE ? OR co.name LIKE ? OR cs.venue LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += f" ORDER BY {SORTABLE_COLUMNS[sort]} {direction.upper()}, cs.start_date DESC"

    rows = db.query(sql, args)
    # jd14_stage() only reads jd14_file/signed_at/sent_at, all three already
    # on this one merged row - see its own docstring for why passing the
    # same row as both its session_row and jd14_row arguments is fine here.
    sessions = [dict(r, jd14_stage=jd14_stage(r, r)) for r in rows]
    return render_template("jd14/index.html", sessions=sessions, q=q, sort=sort, direction=direction)


def _default_send_subject(session_row):
    return f"JD14 Form - {session_row['course_title']}"


def _default_send_body(session_row, return_url):
    greeting = session_row["pic_name"] if session_row["pic_name"] else "there"
    return (
        f"Hi {greeting},\n\nPlease find attached the JD14 Joint Declaration Form (PSMB/SBL-KHAS/JD/14) for "
        f"{session_row['course_title']}, with our part completed and signed.\n\n"
        "Please fill in your part, sign and stamp it, then upload the signed copy directly here "
        f"for our reference - no need to email it separately:\n{return_url}\n\n"
        "Thank you."
    )


def _return_url(session_id):
    return url_for("jd14_return.details", token=ensure_jd14_return_token(session_id), _external=True)


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
    # Fix92: once signed, the page shows the actual email that "Send to
    # PIC" will send (to/CC/subject/message, all editable) before sending.
    email_defaults = None
    is_admin = bool(g.user and g.user["role"] == "admin")
    if jd14_row["signed_at"] and is_admin:
        email_defaults = {
            "to_email": session_row["pic_email"] or "",
            "subject": _default_send_subject(session_row),
            "body": _default_send_body(session_row, _return_url(session_id)),
        }
    return render_template(
        "jd14/edit.html", s=session_row, f=jd14_row, signed_by_user=signed_by_user,
        preview_user=preview_user, company_stamp_file=settings.get_company_stamp_file(),
        is_admin=is_admin, email_defaults=email_defaults,
        jd14_stage_value=jd14_stage(session_row, jd14_row),
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


@bp.route("/sessions/<int:session_id>/live-preview", methods=("POST",))
@login_required
def live_preview(session_id):
    """Fix93: the edit page's live preview. Returns the exact HTML the PDF
    is built from (pdfgen._build_jd14_html), using the values currently
    typed on the page (not yet saved), so the on-screen preview IS the PDF
    layout. Before anyone has signed, the logged-in user's own signature/
    name/stamp are shown as a preview of what signing will produce -
    sign() is what actually records who signed."""
    session_row = _session_or_none(session_id)
    if session_row is None:
        return Response("Session not found.", status=404)
    stored = _jd14_row_or_derived(session_row)
    row = {key: stored[key] for key in stored.keys()}
    for field in _FORM_FIELDS:
        if field in request.form:
            row[field] = request.form.get(field, "")
    signer = None
    if row.get("signed_by_user_id"):
        signer = db.query("SELECT * FROM users WHERE id = ?", (row["signed_by_user_id"],), one=True)
    elif g.user is not None and g.user["signature_file"]:
        signer = g.user
    html = _build_jd14_html(session_row, row, signer, for_browser=True)
    return Response(html, mimetype="text/html")


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
    # Fix93: guard against double sending - once sent, a resend has to come
    # from the page's explicit "send it again" box (which carries
    # confirm_resend), never from a stray double-click or re-submitted form.
    if jd14_row["sent_at"] and request.form.get("confirm_resend") != "1":
        flash(f"Not sent again: this JD14 Form was already emailed to {jd14_row['sent_to']}. "
              "Use \"Need to send it again?\" if you really want to resend.", "warning")
        return redirect(url_for("jd14.edit", session_id=session_id))

    # Fix92: goes to the class's PIC (the person handling it on the client
    # side), not the company's general email - and whatever was typed in
    # the email preview box on the page wins over the defaults.
    to_email = (request.form.get("to_email") or session_row["pic_email"] or "").strip()
    if not to_email:
        flash("No PIC email on file for this class. Select a PIC on the class's Edit page, "
              "or type an address to send to.", "danger")
        return redirect(url_for("jd14.edit", session_id=session_id))
    cc_email = (request.form.get("cc_email") or "").strip() or None

    signed_by_user = db.query("SELECT * FROM users WHERE id = ?", (jd14_row["signed_by_user_id"],), one=True)
    try:
        pdf_bytes = generate_jd14_pdf(session_row, jd14_row, signed_by_user)
    except Exception:  # noqa: BLE001
        current_app.logger.exception("JD14 send PDF generation failed for session %s", session_id)
        flash("Could not generate the JD14 Form PDF. Is wkhtmltopdf installed on the server?", "danger")
        return redirect(url_for("jd14.edit", session_id=session_id))

    subject = (request.form.get("subject") or "").strip() or _default_send_subject(session_row)
    body = (request.form.get("body") or "").strip() or _default_send_body(session_row, _return_url(session_id))
    attachments = [("JD14_Form.pdf", pdf_bytes, "application/pdf")]
    try:
        mailer.send_email(to_email, subject, body, attachments=attachments,
                           related_type="course_session", related_id=session_id, cc_email=cc_email)
    except mailer.MailNotConfigured as exc:
        flash(str(exc), "danger")
        return redirect(url_for("jd14.edit", session_id=session_id))
    except mailer.MailSendError as exc:
        flash(f"Email failed to send: {exc}", "danger")
        return redirect(url_for("jd14.edit", session_id=session_id))

    # Drives the Sent -> Awaiting Return -> Received status tracker
    # (jd14_stage()) - the receiving half (course_sessions.jd14_file) is
    # still tracked entirely by the EXISTING jd14_return.py flow, untouched
    # here.
    db.execute(
        "UPDATE jd14_forms SET sent_at = datetime('now'), sent_to = ? WHERE session_id = ?",
        (to_email, session_id),
    )

    activity.log("send_email", "session", session_id, f"Sent JD14 Form to {to_email}")
    flash(f"JD14 Form emailed to {to_email}.", "success")
    return redirect(url_for("jd14.edit", session_id=session_id))
