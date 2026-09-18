"""Staff-side "Certificates" tab — every generated e-Certificate, grouped by
Class, browsable without needing each participant to claim theirs on the
public front-end. Certificates themselves are generated (and kept in sync)
by certificates.generate_and_store_certificate, called the moment a
participant is marked attended (see t3.bulk_attended) — this module only
browses/serves what's already on file, self-healing on the fly via
certificates.get_or_create_certificate_bytes for the rare case a file is
missing (e.g. very old data marked attended before this tab existed).
"""
import io
import zipfile

from flask import (Blueprint, Response, current_app, flash, redirect, render_template,
                   request, url_for)

from . import db, fmtdate, fmtdaterange
from .auth import login_required
from .certificates import certificate_filename, get_or_create_certificate_bytes, safe_slug
from .pdfgen import generate_certificate_pdf

# A hand-typed batch is for filling gaps (a walk-in, a corrected spelling, a
# reprint), not for bulk issuing, so the list is capped at something a person
# would plausibly type.
MANUAL_NAME_LIMIT = 50

bp = Blueprint("cert_admin", __name__, url_prefix="/certificates")


@bp.route("/")
@login_required
def index():
    sessions = db.query(
        """SELECT cs.id, cs.start_date, cs.end_date, cs.status, c.title AS course_title,
                  COUNT(p.id) AS attended_count,
                  SUM(CASE WHEN cert.id IS NOT NULL THEN 1 ELSE 0 END) AS cert_count
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           JOIN t3_participants p ON p.session_id = cs.id AND p.attended = 1
           LEFT JOIN certificates cert ON cert.participant_id = p.id
           GROUP BY cs.id
           ORDER BY cs.start_date DESC"""
    )
    # Every class is offered for manual issuing, not just those with an
    # attendance list — the whole point is to cover people the T3 flow never
    # captured.
    classes = db.query(
        """SELECT cs.id, cs.start_date, cs.end_date, c.title AS course_title
           FROM course_sessions cs JOIN courses c ON c.id = cs.course_id
           ORDER BY cs.start_date DESC"""
    )
    return render_template("cert_admin/index.html", sessions=sessions, classes=classes,
                            manual_name_limit=MANUAL_NAME_LIMIT)


@bp.route("/manual", methods=("POST",))
@login_required
def manual():
    """Issues certificates for names typed in by hand, taking the course
    title and training dates from the chosen class exactly as the automatic
    path does.

    Deliberately does NOT write to t3_participants or the certificates
    table. The T3 attendance list is the signed document behind an HRDCorp
    claim, so adding names to it for people who never signed would corrupt a
    record the business has to stand behind; and `certificates` rows are
    keyed to a participant row that doesn't exist here. These come back as a
    direct download instead: one PDF for a single name, a zip for several.
    """
    session_id = request.form.get("session_id", type=int)
    raw_names = request.form.get("names") or ""
    names = [n.strip() for n in raw_names.splitlines() if n.strip()]

    session_row = db.query(
        """SELECT cs.id, cs.start_date, cs.end_date, c.title AS course_title
           FROM course_sessions cs JOIN courses c ON c.id = cs.course_id
           WHERE cs.id = ?""",
        (session_id,), one=True,
    ) if session_id else None

    if session_row is None:
        flash("Choose which class the certificate is for.", "danger")
        return redirect(url_for("cert_admin.index"))
    if not names:
        flash("Type at least one participant name, one per line.", "danger")
        return redirect(url_for("cert_admin.index"))
    if len(names) > MANUAL_NAME_LIMIT:
        flash(f"That is {len(names)} names. Manual issuing handles up to {MANUAL_NAME_LIMIT} at a "
              f"time; for a whole cohort, mark them attended on the T3 Attendance List instead.",
              "danger")
        return redirect(url_for("cert_admin.index"))

    date_range = fmtdaterange(session_row["start_date"], session_row["end_date"])
    course_title = session_row["course_title"]
    try:
        start_date = session_row["start_date"]
        if len(names) == 1:
            pdf_bytes = generate_certificate_pdf(names[0], course_title, date_range)
            filename = certificate_filename(names[0], course_title, start_date)
            return Response(
                pdf_bytes, mimetype="application/pdf",
                headers={"Content-Disposition": f'inline; filename="{filename}.pdf"'},
            )

        buffer = io.BytesIO()
        seen_names = {}
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in names:
                base_name = certificate_filename(name, course_title, start_date)
                # Two people with the same name would otherwise overwrite each
                # other inside the zip — same disambiguation the bulk download uses.
                count = seen_names.get(base_name, 0)
                seen_names[base_name] = count + 1
                entry_name = f"{base_name}.pdf" if count == 0 else f"{base_name}_{count + 1}.pdf"
                archive.writestr(entry_name, generate_certificate_pdf(name, course_title, date_range))
        buffer.seek(0)
        zip_name = f"Certificates_{safe_slug(course_title)}_{safe_slug(fmtdate(start_date))}.zip"
        return Response(
            buffer.read(), mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
        )
    except Exception:  # noqa: BLE001 - surface a clean message rather than a 500
        current_app.logger.exception("Manual certificate generation failed for class %s", session_id)
        flash("Could not generate the certificate. Check that PDF generation is set up on the server.",
              "danger")
        return redirect(url_for("cert_admin.index"))


@bp.route("/sessions/<int:session_id>")
@login_required
def by_session(session_id):
    session_row = db.query(
        """SELECT cs.*, c.title AS course_title FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?""",
        (session_id,), one=True,
    )
    if session_row is None:
        flash("Class not found.", "danger")
        return redirect(url_for("cert_admin.index"))
    participants = db.query(
        """SELECT p.*, cert.filename AS cert_filename, cert.generated_at AS cert_generated_at
           FROM t3_participants p
           LEFT JOIN certificates cert ON cert.participant_id = p.id
           WHERE p.session_id = ? AND p.attended = 1
           ORDER BY p.name COLLATE NOCASE""",
        (session_id,),
    )
    return render_template("cert_admin/session.html", s=session_row, participants=participants)


@bp.route("/participants/<int:participant_id>/download")
@login_required
def download_one(participant_id):
    participant = db.query(
        """SELECT p.*, cs.start_date, c.title AS course_title
           FROM t3_participants p
           JOIN course_sessions cs ON cs.id = p.session_id
           JOIN courses c ON c.id = cs.course_id
           WHERE p.id = ?""",
        (participant_id,), one=True,
    )
    if participant is None or not participant["attended"]:
        flash("Certificate not available for this participant.", "danger")
        return redirect(url_for("cert_admin.index"))
    try:
        pdf_bytes = get_or_create_certificate_bytes(participant_id)
    except Exception:  # noqa: BLE001 - surface a clean message rather than a 500/blank response
        current_app.logger.exception("Failed to generate certificate for participant %s", participant_id)
        pdf_bytes = None
    if not pdf_bytes:
        flash("Couldn't generate this certificate. Is wkhtmltopdf installed on the server?", "danger")
        return redirect(url_for("cert_admin.by_session", session_id=participant["session_id"]))
    filename = certificate_filename(participant["name"], participant["course_title"], participant["start_date"])
    return Response(
        pdf_bytes, mimetype="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}.pdf"'},
    )


@bp.route("/sessions/<int:session_id>/download-zip")
@login_required
def download_zip(session_id):
    session_row = db.query(
        """SELECT cs.*, c.title AS course_title FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?""",
        (session_id,), one=True,
    )
    if session_row is None:
        flash("Class not found.", "danger")
        return redirect(url_for("cert_admin.index"))
    participants = db.query(
        "SELECT * FROM t3_participants WHERE session_id = ? AND attended = 1 ORDER BY name COLLATE NOCASE",
        (session_id,),
    )
    if not participants:
        flash("No attended participants with certificates for this class yet.", "warning")
        return redirect(url_for("cert_admin.by_session", session_id=session_id))

    buf = io.BytesIO()
    included = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        seen_names = {}
        for p in participants:
            try:
                pdf_bytes = get_or_create_certificate_bytes(p["id"])
            except Exception:  # noqa: BLE001 - skip this one certificate rather than failing the whole zip
                current_app.logger.exception("Failed to generate certificate for participant %s", p["id"])
                pdf_bytes = None
            if not pdf_bytes:
                continue
            base_name = certificate_filename(p["name"], session_row["course_title"], session_row["start_date"])
            # Disambiguate same-name participants within one zip.
            count = seen_names.get(base_name, 0)
            seen_names[base_name] = count + 1
            entry_name = f"{base_name}.pdf" if count == 0 else f"{base_name}_{count + 1}.pdf"
            zf.writestr(entry_name, pdf_bytes)
            included += 1

    if not included:
        flash("Couldn't generate any certificates for this class. Is wkhtmltopdf installed on the server?", "danger")
        return redirect(url_for("cert_admin.by_session", session_id=session_id))

    buf.seek(0)
    zip_name = safe_slug(f"Certificates_{session_row['course_title']}_{session_row['start_date']}")
    return Response(
        buf.read(), mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}.zip"'},
    )
