"""T3 (HRDCorp SBL-KHAS) Attendance List participants.

Deliberately kept separate from `enrollments` — the T3 attendance list is
just the physical/printed sign-in sheet for one training day, and its
names shouldn't affect enrollment counts, capacity, or HRDF claim
tracking. Supports manual add, paste-based bulk add, and CSV upload for
large cohorts.

Also home to the e-Signature attendance helpers shared with t3_public.py
(check_and_record_sign_attempt, decode_signature_png, _t3_signature_dir) —
see t3_public.sign for the public-facing route that uses them.
"""
import base64
import binascii
import csv
import io
import os
from datetime import datetime, timedelta

from flask import Blueprint, current_app, flash, redirect, render_template, request, send_from_directory, url_for

from . import ai_match, attendance_days, db, uploadutil
from . import certificates as _certificates
from .auth import login_required

bp = Blueprint("t3", __name__, url_prefix="/t3")

GENDERS = ["Male", "Female"]
CITIZENSHIPS = ["Malaysian", "Non-Malaysian"]

# e-Signature identity check: how many wrong IC-number guesses a participant
# row tolerates before signing locks for a while — mirrors the existing
# users.locked_until pattern used for staff login, so someone can't just
# try every combination to sign attendance as somebody else on the list.
SIGN_FAIL_LIMIT = 5
SIGN_LOCKOUT_MINUTES = 15

# A genuinely blank/empty signature-pad PNG is a few hundred bytes; a real
# hand-drawn signature is comfortably larger. 300 KB is far more than a
# simple line drawing ever needs, so anything past that is rejected rather
# than trusted.
MIN_SIGNATURE_BYTES = 300
MAX_SIGNATURE_BYTES = 300 * 1024


def _normalize_gender(raw):
    raw = (raw or "").strip().lower()
    if raw in ("m", "male"):
        return "Male"
    if raw in ("f", "female"):
        return "Female"
    return (raw.strip().title() if raw else None)


def _ic_taken(session_id, ic_no, exclude_participant_id=None):
    """True if another participant already on this class's T3 attendance
    list has this IC number (case/whitespace-insensitive comparison).
    A blank IC is exempt — only real values need to be unique, and
    uniqueness is scoped to this one class's list, not across the whole
    system (the same person legitimately attends multiple trainings)."""
    ic_no = (ic_no or "").strip()
    if not ic_no:
        return False
    sql = "SELECT id FROM t3_participants WHERE session_id = ? AND lower(trim(ic_no)) = ?"
    params = [session_id, ic_no.lower()]
    if exclude_participant_id:
        sql += " AND id != ?"
        params.append(exclude_participant_id)
    return db.query(sql, params, one=True) is not None


def _normalize_ic(raw):
    """Loose IC-number comparison: strip everything but letters/digits and
    lowercase, so '900101-14-5566', '900101 14 5566', and '900101145566'
    all match the same stored value — a participant shouldn't fail the
    identity check over formatting, only over an actually wrong number."""
    return "".join(ch for ch in (raw or "").lower() if ch.isalnum())


def check_and_record_sign_attempt(participant, entered_ic):
    """The e-Signature identity check: does entered_ic match this
    participant's IC on file? Tracks repeated wrong guesses (sign_fail_count
    / sign_locked_until on t3_participants) so someone can't just try every
    combination to sign as somebody else on the list — mirrors the same
    fail-count/lockout shape already used for staff login. Returns
    (ok, error_message); error_message is None when ok is True. Resets the
    fail count on a correct match."""
    now = datetime.utcnow()
    locked_until = participant["sign_locked_until"]
    if locked_until:
        try:
            if datetime.fromisoformat(locked_until) > now:
                return False, ("Too many incorrect attempts for this name — signing is locked for a few "
                                "minutes. Ask a staff member for help if you need to sign right now.")
        except ValueError:
            pass

    if not (participant["ic_no"] or "").strip():
        return False, "No IC number is on file for this participant yet — ask staff to add one before signing."

    if _normalize_ic(entered_ic) != _normalize_ic(participant["ic_no"]):
        fail_count = (participant["sign_fail_count"] or 0) + 1
        if fail_count >= SIGN_FAIL_LIMIT:
            lock_until = (now + timedelta(minutes=SIGN_LOCKOUT_MINUTES)).isoformat(" ")
            db.execute("UPDATE t3_participants SET sign_fail_count = ?, sign_locked_until = ? WHERE id = ?",
                       (fail_count, lock_until, participant["id"]))
        else:
            db.execute("UPDATE t3_participants SET sign_fail_count = ? WHERE id = ?",
                       (fail_count, participant["id"]))
        return False, "That IC number doesn't match our records for this name — please try again."

    db.execute("UPDATE t3_participants SET sign_fail_count = 0, sign_locked_until = NULL WHERE id = ?",
               (participant["id"],))
    return True, None


def decode_signature_png(data_url):
    """Parses a 'data:image/png;base64,....' string from the signature-pad
    canvas into raw PNG bytes — or None if it's missing, malformed, not
    actually a PNG, or clearly too small (an empty canvas) or too large to
    be a real hand-drawn signature. Never trusts the browser's own
    blank-canvas check alone; this is the server-side backstop."""
    if not data_url or "," not in data_url:
        return None
    header, _, encoded = data_url.partition(",")
    if "image/png" not in header:
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(raw) < MIN_SIGNATURE_BYTES or len(raw) > MAX_SIGNATURE_BYTES:
        return None
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    return raw


def _t3_signature_dir(session_id):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "sessions", str(session_id), "signatures")
    os.makedirs(path, exist_ok=True)
    return path


def _t3_count(session_id):
    row = db.query("SELECT COUNT(*) AS n FROM t3_participants WHERE session_id = ?", (session_id,), one=True)
    return row["n"] if row else 0


def t3_remaining_capacity(session_row):
    """The class's `capacity` (course_sessions.capacity, e.g. 20 pax) caps
    the T3 attendance list the same way it's meant to cap the class itself —
    returns how many more participants can still be added, or None if the
    class has no capacity limit set (falsy/zero, treated as unlimited).
    Never negative — a list that's already over capacity (e.g. capacity was
    lowered after names were added) just reports 0 remaining, not overfull."""
    capacity = session_row["capacity"] if session_row else None
    if not capacity or capacity <= 0:
        return None
    return max(0, capacity - _t3_count(session_row["id"]))


def _insert_participants(session_id, participants, remaining_capacity):
    """Shared bulk-insert loop for paste/CSV adds (both the staff-facing T3
    manage page and the public T3 Attendance Form use this) — participants
    is a list of (name, ic_no, employer, gender, citizenship) tuples.
    Stops accepting new rows once remaining_capacity is used up (None means
    no cap) rather than silently letting the list run over the class's
    capacity. Returns (added, skipped_duplicate, skipped_capacity)."""
    seen_ics = set()
    added = 0
    skipped_dup = 0
    skipped_capacity = 0
    for name, ic_no, employer, gender, citizenship in participants:
        if remaining_capacity is not None and added >= remaining_capacity:
            skipped_capacity += 1
            continue
        ic_key = (ic_no or "").strip().lower()
        if ic_key and (ic_key in seen_ics or _ic_taken(session_id, ic_no)):
            skipped_dup += 1
            continue
        if ic_key:
            seen_ics.add(ic_key)
        db.execute(
            """INSERT INTO t3_participants (session_id, name, ic_no, employer_name, gender, citizenship)
               VALUES (?,?,?,?,?,?)""",
            (session_id, name, ic_no, employer, gender, citizenship or "Malaysian"),
        )
        added += 1
    return added, skipped_dup, skipped_capacity


def _bulk_result_flash(added, skipped_dup, skipped_capacity, capacity, verb="Added"):
    """Builds the flash message + category for a bulk paste/CSV add, covering
    every combination of added/skipped-duplicate/skipped-capacity."""
    parts = []
    if added:
        parts.append(f"{verb} {added} participant(s).")
    if skipped_dup:
        parts.append(f"Skipped {skipped_dup} with a duplicate IC number already on this list.")
    if skipped_capacity:
        parts.append(f"Skipped {skipped_capacity} — this class's attendance list is capped at {capacity} pax.")
    if not parts:
        return "Nothing to add.", "danger"
    category = "success" if added and not skipped_dup and not skipped_capacity else ("warning" if added else "danger")
    return " ".join(parts), category


def _session_or_none(session_id):
    return db.query(
        """SELECT cs.*, c.title AS course_title FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?""",
        (session_id,), one=True,
    )


def _parse_bulk_lines(raw_text):
    """One participant per line: Name[, IC No][, Employer][, Gender]."""
    participants = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        name = parts[0] if parts else ""
        if not name:
            continue
        ic_no = parts[1] if len(parts) > 1 and parts[1] else None
        employer = parts[2] if len(parts) > 2 and parts[2] else None
        gender = _normalize_gender(parts[3]) if len(parts) > 3 else None
        participants.append((name, ic_no, employer, gender))
    return participants


def _parse_csv(file_storage):
    """Accepts a CSV with an optional header row. Recognized header names
    (case-insensitive): Name, IC No/NRIC, Employer, Gender/Sex, Citizenship.
    Without a recognized header, columns are read positionally in that order."""
    raw = file_storage.stream.read().decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(raw)))
    if not rows:
        return []

    header = [h.strip().lower() for h in rows[0]]
    known = {"name", "ic no", "ic_no", "nric", "employer", "employer name",
             "gender", "sex", "citizenship"}
    has_header = any(h in known for h in header)
    data_rows = rows[1:] if has_header else rows

    def col_index(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    if has_header:
        idx_name = col_index("name")
        idx_ic = col_index("ic no", "ic_no", "nric")
        idx_emp = col_index("employer", "employer name")
        idx_gender = col_index("gender", "sex")
        idx_cit = col_index("citizenship")
    else:
        idx_name, idx_ic, idx_emp, idx_gender, idx_cit = 0, 1, 2, 3, 4

    def get(row, i):
        return row[i].strip() if i is not None and i < len(row) and row[i] else ""

    participants = []
    for row in data_rows:
        if not row or not any((c or "").strip() for c in row):
            continue
        name = get(row, idx_name)
        if not name:
            continue
        participants.append((
            name,
            get(row, idx_ic) or None,
            get(row, idx_emp) or None,
            _normalize_gender(get(row, idx_gender)),
            get(row, idx_cit) or None,
        ))
    return participants


@bp.route("/sessions/<int:session_id>/manage")
@login_required
def manage(session_id):
    session_row = _session_or_none(session_id)
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))
    participants = db.query(
        "SELECT * FROM t3_participants WHERE session_id = ? ORDER BY id", (session_id,)
    )
    training_days = attendance_days.training_days_iso_for_session(session_row)
    total_days = len(training_days)
    # Attach each participant's per-day attendance count so a multi-day
    # class can show "2/3 Days" instead of a plain yes/no — invisible for
    # an ordinary single-day class, where total_days is always 1 and this
    # collapses back to the familiar attended/not-attended badge.
    #
    # Also build a participant_id -> {training_date: signature_file} map so
    # the template can show a small pen icon per scheduled day, filled in
    # once that day has a captured e-signature — an audit trail for staff,
    # regardless of whether e-signature is currently on or off.
    sig_rows = db.query(
        """SELECT tda.participant_id, tda.training_date, tda.signature_file
           FROM t3_day_attendance tda
           JOIN t3_participants p ON p.id = tda.participant_id
           WHERE p.session_id = ?""",
        (session_id,),
    )
    signatures_by_participant = {}
    for row in sig_rows:
        signatures_by_participant.setdefault(row["participant_id"], {})[row["training_date"]] = row["signature_file"]

    participants_with_status = []
    for p in participants:
        days_done, _ = attendance_days.attendance_status(p["id"], session_row)
        participants_with_status.append({"p": p, "days_done": days_done})
    return render_template("t3/manage.html", s=session_row, participants=participants_with_status,
                            total_days=total_days, genders=GENDERS, citizenships=CITIZENSHIPS,
                            remaining=t3_remaining_capacity(session_row),
                            training_days=training_days, signatures_by_participant=signatures_by_participant)


@bp.route("/sessions/<int:session_id>/add", methods=("POST",))
@login_required
def add(session_id):
    session_row = _session_or_none(session_id)
    name = request.form.get("name", "").strip()
    ic_no = request.form.get("ic_no") or None
    remaining = t3_remaining_capacity(session_row) if session_row else None
    if not name:
        flash("Name is required.", "danger")
    elif remaining is not None and remaining <= 0:
        flash(f"This class's attendance list is already full ({session_row['capacity']} pax capacity) — "
              f"remove a participant to add another.", "danger")
    elif _ic_taken(session_id, ic_no):
        flash(f"IC number {ic_no} is already on this class's attendance list.", "danger")
    else:
        db.execute(
            """INSERT INTO t3_participants (session_id, name, ic_no, employer_name, gender, citizenship)
               VALUES (?,?,?,?,?,?)""",
            (session_id, name, ic_no,
             request.form.get("employer_name") or None,
             request.form.get("gender") or None,
             request.form.get("citizenship") or "Malaysian"),
        )
        flash("Participant added to the T3 attendance list.", "success")
    return redirect(url_for("t3.manage", session_id=session_id))


@bp.route("/<int:participant_id>/edit", methods=("GET", "POST"))
@login_required
def edit(participant_id):
    participant = db.query("SELECT * FROM t3_participants WHERE id = ?", (participant_id,), one=True)
    if participant is None:
        flash("Participant not found.", "danger")
        return redirect(url_for("sessions.index"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        ic_no = request.form.get("ic_no") or None
        if not name:
            flash("Name is required.", "danger")
        elif _ic_taken(participant["session_id"], ic_no, exclude_participant_id=participant_id):
            flash(f"IC number {ic_no} is already on this class's attendance list.", "danger")
        else:
            db.execute(
                """UPDATE t3_participants SET name=?, ic_no=?, employer_name=?, gender=?, citizenship=?
                   WHERE id=?""",
                (name, ic_no, request.form.get("employer_name") or None,
                 request.form.get("gender") or None, request.form.get("citizenship") or "Malaysian",
                 participant_id),
            )
            flash("Participant updated.", "success")
            return redirect(url_for("t3.manage", session_id=participant["session_id"]))

    return render_template("t3/edit.html", participant=participant, genders=GENDERS,
                            citizenships=CITIZENSHIPS)


@bp.route("/<int:participant_id>/delete", methods=("POST",))
@login_required
def delete(participant_id):
    participant = db.query("SELECT session_id FROM t3_participants WHERE id = ?",
                            (participant_id,), one=True)
    db.execute("DELETE FROM t3_participants WHERE id = ?", (participant_id,))
    flash("Participant removed from the attendance list.", "success")
    if participant:
        return redirect(url_for("t3.manage", session_id=participant["session_id"]))
    return redirect(url_for("sessions.index"))


@bp.route("/sessions/<int:session_id>/bulk-add", methods=("POST",))
@login_required
def bulk_add(session_id):
    session_row = _session_or_none(session_id)
    raw_text = request.form.get("bulk_text", "")
    parsed = _parse_bulk_lines(raw_text)
    if not parsed:
        flash("Nothing to add — enter at least one name.", "danger")
        return redirect(url_for("t3.manage", session_id=session_id))

    participants = [(name, ic_no, employer, gender, "Malaysian") for name, ic_no, employer, gender in parsed]
    remaining = t3_remaining_capacity(session_row) if session_row else None
    added, skipped_dup, skipped_capacity = _insert_participants(session_id, participants, remaining)
    capacity = session_row["capacity"] if session_row else None
    msg, category = _bulk_result_flash(added, skipped_dup, skipped_capacity, capacity)
    flash(msg, category)
    return redirect(url_for("t3.manage", session_id=session_id))


@bp.route("/sessions/<int:session_id>/csv-upload", methods=("POST",))
@login_required
def csv_upload(session_id):
    session_row = _session_or_none(session_id)
    file_storage = request.files.get("csv_file")
    if not file_storage or not file_storage.filename:
        flash("Choose a CSV file first.", "danger")
        return redirect(url_for("t3.manage", session_id=session_id))
    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.CSV_EXTENSIONS)
    if error:
        flash(error, "danger")
        return redirect(url_for("t3.manage", session_id=session_id))

    try:
        participants = _parse_csv(file_storage)
    except Exception:
        flash("Couldn't read that CSV file — check the format and try again.", "danger")
        return redirect(url_for("t3.manage", session_id=session_id))

    if not participants:
        flash("No participant rows found in that CSV.", "danger")
        return redirect(url_for("t3.manage", session_id=session_id))

    remaining = t3_remaining_capacity(session_row) if session_row else None
    added, skipped_dup, skipped_capacity = _insert_participants(session_id, participants, remaining)
    capacity = session_row["capacity"] if session_row else None
    msg, category = _bulk_result_flash(added, skipped_dup, skipped_capacity, capacity, verb="Imported")
    flash(msg, category)
    return redirect(url_for("t3.manage", session_id=session_id))


@bp.route("/bulk-delete", methods=("POST",))
@login_required
def bulk_delete():
    session_id = request.form.get("session_id", type=int)
    ids = request.form.getlist("participant_ids")
    if ids:
        placeholders = ",".join("?" * len(ids))
        db.execute(f"DELETE FROM t3_participants WHERE id IN ({placeholders})", ids)
        flash(f"Removed {len(ids)} participant(s).", "success")
    if session_id:
        return redirect(url_for("t3.manage", session_id=session_id))
    return redirect(url_for("sessions.index"))


@bp.route("/bulk-attended", methods=("POST",))
@login_required
def bulk_attended():
    """Staff cross-check against the signed/returned attendance form: mark
    (or un-mark) which participants actually attended. This is a direct
    human certification, so — unlike a single AI-matched photo, which only
    covers the one day it was submitted for — ticking "Mark Attended" here
    marks a participant attended for *every* one of the class's scheduled
    training days at once. For an ordinary single-day class that's exactly
    what it always did; for a multi-day class it's staff saying "yes, this
    person fully attended", the same certification a signature would mean.
    Only fully-attended participants are eligible to claim their
    e-Certificate."""
    session_id = request.form.get("session_id", type=int)
    ids = request.form.getlist("participant_ids")
    attended = 1 if request.form.get("attended") == "1" else 0
    session_row = _session_or_none(session_id) if session_id else None
    if ids and session_row is not None:
        for pid in ids:
            try:
                if attended:
                    became_fully_attended = attendance_days.mark_all_days_attended(
                        int(pid), session_id, session_row, source="manual")
                    if became_fully_attended:
                        _certificates.generate_and_store_certificate(int(pid))
                else:
                    attendance_days.clear_all_days(int(pid))
                    _certificates.remove_certificate(int(pid))
            except Exception:  # noqa: BLE001
                current_app.logger.exception("Failed to sync certificate for participant %s", pid)
        verb = "Marked" if attended else "Unmarked"
        flash(f"{verb} {len(ids)} participant(s) as attended.", "success")
    else:
        flash("Select at least one participant first.", "danger")
    if session_id:
        return redirect(url_for("t3.manage", session_id=session_id))
    return redirect(url_for("sessions.index"))


@bp.route("/confirm-day-attendance", methods=("POST",))
@login_required
def confirm_day_attendance():
    """Confirms AI-read names for one specific returned photo's training
    day, from the AI Match Attendance review page. Unlike bulk_attended
    (a direct staff certification that marks every scheduled day at once),
    this only marks the one day the photo covers — so a multi-day class
    still needs every day confirmed (by AI or by hand) before a
    participant becomes certificate-eligible."""
    session_id = request.form.get("session_id", type=int)
    training_date = request.form.get("training_date")
    ids = request.form.getlist("participant_ids")
    if not (session_id and training_date and ids):
        flash("Select at least one participant first.", "danger")
        return redirect(url_for("t3.ai_match_review", session_id=session_id) if session_id
                         else url_for("sessions.index"))
    marked = 0
    for pid in ids:
        try:
            became_fully_attended = attendance_days.mark_day_attended(
                int(pid), session_id, training_date, source="manual")
            if became_fully_attended:
                _certificates.generate_and_store_certificate(int(pid))
            marked += 1
        except Exception:  # noqa: BLE001
            current_app.logger.exception(
                "Failed to confirm day attendance for participant %s", pid)
    flash(f"Marked {marked} participant(s) attended for that day.", "success")
    return redirect(url_for("t3.ai_match_review", session_id=session_id))


@bp.route("/sessions/<int:session_id>/ai-match")
@login_required
def ai_match_review(session_id):
    """Review screen for AI-assisted attendance matching: one section per
    returned photo, showing which training day it was resolved to (or why
    it was flagged as a mismatch — wrong class/date read off the sheet
    itself), and the names Claude read off it pre-matched (or not) to a
    participant on this class's T3 list, for staff to check and confirm.
    Confirming a suggestion posts to confirm_day_attendance, which marks
    just that one training day — a multi-day class still needs every day
    covered before a participant is certificate-eligible."""
    session_row = _session_or_none(session_id)
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))
    returns = db.query(
        "SELECT id, ai_analyzed_at FROM attendance_returns WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    )
    unanalyzed_count = sum(1 for r in returns if not r["ai_analyzed_at"])
    photos = ai_match.get_review_data(session_id) if returns else []
    return render_template(
        "t3/ai_match.html", s=session_row, returns=returns,
        unanalyzed_count=unanalyzed_count, photos=photos,
        multi_day=len(attendance_days.training_days_for_session(session_row)) > 1,
        ai_configured=ai_match.is_configured(),
    )


@bp.route("/sessions/<int:session_id>/ai-match/run", methods=("POST",))
@login_required
def ai_match_run(session_id):
    session_row = _session_or_none(session_id)
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))
    if not ai_match.is_configured():
        flash("AI attendance matching isn't set up yet — see README \"Setting up AI attendance "
              "matching\" to enable it.", "danger")
        return redirect(url_for("t3.ai_match_review", session_id=session_id))
    count = ai_match.analyze_unprocessed_returns(session_id)
    if count:
        summary = ai_match.auto_mark_attendance(session_id)
        msg = f"Read {count} photo(s), auto-marked {summary['marked']} attended."
        if summary["mismatches"]:
            msg += f" {len(summary['mismatches'])} photo(s) flagged below — check they're the right sheet."
        flash(msg, "success")
    else:
        flash("Nothing new to analyze — every submitted photo has already been read.", "info")
    return redirect(url_for("t3.ai_match_review", session_id=session_id))


@bp.route("/sessions/<int:session_id>/toggle-e-signature", methods=("POST",))
@login_required
def toggle_e_signature(session_id):
    """Turns e-Signature attendance on/off for one class. When on, the same
    public T3 Attendance Form link (t3_public.form) also lets participants
    sign their own row from their phone on a scheduled training day — see
    t3_public.sign for the identity-checked signing flow itself."""
    session_row = _session_or_none(session_id)
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))
    new_value = 0 if session_row["e_signature_enabled"] else 1
    db.execute("UPDATE course_sessions SET e_signature_enabled = ? WHERE id = ?", (new_value, session_id))
    flash("e-Signature attendance " + ("enabled" if new_value else "disabled") + " for this class.", "success")
    return redirect(url_for("t3.manage", session_id=session_id))


@bp.route("/<int:participant_id>/signature/<training_date>")
@login_required
def view_signature(participant_id, training_date):
    """Staff-only audit view of one participant's captured e-signature for
    one training day — linked from the small pen icons on the Attendance
    List (t3/manage.html)."""
    participant = db.query("SELECT session_id FROM t3_participants WHERE id = ?", (participant_id,), one=True)
    row = db.query(
        "SELECT signature_file FROM t3_day_attendance WHERE participant_id = ? AND training_date = ?",
        (participant_id, training_date), one=True,
    )
    if participant is None or row is None or not row["signature_file"]:
        flash("No signature on file for that day.", "danger")
        return redirect(url_for("sessions.index"))
    return send_from_directory(_t3_signature_dir(participant["session_id"]), row["signature_file"])
