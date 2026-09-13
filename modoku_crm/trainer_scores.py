"""Trainer quality scorecard — what the evaluation forms say about each
trainer, rolled up across every class they've taught.

training_reports.py already computes each class's evaluation ratings
exactly (plain arithmetic, no AI) and caches them per class. Nothing ever
read those numbers back *by trainer*, though: a trainer's own page showed
their rate history, utilization and qualifications, but no quality signal
at all, and the person picking who teaches the next class had nothing to
go on. This module is only that rollup — it stores nothing of its own and
computes nothing new, it re-reads the cached per-class reports and averages
them per trainer.

Scores are normalized to a percentage before being combined, because two
classes can use forms with different scales (a 1-5 linear scale and a
Poor..Excellent five-point grid, say) and averaging 4.2 with 4.6 would be
meaningless if one of them were out of 10. A class only contributes when
its own report declares the scale its ratings sit on:

  * a combined worded-scale rating (the classic evaluation grid, pooled
    across every criterion sharing one scale) — preferred, it's the
    broadest measure of the class;
  * failing that, the individual rating questions that carry a declared
    scale_max, averaged together.

A class whose report has no declared-scale ratings is skipped rather than
guessed at, and says so on the trainer's page. Older cached reports
predate scale_max being stored for linear-scale questions, so they may
show as unscored until "Refresh Report" is clicked on that class — that's
deliberate: a silently assumed ceiling would put a wrong number on a
person's record.

Attribution: a class counts for its primary trainer (course_sessions.
trainer_id) AND everyone on its session_trainers roster, since a co-taught
class was delivered by all of them and the form rates the delivery.
"""
import json

from . import db

# Weighted so a class with 30 responses counts for more than one with 2,
# rather than every class counting the same regardless of how many people
# actually filled the form in.
WEIGHT_BY_RESPONSES = True

# Below this many rated classes, an average is shown but flagged as thin —
# one enthusiastic cohort shouldn't read as a settled track record.
THIN_EVIDENCE_BELOW = 3


def _percentage(average, scale_max):
    """A rating as a percentage of its own scale's top mark, so ratings
    from differently-scaled forms can be averaged together. A 1..N scale's
    floor is 1, not 0 (you cannot score below the lowest label), so the
    bottom of the scale is 0%, not 1/N%."""
    try:
        average = float(average)
        scale_max = float(scale_max)
    except (TypeError, ValueError):
        return None
    if scale_max <= 1 or average <= 0:
        return None
    pct = (average - 1) / (scale_max - 1) * 100
    return max(0.0, min(100.0, round(pct, 1)))


def class_score(numeric_summary_json):
    """This class's overall evaluation score from its cached report, as
    {"pct", "average", "scale_max", "criteria"} — or None when the report
    has no ratings on a declared scale. `criteria` is how many rating
    questions went into it, for showing what the number is based on."""
    if not numeric_summary_json:
        return None
    try:
        payload = json.loads(numeric_summary_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    # Preferred: the pooled worded-scale rating(s) — the grid of criteria
    # every evaluation form has, already combined by training_reports.
    combined = [c for c in (payload.get("combined") or [])
                if isinstance(c, dict) and c.get("average") and c.get("scale_max")]
    if combined:
        best = max(combined, key=lambda c: c.get("count") or 0)
        pct = _percentage(best["average"], best["scale_max"])
        if pct is not None:
            return {"pct": pct, "average": float(best["average"]),
                    "scale_max": float(best["scale_max"]),
                    "criteria": len(best.get("criteria") or [])}

    # Otherwise: every individual rating question that declares its scale.
    percentages = []
    averages = []
    scale_maxes = []
    for question in (payload.get("questions") or []):
        if not isinstance(question, dict):
            continue
        if question.get("kind") not in ("scale", "choice_numeric", "choice_ordinal"):
            continue
        pct = _percentage(question.get("average"), question.get("scale_max"))
        if pct is None:
            continue
        percentages.append(pct)
        averages.append(float(question["average"]))
        scale_maxes.append(float(question["scale_max"]))
    if not percentages:
        return None
    return {
        "pct": round(sum(percentages) / len(percentages), 1),
        "average": round(sum(averages) / len(averages), 2),
        # Only meaningful as a x/y reading when every question shared one
        # ceiling; mixed scales fall back to the percentage alone.
        "scale_max": scale_maxes[0] if len(set(scale_maxes)) == 1 else None,
        "criteria": len(percentages),
    }


def _rated_classes(trainer_id=None):
    """Every class that has a cached training report, joined to the
    trainer(s) it counts for. One row per (class, trainer) pair, so a
    co-taught class appears once for each trainer on it."""
    sql = """
        SELECT tr.session_id, tr.response_count, tr.numeric_summary_json, tr.generated_at,
               cs.start_date, c.title AS course_title, x.trainer_id
        FROM training_reports tr
        JOIN course_sessions cs ON cs.id = tr.session_id
        JOIN courses c ON c.id = cs.course_id
        JOIN (
            SELECT id AS session_id, trainer_id FROM course_sessions WHERE trainer_id IS NOT NULL
            UNION
            SELECT session_id, trainer_id FROM session_trainers
        ) x ON x.session_id = tr.session_id
        WHERE tr.response_count > 0
    """
    args = []
    if trainer_id is not None:
        sql += " AND x.trainer_id = ?"
        args.append(trainer_id)
    sql += " ORDER BY cs.start_date DESC"
    return db.query(sql, args)


def _summarize(rows):
    """Turns (class, trainer) rows into per-class entries plus the overall
    weighted average. Classes whose report has no usable rating are kept
    in the list (marked unscored) so the trainer's page can say why a
    class isn't counted, rather than silently dropping it."""
    classes = []
    weighted_total = 0.0
    weight_total = 0.0
    responses_total = 0
    scored = 0
    for row in rows:
        score = class_score(row["numeric_summary_json"])
        entry = {
            "session_id": row["session_id"],
            "course_title": row["course_title"],
            "start_date": row["start_date"],
            "responses": row["response_count"],
            "score": score,
        }
        classes.append(entry)
        if not score:
            continue
        scored += 1
        responses_total += row["response_count"] or 0
        weight = (row["response_count"] or 1) if WEIGHT_BY_RESPONSES else 1
        weighted_total += score["pct"] * weight
        weight_total += weight
    overall = round(weighted_total / weight_total, 1) if weight_total else None
    return {
        "classes": classes,
        "overall_pct": overall,
        "rated_classes": scored,
        "unscored_classes": len(classes) - scored,
        "responses": responses_total,
        "thin": bool(scored and scored < THIN_EVIDENCE_BELOW),
    }


def for_trainer(trainer_id):
    """One trainer's scorecard: every class of theirs that has a report,
    most recent first, plus the response-weighted overall. Returns the
    same shape with empty/None values when they have none yet."""
    return _summarize(_rated_classes(trainer_id))


def overall_by_trainer():
    """{trainer_id: {"overall_pct", "rated_classes", "responses", "thin"}}
    for every trainer with at least one scored class — for the trainer
    list and the pickers where someone chooses who teaches a class.
    Trainers with nothing rated are simply absent from the mapping."""
    by_trainer = {}
    rows = _rated_classes()
    grouped = {}
    for row in rows:
        grouped.setdefault(row["trainer_id"], []).append(row)
    for trainer_id, trainer_rows in grouped.items():
        summary = _summarize(trainer_rows)
        if summary["overall_pct"] is None:
            continue
        by_trainer[trainer_id] = {
            "overall_pct": summary["overall_pct"],
            "rated_classes": summary["rated_classes"],
            "responses": summary["responses"],
            "thin": summary["thin"],
        }
    return by_trainer


def badge_class(pct):
    """Bootstrap classes for a score chip — the same green/amber/red
    reading used elsewhere in the app for status badges. Deliberately
    generous bands: an evaluation average is a soft signal, and anything
    above 80% of scale is a well-received class, not a merely adequate
    one."""
    if pct is None:
        return "bg-light text-muted"
    if pct >= 80:
        return "bg-success-subtle text-success-emphasis"
    if pct >= 60:
        return "bg-warning-subtle text-warning-emphasis"
    return "bg-danger-subtle text-danger-emphasis"
