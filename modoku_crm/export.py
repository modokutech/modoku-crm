"""Per-year data export — an admin-only "give me everything from a given
year as a zip" button, organized into one folder per Class (matching how
Erik thinks about the data day to day) with every file that class
accumulated (attendance sheets/photos, evaluation report, training banner,
JD14, client logo, evaluation QR poster, trainer invoice documents, PO
documents) plus a small text summary and a CSV rollup of the whole year.

This is a LOCAL download only — there's no Google Drive (or other cloud)
auto-push wired up, since that needs a service account / OAuth credentials
Erik hasn't set up. The zip this produces can be dragged into Drive (or
anywhere else) by hand after downloading.
"""
import csv
import io
import os
import re
import sqlite3
import zipfile
from datetime import datetime

from flask import Blueprint, Response, current_app, render_template

from . import db
from .auth import admin_required, login_required
from .docutil import content_disposition

bp = Blueprint("export", __name__, url_prefix="/export")


def _slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_") or "untitled"


def _add_dir_to_zip(zf, source_dir, arc_prefix):
    """Copies every file (non-recursive — none of our upload dirs nest) from
    source_dir into the zip under arc_prefix, if source_dir exists."""
    if not os.path.isdir(source_dir):
        return
    for fname in sorted(os.listdir(source_dir)):
        fpath = os.path.join(source_dir, fname)
        if os.path.isfile(fpath):
            zf.write(fpath, f"{arc_prefix}/{fname}")


@bp.route("/")
@login_required
@admin_required
def index():
    years = db.query(
        """SELECT DISTINCT substr(start_date, 1, 4) AS yr FROM course_sessions
           WHERE start_date IS NOT NULL AND start_date != '' ORDER BY yr DESC"""
    )
    return render_template("export/index.html", years=[y["yr"] for y in years if y["yr"]])


@bp.route("/db-backup")
@login_required
@admin_required
def db_backup():
    """A one-click, admin-only snapshot of the whole database — separate
    from the per-year Class export above (which only covers Classes and
    their files, not every table). Uses SQLite's own online backup API
    rather than just copying the .db file, so a snapshot taken while the
    app is live and being written to is never corrupted mid-copy. This is
    a manual, on-demand copy: real day-to-day protection should still come
    from an automated server-level backup (e.g. a nightly cron job copying
    the database file to another disk or off-site) — this button is the
    supplement for "let me grab a copy right now", not a replacement."""
    source_path = current_app.config["DATABASE"]
    buffer = io.BytesIO()
    tmp_path = source_path + ".backup_tmp"
    try:
        source_conn = sqlite3.connect(source_path)
        backup_conn = sqlite3.connect(tmp_path)
        with backup_conn:
            source_conn.backup(backup_conn)
        source_conn.close()
        backup_conn.close()
        with open(tmp_path, "rb") as f:
            buffer.write(f.read())
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    buffer.seek(0)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    filename = f"modoku_crm_backup_{stamp}.db"
    return Response(
        buffer.read(), mimetype="application/octet-stream",
        headers={"Content-Disposition": content_disposition(filename)},
    )


@bp.route("/<int:year>.zip")
@login_required
@admin_required
def download_year(year):
    sessions = db.query(
        """SELECT cs.*, c.title AS course_title, t.name AS trainer_name, cl.name AS client_name
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN trainers t ON t.id = cs.trainer_id
           LEFT JOIN companies cl ON cl.id = cs.client_company_id
           WHERE substr(cs.start_date, 1, 4) = ?
           ORDER BY cs.start_date, cs.id""",
        (str(year),),
    )

    upload_root = current_app.config["UPLOAD_FOLDER"]
    summary_buf = io.StringIO()
    summary_writer = csv.writer(summary_buf)
    summary_writer.writerow(["Session Code", "Course", "Start Date", "End Date", "Venue",
                              "Trainer", "Client", "Status", "Folder"])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        used_folders = set()
        for s in sessions:
            base_folder = f"{s['start_date']}_{_slug(s['session_code'] or str(s['id']))}_{_slug(s['course_title'])}"
            folder = base_folder
            n = 2
            while folder in used_folders:  # extremely unlikely, but keep folder names unique
                folder = f"{base_folder}_{n}"
                n += 1
            used_folders.add(folder)

            info = (
                f"Class: {s['course_title']}\n"
                f"Session Code: {s['session_code'] or '-'}\n"
                f"Dates: {s['start_date']} to {s['end_date'] or s['start_date']}\n"
                f"Time: {s['training_time'] or '-'}\n"
                f"Venue: {s['venue'] or '-'}\n"
                f"Training Type: {s['training_type'] or '-'}\n"
                f"Trainer: {s['trainer_name'] or '-'}\n"
                f"Client: {s['client_name'] or '-'}\n"
                f"Status: {s['status']}\n"
            )
            zf.writestr(f"{folder}/class_info.txt", info)

            # Attendance sheets/photos, evaluation report, training banner,
            # JD14, client logo, evaluation QR poster — all saved under the
            # same sessions/<id>/ directory.
            _add_dir_to_zip(zf, os.path.join(upload_root, "sessions", str(s["id"])), f"{folder}/class_files")

            # Trainer invoice documents (claims/receipts the trainer uploaded).
            _add_dir_to_zip(zf, os.path.join(upload_root, "trainer_invoices", str(s["id"])),
                             f"{folder}/trainer_invoice_documents")

            # Signed/returned documents for any Purchase Order tied to this class.
            pos = db.query("SELECT id, po_no FROM purchase_orders WHERE session_id = ?", (s["id"],))
            for po in pos:
                po_dir = os.path.join(upload_root, "purchase_orders", str(po["id"]))
                _add_dir_to_zip(zf, po_dir, f"{folder}/purchase_order_documents/{_slug(po['po_no'])}")

            summary_writer.writerow([
                s["session_code"] or "", s["course_title"], s["start_date"], s["end_date"] or "",
                s["venue"] or "", s["trainer_name"] or "", s["client_name"] or "", s["status"], folder,
            ])

        # UTF-8 BOM so Excel opens accented text correctly, matching the rest
        # of the app's CSV exports.
        zf.writestr(f"classes_{year}_summary.csv", "﻿" + summary_buf.getvalue())

    buf.seek(0)
    return Response(
        buf.getvalue(), mimetype="application/zip",
        headers={"Content-Disposition": content_disposition(f"modoku_classes_{year}.zip")},
    )
