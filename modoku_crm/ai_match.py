"""AI-assisted attendance matching.

When a trainer returns photo(s) of the signed T3 attendance sheet through
the public "Return Attendance Form" link (see attendance_return.py), this
module reads each photo with Claude's vision API — who signed, and what
course title/date is printed at the top of that sheet — then:

  1. Cross-checks the sheet's own title/date against the class it was
     actually submitted against. A confident mismatch (wrong class, or a
     date that isn't one of this class's scheduled training days) blocks
     auto-marking entirely for that photo and flags it for a human to
     look at — see resolve_return_date. A photo the AI simply couldn't
     read clearly (blurry, illegible date) is NOT treated as a mismatch
     by itself; only a positively wrong reading blocks it.
  2. For a photo that checks out, fuzzy-matches the signed names against
     the class's T3 participant list and marks each confident match
     attended for that specific training day (see attendance_days.py —
     for a multi-day class, every scheduled day has to be covered before
     a participant becomes certificate-eligible, not just one).

Entirely optional and additive — if ANTHROPIC_API_KEY isn't set (see
config.py / README "Setting up AI attendance matching"), is_configured()
returns False, the review page explains that plainly, and the existing
fully-manual workflow (open the photo, tick names by hand on t3.manage)
keeps working exactly as it always has.
"""
import base64
import difflib
import json
import mimetypes
import re

import requests
from flask import current_app

from . import attendance_days, db, fmtdate

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"

# Below this fuzzy-match score (0..1) a read name isn't confidently linked to
# any existing participant — shown as unmatched on the review page instead of
# risking a wrong tick.
MATCH_CONFIDENCE_THRESHOLD = 0.6

# Auto-marking attended from a returned photo (see auto_mark_attendance) is
# irreversible-by-default (no one reviews it first), so it uses a stricter
# bar than the manual review page — a name has to read unambiguously close
# to one participant before it's trusted with no human in the loop.
AUTO_ATTEND_CONFIDENCE_THRESHOLD = 0.75

# How closely the AI-read course title has to match this class's actual
# title before it's trusted. Looser than the name-match thresholds above —
# course titles are full sentences, so OCR/handwriting noise costs more
# characters — but still tight enough to catch "wrong class entirely".
TITLE_MATCH_THRESHOLD = 0.5

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

EXTRACTION_PROMPT = (
    "This is a photo of a printed or handwritten HRDCorp training attendance sign-in sheet "
    "(form PSMB/SBL-KHAS/T3/01). Read three things: (1) the course title written next to "
    "\"Course Title\", (2) the date written next to \"Dates of Training\", normalized to "
    "YYYY-MM-DD if you can confidently determine it (use null if it's illegible, ambiguous, or "
    "not visible), and (3) the full name of every participant who has actually signed or "
    "initialed their row — skip blank rows, headers, and the trainer's own name if it's printed "
    "at the top. Reply with ONLY a JSON object, nothing else — no markdown, no explanation. "
    "Example: {\"course_title\": \"Effective Leadership for New Managers\", "
    "\"training_date\": \"2026-09-10\", \"names\": [\"Ali bin Ahmad\", \"Siti Aminah\"]}"
)


def is_configured():
    return bool(current_app.config.get("ANTHROPIC_API_KEY"))


def _encode_image(path):
    mime, _ = mimetypes.guess_type(path)
    if not mime or not mime.startswith("image/"):
        mime = "image/jpeg"
    with open(path, "rb") as fh:
        data = base64.b64encode(fh.read()).decode("ascii")
    return mime, data


def analyze_attendance_photo(image_path):
    """Calls Claude's vision API on one attendance-form photo and returns
    {"course_title": str|None, "training_date": "YYYY-MM-DD"|None,
    "names": [str, ...]}. Best-effort: returns all-empty/None if the
    feature isn't configured, the request fails, or the response isn't
    parseable — callers should treat that as "nothing to suggest", never
    as "no one attended" or "this is the wrong class". Never raises."""
    empty = {"course_title": None, "training_date": None, "names": []}
    api_key = current_app.config.get("ANTHROPIC_API_KEY")
    if not api_key:
        return empty
    try:
        mime, b64_data = _encode_image(image_path)
        response = requests.post(
            ANTHROPIC_API_URL,
            timeout=45,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_API_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": current_app.config.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
                "max_tokens": 1024,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64_data}},
                        {"type": "text", "text": EXTRACTION_PROMPT},
                    ],
                }],
            },
        )
        response.raise_for_status()
        text = response.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.strip("`")
            if "\n" in text:
                text = text.split("\n", 1)[1]
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            return empty
        names = parsed.get("names") or []
        names = [n.strip() for n in names if isinstance(n, str) and n.strip()]
        course_title = parsed.get("course_title")
        course_title = course_title.strip() if isinstance(course_title, str) and course_title.strip() else None
        training_date = parsed.get("training_date")
        training_date = training_date.strip() if isinstance(training_date, str) else None
        if not training_date or not _ISO_DATE_RE.match(training_date):
            training_date = None
        return {"course_title": course_title, "training_date": training_date, "names": names}
    except Exception:  # noqa: BLE001 - a bad photo/response must never break the review page
        current_app.logger.exception("AI attendance-sheet analysis failed for %s", image_path)
        return empty


def resolve_return_date(session_row, detected_title, detected_date):
    """Cross-checks what the AI read off a returned photo's header against
    the class it was actually submitted against. Returns (resolved_date,
    mismatch_reason) — exactly one of the two is set. A non-None reason
    means auto-marking should be blocked entirely for this photo and a
    human should look at it before anything is marked from it.

    Deliberately only blocks on a *confident* mismatch: a blurry photo
    that yields no readable title/date isn't treated as suspicious by
    itself (most bad-lighting phone photos would otherwise get needlessly
    blocked, which would make the feature worse than not having it). It
    only blocks when the AI *did* read something and that something
    doesn't check out — or, for a multi-day class, when it couldn't tell
    which of the several valid days the sheet is for."""
    valid_days = attendance_days.training_days_iso_for_session(session_row)

    if detected_title:
        score = difflib.SequenceMatcher(
            None, detected_title.lower(), (session_row["course_title"] or "").lower()
        ).ratio()
        if score < TITLE_MATCH_THRESHOLD:
            return None, (
                f"The photo looks like it's for “{detected_title}”, but this class is "
                f"“{session_row['course_title']}” — check it's the right sheet before it's counted."
            )

    if detected_date:
        if detected_date in valid_days:
            return detected_date, None
        pretty_days = ", ".join(fmtdate(d) for d in valid_days)
        return None, (
            f"The photo is dated {fmtdate(detected_date)}, which isn't one of this class's training "
            f"dates ({pretty_days}) — check it's the right day's sheet before it's counted."
        )

    # No date could be confidently read off the sheet.
    if len(valid_days) == 1:
        return valid_days[0], None  # single-day class — nothing to disambiguate
    return None, (
        "Couldn't read which day this sheet is for, and this class runs more than one day — "
        "check it against the Attendance List manually."
    )


def match_names_to_participants(names, session_id, threshold=MATCH_CONFIDENCE_THRESHOLD, training_date=None):
    """Fuzzy-matches each extracted name against this class's T3 participant
    list. Returns one dict per input name: {"extracted", "participant_id",
    "participant_name", "confidence", "already_attended"} — participant_id
    is None when nothing scored above `threshold`, leaving that one
    unmatched rather than risking a wrong tick. Each participant is matched
    to at most one name.

    already_attended reflects whether the matched participant is already
    marked attended for `training_date` specifically (per-day, via
    t3_day_attendance) when a training_date is given — e.g. the same photo
    re-analyzed, or two photos of the same day. Without a training_date
    (used only by the review page's own display logic) it falls back to
    the participant's overall attended flag."""
    participants = db.query(
        "SELECT id, name, attended FROM t3_participants WHERE session_id = ? ORDER BY id", (session_id,)
    )
    already_for_day = set()
    if training_date:
        rows = db.query(
            """SELECT participant_id FROM t3_day_attendance
               WHERE training_date = ? AND participant_id IN (
                   SELECT id FROM t3_participants WHERE session_id = ?)""",
            (training_date, session_id),
        )
        already_for_day = {r["participant_id"] for r in rows}
    used_ids = set()
    results = []
    for name in names:
        best, best_score = None, 0.0
        for p in participants:
            if p["id"] in used_ids:
                continue
            score = difflib.SequenceMatcher(None, name.lower(), p["name"].lower()).ratio()
            if score > best_score:
                best, best_score = p, score
        if best is not None and best_score >= threshold:
            used_ids.add(best["id"])
            already = (best["id"] in already_for_day) if training_date else bool(best["attended"])
            results.append({"extracted": name, "participant_id": best["id"],
                             "participant_name": best["name"], "confidence": round(best_score, 2),
                             "already_attended": already})
        else:
            results.append({"extracted": name, "participant_id": None, "participant_name": None,
                             "confidence": round(best_score, 2), "already_attended": False})
    return results


def analyze_unprocessed_returns(session_id):
    """Runs analyze_attendance_photo + persists the result for every
    attendance_returns photo of this session that hasn't been analyzed yet
    (ai_analyzed_at IS NULL) — cached so re-opening the review page doesn't
    re-call the API for photos already read. Returns the number analyzed."""
    from flask import current_app as app
    import os
    upload_folder = app.config["UPLOAD_FOLDER"]
    rows = db.query(
        "SELECT id, session_id, filename FROM attendance_returns WHERE session_id = ? AND ai_analyzed_at IS NULL",
        (session_id,),
    )
    analyzed = 0
    for row in rows:
        path = os.path.join(upload_folder, "sessions", str(row["session_id"]), row["filename"])
        result = analyze_attendance_photo(path)
        db.execute(
            """UPDATE attendance_returns
               SET ai_names_json = ?, ai_detected_title = ?, ai_detected_date = ?, ai_analyzed_at = datetime('now')
               WHERE id = ?""",
            (json.dumps(result["names"]), result["course_title"], result["training_date"], row["id"]),
        )
        analyzed += 1
    return analyzed


def get_review_data(session_id, threshold=MATCH_CONFIDENCE_THRESHOLD):
    """Per-photo breakdown for the AI Match Attendance review page: for
    each returned photo that's been read, the training day it was
    resolved to (or its mismatch reason, if it didn't check out), and
    name-matching suggestions for that specific day. A photo still
    awaiting its first analysis isn't included — the page shows a
    separate "N not read yet" count for those."""
    session_row = db.query(
        "SELECT cs.*, c.title AS course_title FROM course_sessions cs "
        "JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?",
        (session_id,), one=True,
    )
    rows = db.query(
        "SELECT * FROM attendance_returns WHERE session_id = ? AND ai_analyzed_at IS NOT NULL ORDER BY created_at",
        (session_id,),
    )
    photos = []
    for row in rows:
        try:
            names = json.loads(row["ai_names_json"]) if row["ai_names_json"] else []
        except (TypeError, ValueError):
            names = []
        entry = {
            "return_id": row["id"], "original_name": row["original_name"],
            "detected_title": row["ai_detected_title"], "detected_date": row["ai_detected_date"],
            "training_date": row["training_date"], "mismatch": bool(row["ai_mismatch"]),
            "mismatch_reason": row["ai_mismatch_reason"], "names_read": len(names),
            "suggestions": [],
        }
        if not row["ai_mismatch"] and names and session_row is not None:
            entry["suggestions"] = match_names_to_participants(
                names, session_id, threshold=threshold, training_date=row["training_date"])
        photos.append(entry)
    return photos


def auto_mark_attendance(session_id):
    """Fully-automated counterpart to the manual review page: as soon as a
    trainer's returned photo has been read, this cross-checks it against
    the class (see resolve_return_date) and — if it checks out — marks
    every confidently matched participant attended for that specific
    training day, generating a certificate the moment a participant
    becomes fully attended across every scheduled day. Only processes
    photos not yet acted on (ai_action IS NULL), so re-running this for a
    session already handled is a no-op rather than re-marking or
    re-notifying about the same photo twice.

    Uses AUTO_ATTEND_CONFIDENCE_THRESHOLD (stricter than the review page's
    own threshold) precisely because no one is reviewing these before
    they take effect. A name that doesn't clear that bar, or a photo that
    fails its title/date cross-check, is left for a quick manual look on
    the AI Match Attendance page rather than guessed — the two safety
    nets kept in an otherwise hands-off flow. Never raises — a failure
    here must never break the trainer's photo upload; returns a summary
    dict either way: {"total_read", "marked", "unmatched", "mismatches"}."""
    from . import certificates as _certificates
    session_row = db.query(
        "SELECT cs.*, c.title AS course_title FROM course_sessions cs "
        "JOIN courses c ON c.id = cs.course_id WHERE cs.id = ?",
        (session_id,), one=True,
    )
    if session_row is None:
        return {"total_read": 0, "marked": 0, "unmatched": [], "mismatches": []}

    rows = db.query(
        """SELECT * FROM attendance_returns
           WHERE session_id = ? AND ai_analyzed_at IS NOT NULL AND ai_action IS NULL""",
        (session_id,),
    )
    total_read = 0
    marked = 0
    unmatched_names = []
    mismatches = []
    for row in rows:
        try:
            names = json.loads(row["ai_names_json"]) if row["ai_names_json"] else []
        except (TypeError, ValueError):
            names = []
        total_read += len(names)

        resolved_date, reason = resolve_return_date(session_row, row["ai_detected_title"], row["ai_detected_date"])
        if reason:
            db.execute(
                "UPDATE attendance_returns SET ai_mismatch = 1, ai_mismatch_reason = ?, ai_action = 'mismatch' WHERE id = ?",
                (reason, row["id"]),
            )
            mismatches.append({
                "return_id": row["id"], "reason": reason,
                "detected_title": row["ai_detected_title"], "detected_date": row["ai_detected_date"],
            })
            continue

        db.execute(
            "UPDATE attendance_returns SET training_date = ?, ai_action = 'auto_marked' WHERE id = ?",
            (resolved_date, row["id"]),
        )
        if not names:
            continue
        matches = match_names_to_participants(
            names, session_id, threshold=AUTO_ATTEND_CONFIDENCE_THRESHOLD, training_date=resolved_date)
        for m in matches:
            if not m["participant_id"]:
                unmatched_names.append(m["extracted"])
                continue
            if m["already_attended"]:
                continue  # already marked for this specific day — nothing new to do
            try:
                became_fully_attended = attendance_days.mark_day_attended(
                    m["participant_id"], session_id, resolved_date, source="ai")
                if became_fully_attended:
                    _certificates.generate_and_store_certificate(m["participant_id"])
                marked += 1
            except Exception:  # noqa: BLE001 - one bad row must never stop the rest of the batch
                current_app.logger.exception(
                    "Failed to auto-mark T3 participant %s attended for session %s",
                    m["participant_id"], session_id,
                )
                unmatched_names.append(m["extracted"])
    return {"total_read": total_read, "marked": marked, "unmatched": unmatched_names, "mismatches": mismatches}
