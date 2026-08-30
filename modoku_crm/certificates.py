"""Public "Claim Your e-Certificate" flow — no login required.

Participants aren't given a unique code; instead they identify themselves
with their training's start date plus their IC number (matched digits-only
so dashes/spaces typed either way still match). A match is only eligible
if staff have cross-checked the returned attendance form and marked that
participant `attended` on the T3 list (see t3.bulk_attended) — otherwise
we show a "didn't complete" message rather than a certificate, and if no
participant matches at all we show a "not found" message. Both failure
states point the person to contact Modoku's PIC/trainer rather than dead-
ending them.

Certificates are generated once and stored on disk (see
generate_and_store_certificate, called from t3.bulk_attended the moment a
participant is marked attended) so the same file is served here, browsable
by staff under the Certificates tab (cert_admin.py), and in a bulk zip
download — rather than each surface regenerating its own copy. This module
still self-heals (generates on the spot) if a stored file is somehow
missing, so the public claim flow never hard-depends on that having
happened first. The claim page re-validates and re-looks up on the actual
"Download" submit rather than trusting a participant id in a URL, so
nothing enumerable is ever exposed.
"""
import os
import re

from flask import Blueprint, Response, current_app, render_template, request

from . import db, fmtdate, fmtdaterange
from .pdfgen import generate_certificate_pdf

bp = Blueprint("certificates", __name__, url_prefix="/cert")


def _digits_only(value):
    return re.sub(r"\D", "", value or "")


def cert_dir(session_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "certificates", str(session_id))
    os.makedirs(path, exist_ok=True)
    return path


def safe_slug(name):
    return re.sub(r"[^A-Za-z0-9]+", "_", name or "").strip("_") or "certificate"


def certificate_filename(name, course_title, start_date):
    """The download-facing certificate filename (without extension), per
    Erik's naming convention: Cert_Modoku_<name>_<course_title>_<date>.
    Used everywhere a certificate is actually handed to someone — the
    public claim download, the staff Certificates tab's single download,
    and each entry in the staff bulk zip — so the name is consistent no
    matter how it was fetched. (The on-disk stored filename used internally
    by generate_and_store_certificate is unrelated and never shown to
    anyone.)"""
    parts = ["Cert_Modoku", safe_slug(name), safe_slug(course_title)]
    if start_date:
        parts.append(safe_slug(fmtdate(start_date)))
    return "_".join(p for p in parts if p)


def generate_and_store_certificate(participant_id):
    """(Re)generates the certificate PDF for one t3_participants row (must
    already be marked attended) and records/overwrites it in the
    `certificates` table + on disk. Called from t3.bulk_attended the moment
    staff mark a participant attended, so certificates are ready and
    browsable ahead of time under the Certificates tab rather than only
    ever being built on demand when a participant claims theirs. Returns
    the stored filename, or None if the participant isn't (or is no
    longer) eligible."""
    row = db.query(
        """SELECT p.*, cs.start_date, cs.end_date, c.title AS course_title
           FROM t3_participants p
           JOIN course_sessions cs ON cs.id = p.session_id
           JOIN courses c ON c.id = cs.course_id
           WHERE p.id = ?""",
        (participant_id,), one=True,
    )
    if row is None or not row["attended"]:
        return None
    date_range = fmtdaterange(row["start_date"], row["end_date"])
    pdf_bytes = generate_certificate_pdf(row["name"], row["course_title"], date_range)
    filename = f"cert_{participant_id}_{safe_slug(row['name'])}.pdf"
    with open(os.path.join(cert_dir(row["session_id"]), filename), "wb") as f:
        f.write(pdf_bytes)
    db.execute(
        """INSERT INTO certificates (session_id, participant_id, filename, generated_at)
           VALUES (?,?,?,datetime('now'))
           ON CONFLICT(participant_id) DO UPDATE SET filename=excluded.filename,
               generated_at=excluded.generated_at""",
        (row["session_id"], participant_id, filename),
    )
    return filename


def remove_certificate(participant_id):
    """Deletes a participant's stored certificate file + row — called from
    t3.bulk_attended when staff un-mark someone as attended, so the
    Certificates tab and claim flow never keep serving a stale/ineligible
    certificate."""
    row = db.query("SELECT session_id, filename FROM certificates WHERE participant_id = ?",
                    (participant_id,), one=True)
    if row is None:
        return
    try:
        os.remove(os.path.join(cert_dir(row["session_id"]), row["filename"]))
    except OSError:
        pass
    db.execute("DELETE FROM certificates WHERE participant_id = ?", (participant_id,))


def get_or_create_certificate_bytes(participant_id):
    """Returns the PDF bytes for an attended participant's certificate,
    reading the stored file when one is on record and self-healing
    (generating + storing one now) otherwise. Returns None if the
    participant isn't eligible or generation fails."""
    row = db.query(
        """SELECT p.session_id, p.attended, cert.filename AS cert_filename
           FROM t3_participants p LEFT JOIN certificates cert ON cert.participant_id = p.id
           WHERE p.id = ?""",
        (participant_id,), one=True,
    )
    if row is None or not row["attended"]:
        return None
    filename = row["cert_filename"]
    if filename:
        try:
            with open(os.path.join(cert_dir(row["session_id"]), filename), "rb") as f:
                return f.read()
        except OSError:
            pass  # file missing on disk somehow — fall through and regenerate
    filename = generate_and_store_certificate(participant_id)
    if not filename:
        return None
    try:
        with open(os.path.join(cert_dir(row["session_id"]), filename), "rb") as f:
            return f.read()
    except OSError:
        return None


def _find_participant(start_date, ic_no):
    """Returns the matching t3_participants row (joined with its session's
    course/date info), or None. Matching is scoped to one session's start
    date, then narrowed by IC number (digits-only) among that date's
    participants — cheap since a single training day has few cohorts."""
    ic_digits = _digits_only(ic_no)
    if not start_date or not ic_digits:
        return None
    rows = db.query(
        """SELECT p.*, cs.id AS session_id, cs.start_date, cs.end_date,
                  c.title AS course_title
           FROM t3_participants p
           JOIN course_sessions cs ON cs.id = p.session_id
           JOIN courses c ON c.id = cs.course_id
           WHERE cs.start_date = ?""",
        (start_date,),
    )
    for row in rows:
        if _digits_only(row["ic_no"]) == ic_digits:
            return row
    return None


@bp.route("/", methods=("GET",))
def lookup():
    return render_template("certificates/lookup.html")


@bp.route("/claim", methods=("POST",))
def claim():
    start_date = (request.form.get("start_date") or "").strip()
    ic_no = (request.form.get("ic_no") or "").strip()

    participant = _find_participant(start_date, ic_no)
    if participant is None:
        return render_template("certificates/not_found.html", start_date=start_date, ic_no=ic_no)
    if not participant["attended"]:
        return render_template(
            "certificates/not_completed.html", p=participant, start_date=start_date, ic_no=ic_no,
        )
    date_range = fmtdaterange(participant["start_date"], participant["end_date"])
    return render_template(
        "certificates/success.html", p=participant, date_range=date_range,
        start_date=start_date, ic_no=ic_no,
    )


@bp.route("/download", methods=("POST",))
def download():
    start_date = (request.form.get("start_date") or "").strip()
    ic_no = (request.form.get("ic_no") or "").strip()

    participant = _find_participant(start_date, ic_no)
    if participant is None or not participant["attended"]:
        # Re-validated on every download so an edited/replayed form can
        # never produce a certificate for someone not actually eligible.
        return render_template("certificates/not_found.html", start_date=start_date, ic_no=ic_no)

    try:
        pdf_bytes = get_or_create_certificate_bytes(participant["id"])
    except Exception:  # noqa: BLE001 - surface a clean message rather than a broken/blank download
        current_app.logger.exception("Failed to generate certificate for participant %s", participant["id"])
        pdf_bytes = None
    if not pdf_bytes:
        return render_template(
            "certificates/generation_failed.html", p=participant, start_date=start_date, ic_no=ic_no,
        )

    filename = certificate_filename(participant["name"], participant["course_title"], participant["start_date"])
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}.pdf"'},
    )
