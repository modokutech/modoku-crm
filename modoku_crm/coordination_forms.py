"""Coordination Forms (Fix225).

A safety step, issued only when needed: the client fills in and signs every
logistics detail of a programme (room setup, accommodation, food & beverage,
transport, T-shirt design, laptops, notes) on a public link, and Modoku
countersigns, so both sides have agreed the same details in writing.

- Standalone, or linked to a class (pre-filled from it).
- Staff pick the sections that apply and can pre-fill answers.
- The client can save, come back, Sign & Submit, and keep editing until the
  due date. Any change after signing (also after Modoku confirmed) needs
  their signature again: the form goes back to Sent until they re-sign, and
  then waits for Modoku to confirm again.
- The link locks at the end of the due date (Malaysia time). Staff can
  extend the due date, and can still edit; every change is logged.

Statuses: Draft -> Sent -> Submitted (client signed) -> Confirmed (Modoku
signed). A Draft/Sent form past its due date shows as Closed.
"""
import json
import os
import secrets
import uuid
from datetime import date, datetime, timedelta

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request,
                   send_from_directory, url_for)
from werkzeug.utils import secure_filename

from . import APP_TZ, activity, db, mailer, notifications, uploadutil
from .auth import admin_required, login_required

bp = Blueprint("coordination_forms", __name__, url_prefix="/coordination-forms")
public_bp = Blueprint("coordination_form_public", __name__, url_prefix="/coordination-form")

STATUSES = ["Draft", "Sent", "Submitted", "Confirmed"]
FILE_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_DUE_DAYS = 7

CONSENT_TEXT = [
    "I, the undersigned, confirm that the details provided in this form are accurate to the best of my knowledge.",
    "I understand that any changes or additional requests should be communicated promptly.",
    "By signing this form, I agree to the terms and conditions outlined for the successful coordination "
    "of this programme.",
]

# The form's sections, in order. "details" is always on; the rest can be
# switched off per form (e.g. no accommodation for a one-day virtual class).
SECTIONS = [
    ("details", "Details"),
    ("room_setup", "Room Setup Preferences"),
    ("accommodation", "Accommodation Information"),
    ("fnb", "Food & Beverage Requirements"),
    ("transport", "Transportation Details"),
    ("design", "Design Specifications"),
    ("technical", "Technical Requirements"),
    ("notes", "Additional Notes/Requests"),
]
SECTION_LABELS = dict(SECTIONS)
OPTIONAL_SECTIONS = [key for key, _ in SECTIONS if key != "details"]

ROOM_SETUPS = ["Classroom", "Theater", "U-Shape", "Banquet", "Other"]
OCCUPANCIES = ["Single", "Double"]
BREAKFAST = ["With breakfast", "Without breakfast"]
YES_NO = ["Yes", "No"]
DIETS = ["No restrictions", "Vegetarian", "Vegan", "Kosher", "Gluten-free", "Lactose-free", "Other"]
ALLERGIES = ["None", "Yes"]
TRANSPORT_TYPES = ["Van", "Bus", "Flight", "Other"]

# Every field: (key, label, section). Used for saving, the change log and
# the summary/PDF. Types: text, textarea, date, time, int, choice (one),
# multi (several).
FIELDS = [
    ("programme", "Programme name", "details", "text"),
    ("start_date", "Start date", "details", "date"),
    ("start_time", "Start time", "details", "time"),
    ("end_date", "End date", "details", "date"),
    ("end_time", "End time", "details", "time"),
    ("company", "Company", "details", "text"),
    ("pax", "No. of pax", "details", "int"),
    ("venue", "Venue", "details", "text"),
    ("address", "Address", "details", "textarea"),
    ("room_setup", "Room setup", "room_setup", "choice"),
    ("room_setup_other", "Room setup (other)", "room_setup", "text"),
    ("occupancy", "Room occupancy", "accommodation", "multi"),
    ("breakfast", "Breakfast", "accommodation", "choice"),
    ("special_assistance", "Special assistance (disabled, etc.)", "accommodation", "choice"),
    ("special_assistance_details", "Special assistance details", "accommodation", "text"),
    ("diet", "Dietary preferences", "fnb", "multi"),
    *[(f"diet_pax_{i}", f"{d} (pax)", "fnb", "int") for i, d in enumerate(DIETS)],
    ("diet_other", "Dietary preference (other)", "fnb", "text"),
    ("allergies", "Allergies", "fnb", "choice"),
    ("allergies_details", "Allergy details", "fnb", "text"),
    ("transport_required", "Transportation required", "transport", "choice"),
    ("transport_type", "Type of transportation", "transport", "choice"),
    ("transport_other", "Type of transportation (other)", "transport", "text"),
    ("transport_details", "Transportation details", "transport", "textarea"),
    ("transport_pax", "No. of participants using this transportation", "transport", "int"),
    ("tshirt_design", "T-shirt design", "design", "textarea"),
    ("laptop_rental", "Laptop rental", "technical", "choice"),
    ("laptop_pcs", "Laptop rental (pcs)", "technical", "int"),
    ("notes", "Additional notes/requests", "notes", "textarea"),
]
FIELD_TYPES = {key: ftype for key, _l, _s, ftype in FIELDS}
FIELD_LABELS = {key: label for key, label, _s, _t in FIELDS}
FIELD_SECTIONS = {key: section for key, _l, section, _t in FIELDS}


# --- helpers ---------------------------------------------------------------

def today_kl():
    return datetime.now(APP_TZ).date()


def _parse_date(value):
    try:
        return date.fromisoformat((value or "").strip()[:10])
    except ValueError:
        return None


def is_open(form):
    """The public link takes changes until the end of the due date (KL)."""
    due = _parse_date(form["due_date"])
    return due is None or today_kl() <= due


def display_status(form):
    if form["status"] in ("Draft", "Sent") and not is_open(form):
        return "Closed"
    return form["status"]


STATUS_BADGES = {"Draft": "bg-secondary", "Sent": "bg-info text-dark", "Submitted": "bg-warning text-dark",
                 "Confirmed": "bg-success", "Closed": "bg-dark"}


def due_info(form):
    """{'text', 'level', 'days_left', 'open'} for the due-date banner/chips.
    level: 'ok', 'soon' (2 days or less), 'today', 'closed', 'none'."""
    due = _parse_date(form["due_date"])
    if due is None:
        return {"text": "No due date", "level": "none", "days_left": None, "open": True}
    days = (due - today_kl()).days
    nice = due.strftime("%a, %-d %b %Y")
    if days < 0:
        return {"text": f"Closed on {nice}", "level": "closed", "days_left": days, "open": False, "due": nice}
    if days == 0:
        return {"text": f"Due today ({nice})", "level": "today", "days_left": 0, "open": True, "due": nice}
    left = "1 day left" if days == 1 else f"{days} days left"
    return {"text": f"Due {nice} · {left}", "level": "soon" if days <= 2 else "ok",
            "days_left": days, "open": True, "due": nice, "left": left}


def load_answers(form):
    try:
        data = json.loads(form["answers"] or "{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def load_sections(form):
    try:
        data = json.loads(form["sections"] or "[]")
    except ValueError:
        data = []
    on = [s for s in data if s in OPTIONAL_SECTIONS] if isinstance(data, list) else []
    return ["details"] + on


def answers_from_form(form_data, sections):
    """Reads the posted fields of the switched-on sections into a clean dict."""
    out = {}
    for key, _label, section, ftype in FIELDS:
        if section not in sections:
            continue
        if ftype == "multi":
            values = [v for v in form_data.getlist(key) if v]
            if values:
                out[key] = values
            continue
        value = (form_data.get(key) or "").strip()
        if not value:
            continue
        if ftype == "int":
            try:
                value = max(0, int(float(value)))
            except ValueError:
                continue
        elif ftype == "date":
            parsed = _parse_date(value)
            if parsed is None:
                continue
            value = parsed.isoformat()
        out[key] = value
    return out


def _value_text(key, value):
    if value in (None, "", []):
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if FIELD_TYPES.get(key) == "date":
        d = _parse_date(value)
        return d.strftime("%-d %b %Y") if d else str(value)
    if FIELD_TYPES.get(key) == "time":
        try:
            return datetime.strptime(value, "%H:%M").strftime("%-I:%M %p")
        except ValueError:
            return str(value)
    return str(value)


def diff_answers(old, new, sections=None):
    """Human-readable lines for each changed field: 'Pax: 59 → 62'."""
    lines = []
    for key, label, section, _t in FIELDS:
        if sections is not None and section not in sections:
            continue
        a, b = _value_text(key, old.get(key)), _value_text(key, new.get(key))
        if a != b:
            lines.append(f"{label}: {a or '(blank)'} → {b or '(blank)'}")
    return lines


def summary(form):
    """[(section label, [(field label, text), ...]), ...] of the switched-on
    sections - for the staff view page and the PDF. Fields left blank are
    kept (shown as '-') so it's clear what wasn't answered."""
    answers = load_answers(form)
    sections = load_sections(form)
    out = []
    for skey, slabel in SECTIONS:
        if skey not in sections:
            continue
        rows = []
        for key, label, section, _t in FIELDS:
            if section != skey:
                continue
            if key.startswith("diet_pax_"):
                continue  # folded into the "Dietary preferences" line
            if key in ("room_setup_other", "transport_other", "diet_other") or key.endswith("_details") and key != "transport_details":
                continue  # folded into their parent line
            text = _value_text(key, answers.get(key))
            if key == "room_setup" and answers.get("room_setup") == "Other" and answers.get("room_setup_other"):
                text = f"Other: {answers['room_setup_other']}"
            if key == "transport_type" and answers.get("transport_type") == "Other" and answers.get("transport_other"):
                text = f"Other: {answers['transport_other']}"
            if key == "diet" and answers.get("diet"):
                parts = []
                for i, d in enumerate(DIETS):
                    if d not in answers["diet"]:
                        continue
                    name = f"Other ({answers['diet_other']})" if d == "Other" and answers.get("diet_other") else d
                    pax = answers.get(f"diet_pax_{i}")
                    parts.append(f"{name}: {pax} pax" if pax else name)
                text = ", ".join(parts)
            if key == "special_assistance" and text == "Yes" and answers.get("special_assistance_details"):
                text = f"Yes: {answers['special_assistance_details']}"
            if key == "allergies" and text == "Yes" and answers.get("allergies_details"):
                text = f"Yes: {answers['allergies_details']}"
            if key == "laptop_rental" and text == "Yes" and answers.get("laptop_pcs"):
                text = f"Yes, {answers['laptop_pcs']} pcs"
            if key == "laptop_pcs":
                continue
            rows.append((label, text))
        if skey == "design":
            names = [r["original_name"] for r in _files(form["id"]) if r["category"] == "design"]
            rows.append(("T-shirt design files", ", ".join(names)))
        out.append((slabel, rows))
    return out


def log(form_id, actor, action, details=None):
    db.execute("INSERT INTO coordination_form_log (form_id, actor, action, details, created_at) "
               "VALUES (?,?,?,?,datetime('now'))", (form_id, actor, action, details))


def _staff_name():
    return g.user["name"] if getattr(g, "user", None) else "Staff"


def _upload_dir(form_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "coordination_forms", str(form_id))
    os.makedirs(path, exist_ok=True)
    return path


def _files(form_id):
    return db.query("SELECT * FROM coordination_form_files WHERE form_id = ? ORDER BY uploaded_at, id", (form_id,))


def _file_dicts(form, public=False):
    """Files with their download link and who added them, for the templates.
    category 'design' = T-shirt design files (shown in the Design section)."""
    out = []
    for row in _files(form["id"]):
        d = dict(row)
        d["url"] = (url_for("coordination_form_public.public_file", token=form["token"], file_id=row["id"]) if public
                    else url_for("coordination_forms.file", form_id=form["id"], file_id=row["id"]))
        d["who"] = ("You" if row["uploaded_by"] == "Client" else "Modoku") if public else row["uploaded_by"]
        out.append(d)
    return out


def _save_files(form_id, files, uploaded_by, category=None):
    """Saves uploaded attachments; returns (saved names, error messages)."""
    saved, errors = [], []
    for fs in files:
        if not fs or not fs.filename:
            continue
        error = uploadutil.validate_upload(fs, allowed_extensions=uploadutil.DEFAULT_EXTENSIONS,
                                           max_bytes=FILE_MAX_BYTES)
        if error:
            errors.append(f"{fs.filename}: {error}")
            continue
        stored = f"{uuid.uuid4().hex[:8]}_{secure_filename(fs.filename) or 'file'}"
        fs.save(os.path.join(_upload_dir(form_id), stored))
        db.execute("INSERT INTO coordination_form_files (form_id, filename, original_name, uploaded_by, category) "
                   "VALUES (?,?,?,?,?)", (form_id, stored, fs.filename, uploaded_by, category))
        saved.append(fs.filename)
    return saved, errors


def _delete_file(form_id, file_id):
    row = db.query("SELECT * FROM coordination_form_files WHERE id = ? AND form_id = ?", (file_id, form_id), one=True)
    if row is None:
        return None
    try:
        os.remove(os.path.join(_upload_dir(form_id), row["filename"]))
    except OSError:
        pass
    db.execute("DELETE FROM coordination_form_files WHERE id = ?", (file_id,))
    return row


def _get(form_id):
    return db.query(
        """SELECT cf.*, co.name AS company_name, c.title AS course_title, cs.start_date AS class_start_date,
                  cs.end_date AS class_end_date, u.name AS confirmed_by_name, u.position AS confirmed_by_position,
                  u.signature_file AS confirmed_by_signature, cb.name AS created_by_name, cb.email AS created_by_email
           FROM coordination_forms cf
           LEFT JOIN companies co ON co.id = cf.client_company_id
           LEFT JOIN course_sessions cs ON cs.id = cf.session_id
           LEFT JOIN courses c ON c.id = cs.course_id
           LEFT JOIN users u ON u.id = cf.confirmed_by
           LEFT JOIN users cb ON cb.id = cf.created_by
           WHERE cf.id = ?""", (form_id,), one=True)


def _get_by_token(token):
    if not token:
        return None
    row = db.query("SELECT id FROM coordination_forms WHERE token = ?", (token,), one=True)
    return _get(row["id"]) if row else None


def _next_form_no():
    year = today_kl().year
    prefix = f"CF-{year}-"
    rows = db.query("SELECT form_no FROM coordination_forms WHERE form_no LIKE ?", (prefix + "%",))
    numbers = []
    for r in rows:
        try:
            numbers.append(int(r["form_no"].rsplit("-", 1)[1]))
        except (ValueError, IndexError, AttributeError):
            pass
    return f"{prefix}{(max(numbers) if numbers else 0) + 1:04d}"


def public_url(form):
    return url_for("coordination_form_public.public_view", token=form["token"], _external=True)


def _class_prefill(session_id):
    """Header + Details answers pre-filled from a class."""
    from . import schedule
    from .sessions import split_training_time
    s = db.query(
        """SELECT cs.*, c.title AS course_title, co.name AS company_name, co.address AS company_address,
                  pic.name AS pic_name, pic.email AS pic_email
           FROM course_sessions cs JOIN courses c ON c.id = cs.course_id
           LEFT JOIN companies co ON co.id = cs.client_company_id
           LEFT JOIN leads pic ON pic.id = cs.pic_lead_id
           WHERE cs.id = ?""", (session_id,), one=True)
    if s is None:
        return None, {}, {}
    days = schedule.row_days(s)
    first_start, _e = (days[0][1], days[0][2]) if days else split_training_time(s["training_time"])
    _s, last_end = (days[-1][1], days[-1][2]) if days else split_training_time(s["training_time"])
    answers = {
        "programme": s["course_title"],
        "start_date": s["start_date"],
        "end_date": s["end_date"] or s["start_date"],
        "company": s["company_name"],
        "pax": s["capacity"],
        "venue": s["venue"],
    }
    if first_start:
        answers["start_time"] = first_start
    if last_end:
        answers["end_time"] = last_end
    if s["room_setup"]:
        setup = {"Theatre": "Theater", "U-Shape": "U-Shape"}.get(s["room_setup"], s["room_setup"])
        if setup in ROOM_SETUPS:
            answers["room_setup"] = setup
        else:
            answers["room_setup"], answers["room_setup_other"] = "Other", s["room_setup"]
    if s["requires_laptop_rental"]:
        answers["laptop_rental"] = "Yes"
        if s["laptop_rental_qty"]:
            answers["laptop_pcs"] = s["laptop_rental_qty"]
    header = {"title": s["course_title"], "client_company_id": s["client_company_id"],
              "pic_name": s["pic_name"], "pic_email": s["pic_email"]}
    sections = list(OPTIONAL_SECTIONS)
    if (s["training_mode"] or "") == "Virtual":
        sections = ["design", "technical", "notes"]
    return header, {k: v for k, v in answers.items() if v not in (None, "")}, sections


def for_session(session_id):
    """The newest Coordination Form linked to a class, or None (class page card)."""
    row = db.query("SELECT id FROM coordination_forms WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                   (session_id,), one=True)
    return _get(row["id"]) if row else None


# --- PDF -------------------------------------------------------------------

def _data_uri(path, mimetype="image/png"):
    import base64
    try:
        with open(path, "rb") as fh:
            return f"data:{mimetype};base64," + base64.b64encode(fh.read()).decode()
    except OSError:
        return ""


IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def generate_pdf(form):
    """Fix231: the PDF in the same design as Modoku's Canva form, drawn with
    ReportLab (see coordination_form_pdf)."""
    from . import fmtdate
    from .coordination_form_pdf import render
    folder = _upload_dir(form["id"])
    design_images, design_other, other = [], [], []
    for row in _files(form["id"]):
        if row["category"] == "design":
            if row["filename"].lower().endswith(IMAGE_EXTENSIONS):
                design_images.append(os.path.join(folder, row["filename"]))
            else:
                design_other.append(row["original_name"])
        else:
            other.append(row["original_name"])
    client_sig = os.path.join(folder, form["client_signature_file"]) if form["client_signature_file"] else None
    staff_sig = None
    if form["confirmed_by"] and form["confirmed_by_signature"]:
        staff_sig = os.path.join(current_app.config["UPLOAD_FOLDER"], "users", str(form["confirmed_by"]),
                                 form["confirmed_by_signature"])
    confirmed = form["status"] == "Confirmed"
    return render(form, load_answers(form), load_sections(form), design_images, other, design_other,
                  client_sig_path=client_sig, client_name=form["client_signed_name"],
                  client_date=fmtdate(form["client_signed_at"]) if form["client_signed_at"] else None,
                  staff_sig_path=staff_sig if confirmed else None,
                  staff_name=form["confirmed_by_name"] if confirmed else None,
                  staff_position=form["confirmed_by_position"] if confirmed else None,
                  staff_date=fmtdate(form["confirmed_at"]) if confirmed and form["confirmed_at"] else None)


def pdf_filename(form):
    import re
    title = re.sub(r"[^A-Za-z0-9]+", "_", form["title"] or "Form")[:50].strip("_")
    return f"Coordination_Form_{title}.pdf"


# --- staff pages -----------------------------------------------------------

@bp.route("/")
@login_required
def index():
    status = request.args.get("status", "")
    q = (request.args.get("q") or "").strip()
    rows = db.query(
        """SELECT cf.*, co.name AS company_name, c.title AS course_title, cs.start_date AS class_start_date
           FROM coordination_forms cf
           LEFT JOIN companies co ON co.id = cf.client_company_id
           LEFT JOIN course_sessions cs ON cs.id = cf.session_id
           LEFT JOIN courses c ON c.id = cs.course_id
           ORDER BY COALESCE(cf.due_date, '9999') DESC, cf.id DESC""")
    forms = []
    for r in rows:
        st = display_status(r)
        if status and st != status:
            continue
        if q and q.lower() not in " ".join(str(r[k] or "") for k in ("title", "form_no", "company_name", "pic_name")).lower():
            continue
        forms.append({"row": r, "status": st, "due": due_info(r)})
    return render_template("coordination_forms/list.html", forms=forms, status=status, q=q,
                           statuses=STATUSES + ["Closed"], badges=STATUS_BADGES)


def _form_choices():
    companies = db.query("SELECT id, name FROM companies ORDER BY name")
    classes = db.query(
        """SELECT cs.id, cs.start_date, cs.end_date, c.title AS course_title, co.name AS company_name
           FROM course_sessions cs JOIN courses c ON c.id = cs.course_id
           LEFT JOIN companies co ON co.id = cs.client_company_id
           WHERE cs.status != 'Cancelled' AND cs.start_date >= date('now', '-60 days')
           ORDER BY cs.start_date""")
    return companies, classes


def _header_from_form(form_data):
    return {
        "title": (form_data.get("title") or "").strip(),
        "session_id": form_data.get("session_id", type=int) or None,
        "client_company_id": form_data.get("client_company_id", type=int) or None,
        "pic_name": (form_data.get("pic_name") or "").strip() or None,
        "pic_email": (form_data.get("pic_email") or "").strip() or None,
        "due_date": (_parse_date(form_data.get("due_date")) or None),
    }


@bp.route("/new", methods=("GET", "POST"))
@login_required
def new():
    companies, classes = _form_choices()
    if request.method == "POST":
        header = _header_from_form(request.form)
        sections = ["details"] + [s for s in request.form.getlist("sections") if s in OPTIONAL_SECTIONS]
        answers = answers_from_form(request.form, sections)
        if not header["title"]:
            header["title"] = answers.get("programme") or ""
        if not header["title"]:
            flash("Enter the programme name.", "danger")
            return render_template("coordination_forms/edit.html", f=None, header=header, answers=answers,
                                   sections=sections, companies=companies, classes=classes, **_choices())
        form_id = db.execute(
            """INSERT INTO coordination_forms (form_no, token, session_id, client_company_id, title, pic_name,
                   pic_email, due_date, sections, answers, status, created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,'Draft',?,datetime('now'),datetime('now'))""",
            (_next_form_no(), secrets.token_urlsafe(24), header["session_id"], header["client_company_id"],
             header["title"], header["pic_name"], header["pic_email"],
             header["due_date"].isoformat() if header["due_date"] else None,
             json.dumps(sections[1:]), json.dumps(answers), g.user["id"]))
        # Fix230: files chosen on the New form are saved too (before, only Edit saved them).
        saved, errors = _save_files(form_id, request.files.getlist("files"), _staff_name())
        if "design" in sections:
            more, more_errors = _save_files(form_id, request.files.getlist("design_files"), _staff_name(), "design")
            saved, errors = saved + more, errors + more_errors
        log(form_id, _staff_name(), "Created the form", ("Files added: " + ", ".join(saved)) if saved else None)
        for e in errors:
            flash(e, "danger")
        activity.log("create", "coordination_form", form_id, f"Coordination Form: {header['title']}")
        flash("Coordination Form created. Check the details, then send the link to the client.", "success")
        return redirect(url_for("coordination_forms.view", form_id=form_id))

    header = {"title": "", "session_id": None, "client_company_id": None, "pic_name": None, "pic_email": None,
              "due_date": today_kl() + timedelta(days=DEFAULT_DUE_DAYS)}
    answers, sections = {}, ["details"] + OPTIONAL_SECTIONS
    session_id = request.args.get("session_id", type=int)
    if session_id:
        h, answers, opt = _class_prefill(session_id)
        if h:
            header.update(h)
            header["session_id"] = session_id
            sections = ["details"] + opt
            start = _parse_date(answers.get("start_date"))
            if start and start - timedelta(days=7) > today_kl():
                header["due_date"] = start - timedelta(days=7)
    return render_template("coordination_forms/edit.html", f=None, header=header, answers=answers,
                           sections=sections, companies=companies, classes=classes, **_choices())


@bp.route("/class-prefill/<int:session_id>")
@login_required
def class_prefill(session_id):
    """Fix226: the class's details as JSON, for filling the form in the
    browser when a class is picked under "Linked class"."""
    header, answers, _sections = _class_prefill(session_id)
    if header is None:
        abort(404)
    due = None
    start = _parse_date(answers.get("start_date"))
    if start and start - timedelta(days=7) > today_kl():
        due = (start - timedelta(days=7)).isoformat()
    return {"header": header, "answers": answers, "due_date": due}


def _choices():
    return {"ROOM_SETUPS": ROOM_SETUPS, "OCCUPANCIES": OCCUPANCIES, "BREAKFAST": BREAKFAST, "YES_NO": YES_NO,
            "DIETS": DIETS, "ALLERGIES": ALLERGIES, "TRANSPORT_TYPES": TRANSPORT_TYPES,
            "SECTIONS": SECTIONS, "OPTIONAL_SECTIONS": OPTIONAL_SECTIONS}


@bp.route("/<int:form_id>")
@login_required
def view(form_id):
    f = _get(form_id)
    if f is None:
        abort(404)
    entries = db.query("SELECT * FROM coordination_form_log WHERE form_id = ? ORDER BY id DESC", (form_id,))
    return render_template("coordination_forms/view.html", f=f, status=display_status(f), badges=STATUS_BADGES,
                           due=due_info(f), sections=summary(f), files=_file_dicts(f), log_entries=entries,
                           link=public_url(f), mail_configured=mailer.is_configured(),
                           email_subject=_email_subject(f), email_body=_email_body(f),
                           has_signature=bool(g.user["signature_file"]))


@bp.route("/<int:form_id>/edit", methods=("GET", "POST"))
@login_required
def edit(form_id):
    f = _get(form_id)
    if f is None:
        abort(404)
    companies, classes = _form_choices()
    if request.method == "POST":
        header = _header_from_form(request.form)
        sections = ["details"] + [s for s in request.form.getlist("sections") if s in OPTIONAL_SECTIONS]
        old_answers, old_sections = load_answers(f), load_sections(f)
        answers = answers_from_form(request.form, sections)
        # Answers of a section switched off are kept, so switching it back on doesn't lose them.
        for key, value in old_answers.items():
            if FIELD_SECTIONS.get(key) not in sections:
                answers[key] = value
        header["title"] = header["title"] or f["title"]
        changes = diff_answers(old_answers, answers, sections)
        for label, old, new_ in (("Programme name (header)", f["title"], header["title"]),
                                 ("PIC name", f["pic_name"], header["pic_name"]),
                                 ("PIC email", f["pic_email"], header["pic_email"]),
                                 ("Due date", f["due_date"], header["due_date"].isoformat() if header["due_date"] else None)):
            if (old or "") != (new_ or ""):
                changes.append(f"{label}: {old or '(blank)'} → {new_ or '(blank)'}")
        added = [SECTION_LABELS[s] for s in sections if s not in old_sections]
        removed = [SECTION_LABELS[s] for s in old_sections if s not in sections]
        if added:
            changes.append("Sections turned on: " + ", ".join(added))
        if removed:
            changes.append("Sections turned off: " + ", ".join(removed))
        db.execute(
            """UPDATE coordination_forms SET title=?, session_id=?, client_company_id=?, pic_name=?, pic_email=?,
                   due_date=?, sections=?, answers=?, updated_at=datetime('now') WHERE id=?""",
            (header["title"], header["session_id"], header["client_company_id"], header["pic_name"],
             header["pic_email"], header["due_date"].isoformat() if header["due_date"] else None,
             json.dumps(sections[1:]), json.dumps(answers), form_id))
        saved, errors = _save_files(form_id, request.files.getlist("files"), _staff_name())
        if "design" in sections:
            more, more_errors = _save_files(form_id, request.files.getlist("design_files"), _staff_name(), "design")
            saved, errors = saved + more, errors + more_errors
        if saved:
            changes.append("Files added: " + ", ".join(saved))
        for e in errors:
            flash(e, "danger")
        if changes:
            log(form_id, _staff_name(), "Edited the form", "\n".join(changes))
            activity.log("update", "coordination_form", form_id, f"Coordination Form: {header['title']}")
            flash("Saved. The change is in the form's change log.", "success")
        else:
            flash("No changes.", "info")
        return redirect(url_for("coordination_forms.view", form_id=form_id))
    header = {"title": f["title"], "session_id": f["session_id"], "client_company_id": f["client_company_id"],
              "pic_name": f["pic_name"], "pic_email": f["pic_email"], "due_date": _parse_date(f["due_date"])}
    return render_template("coordination_forms/edit.html", f=f, header=header, answers=load_answers(f),
                           sections=load_sections(f), companies=companies, classes=classes,
                           files=_file_dicts(f), **_choices())


def _email_subject(f):
    due = due_info(f)
    subject = f"Coordination Form: {f['title']}"
    if due.get("due"):
        subject += f" (please complete by {due['due']})"
    return subject


def _email_body(f):
    due = due_info(f)
    greeting = f"Dear {f['pic_name']}," if f["pic_name"] else "Dear Sir/Madam,"
    due_line = f"Please complete and sign it by {due['due']}. After that the form locks.\n\n" if due.get("due") else ""
    return (f"{greeting}\n\n"
            f"To make sure every detail of {f['title']} is in place, please fill in our Coordination Form "
            f"(room setup, food & beverage, accommodation, transport and other requirements) at this link:\n\n"
            f"{public_url(f)}\n\n"
            f"{due_line}"
            "You can save it and come back to it anytime before then, and upload any files we need "
            "(for example a T-shirt design). Once you sign and submit, we'll confirm it from our side.\n\n"
            "Thank you.")


def _mark_sent(f, to=None):
    if f["status"] == "Draft":
        db.execute("UPDATE coordination_forms SET status='Sent' WHERE id=?", (f["id"],))
    db.execute("UPDATE coordination_forms SET sent_at=datetime('now'), sent_to=COALESCE(?, sent_to) WHERE id=?",
               (to, f["id"]))


@bp.route("/<int:form_id>/send-email", methods=("POST",))
@login_required
def send_email(form_id):
    f = _get(form_id)
    if f is None:
        abort(404)
    to = (request.form.get("to_email") or "").strip()
    cc = (request.form.get("cc_email") or "").strip() or None
    if not to:
        flash("Enter the client's email address.", "danger")
        return redirect(url_for("coordination_forms.view", form_id=form_id))
    try:
        mailer.send_email(to, request.form.get("subject") or _email_subject(f), request.form.get("body") or _email_body(f),
                          related_type="coordination_form", related_id=form_id, cc_email=cc)
    except (mailer.MailNotConfigured, mailer.MailSendError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for("coordination_forms.view", form_id=form_id))
    _mark_sent(f, to)
    log(form_id, _staff_name(), "Emailed the link", f"To: {to}" + (f"\nCC: {cc}" if cc else ""))
    flash(f"Link emailed to {to}.", "success")
    return redirect(url_for("coordination_forms.view", form_id=form_id))


@bp.route("/<int:form_id>/mark-sent", methods=("POST",))
@login_required
def mark_sent(form_id):
    """'Copy link' also counts as sending it (Draft -> Sent)."""
    f = _get(form_id)
    if f is None:
        abort(404)
    if f["status"] == "Draft":
        _mark_sent(f)
        log(form_id, _staff_name(), "Copied the link to share")
    return {"ok": True}


@bp.route("/<int:form_id>/due-date", methods=("POST",))
@login_required
def set_due_date(form_id):
    f = _get(form_id)
    if f is None:
        abort(404)
    new_due = _parse_date(request.form.get("due_date"))
    if new_due is None:
        flash("Pick a due date.", "danger")
        return redirect(url_for("coordination_forms.view", form_id=form_id))
    db.execute("UPDATE coordination_forms SET due_date=?, updated_at=datetime('now') WHERE id=?",
               (new_due.isoformat(), form_id))
    was_open = is_open(f)
    log(form_id, _staff_name(), "Reopened the form" if not was_open and new_due >= today_kl() else "Changed the due date",
        f"Due date: {_value_text('start_date', f['due_date']) or '(none)'} → {_value_text('start_date', new_due.isoformat())}")
    flash(f"Due date set to {new_due.strftime('%-d %b %Y')}.", "success")
    return redirect(url_for("coordination_forms.view", form_id=form_id))


@bp.route("/<int:form_id>/confirm", methods=("POST",))
@login_required
def confirm(form_id):
    """Modoku countersigns (whoever clicks), then the signed PDF is emailed
    to the client's PIC with the confirming staff member in CC."""
    f = _get(form_id)
    if f is None:
        abort(404)
    if f["status"] != "Submitted":
        flash("Only a form the client has signed and submitted can be confirmed.", "warning")
        return redirect(url_for("coordination_forms.view", form_id=form_id))
    user = db.query("SELECT * FROM users WHERE id = ?", (g.user["id"],), one=True)
    if not user["signature_file"]:
        flash("Add your signature in My Profile first, it goes on the confirmed form.", "warning")
        return redirect(url_for("coordination_forms.view", form_id=form_id))
    db.execute("UPDATE coordination_forms SET status='Confirmed', confirmed_by=?, confirmed_at=datetime('now') WHERE id=?",
               (g.user["id"], form_id))
    log(form_id, _staff_name(), "Confirmed and signed for Modoku")
    activity.log("confirm", "coordination_form", form_id, f"Coordination Form: {f['title']}")
    f = _get(form_id)
    to = f["pic_email"]
    if to and mailer.is_configured():
        try:
            pdf = generate_pdf(f)
            body = (f"Dear {f['pic_name'] or 'Sir/Madam'},\n\nThank you for completing the Coordination Form for "
                    f"{f['title']}. We've confirmed it from our side, a copy signed by both parties is attached.\n\n"
                    "If anything changes, please let us know as soon as possible.\n\nThank you.")
            mailer.send_email(to, f"Confirmed: Coordination Form for {f['title']}", body,
                              attachments=[(pdf_filename(f), pdf, "application/pdf")],
                              related_type="coordination_form", related_id=form_id, cc_email=user["email"])
            log(form_id, _staff_name(), "Emailed the confirmed copy", f"To: {to}\nCC: {user['email']}")
            flash(f"Confirmed. The signed copy was emailed to {to}, with you in CC.", "success")
        except Exception as exc:  # noqa: BLE001 - the confirmation itself is saved either way
            current_app.logger.exception("Coordination Form %s: confirmed copy email failed", form_id)
            flash(f"Confirmed, but the signed copy couldn't be emailed: {exc}", "warning")
    else:
        flash("Confirmed. Download the PDF to send it to the client"
              + ("" if to else " (no PIC email on this form)") + ".", "success")
    return redirect(url_for("coordination_forms.view", form_id=form_id))


@bp.route("/<int:form_id>/pdf")
@login_required
def pdf(form_id):
    f = _get(form_id)
    if f is None:
        abort(404)
    from .docutil import content_disposition
    return Response(generate_pdf(f), mimetype="application/pdf",
                    headers={"Content-Disposition": content_disposition(pdf_filename(f), as_attachment=False)})


@bp.route("/<int:form_id>/files/<int:file_id>")
@login_required
def file(form_id, file_id):
    row = db.query("SELECT * FROM coordination_form_files WHERE id = ? AND form_id = ?", (file_id, form_id), one=True)
    if row is None:
        abort(404)
    return send_from_directory(_upload_dir(form_id), row["filename"], download_name=row["original_name"])


@bp.route("/<int:form_id>/files/<int:file_id>/delete", methods=("POST",))
@login_required
def delete_file(form_id, file_id):
    row = _delete_file(form_id, file_id)
    if row:
        log(form_id, _staff_name(), "Removed a file", row["original_name"])
        flash("File removed.", "success")
    return redirect(url_for("coordination_forms.view", form_id=form_id))


@bp.route("/<int:form_id>/client-signature")
@login_required
def client_signature(form_id):
    f = _get(form_id)
    if f is None or not f["client_signature_file"]:
        abort(404)
    return send_from_directory(_upload_dir(form_id), f["client_signature_file"])


@bp.route("/<int:form_id>/delete", methods=("POST",))
@admin_required
def delete(form_id):
    f = _get(form_id)
    if f is None:
        abort(404)
    db.execute("DELETE FROM coordination_forms WHERE id = ?", (form_id,))
    activity.log("delete", "coordination_form", form_id, f"Coordination Form: {f['title']}")
    flash("Coordination Form deleted.", "success")
    return redirect(url_for("coordination_forms.index"))


# --- public (client) pages -------------------------------------------------

def _not_found():
    return render_template("coordination_forms/public_not_found.html"), 404


@public_bp.route("/<token>")
def public_view(token):
    f = _get_by_token(token)
    if f is None:
        return _not_found()
    return render_template("coordination_forms/public.html", f=f, token=token, answers=load_answers(f),
                           sections=load_sections(f), due=due_info(f), open_=is_open(f), files=_file_dicts(f, public=True),
                           consent=CONSENT_TEXT, **_choices())


@public_bp.route("/<token>/save", methods=("POST",))
def save(token):
    """'Save for later' or 'Sign & Submit'. A change to a signed form needs
    the client's signature again (status back to Sent until they re-sign)."""
    from .t3 import decode_signature_png
    f = _get_by_token(token)
    if f is None:
        return _not_found()
    if not is_open(f):
        flash("This form closed on its due date, so changes can't be saved. Please contact Modoku.", "warning")
        return redirect(url_for("coordination_form_public.public_view", token=token))
    submitting = request.form.get("action") == "submit"
    sections = load_sections(f)
    old_answers = load_answers(f)
    answers = answers_from_form(request.form, sections)
    for key, value in old_answers.items():  # keep answers of sections staff switched off
        if FIELD_SECTIONS.get(key) not in sections:
            answers[key] = value
    changes = diff_answers(old_answers, answers, sections)
    saved, errors = _save_files(f["id"], request.files.getlist("files"), "Client")
    if "design" in sections:
        more, more_errors = _save_files(f["id"], request.files.getlist("design_files"), "Client", "design")
        saved, errors = saved + more, errors + more_errors
    if saved:
        changes.append("Files added: " + ", ".join(saved))
    for fid in request.form.getlist("remove_file", type=int):
        row = db.query("SELECT * FROM coordination_form_files WHERE id = ? AND form_id = ?", (fid, f["id"]), one=True)
        if row and row["uploaded_by"] == "Client":
            _delete_file(f["id"], fid)
            changes.append(f"File removed: {row['original_name']}")

    signer = (request.form.get("signed_name") or "").strip()
    if submitting:
        signature = decode_signature_png(request.form.get("signature_data"))
        if not signer or signature is None:
            # Save what they typed so nothing is lost, then ask for the signature.
            db.execute("UPDATE coordination_forms SET answers=?, updated_at=datetime('now') WHERE id=?",
                       (json.dumps(answers), f["id"]))
            if changes:
                log(f["id"], f"Client ({signer or f['client_signed_name'] or 'unsigned'})", "Saved changes", "\n".join(changes))
            flash("Your answers are saved. To submit, type your name and sign in the box.", "warning")
            return redirect(url_for("coordination_form_public.public_view", token=token) + "#sign")
        stored = f"client_signature_{uuid.uuid4().hex[:8]}.png"
        with open(os.path.join(_upload_dir(f["id"]), stored), "wb") as fh:
            fh.write(signature)
        resubmit = bool(f["client_signed_at"])  # signed before, then changed or signed again
        db.execute(
            """UPDATE coordination_forms SET answers=?, status='Submitted', client_signed_name=?,
                   client_signature_file=?, client_signed_at=datetime('now'), confirmed_by=NULL, confirmed_at=NULL,
                   updated_at=datetime('now') WHERE id=?""",
            (json.dumps(answers), signer, stored, f["id"]))
        log(f["id"], f"Client ({signer})", "Signed and re-submitted" if resubmit else "Signed and submitted",
            "\n".join(changes) or None)
        _notify_staff(f, signer, resubmit, changes)
        for e in errors:
            flash(e, "danger")
        flash("Thank you, your Coordination Form is submitted. Modoku will confirm it from our side. "
              "If you edit anything later, you need to sign and submit again.", "success")
        return redirect(url_for("coordination_form_public.public_view", token=token))

    status = f["status"]
    if changes and status in ("Submitted", "Confirmed"):
        status = "Sent"  # what they signed has changed: needs signing again
    db.execute("UPDATE coordination_forms SET answers=?, status=?, updated_at=datetime('now') WHERE id=?",
               (json.dumps(answers), "Sent" if status == "Draft" else status, f["id"]))
    if changes:
        log(f["id"], f"Client ({f['client_signed_name'] or 'not signed yet'})", "Saved changes", "\n".join(changes))
        if f["status"] in ("Submitted", "Confirmed"):
            notifications.notify_admins("coordination_form", f"Coordination Form changed: {f['title']}",
                                        body="The client changed a signed form. It needs their signature again.",
                                        link=url_for("coordination_forms.view", form_id=f["id"]))
    for e in errors:
        flash(e, "danger")
    if changes and f["status"] in ("Submitted", "Confirmed"):
        flash("Changes saved. Please sign and submit again so we have your confirmation of the updated details.", "warning")
    else:
        flash("Saved. You can come back to this link anytime before the due date.", "success")
    return redirect(url_for("coordination_form_public.public_view", token=token))


def _notify_staff(f, signer, resubmit, changes):
    title = f"Coordination Form {'re-submitted' if resubmit else 'submitted'}: {f['title']}"
    link = url_for("coordination_forms.view", form_id=f["id"])
    body = f"Signed by {signer}. Check it and click Confirm & Sign."
    # Admins plus whoever created the form, once each (the creator is often an admin too).
    recipients = {r["id"] for r in db.query("SELECT id FROM users WHERE role = 'admin' AND active = 1")}
    if f["created_by"]:
        recipients.add(f["created_by"])
    for user_id in recipients:
        notifications.notify(user_id, "coordination_form", title, body=body, link=link)
    if f["created_by_email"] and mailer.is_configured():
        try:
            text = (f"{signer} has {'re-submitted' if resubmit else 'signed and submitted'} the Coordination Form "
                    f"for {f['title']}.\n\n" + ("Changes:\n" + "\n".join(changes) + "\n\n" if changes else "")
                    + f"Check it and click Confirm & Sign:\n{url_for('coordination_forms.view', form_id=f['id'], _external=True)}")
            mailer.send_email(f["created_by_email"], title, text, related_type="coordination_form", related_id=f["id"])
        except Exception:  # noqa: BLE001 - the bell notification is enough if email fails
            current_app.logger.exception("Coordination Form %s: staff email failed", f["id"])


@public_bp.route("/<token>/files/<int:file_id>")
def public_file(token, file_id):
    f = _get_by_token(token)
    if f is None:
        return _not_found()
    row = db.query("SELECT * FROM coordination_form_files WHERE id = ? AND form_id = ?", (file_id, f["id"]), one=True)
    if row is None:
        abort(404)
    return send_from_directory(_upload_dir(f["id"]), row["filename"], download_name=row["original_name"])


@public_bp.route("/<token>/pdf")
def public_pdf(token):
    f = _get_by_token(token)
    if f is None or f["status"] not in ("Submitted", "Confirmed"):
        return _not_found()
    from .docutil import content_disposition
    return Response(generate_pdf(f), mimetype="application/pdf",
                    headers={"Content-Disposition": content_disposition(pdf_filename(f), as_attachment=False)})
