"""QR Posters (Fix179): branded QR posters for links participants open on
their phones during a class - mainly the Pre-Assessment Test and
Post-Assessment Test Google Forms. Flow: choose the class, Pre or Post,
paste the link, Generate.

Same look as the Training Evaluation poster (poster.generate_qr_poster):
heading, QR code, course title and date. The Google Form itself is still made
by hand; this saves the poster work. Each poster can be linked to a class
(the course title and dates come from it, and the poster is listed on the
class page).

After building a poster, its QR code is read back with OpenCV and the result
compared with the link, so a poster that wouldn't scan never goes out
unnoticed.
"""
import os
import re
import uuid

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request,
                   send_from_directory, url_for)

from . import activity, banner, db, poster
from .auth import login_required

bp = Blueprint("qr_posters", __name__, url_prefix="/qr-posters")

KINDS = {
    "pre": "Pre-Assessment Test",
    "post": "Post-Assessment Test",
}
INSTRUCTION = "Scan the QR code with your phone camera"


def _poster_dir():
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "qr_posters")
    os.makedirs(path, exist_ok=True)
    return path


def _logo_path():
    return os.path.join(current_app.root_path, "static", "img", "logo.png")


def _get(poster_id):
    row = db.query(
        """SELECT qp.*, c.title AS class_course_title, cs.start_date, cs.end_date, cs.status AS class_status,
                  u.name AS created_by_name
           FROM qr_posters qp
           LEFT JOIN course_sessions cs ON cs.id = qp.session_id
           LEFT JOIN courses c ON c.id = cs.course_id
           LEFT JOIN users u ON u.id = qp.created_by
           WHERE qp.id = ?""",
        (poster_id,), one=True,
    )
    if row is None:
        abort(404)
    return row


def _class_choices():
    """Classes to pick from (newest first, cancelled left out), each with the
    course title and the same date wording the evaluation poster uses."""
    rows = db.query(
        """SELECT cs.id, cs.start_date, cs.end_date, c.title AS course_title, co.name AS client_name
           FROM course_sessions cs JOIN courses c ON c.id = cs.course_id
           LEFT JOIN companies co ON co.id = cs.client_company_id
           WHERE cs.status != 'Cancelled'
           ORDER BY cs.start_date DESC"""
    )
    return [dict(r, date_text=banner._fmt_date_range(r["start_date"], r["end_date"])) for r in rows]


def _form_values():
    """Class + Pre/Post + link from the form; the course title and dates are
    always taken from the class itself."""
    kind = request.form.get("kind", "pre")
    if kind not in KINDS:
        kind = "pre"
    try:
        session_id = int(request.form.get("session_id") or 0) or None
    except ValueError:
        session_id = None
    cls = next((c for c in _class_choices() if c["id"] == session_id), None) if session_id else None
    return {
        "kind": kind,
        "heading": KINDS[kind],
        "session_id": session_id,
        "course_title": cls["course_title"] if cls else "",
        "date_text": cls["date_text"] if cls else "",
        "link": request.form.get("link", "").strip(),
    }


def _validate(v):
    if not v["session_id"] or not v["course_title"]:
        return "Choose the class first."
    if not re.match(r"^https?://\S+$", v["link"]):
        return "Paste the full Google Form link, starting with https:// (e.g. https://forms.gle/...)."
    return None


def _build(v):
    """Renders the poster, saves it and returns (filename, scan_ok)."""
    jpeg = poster.generate_qr_poster(v["heading"], v["course_title"], v["date_text"], v["link"],
                                     logo_path=_logo_path(), square_qr=True, instruction=INSTRUCTION)
    name = f"qrposter_{uuid.uuid4().hex[:10]}.jpg"
    with open(os.path.join(_poster_dir(), name), "wb") as f:
        f.write(jpeg)
    return name, poster.qr_scans_to(jpeg, v["link"])


def _flash_scan(scan_ok):
    if scan_ok is False:
        flash("Heads up: the QR code on this poster didn't read back as your link in our check. Test it "
              "with your phone before using it, and shorten the link if it doesn't open.", "warning")


def _download_name(row, ext):
    base = f"{row['heading']} - {row['course_title']}"
    base = re.sub(r"[^A-Za-z0-9 &()_.-]+", "", base).strip()[:120] or "QR Poster"
    return f"{base}.{ext}"


@bp.route("/")
@login_required
def index():
    rows = db.query(
        """SELECT qp.*, cs.start_date, u.name AS created_by_name
           FROM qr_posters qp
           LEFT JOIN course_sessions cs ON cs.id = qp.session_id
           LEFT JOIN users u ON u.id = qp.created_by
           ORDER BY qp.id DESC"""
    )
    return render_template("qr_posters/index.html", posters=rows)


@bp.route("/new", methods=("GET", "POST"))
@login_required
def new():
    if request.method == "POST":
        v = _form_values()
        error = _validate(v)
        if error:
            flash(error, "danger")
        else:
            filename, scan_ok = _build(v)
            poster_id = db.execute(
                """INSERT INTO qr_posters (session_id, kind, heading, course_title, date_text, link, poster_file,
                                           created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (v["session_id"], v["kind"], v["heading"], v["course_title"], v["date_text"], v["link"],
                 filename, g.user["id"] if g.user else None),
            )
            activity.log("create", "qr_poster", poster_id, f"Created QR poster: {v['heading']} - {v['course_title']}")
            flash("QR poster ready.", "success")
            _flash_scan(scan_ok)
            return redirect(url_for("qr_posters.view", poster_id=poster_id))
        values = v
    else:
        kind = request.args.get("kind", "pre")
        values = {"kind": kind if kind in KINDS else "pre", "link": "",
                  "session_id": request.args.get("session_id", type=int)}
    return render_template("qr_posters/form.html", values=values, classes=_class_choices(), kinds=KINDS,
                           poster_row=None)


@bp.route("/<int:poster_id>")
@login_required
def view(poster_id):
    return render_template("qr_posters/view.html", p=_get(poster_id), kinds=KINDS)


@bp.route("/<int:poster_id>/edit", methods=("GET", "POST"))
@login_required
def edit(poster_id):
    row = _get(poster_id)
    if request.method == "POST":
        v = _form_values()
        error = _validate(v)
        if error:
            flash(error, "danger")
            values = v
        else:
            filename, scan_ok = _build(v)
            old = row["poster_file"]
            db.execute(
                """UPDATE qr_posters SET session_id = ?, kind = ?, heading = ?, course_title = ?, date_text = ?,
                          link = ?, poster_file = ?, updated_at = datetime('now') WHERE id = ?""",
                (v["session_id"], v["kind"], v["heading"], v["course_title"], v["date_text"], v["link"],
                 filename, poster_id),
            )
            if old and old != filename:
                try:
                    os.remove(os.path.join(_poster_dir(), old))
                except OSError:
                    pass
            activity.log("update", "qr_poster", poster_id, f"Updated QR poster: {v['heading']} - {v['course_title']}")
            flash("QR poster updated.", "success")
            _flash_scan(scan_ok)
            return redirect(url_for("qr_posters.view", poster_id=poster_id))
    else:
        values = dict(row)
    return render_template("qr_posters/form.html", values=values, classes=_class_choices(), kinds=KINDS,
                           poster_row=row)


@bp.route("/<int:poster_id>/delete", methods=("POST",))
@login_required
def delete(poster_id):
    row = _get(poster_id)
    db.execute("DELETE FROM qr_posters WHERE id = ?", (poster_id,))
    try:
        os.remove(os.path.join(_poster_dir(), row["poster_file"]))
    except (OSError, TypeError):
        pass
    activity.log("delete", "qr_poster", poster_id, f"Deleted QR poster: {row['heading']} - {row['course_title']}")
    flash("QR poster deleted.", "success")
    if request.form.get("back") == "class" and row["session_id"]:
        return redirect(url_for("sessions.view", session_id=row["session_id"]))
    return redirect(url_for("qr_posters.index"))


@bp.route("/<int:poster_id>/poster.jpg")
@login_required
def poster_image(poster_id):
    row = _get(poster_id)
    return send_from_directory(_poster_dir(), row["poster_file"], mimetype="image/jpeg",
                               as_attachment=bool(request.args.get("download")),
                               download_name=_download_name(row, "jpg"))


@bp.route("/<int:poster_id>/qr.png")
@login_required
def qr_image(poster_id):
    """Just the QR code (no poster), e.g. to drop onto a slide."""
    row = _get(poster_id)
    png = poster.qr_png(row["link"], box_size=1000, logo_path=_logo_path())
    disposition = "attachment" if request.args.get("download") else "inline"
    return Response(png, mimetype="image/png",
                    headers={"Content-Disposition": f'{disposition}; filename="{_download_name(row, "png")}"'})


@bp.route("/<int:poster_id>/present")
@login_required
def present(poster_id):
    """Full-screen view for the projector / TV in the training room."""
    return render_template("qr_posters/present.html", p=_get(poster_id))


def posters_for_session(session_id):
    """The class page's "Assessment QR Posters" card."""
    return db.query("SELECT id, kind, heading, created_at FROM qr_posters WHERE session_id = ? ORDER BY id",
                    (session_id,))
