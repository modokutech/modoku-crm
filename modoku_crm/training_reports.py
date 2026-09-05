"""Training Report — the evaluation-feedback rollup (idea #3's second half;
see evaluation_forms.py for the creation half).

Once a class's Google Form has collected some responses, this module:

  1. Reads every response back via the Forms API (evaluation_forms.
     list_form_responses), matched against the Form's own question
     structure (evaluation_forms.get_form_structure) so each question is
     classified as a rating (scale, or a multiple-choice question whose
     options are themselves numbers), a categorical choice (e.g.
     Excellent/Good/Fair/Poor), or open text.
  2. Aggregates the rating/choice questions with plain arithmetic — no AI
     involved, so those numbers are exact.
  3. Summarizes the open-text questions with Claude — grouping recurring
     themes, praise and criticism, per question — since there's no
     reliable non-AI way to roll up free text. This part is best-effort:
     if ANTHROPIC_API_KEY isn't set, or the call/parse fails, the report
     still saves the numeric half and shows the raw open-text answers
     instead of a summary, rather than failing outright.

Deliberately a cache, not something rebuilt on every page view — building
it re-reads every response from Google and re-runs the AI summary, which
isn't cheap. A class page's report shows whatever was last generated,
with a "Refresh Report" button to regenerate on demand.

Only available for classes with an auto-generated Form
(course_sessions.evaluation_form_id) — that's the only case where Modoku
Hub controls a Google Form ID it can call the Forms API against. A class
using a hand-pasted Evaluation Form link (from before this automation
existed, or where Google Forms automation was never connected) has no
Training Report option; the manual evaluation_form_link on that class is
just a URL, not something Modoku Hub can read responses back from.
"""
import json
from collections import Counter

import requests
from flask import Blueprint, flash, g, redirect, render_template, url_for
from flask import current_app

from . import activity, db, evaluation_forms
from .ai_match import ANTHROPIC_API_URL, ANTHROPIC_API_VERSION
from .auth import login_required

bp = Blueprint("training_reports", __name__, url_prefix="/training-report")

# Bounds on what gets sent to Claude for the open-text summary — a class
# with an unusually large number of responses still produces one bounded
# request rather than an unbounded one.
MAX_ANSWERS_PER_QUESTION = 300
MAX_CHARS_PER_ANSWER = 500


class TrainingReportError(Exception):
    """Raised for any failure building a report that isn't already an
    EvaluationFormError (e.g. no Form generated for this class at all).
    Always written to be directly flashable."""


def _collect_answer_values(response, question_id):
    """The Forms API returns every answer type — short text, paragraph,
    choice, and scale alike — as a list of plain string values under
    textAnswers, so this one path reads all of them."""
    answers = response.get("answers") or {}
    ans = answers.get(question_id)
    if not ans:
        return []
    text_answers = (ans.get("textAnswers") or {}).get("answers", [])
    return [a.get("value", "").strip() for a in text_answers if a.get("value", "").strip()]


def _aggregate_numeric(values):
    nums = []
    for v in values:
        try:
            nums.append(float(v))
        except ValueError:
            continue
    if not nums:
        return None
    return {
        "count": len(nums),
        "average": round(sum(nums) / len(nums), 2),
        "min": min(nums),
        "max": max(nums),
    }


def _aggregate_categorical(values):
    counts = Counter(values)
    return {
        "count": sum(counts.values()),
        "distribution": [{"option": option, "count": n} for option, n in counts.most_common()],
    }


def is_ai_configured():
    return bool(current_app.config.get("ANTHROPIC_API_KEY"))


def _summarize_open_text(session_row, text_summary):
    """Best-effort AI qualitative summary of the open-text evaluation
    answers, grouped by question. Never raises — returns None if the
    feature isn't configured or the call/parse fails, so build_report()
    still saves the numeric half and the raw open-text answers (shown as
    a fallback in the UI) either way."""
    api_key = current_app.config.get("ANTHROPIC_API_KEY")
    if not api_key or not text_summary:
        return None
    try:
        lines = [f"Class: {session_row['course_title']} (Trainer: {session_row['trainer_name'] or 'TBC'})", ""]
        for q in text_summary:
            lines.append(f"Question: {q['question']}")
            for i, ans in enumerate(q["answers"][:MAX_ANSWERS_PER_QUESTION], 1):
                lines.append(f"  {i}. {ans[:MAX_CHARS_PER_ANSWER]}")
            lines.append("")
        transcript = "\n".join(lines)
        prompt = (
            "Below are open-ended post-training evaluation answers from participants of a corporate "
            "training class, grouped by question. Summarize them honestly and specifically — group "
            "recurring themes, note both praise and criticism, and don't invent anything not actually "
            "said. If a question has few or contradictory answers, say so plainly rather than "
            "overstating a pattern. Reply with ONLY a JSON object, nothing else — no markdown, no "
            "explanation. Shape: {\"overall\": \"2-4 sentence overall takeaway across all questions\", "
            "\"by_question\": [{\"question\": \"<question text>\", \"summary\": \"2-4 sentence summary "
            "of themes for this question specifically\"}, ...]}\n\n" + transcript
        )
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
                "max_tokens": 1500,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        response.raise_for_status()
        text = response.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.strip("`")
            if "\n" in text:
                text = text.split("\n", 1)[1]
        parsed = json.loads(text)
        if not isinstance(parsed, dict) or "overall" not in parsed:
            return None
        return parsed
    except Exception:  # noqa: BLE001 - a bad response must never break the report
        current_app.logger.exception("AI evaluation-feedback summary failed for session %s", session_row["id"])
        return None


def get_report(session_id):
    """The last-generated Training Report for a class, or None if one has
    never been built."""
    row = db.query("SELECT * FROM training_reports WHERE session_id = ?", (session_id,), one=True)
    if row is None:
        return None
    return {
        "response_count": row["response_count"],
        "numeric_summary": json.loads(row["numeric_summary_json"] or "[]"),
        "text_summary": json.loads(row["text_summary_json"] or "[]"),
        "ai_summary": json.loads(row["ai_summary_json"]) if row["ai_summary_json"] else None,
        "generated_at": row["generated_at"],
    }


def build_report(session_id, user_id=None):
    """Pulls this class's evaluation responses fresh from Google, rebuilds
    the numeric aggregates and AI summary, and saves it as the class's
    current Training Report (replacing whatever was there before). Raises
    TrainingReportError or evaluation_forms.EvaluationFormError — both are
    plain Exceptions with an already user-facing message — on any
    failure; nothing is overwritten in that case, so a stale-but-working
    report is never clobbered by a failed refresh."""
    session_row = db.query(
        """SELECT cs.*, c.title AS course_title, t.name AS trainer_name
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN trainers t ON t.id = cs.trainer_id
           WHERE cs.id = ?""",
        (session_id,), one=True,
    )
    if session_row is None:
        raise TrainingReportError("Class not found.")
    form_id = session_row["evaluation_form_id"]
    if not form_id:
        raise TrainingReportError(
            "No Evaluation Form has been generated for this class yet — generate one from the class "
            "page first."
        )
    access_token = evaluation_forms.get_valid_access_token()
    if not access_token:
        raise TrainingReportError(
            "Couldn't get a valid Google access token — the connection under Settings may need to be "
            "reconnected."
        )

    structure = evaluation_forms.get_form_structure(form_id, access_token)
    responses = evaluation_forms.list_form_responses(form_id, access_token)

    numeric_summary = []
    text_summary = []
    for question_id, meta in structure.items():
        kind = meta["kind"]
        if kind not in ("scale", "choice_numeric", "choice_text", "text"):
            continue
        values = []
        for response in responses:
            values.extend(_collect_answer_values(response, question_id))
        if not values:
            continue
        if kind in ("scale", "choice_numeric"):
            agg = _aggregate_numeric(values)
            if agg:
                numeric_summary.append({"question": meta["title"], "kind": kind, **agg})
        elif kind == "choice_text":
            numeric_summary.append({"question": meta["title"], "kind": kind, **_aggregate_categorical(values)})
        elif kind == "text":
            text_summary.append({"question": meta["title"], "answers": values})

    ai_summary = _summarize_open_text(session_row, text_summary)

    db.execute(
        """INSERT INTO training_reports
               (session_id, response_count, numeric_summary_json, text_summary_json, ai_summary_json,
                generated_at, generated_by)
           VALUES (?, ?, ?, ?, ?, datetime('now'), ?)
           ON CONFLICT(session_id) DO UPDATE SET
               response_count = excluded.response_count,
               numeric_summary_json = excluded.numeric_summary_json,
               text_summary_json = excluded.text_summary_json,
               ai_summary_json = excluded.ai_summary_json,
               generated_at = excluded.generated_at,
               generated_by = excluded.generated_by""",
        (session_id, len(responses), json.dumps(numeric_summary), json.dumps(text_summary),
         json.dumps(ai_summary) if ai_summary else None, user_id),
    )
    return get_report(session_id)


@bp.route("/")
@login_required
def index():
    rows = db.query(
        """SELECT cs.id, c.title AS course_title, t.name AS trainer_name, cs.start_date, cs.end_date,
                  cs.evaluation_form_link, tr.response_count, tr.generated_at
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN trainers t ON t.id = cs.trainer_id
           LEFT JOIN training_reports tr ON tr.session_id = cs.id
           WHERE cs.evaluation_form_id IS NOT NULL
           ORDER BY cs.start_date DESC"""
    )
    return render_template("training_reports/index.html", rows=rows)


@bp.route("/<int:session_id>")
@login_required
def view(session_id):
    session_row = db.query(
        """SELECT cs.*, c.title AS course_title, t.name AS trainer_name
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN trainers t ON t.id = cs.trainer_id
           WHERE cs.id = ?""",
        (session_id,), one=True,
    )
    if session_row is None:
        flash("Class not found.", "danger")
        return redirect(url_for("training_reports.index"))
    return render_template(
        "training_reports/view.html",
        s=session_row,
        report=get_report(session_id),
        ai_configured=is_ai_configured(),
    )


@bp.route("/<int:session_id>/generate", methods=("POST",))
@login_required
def generate(session_id):
    try:
        build_report(session_id, user_id=g.user["id"] if g.user else None)
    except (TrainingReportError, evaluation_forms.EvaluationFormError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for("training_reports.view", session_id=session_id))
    activity.log("update", "session", session_id, "Generated Training Report from evaluation responses")
    flash("Training Report generated.", "success")
    return redirect(url_for("training_reports.view", session_id=session_id))
