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
from datetime import datetime, timedelta

from . import db


def training_days_for_session(session_row):
    """List of date objects, one per calendar day of the training,
    start_date..end_date inclusive. A training with no end_date (or an
    end_date before start_date, which shouldn't happen but is handled
    defensively) is treated as one day long."""
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


def mark_day_attended(participant_id, session_id, training_date_iso, source="manual"):
    """Marks one participant attended for one specific training day.
    Idempotent — safe to call repeatedly (e.g. the same photo re-analyzed,
    or a name appearing on more than one day's photo). Returns True if
    this call is what made the participant newly *fully* attended (every
    scheduled day now covered) — the caller uses that to know a
    certificate should be generated now, not before."""
    db.execute(
        """INSERT INTO t3_day_attendance (participant_id, training_date, marked_at, source)
           VALUES (?, ?, datetime('now'), ?)
           ON CONFLICT(participant_id, training_date) DO UPDATE SET source = excluded.source""",
        (participant_id, training_date_iso, source),
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
