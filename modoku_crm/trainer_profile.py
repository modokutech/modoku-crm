"""Generate Trainer Profile - a standalone module for building the
branded, multi-page "Trainer Profile" brochure (see trainer_profile_pdf.py
for how it's turned into the actual PDF) without needing to go through a
trainer's own record first.

This exists as an in-house fallback for producing a finished profile
document even when whoever normally designs these by hand (e.g. Erik's
graphic designer) is unavailable - so a profile can be created, edited and
regenerated entirely on its own (its own name, its own photo), independent
of whether the subject is already in the Trainers list. Once a profile is
ready, it can optionally be linked to an existing trainer - linking copies
the generated PDF into that trainer's own profile_file slot (see
DOCUMENT_FIELDS in trainers.py, the same slot a manual upload used before
this module existed, and which a class's HRDCorp Grant Documents pack
already pulls in automatically), and keeps it in sync there on every later
regeneration for as long as the link stays in place.

Most repeatable lists (Areas of Expertise, Companies Trained, Academic
Qualifications, Professional Certifications, Professional Experience) are
edited as one-entry-per-line textareas rather than dynamic add/remove
rows - faster to fill in from an existing CV/profile document (paste a
list, done) and just as easy to reorder (reorder the lines) or remove
(delete the line). Multi-field entries use a simple " | " delimiter per
line (see the placeholder text in the template for the exact format each
field expects). Only custom sections (title + its own bullet list) get a
repeatable block in the form, since there are normally just a couple of
them.
"""
import os
import uuid

from flask import (Blueprint, current_app, flash, redirect, render_template,
                    request, send_from_directory, url_for)
from werkzeug.utils import secure_filename

from . import activity, db, uploadutil
from .auth import login_required
from .trainer_profile_pdf import generate_trainer_profile_pdf

bp = Blueprint("trainer_profiles", __name__, url_prefix="/trainer-profiles")


def _lines(raw):
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def _split(line, n):
    """Splits a ' | '-delimited line into exactly n parts, padding missing
    trailing parts with None rather than raising on a short line."""
    parts = [p.strip() or None for p in line.split("|")]
    parts += [None] * (n - len(parts))
    return parts[:n]


def _profile_upload_dir(profile_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "trainer_profiles", str(profile_id))
    os.makedirs(path, exist_ok=True)
    return path


def _trainer_options():
    return db.query("SELECT id, name FROM trainers ORDER BY name")


def _load_children(profile_id):
    expertise = db.query(
        "SELECT * FROM trainer_profile_expertise WHERE profile_id = ? ORDER BY sort_order, id", (profile_id,))
    companies = db.query(
        "SELECT * FROM trainer_profile_companies WHERE profile_id = ? ORDER BY sort_order, id", (profile_id,))
    academic = db.query(
        "SELECT * FROM trainer_profile_academic WHERE profile_id = ? ORDER BY sort_order, id", (profile_id,))
    certifications = db.query(
        "SELECT * FROM trainer_profile_certifications WHERE profile_id = ? ORDER BY sort_order, id", (profile_id,))
    experience = db.query(
        "SELECT * FROM trainer_profile_experience WHERE profile_id = ? ORDER BY sort_order, id", (profile_id,))
    section_rows = db.query(
        "SELECT * FROM trainer_profile_sections WHERE profile_id = ? ORDER BY sort_order, id", (profile_id,))
    sections = []
    for s in section_rows:
        items = db.query(
            "SELECT * FROM trainer_profile_section_items WHERE section_id = ? ORDER BY sort_order, id", (s["id"],))
        # Key is "bullets", not "items" - a plain dict's own .items() method
        # would otherwise shadow it in a template's `s.items` lookup.
        sections.append({"id": s["id"], "title": s["title"], "bullets": [i["text"] for i in items]})
    return {
        "expertise": expertise, "companies": companies, "academic": academic,
        "certifications": certifications, "experience": experience, "sections": sections,
    }


def _save_children(profile_id, form):
    db.execute("DELETE FROM trainer_profile_expertise WHERE profile_id = ?", (profile_id,))
    for i, label in enumerate(_lines(form.get("expertise"))):
        db.execute("INSERT INTO trainer_profile_expertise (profile_id, label, sort_order) VALUES (?,?,?)",
                   (profile_id, label.lstrip("·• ").strip(), i))

    db.execute("DELETE FROM trainer_profile_companies WHERE profile_id = ?", (profile_id,))
    for i, name in enumerate(_lines(form.get("companies"))):
        db.execute("INSERT INTO trainer_profile_companies (profile_id, name, sort_order) VALUES (?,?,?)",
                   (profile_id, name.lstrip("·• ").strip(), i))

    db.execute("DELETE FROM trainer_profile_academic WHERE profile_id = ?", (profile_id,))
    for i, line in enumerate(_lines(form.get("academic"))):
        qualification, institution, year, location = _split(line, 4)
        if not qualification:
            continue
        db.execute(
            """INSERT INTO trainer_profile_academic
                   (profile_id, qualification, institution, year, location, sort_order) VALUES (?,?,?,?,?,?)""",
            (profile_id, qualification, institution, year, location, i))

    db.execute("DELETE FROM trainer_profile_certifications WHERE profile_id = ?", (profile_id,))
    for i, line in enumerate(_lines(form.get("certifications"))):
        title, issuer = _split(line, 2)
        if not title:
            continue
        db.execute(
            "INSERT INTO trainer_profile_certifications (profile_id, title, issuer, sort_order) VALUES (?,?,?,?)",
            (profile_id, title, issuer, i))

    db.execute("DELETE FROM trainer_profile_experience WHERE profile_id = ?", (profile_id,))
    for i, line in enumerate(_lines(form.get("experience"))):
        company, period, role = _split(line, 3)
        if not company:
            continue
        db.execute(
            "INSERT INTO trainer_profile_experience (profile_id, company, period, role, sort_order) VALUES (?,?,?,?,?)",
            (profile_id, company, period, role, i))

    db.execute("DELETE FROM trainer_profile_sections WHERE profile_id = ?", (profile_id,))
    section_titles = request.form.getlist("section_title")
    section_bullets = request.form.getlist("section_items")
    for i, (title, bullets_raw) in enumerate(zip(section_titles, section_bullets)):
        title = title.strip()
        bullets = _lines(bullets_raw)
        if not title or not bullets:
            continue
        section_id = db.execute(
            "INSERT INTO trainer_profile_sections (profile_id, title, sort_order) VALUES (?,?,?)",
            (profile_id, title, i))
        for j, item in enumerate(bullets):
            db.execute(
                "INSERT INTO trainer_profile_section_items (section_id, text, sort_order) VALUES (?,?,?)",
                (section_id, item.lstrip("·• ").strip(), j))


def _handle_photo_upload(profile_id):
    """Saves a newly-uploaded photo, if one was submitted, and returns its
    stored filename - or None if no file was chosen this time (an existing
    photo_file, if any, is left untouched)."""
    file_storage = request.files.get("photo")
    if not file_storage or not file_storage.filename:
        return None
    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.IMAGE_EXTENSIONS)
    if error:
        flash(error, "danger")
        return None
    safe_name = secure_filename(file_storage.filename)
    stored_name = f"photo_{uuid.uuid4().hex[:8]}_{safe_name}"
    file_storage.save(os.path.join(_profile_upload_dir(profile_id), stored_name))
    return stored_name


def _photo_path(profile):
    if not profile.get("photo_file"):
        return None
    return os.path.join(_profile_upload_dir(profile["id"]), profile["photo_file"])


def _push_to_trainer(profile_id, trainer_id, pdf_bytes):
    """Copies a profile's already-built PDF bytes into a linked trainer's
    own profile_file slot - the same directory/column a manual document
    upload on the trainer's own page has always used."""
    trainer_dir = os.path.join(current_app.config["UPLOAD_FOLDER"], "trainers", str(trainer_id))
    os.makedirs(trainer_dir, exist_ok=True)
    stored_name = f"trainer_profile_{profile_id}.pdf"
    with open(os.path.join(trainer_dir, stored_name), "wb") as f:
        f.write(pdf_bytes)
    db.execute("UPDATE trainers SET profile_file = ? WHERE id = ?", (stored_name, trainer_id))


def _regenerate(profile_id):
    """Rebuilds this profile's own PDF from whatever is currently saved. If
    the profile is linked to a trainer, the fresh PDF is also copied into
    that trainer's own profile_file slot, keeping the two in sync on every
    regeneration for as long as the link stays in place. Returns None on
    success, or an error message to flash."""
    profile_row = db.query("SELECT * FROM trainer_profiles WHERE id = ?", (profile_id,), one=True)
    if profile_row is None:
        return "Trainer profile not found."
    profile = dict(profile_row)
    if not profile.get("background_text"):
        return "Add at least a Background section before generating the PDF."
    children = _load_children(profile_id)
    try:
        pdf_bytes = generate_trainer_profile_pdf(
            {"name": profile["display_name"]}, profile, children["expertise"], children["companies"],
            children["academic"], children["certifications"], children["experience"], children["sections"],
            photo_path=_photo_path(profile),
        )
    except Exception:  # noqa: BLE001 - surface a clean flash instead of a 500
        current_app.logger.exception("Failed to build Trainer Profile PDF for profile %s", profile_id)
        return "Something went wrong building the PDF. The data you entered has been saved either way."

    stored_name = f"trainer_profile_{profile_id}.pdf"
    with open(os.path.join(_profile_upload_dir(profile_id), stored_name), "wb") as f:
        f.write(pdf_bytes)
    db.execute(
        "UPDATE trainer_profiles SET profile_file = ?, generated_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
        (stored_name, profile_id))

    if profile.get("trainer_id"):
        _push_to_trainer(profile_id, profile["trainer_id"], pdf_bytes)
    return None


def _save_base_fields(form, profile_id=None):
    """INSERTs a new trainer_profiles row (profile_id=None) or UPDATEs an
    existing one, from the shared set of top-level form fields. Returns the
    row's id either way."""
    display_name = (form.get("display_name") or "").strip()
    credentials_line = form.get("credentials_line", "").strip() or None
    background_text = form.get("background_text", "").strip() or None
    experience_text = form.get("experience_text", "").strip() or None
    if profile_id is None:
        return db.execute(
            """INSERT INTO trainer_profiles (display_name, credentials_line, background_text, experience_text, updated_at)
               VALUES (?,?,?,?,datetime('now'))""",
            (display_name, credentials_line, background_text, experience_text))
    db.execute(
        """UPDATE trainer_profiles SET display_name=?, credentials_line=?, background_text=?,
               experience_text=?, updated_at=datetime('now') WHERE id=?""",
        (display_name, credentials_line, background_text, experience_text, profile_id))
    return profile_id


@bp.route("/")
@login_required
def index():
    profiles = db.query(
        """SELECT tp.*, t.name AS trainer_name FROM trainer_profiles tp
           LEFT JOIN trainers t ON t.id = tp.trainer_id
           ORDER BY tp.updated_at DESC""")
    return render_template("trainer_profile/index.html", profiles=profiles)


@bp.route("/new", methods=("GET", "POST"))
@login_required
def new():
    if request.method == "POST":
        if not (request.form.get("display_name") or "").strip():
            flash("Enter a name for this profile before saving.", "danger")
            return render_template(
                "trainer_profile/edit.html", profile={"display_name": request.form.get("display_name", "")},
                trainers=_trainer_options(), expertise=[], companies=[], academic=[],
                certifications=[], experience=[], sections=[])
        profile_id = _save_base_fields(request.form)
        _save_children(profile_id, request.form)
        photo = _handle_photo_upload(profile_id)
        if photo:
            db.execute("UPDATE trainer_profiles SET photo_file = ? WHERE id = ?", (photo, profile_id))
        error = _regenerate(profile_id)
        if error:
            flash(error, "warning")
        else:
            activity.log("create", "trainer_profile", profile_id,
                          f"Generated Trainer Profile for {request.form.get('display_name', '').strip()}")
            flash("Trainer Profile created and PDF generated. You can link it to a trainer below.", "success")
        return redirect(url_for("trainer_profiles.edit", profile_id=profile_id))

    prefill_name = ""
    prefill_trainer_id = request.args.get("trainer_id", type=int)
    if prefill_trainer_id:
        trainer = db.query("SELECT name FROM trainers WHERE id = ?", (prefill_trainer_id,), one=True)
        if trainer:
            prefill_name = trainer["name"]
    return render_template(
        "trainer_profile/edit.html", profile={"display_name": prefill_name}, trainers=_trainer_options(),
        expertise=[], companies=[], academic=[], certifications=[], experience=[], sections=[])


@bp.route("/<int:profile_id>/edit", methods=("GET", "POST"))
@login_required
def edit(profile_id):
    profile_row = db.query("SELECT * FROM trainer_profiles WHERE id = ?", (profile_id,), one=True)
    if profile_row is None:
        flash("Trainer profile not found.", "danger")
        return redirect(url_for("trainer_profiles.index"))

    if request.method == "POST":
        if not (request.form.get("display_name") or "").strip():
            flash("Enter a name for this profile before saving.", "danger")
            return redirect(url_for("trainer_profiles.edit", profile_id=profile_id))
        _save_base_fields(request.form, profile_id=profile_id)
        _save_children(profile_id, request.form)
        photo = _handle_photo_upload(profile_id)
        if photo:
            db.execute("UPDATE trainer_profiles SET photo_file = ? WHERE id = ?", (photo, profile_id))
        error = _regenerate(profile_id)
        if error:
            flash(error, "warning")
        else:
            activity.log("update", "trainer_profile", profile_id,
                          f"Generated Trainer Profile PDF for {request.form.get('display_name', '').strip()}")
            flash("Trainer Profile saved and PDF generated.", "success")
        return redirect(url_for("trainer_profiles.edit", profile_id=profile_id))

    profile = dict(profile_row)
    if profile.get("trainer_id"):
        linked = db.query("SELECT name FROM trainers WHERE id = ?", (profile["trainer_id"],), one=True)
        profile["trainer_name"] = linked["name"] if linked else None
    children = _load_children(profile_id)
    return render_template(
        "trainer_profile/edit.html", profile=profile, trainers=_trainer_options(),
        expertise=children["expertise"], companies=children["companies"], academic=children["academic"],
        certifications=children["certifications"], experience=children["experience"], sections=children["sections"])


@bp.route("/<int:profile_id>/photo")
@login_required
def photo(profile_id):
    profile = db.query("SELECT photo_file FROM trainer_profiles WHERE id = ?", (profile_id,), one=True)
    if profile is None or not profile["photo_file"]:
        flash("No photo uploaded yet.", "danger")
        return redirect(url_for("trainer_profiles.edit", profile_id=profile_id))
    return send_from_directory(_profile_upload_dir(profile_id), profile["photo_file"], as_attachment=False)


@bp.route("/<int:profile_id>/download")
@login_required
def download(profile_id):
    profile = db.query("SELECT profile_file FROM trainer_profiles WHERE id = ?", (profile_id,), one=True)
    if profile is None or not profile["profile_file"]:
        flash("No PDF generated yet.", "warning")
        return redirect(url_for("trainer_profiles.edit", profile_id=profile_id))
    return send_from_directory(_profile_upload_dir(profile_id), profile["profile_file"], as_attachment=False)


@bp.route("/<int:profile_id>/link", methods=("POST",))
@login_required
def link(profile_id):
    profile_row = db.query("SELECT * FROM trainer_profiles WHERE id = ?", (profile_id,), one=True)
    if profile_row is None:
        flash("Trainer profile not found.", "danger")
        return redirect(url_for("trainer_profiles.index"))
    trainer_id = request.form.get("trainer_id", type=int)
    trainer = db.query("SELECT * FROM trainers WHERE id = ?", (trainer_id,), one=True) if trainer_id else None
    if not trainer:
        flash("Pick a trainer to link this profile to.", "danger")
        return redirect(url_for("trainer_profiles.edit", profile_id=profile_id))
    db.execute("UPDATE trainer_profiles SET trainer_id = ? WHERE id = ?", (trainer_id, profile_id))
    if profile_row["profile_file"]:
        # Copy what's already generated straight over, rather than making
        # someone hit Save again just to get it onto the trainer's page.
        src = os.path.join(_profile_upload_dir(profile_id), profile_row["profile_file"])
        if os.path.exists(src):
            with open(src, "rb") as f:
                _push_to_trainer(profile_id, trainer_id, f.read())
    activity.log("update", "trainer", trainer_id, f"Linked Trainer Profile to {trainer['name']}")
    flash(f"Linked to {trainer['name']} — the generated PDF now shows on their Documents.", "success")
    return redirect(url_for("trainer_profiles.edit", profile_id=profile_id))


@bp.route("/<int:profile_id>/unlink", methods=("POST",))
@login_required
def unlink(profile_id):
    profile_row = db.query("SELECT trainer_id FROM trainer_profiles WHERE id = ?", (profile_id,), one=True)
    if profile_row is None:
        flash("Trainer profile not found.", "danger")
        return redirect(url_for("trainer_profiles.index"))
    db.execute("UPDATE trainer_profiles SET trainer_id = NULL WHERE id = ?", (profile_id,))
    flash("Unlinked. The PDF already copied to that trainer's Documents is left in place until it's replaced by something else.", "success")
    return redirect(url_for("trainer_profiles.edit", profile_id=profile_id))


@bp.route("/<int:profile_id>/delete", methods=("POST",))
@login_required
def delete(profile_id):
    profile = db.query("SELECT display_name FROM trainer_profiles WHERE id = ?", (profile_id,), one=True)
    if profile is None:
        flash("Trainer profile not found.", "danger")
        return redirect(url_for("trainer_profiles.index"))
    db.execute("DELETE FROM trainer_profiles WHERE id = ?", (profile_id,))  # cascades to every child row
    activity.log("delete", "trainer_profile", profile_id, f"Deleted Trainer Profile for {profile['display_name']}")
    flash("Trainer profile deleted. Any copy already linked to a trainer's Documents is left in place.", "success")
    return redirect(url_for("trainer_profiles.index"))
