"""Per-day T3 attendance tracking, for trainings that run more than one
calendar day.

t3_participants.attended remains exactly what it always was: the single
flag every other part of the app checks for certificate eligibility
(cert_admin.py, certificates.py, the public claim page, the Certificates
tab). What changes is *how* it gets set. Previously it was written
directly, treating "signed the sheet once" as "attended". For a
multi-day programme that's wrong — HRDCorp's own rule is that a trainee
who misses a day of a multi-day training hasn't fully attended, so
shouldn't be certificate-eligible yet.

This module is now the only writer of t3_participants.attended. The
actual per-day marks live in the new t3_day_attendance table; attended
only flips to 1 once a participant has a day-attendance row for *every*
one of the session's scheduled training days. For an ordinary single-day
class this is invisible — there's only ever one day to satisfy, so the
very first mark (AI or manual) still flips attended to 1 immediately,
exactly as before. Only real multi-day classes see the new behaviour.

Deliberately promotion-only: once a participant becomes fully attended,
nothing here demotes them back to 0 (there's no UI path today that
un-marks a single day of a multi-day class, and auto-revoking an
already-issued certificate is a bigger, separate decision than this
module should make on its own).
"""
import glob
import os
from datetime import date, datetime, timedelta

from . import APP_TZ, db


def today_kl_iso():
    """Fix248: today's date in Malaysia - what signing and the class code go
    by. The server's own clock may be UTC, which is still "yesterday" before
    8 AM in Malaysia (a day-2 signature landed on day 1)."""
    return datetime.now(APP_TZ).date().isoformat()


def training_days_for_session(session_row):
    """List of date objects, one per calendar day of the training,
    start_date..end_date inclusive. A training with no end_date (or an
    end_date before start_date, which shouldn't happen but is handled
    defensively) is treated as one day long. Fix248: a class with its own
    list of days (Fix204 day_times, e.g. 10 & 17 Nov) uses exactly those."""
    from . import schedule
    try:
        own_days = schedule.parse_day_times(session_row["day_times"])
    except (KeyError, IndexError):
        own_days = []
    if own_days:
        return [date.fromisoformat(d["date"]) for d in own_days]
    start = datetime.strptime(session_row["start_date"], "%Y-%m-%d").date()
    end = start
    if session_row["end_date"]:
        try:
            end = datetime.strptime(session_row["end_date"], "%Y-%m-%d").date()
        except ValueError:
            end = start
    if end < start:
        end = start
    num_days = (end - start).days + 1
    return [start + timedelta(days=i) for i in range(num_days)]


def training_days_iso_for_session(session_row):
    """Same as training_days_for_session, as a list of 'YYYY-MM-DD' strings
    — the shape most callers here actually want, since t3_day_attendance
    stores dates as plain ISO text."""
    return [d.isoformat() for d in training_days_for_session(session_row)]


def days_attended(participant_id):
    """Set of 'YYYY-MM-DD' strings this participant has a day-attendance
    row for."""
    rows = db.query(
        "SELECT training_date FROM t3_day_attendance WHERE participant_id = ?",
        (participant_id,),
    )
    return {r["training_date"] for r in rows}


def attendance_status(participant_id, session_row):
    """Returns (days_attended_count, total_days) for one participant
    against one session — what the Attendance List uses to show "2/3
    Days" for multi-day classes. For a single-day class total_days is
    always 1, so this collapses to the familiar attended/not-attended."""
    total = len(training_days_for_session(session_row))
    attended = len(days_attended(participant_id) & set(training_days_iso_for_session(session_row)))
    return attended, total


def mark_day_attended(participant_id, session_id, training_date_iso, source="manual",
                       signature_file=None, signed_ip=None):
    """Marks one participant attended for one specific training day.
    Idempotent — safe to call repeatedly (e.g. the same photo re-analyzed,
    or a name appearing on more than one day's photo). Returns True if
    this call is what made the participant newly *fully* attended (every
    scheduled day now covered) — the caller uses that to know a
    certificate should be generated now, not before.

    signature_file/signed_ip are set only by the e-signature flow
    (t3_public.sign) to keep an auditable record of who signed and from
    where; every other caller (manual, AI-matched) leaves them None, and a
    re-mark never erases an existing signature — COALESCE keeps whichever
    value (new or existing) is non-null."""
    db.execute(
        """INSERT INTO t3_day_attendance (participant_id, training_date, marked_at, source, signature_file, signed_ip)
           VALUES (?, ?, datetime('now'), ?, ?, ?)
           ON CONFLICT(participant_id, training_date) DO UPDATE SET
             source = excluded.source,
             signature_file = COALESCE(excluded.signature_file, t3_day_attendance.signature_file),
             signed_ip = COALESCE(excluded.signed_ip, t3_day_attendance.signed_ip)""",
        (participant_id, training_date_iso, source, signature_file, signed_ip),
    )
    return _sync_attended_rollup(participant_id, session_id)


def mark_all_days_attended(participant_id, session_id, session_row, source="manual"):
    """Marks every one of the session's scheduled training days attended
    for this participant in one go — what a staff member ticking
    "Attended" by hand on the Attendance List means: a direct human
    certification that this person fully attended, not a day-by-day
    build-up. Returns True if this newly made the participant fully
    attended (it always does, since every day is marked at once, unless
    they already were)."""
    became_fully_attended = False
    for day_iso in training_days_iso_for_session(session_row):
        if mark_day_attended(participant_id, session_id, day_iso, source=source):
            became_fully_attended = True
    return became_fully_attended


def clear_all_days(participant_id):
    """Removes every day-attendance row for this participant and resets
    the attended rollup to 0 — the "Unmark" counterpart to
    mark_all_days_attended, for staff correcting a mistaken tick.
    Deliberately doesn't touch any certificate already generated; the
    caller (t3.bulk_attended) removes that separately."""
    db.execute("DELETE FROM t3_day_attendance WHERE participant_id = ?", (participant_id,))
    db.execute("UPDATE t3_participants SET attended = 0 WHERE id = ?", (participant_id,))


def signatures_by_participant(session_id):
    """participant_id -> {training_date_iso: signature_file} map of every
    captured e-signature on file for this session. Used by the staff
    Manage Participants page (a small pen icon per scheduled day) and the
    printable/emailed T3 Attendance Form (embedding the actual signature
    image in the Signature* column for any day a participant e-signed,
    instead of leaving that cell blank for a pen-and-paper signature —
    since HRDCorp's own T3/01 form just needs *a* signature, and one
    captured electronically is one the trainee already provided)."""
    rows = db.query(
        """SELECT tda.participant_id, tda.training_date, tda.signature_file
           FROM t3_day_attendance tda
           JOIN t3_participants p ON p.id = tda.participant_id
           WHERE p.session_id = ?""",
        (session_id,),
    )
    result = {}
    for row in rows:
        result.setdefault(row["participant_id"], {})[row["training_date"]] = row["signature_file"]
    return result


def signed_times(session_id):
    """Fix249: participant_id -> {training_date_iso: '8:42 AM'} - when each
    e-signature was signed, in Malaysia time (marked_at is stored as UTC)."""
    from datetime import timezone
    rows = db.query(
        """SELECT tda.participant_id, tda.training_date, tda.marked_at FROM t3_day_attendance tda
           JOIN t3_participants p ON p.id = tda.participant_id
           WHERE p.session_id = ? AND tda.signature_file IS NOT NULL""",
        (session_id,),
    )
    result = {}
    for row in rows:
        try:
            utc = datetime.fromisoformat(str(row["marked_at"]).replace(" ", "T")).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        kl = utc.astimezone(APP_TZ)
        result.setdefault(row["participant_id"], {})[row["training_date"]] = kl.strftime("%-I:%M %p")
    return result


def _sync_attended_rollup(participant_id, session_id):
    session_row = db.query("SELECT * FROM course_sessions WHERE id = ?", (session_id,), one=True)
    if session_row is None:
        return False
    valid_days = set(training_days_iso_for_session(session_row))
    attended = days_attended(participant_id)
    fully_attended = bool(valid_days) and valid_days.issubset(attended)
    participant = db.query("SELECT attended FROM t3_participants WHERE id = ?", (participant_id,), one=True)
    already_attended = bool(participant["attended"]) if participant else False
    if fully_attended and not already_attended:
        db.execute("UPDATE t3_participants SET attended = 1 WHERE id = ?", (participant_id,))
        return True
    return False


def repair_esign_dates(upload_folder, recent_days=30):
    """Fix248 (one-time): e-signatures saved under the wrong day because the
    server's date was UTC - e.g. a day-2 signature before 8 AM Malaysia time
    landed on day 1, or replaced a day-1 signature already there.

    Each signature file's own save time (Malaysia date) says which day it was
    really signed. For each participant of a recent class (training days in
    the last `recent_days`), every e-signed day is rebuilt from those files:
    a day with a file but no row gets a row, a row showing another day's
    signature gets that day's own file back, and an e-signature row whose
    day has no signature file of its own (it was the misdated one) is
    removed. Manual / AI-matched marks are never touched. Returns the number
    of participants changed."""
    cutoff = (datetime.now(APP_TZ).date() - timedelta(days=recent_days)).isoformat()
    sessions = db.query("SELECT * FROM course_sessions WHERE e_signature_enabled = 1 OR id IN "
                        "(SELECT p.session_id FROM t3_day_attendance t JOIN t3_participants p ON p.id = t.participant_id "
                        "WHERE t.source = 'e-signature')")
    changed = 0
    for s in sessions:
        days = training_days_iso_for_session(s)
        if not days or days[-1] < cutoff:
            continue
        sig_dir = os.path.join(upload_folder, "sessions", str(s["id"]), "signatures")
        if not os.path.isdir(sig_dir):
            continue
        for p in db.query("SELECT id FROM t3_participants WHERE session_id = ?", (s["id"],)):
            pid = p["id"]
            by_date, file_date = {}, {}
            for path in glob.glob(os.path.join(sig_dir, f"{pid}-*.png")):
                mtime = os.path.getmtime(path)
                kl_day = datetime.fromtimestamp(mtime, APP_TZ).date().isoformat()
                name = os.path.basename(path)
                file_date[name] = kl_day
                if kl_day in days and (kl_day not in by_date or mtime > by_date[kl_day][1]):
                    by_date[kl_day] = (name, mtime)
            if not by_date:
                continue
            rows = {r["training_date"]: r for r in db.query(
                "SELECT * FROM t3_day_attendance WHERE participant_id = ?", (pid,))}
            touched = False
            for day, (name, mtime) in by_date.items():
                row = rows.get(day)
                if row is None:
                    marked = datetime.utcfromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
                    db.execute("INSERT INTO t3_day_attendance (participant_id, training_date, marked_at, source, "
                               "signature_file) VALUES (?, ?, ?, 'e-signature', ?)", (pid, day, marked, name))
                    touched = True
                elif row["source"] == "e-signature" and row["signature_file"] != name \
                        and file_date.get(row["signature_file"]) != day:
                    db.execute("UPDATE t3_day_attendance SET signature_file = ? WHERE id = ?", (name, row["id"]))
                    touched = True
            for day, row in rows.items():
                if row["source"] == "e-signature" and day not in by_date \
                        and file_date.get(row["signature_file"]) not in (None, day):
                    db.execute("DELETE FROM t3_day_attendance WHERE id = ?", (row["id"],))
                    touched = True
            if touched:
                _sync_attended_rollup(pid, s["id"])
                changed += 1
    return changed
