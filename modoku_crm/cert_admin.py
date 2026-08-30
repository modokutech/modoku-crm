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

from flask import Blueprint, Response, current_app, flash, redirect, render_template, url_for

from . import db
from .auth import login_required
from .certificates import certificate_filename, get_or_create_certificate_bytes, safe_slug

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
    return render_template("cert_admin/index.html", sessions=sessions)


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
        flash("Couldn't generate this certificate — is wkhtmltopdf installed on the server?", "danger")
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
        flash("Couldn't generate any certificates for this class — is wkhtmltopdf installed on the server?", "danger")
        return redirect(url_for("cert_admin.by_session", session_id=session_id))

    buf.seek(0)
    zip_name = safe_slug(f"Certificates_{session_row['course_title']}_{session_row['start_date']}")
    return Response(
        buf.read(), mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}.zip"'},
    )
