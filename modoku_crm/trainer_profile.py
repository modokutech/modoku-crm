"""Trainer Profile editor - the structured data behind the auto-generated
Trainer Profile brochure (see trainer_profile_pdf.py for how it's turned
into the actual PDF). One page: edit everything about a trainer's profile,
then Save both persists it and immediately (re)builds the PDF into
trainers.profile_file - the same document slot a manual upload used
before this module existed (see DOCUMENT_FIELDS in trainers.py), and the
same slot a class's HRDCorp Grant Documents pack and "Trainer Profile"
link already read from. There's no separate "save without generating"
step: the PDF is a pure function of this data, so anything worth saving
is worth regenerating.

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

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from . import activity, db
from .auth import login_required
from .trainer_profile_pdf import generate_trainer_profile_pdf

bp = Blueprint("trainer_profile", __name__, url_prefix="/trainers")


def _lines(raw):
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def _split(line, n):
    """Splits a ' | '-delimited line into exactly n parts, padding missing
    trailing parts with None rather than raising on a short line."""
    parts = [p.strip() or None for p in line.split("|")]
    parts += [None] * (n - len(parts))
    return parts[:n]


def _load_profile_data(trainer_id):
    profile = db.query("SELECT * FROM trainer_profiles WHERE trainer_id = ?", (trainer_id,), one=True)
    expertise = db.query(
        "SELECT * FROM trainer_profile_expertise WHERE trainer_id = ? ORDER BY sort_order, id", (trainer_id,))
    companies = db.query(
        "SELECT * FROM trainer_profile_companies WHERE trainer_id = ? ORDER BY sort_order, id", (trainer_id,))
    academic = db.query(
        "SELECT * FROM trainer_profile_academic WHERE trainer_id = ? ORDER BY sort_order, id", (trainer_id,))
    certifications = db.query(
        "SELECT * FROM trainer_profile_certifications WHERE trainer_id = ? ORDER BY sort_order, id", (trainer_id,))
    experience = db.query(
        "SELECT * FROM trainer_profile_experience WHERE trainer_id = ? ORDER BY sort_order, id", (trainer_id,))
    section_rows = db.query(
        "SELECT * FROM trainer_profile_sections WHERE trainer_id = ? ORDER BY sort_order, id", (trainer_id,))
    sections = []
    for s in section_rows:
        items = db.query(
            "SELECT * FROM trainer_profile_section_items WHERE section_id = ? ORDER BY sort_order, id", (s["id"],))
        # Key is "bullets", not "items" - a plain dict's own .items() method
        # would otherwise shadow it in a template's `s.items` lookup.
        sections.append({"id": s["id"], "title": s["title"], "bullets": [i["text"] for i in items]})
    return {
        "profile": dict(profile) if profile else {},
        "expertise": expertise, "companies": companies, "academic": academic,
        "certifications": certifications, "experience": experience, "sections": sections,
    }


def _save_profile_data(trainer_id, form):
    db.execute(
        """INSERT INTO trainer_profiles (trainer_id, credentials_line, background_text, experience_text, updated_at)
           VALUES (?,?,?,?,datetime('now'))
           ON CONFLICT(trainer_id) DO UPDATE SET
               credentials_line=excluded.credentials_line, background_text=excluded.background_text,
               experience_text=excluded.experience_text, updated_at=excluded.updated_at""",
        (trainer_id, form.get("credentials_line", "").strip() or None,
         form.get("background_text", "").strip() or None,
         form.get("experience_text", "").strip() or None),
    )

    db.execute("DELETE FROM trainer_profile_expertise WHERE trainer_id = ?", (trainer_id,))
    for i, label in enumerate(_lines(form.get("expertise"))):
        db.execute("INSERT INTO trainer_profile_expertise (trainer_id, label, sort_order) VALUES (?,?,?)",
                   (trainer_id, label.lstrip("· ").strip(), i))

    db.execute("DELETE FROM trainer_profile_companies WHERE trainer_id = ?", (trainer_id,))
    for i, name in enumerate(_lines(form.get("companies"))):
        db.execute("INSERT INTO trainer_profile_companies (trainer_id, name, sort_order) VALUES (?,?,?)",
                   (trainer_id, name.lstrip("· ").strip(), i))

    db.execute("DELETE FROM trainer_profile_academic WHERE trainer_id = ?", (trainer_id,))
    for i, line in enumerate(_lines(form.get("academic"))):
        qualification, institution, year, location = _split(line, 4)
        if not qualification:
            continue
        db.execute(
            """INSERT INTO trainer_profile_academic
                   (trainer_id, qualification, institution, year, location, sort_order) VALUES (?,?,?,?,?,?)""",
            (trainer_id, qualification, institution, year, location, i))

    db.execute("DELETE FROM trainer_profile_certifications WHERE trainer_id = ?", (trainer_id,))
    for i, line in enumerate(_lines(form.get("certifications"))):
        title, issuer = _split(line, 2)
        if not title:
            continue
        db.execute(
            "INSERT INTO trainer_profile_certifications (trainer_id, title, issuer, sort_order) VALUES (?,?,?,?)",
            (trainer_id, title, issuer, i))

    db.execute("DELETE FROM trainer_profile_experience WHERE trainer_id = ?", (trainer_id,))
    for i, line in enumerate(_lines(form.get("experience"))):
        company, period, role = _split(line, 3)
        if not company:
            continue
        db.execute(
            "INSERT INTO trainer_profile_experience (trainer_id, company, period, role, sort_order) VALUES (?,?,?,?,?)",
            (trainer_id, company, period, role, i))

    db.execute(
        "DELETE FROM trainer_profile_sections WHERE trainer_id = ?", (trainer_id,))
    section_titles = request.form.getlist("section_title")
    section_bullets = request.form.getlist("section_items")
    for i, (title, bullets_raw) in enumerate(zip(section_titles, section_bullets)):
        title = title.strip()
        bullets = _lines(bullets_raw)
        if not title or not bullets:
            continue
        section_id = db.execute(
            "INSERT INTO trainer_profile_sections (trainer_id, title, sort_order) VALUES (?,?,?)",
            (trainer_id, title, i))
        for j, item in enumerate(bullets):
            db.execute(
                "INSERT INTO trainer_profile_section_items (section_id, text, sort_order) VALUES (?,?,?)",
                (section_id, item.lstrip("· ").strip(), j))


def _photo_path(trainer_row):
    if not trainer_row["avatar_file"]:
        return None
    return os.path.join(current_app.config["UPLOAD_FOLDER"], "trainers", str(trainer_row["id"]), trainer_row["avatar_file"])


def _regenerate(trainer_id):
    """Rebuilds the PDF from whatever is currently saved and writes it into
    trainers.profile_file, exactly the slot a manual upload used before.
    Returns None on success, or an error message to flash."""
    trainer_row = db.query("SELECT * FROM trainers WHERE id = ?", (trainer_id,), one=True)
    if trainer_row is None:
        return "Trainer not found."
    data = _load_profile_data(trainer_id)
    if not data["profile"].get("background_text"):
        return "Add at least a Background section before generating the PDF."
    try:
        pdf_bytes = generate_trainer_profile_pdf(
            trainer_row, data["profile"], data["expertise"], data["companies"],
            data["academic"], data["certifications"], data["experience"], data["sections"],
            photo_path=_photo_path(trainer_row),
        )
    except Exception:  # noqa: BLE001 - surface a clean flash instead of a 500
        current_app.logger.exception("Failed to build Trainer Profile PDF for trainer %s", trainer_id)
        return "Something went wrong building the PDF. The data you entered has been saved either way."

    stored_name = f"trainer_profile_{trainer_id}.pdf"
    # Same "UPLOAD_FOLDER/trainers/<id>/" directory a manually-uploaded
    # document (trainers._trainer_upload_dir) is saved into - this file
    # simply replaces whatever's currently in trainers.profile_file.
    save_dir = os.path.join(current_app.config["UPLOAD_FOLDER"], "trainers", str(trainer_id))
    os.makedirs(save_dir, exist_ok=True)
    with open(os.path.join(save_dir, stored_name), "wb") as f:
        f.write(pdf_bytes)
    db.execute("UPDATE trainers SET profile_file = ? WHERE id = ?", (stored_name, trainer_id))
    db.execute("UPDATE trainer_profiles SET generated_at = datetime('now') WHERE trainer_id = ?", (trainer_id,))
    return None


@bp.route("/<int:trainer_id>/profile/edit", methods=("GET", "POST"))
@login_required
def edit(trainer_id):
    trainer = db.query("SELECT * FROM trainers WHERE id = ?", (trainer_id,), one=True)
    if trainer is None:
        flash("Trainer not found.", "danger")
        return redirect(url_for("trainers.index"))

    if request.method == "POST":
        _save_profile_data(trainer_id, request.form)
        error = _regenerate(trainer_id)
        if error:
            flash(error, "warning")
        else:
            activity.log("update", "trainer", trainer_id, f"Generated Trainer Profile PDF for {trainer['name']}")
            flash("Trainer Profile saved and PDF generated.", "success")
        return redirect(url_for("trainer_profile.edit", trainer_id=trainer_id))

    data = _load_profile_data(trainer_id)
    return render_template(
        "trainer_profile/edit.html", trainer=trainer,
        profile=data["profile"], expertise=data["expertise"], companies=data["companies"],
        academic=data["academic"], certifications=data["certifications"], experience=data["experience"],
        sections=data["sections"],
    )
