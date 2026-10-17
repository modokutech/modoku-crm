"""QR Posters (Fix179): branded QR posters for links participants open on
their phones during a class - mainly the Pre-Assessment Test and
Post-Assessment Test Google Forms. Flow: choose the class, paste the Pre-Test
and/or Post-Test link, Generate (Fix187: both in one go).

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


def _existing(session_id):
    """This class's current Pre and Post posters (the newest of each)."""
    out = {}
    for r in db.query("SELECT * FROM qr_posters WHERE session_id = ? ORDER BY id", (session_id,)):
        out[r["kind"]] = r
    return out


def _existing_links():
    """{class_id: {"pre": link, "post": link}} - lets the form fill in the
    links a class already has as soon as it's picked."""
    out = {}
    for r in db.query("SELECT session_id, kind, link FROM qr_posters WHERE session_id IS NOT NULL ORDER BY id"):
        out.setdefault(r["session_id"], {})[r["kind"]] = r["link"]
    return out


def _link_ok(link):
    return bool(re.match(r"^https?://\S+$", link or ""))


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
    """Grouped by class (newest class first): one row per class with its Pre
    and Post posters side by side, so a missing Post-Test poster stands out.
    Posters whose class was deleted are grouped at the end."""
    rows = db.query(
        """SELECT qp.*, cs.start_date, cs.status AS class_status, co.name AS client_name
           FROM qr_posters qp
           LEFT JOIN course_sessions cs ON cs.id = qp.session_id
           LEFT JOIN companies co ON co.id = cs.client_company_id
           ORDER BY qp.id"""
    )
    groups = {}
    for r in rows:
        key = r["session_id"] or f"orphan-{r['course_title']}-{r['date_text']}"
        g_ = groups.setdefault(key, {
            "session_id": r["session_id"], "course_title": r["course_title"], "date_text": r["date_text"],
            "start_date": r["start_date"] or "", "client_name": r["client_name"],
            "cancelled": r["class_status"] == "Cancelled", "pre": [], "post": [],
        })
        g_[r["kind"] if r["kind"] in KINDS else "pre"].append(r)
    ordered = sorted(groups.values(), key=lambda x: (x["session_id"] is not None, x["start_date"]), reverse=True)
    return render_template("qr_posters/index.html", groups=ordered, kinds=KINDS)


@bp.route("/new", methods=("GET", "POST"))
@login_required
def new():
    """Fix187: one form for a class's Pre-Assessment Test and Post-Assessment
    Test links - fill in either or both. A test the class already has a
    poster for is updated (re-generated) rather than duplicated; a blank
    link leaves that test alone."""
    classes = _class_choices()
    if request.method == "POST":
        try:
            session_id = int(request.form.get("session_id") or 0) or None
        except ValueError:
            session_id = None
        cls = next((c for c in classes if c["id"] == session_id), None)
        links = {k: (request.form.get(f"link_{k}") or "").strip() for k in KINDS}
        values = {"session_id": session_id, "links": links}
        error = None
        if cls is None:
            error = "Choose the class first."
        elif not any(links.values()):
            error = "Paste at least one Google Form link (Pre-Test, Post-Test or both)."
        else:
            bad = [KINDS[k] for k, link in links.items() if link and not _link_ok(link)]
            if bad:
                error = (f"The {' and '.join(bad)} link should be the full link, starting with https:// "
                         "(e.g. https://forms.gle/...).")
        if error:
            flash(error, "danger")
        else:
            existing = _existing(session_id)
            done, scan_bad = [], False
            for kind, link in links.items():
                if not link:
                    continue
                v = {"kind": kind, "heading": KINDS[kind], "session_id": session_id,
                     "course_title": cls["course_title"], "date_text": cls["date_text"], "link": link}
                row = existing.get(kind)
                if row is not None and row["link"] == link and row["course_title"] == v["course_title"] \
                        and row["date_text"] == v["date_text"]:
                    done.append((row["id"], "unchanged"))
                    continue
                filename, scan_ok = _build(v)
                scan_bad = scan_bad or scan_ok is False
                if row is None:
                    poster_id = db.execute(
                        """INSERT INTO qr_posters (session_id, kind, heading, course_title, date_text, link,
                                                   poster_file, created_by)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (session_id, kind, v["heading"], v["course_title"], v["date_text"], link, filename,
                         g.user["id"] if g.user else None),
                    )
                    activity.log("create", "qr_poster", poster_id, f"Created QR poster: {v['heading']} - {v['course_title']}")
                    done.append((poster_id, "created"))
                else:
                    db.execute(
                        """UPDATE qr_posters SET heading = ?, course_title = ?, date_text = ?, link = ?, poster_file = ?,
                                  updated_at = datetime('now') WHERE id = ?""",
                        (v["heading"], v["course_title"], v["date_text"], link, filename, row["id"]),
                    )
                    try:
                        os.remove(os.path.join(_poster_dir(), row["poster_file"]))
                    except (OSError, TypeError):
                        pass
                    activity.log("update", "qr_poster", row["id"], f"Updated QR poster: {v['heading']} - {v['course_title']}")
                    done.append((row["id"], "updated"))
            changed = [d for d in done if d[1] != "unchanged"]
            if not changed:
                flash("Nothing changed: those links are already on this class's posters.", "info")
            else:
                parts = []
                for verb in ("created", "updated"):
                    n = sum(1 for d in changed if d[1] == verb)
                    if n:
                        parts.append(f"{n} {verb}")
                flash(f"QR poster{'s' if len(changed) > 1 else ''} ready ({', '.join(parts)}).", "success")
            if scan_bad:
                _flash_scan(False)
            if len(done) == 1:
                return redirect(url_for("qr_posters.view", poster_id=done[0][0]))
            return redirect(url_for("qr_posters.index", _anchor=f"class-{session_id}"))
    else:
        session_id = request.args.get("session_id", type=int)
        current = _existing_links().get(session_id, {}) if session_id else {}
        values = {"session_id": session_id, "links": {k: current.get(k, "") for k in KINDS}}
    return render_template("qr_posters/form.html", values=values, classes=classes, kinds=KINDS,
                           existing_links=_existing_links(), focus=request.args.get("kind"))


@bp.route("/<int:poster_id>")
@login_required
def view(poster_id):
    return render_template("qr_posters/view.html", p=_get(poster_id), kinds=KINDS)


@bp.route("/<int:poster_id>/edit")
@login_required
def edit(poster_id):
    """Editing goes through the class's Pre/Post form (both links in one place)."""
    row = _get(poster_id)
    if not row["session_id"]:
        flash("This poster's class was deleted, so it can't be edited. Create a new poster for the right class.",
              "warning")
        return redirect(url_for("qr_posters.view", poster_id=poster_id))
    return redirect(url_for("qr_posters.new", session_id=row["session_id"], kind=row["kind"]))


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
