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
     involved, so those numbers are exact. A worded scale (e.g.
     Poor/Uncertain/Fair/Good/Excellent) is scored 1..N like a numeric
     rating, and every question sharing one exact scale is also pooled
     into a combined rating (see _describe_combined_ratings).
  3. Turns those exact numbers into two AI narratives, both best-effort
     (if ANTHROPIC_API_KEY isn't set, or a call/parse fails, the exact
     numbers/answers are still shown — nothing here is ever load-bearing):
     a plain-English readout of the ratings (_summarize_ratings — reads
     the numbers, never recomputes or invents one) and a summary of the
     open-text questions (_summarize_open_text — genuinely synthesizes,
     since there's no non-AI way to roll up free text).

Deliberately a cache, not something rebuilt on every page view — building
it re-reads every response from Google and re-runs the AI summary, which
isn't cheap. A class page's report shows whatever was last generated,
with a "Refresh Report" button to regenerate on demand.

The one exception: the very first visit to a class's Training Report page
(before anything has ever been generated for it) builds it automatically —
see the view() route — rather than landing on an empty "click Generate"
page. There's nothing cached to overwrite yet at that point, so it costs
nothing a first manual click wouldn't have, and it matches what a class
page's evaluation-form section already implies once a Form is linked.
Every visit after that first one is back to the plain cache-read described
above; only "Refresh Report" rebuilds from then on.

Only available for classes with an auto-generated Form
(course_sessions.evaluation_form_id) — that's the only case where Modoku
Hub controls a Google Form ID it can call the Forms API against. A class
using a hand-pasted Evaluation Form link (from before this automation
existed, or where Google Forms automation was never connected) has no
Training Report option; the manual evaluation_form_link on that class is
just a URL, not something Modoku Hub can read responses back from.

A "Full Name" (or similarly-titled) question — present on Erik's forms
purely to identify the respondent — is dropped before aggregation (see
_is_identity_question) rather than surfaced as its own question: the
report already lists who attended from the signed T3 attendance roster
(full_reports._participant_names), so pulling the Form's own name field
too would just repeat that list under a "question" that isn't real
evaluation content.
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


@bp.before_request
def _block_cancelled_class():
    from .sessions import block_cancelled_class
    return block_cancelled_class("training_reports.index", "training report")

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


def _clean_num(value):
    """Renders a whole-number score as a plain int (3 instead of 3.0)
    everywhere it's displayed or embedded in a summary_text string, while
    leaving a genuine decimal (3.38) untouched. Applied once here, at the
    point every average/min/max is computed, rather than in the template —
    JSON and Jinja both render an int with no trailing decimal on their
    own, so nothing downstream needs to know about this."""
    return int(value) if float(value) == int(value) else value


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
        "average": _clean_num(round(sum(nums) / len(nums), 2)),
        "min": _clean_num(min(nums)),
        "max": _clean_num(max(nums)),
    }


def _declared_scale_max(kind, meta):
    """The top of a numeric question's scale as the Form itself declares
    it — a linear scale's `high`, or the largest option on a question whose
    options are numbers. Returns None rather than a guess when the form
    doesn't say (an average with no known ceiling can't be compared against
    one that has a different ceiling)."""
    if kind == "scale":
        high = _clean_num(meta.get("high"))
        return high if isinstance(high, (int, float)) and high > 0 else None
    if kind == "choice_numeric":
        options = []
        for opt in meta.get("options") or []:
            try:
                options.append(float(opt))
            except (TypeError, ValueError):
                continue
        return _clean_num(max(options)) if options else None
    return None


def _aggregate_categorical(values):
    counts = Counter(values)
    return {
        "count": sum(counts.values()),
        "distribution": [{"option": option, "count": n} for option, n in counts.most_common()],
    }


# A question that just captures who's answering (near-universally titled
# "Full Name" on Erik's forms, matched loosely here in case a form varies
# the wording) rather than actual evaluation content. Redundant with the
# report's own participant list (full_reports._participant_names, pulled
# from the signed T3 attendance form) and, worse, would otherwise show up
# as a bare list of names under a "question" in the numeric/open-text
# summary — so it's dropped in build_report() below, before it ever
# reaches aggregation, rather than filtered out later at render time.
_IDENTITY_QUESTION_TITLES = {
    "full name", "name", "your name", "participant name", "participant's name",
    "trainee name", "trainee's name", "attendee name",
}


def _is_identity_question(title):
    return (title or "").strip().lower() in _IDENTITY_QUESTION_TITLES


def _ordinal_score_map(scale):
    return {label: i + 1 for i, label in enumerate(scale)}


def _score_ordinal_values(values, scale):
    """Maps each raw answer (e.g. "Good") to its 1..N position in `scale`
    (e.g. ("poor","uncertain","fair","good","excellent") -> 4). Answers
    that don't match any label in the scale (shouldn't normally happen —
    they're the actual options on the question) are silently dropped
    rather than guessed at."""
    score_map = _ordinal_score_map(scale)
    scores = []
    for v in values:
        s = score_map.get(v.strip().lower())
        if s is not None:
            scores.append(s)
    return scores


def _describe_combined_ratings(scale, titles, values):
    """A short, deterministic description (no AI — exact arithmetic, same
    as the rest of this module's numeric aggregation) of every individual
    rating pooled across every question that shares one worded scale —
    e.g. every Poor/Uncertain/Fair/Good/Excellent-rated criterion in a
    grid, combined into one overall picture instead of read one row at a
    time. Returns None if none of the values actually matched the scale."""
    scores = _score_ordinal_values(values, scale)
    if not scores:
        return None
    n = len(scale)
    mid = (n + 1) / 2  # e.g. 3 on a 5-point scale (the middle label), 2.5 on a 4-point scale (no middle)
    positive_labels = [scale[i] for i in range(n) if (i + 1) > mid]
    neutral_labels = [scale[i] for i in range(n) if (i + 1) == mid]
    negative_labels = [scale[i] for i in range(n) if (i + 1) < mid]
    positive = sum(1 for s in scores if s > mid)
    negative = sum(1 for s in scores if s < mid)
    neutral = len(scores) - positive - negative
    average = _clean_num(round(sum(scores) / len(scores), 2))
    pct_positive = round(positive / len(scores) * 100)
    pct_negative = round(negative / len(scores) * 100)
    pct_neutral = round(neutral / len(scores) * 100)

    def _label_group(labels):
        return " or ".join(label.title() for label in labels)

    parts = []
    if positive_labels:
        parts.append(f"{pct_positive}% {_label_group(positive_labels)}")
    if neutral_labels:
        parts.append(f"{pct_neutral}% {_label_group(neutral_labels)}")
    if negative_labels:
        parts.append(f"{pct_negative}% {_label_group(negative_labels)}")
    # Deliberately doesn't spell out every criterion by name here — with a
    # long template (a dozen-plus rated criteria isn't unusual) that turned
    # into an unreadable wall of question titles; they're still each shown
    # in their own row below, and in full in `criteria` for anything that
    # wants them. See _summarize_ratings for the AI narrative that DOES
    # call out specific strongest/weakest criteria by name, in prose.
    summary_text = (
        f"Combined across {len(titles)} rating criteria: average {average}/{n} "
        f"from {len(scores)} ratings: " + ", ".join(parts) + "."
    )
    return {
        "scale": list(scale),
        "criteria": titles,
        "count": len(scores),
        "average": average,
        "scale_max": n,
        "pct_positive": pct_positive,
        "pct_negative": pct_negative,
        "pct_neutral": pct_neutral,
        "summary_text": summary_text,
    }


def is_ai_configured():
    return bool(current_app.config.get("ANTHROPIC_API_KEY"))


def _call_claude_json(prompt, max_tokens=1500):
    """POSTs a single-turn prompt to Claude and returns the parsed JSON
    object from its reply (the prompt always asks for a bare JSON object,
    optionally fenced in ```). Raises on any failure — network, non-2xx,
    or an unparseable reply — callers wrap this in their own try/except
    and treat any exception as "AI unavailable right now", per this
    module's best-effort AI policy."""
    api_key = current_app.config.get("ANTHROPIC_API_KEY")
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
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    response.raise_for_status()
    text = response.json()["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    return json.loads(text)


def _summarize_open_text(session_row, text_summary, combined_ratings=None):
    """Best-effort AI qualitative summary of the open-text evaluation
    answers, grouped by question. Never raises — returns None if the
    feature isn't configured or the call/parse fails, so build_report()
    still saves the numeric half and the raw open-text answers (shown as
    a fallback in the UI) either way. Only called when there's open text
    to summarize; combined_ratings (already exact, computed with no AI)
    is passed through purely as context so the AI's "overall" line can
    tie the numbers and the comments together instead of ignoring one."""
    api_key = current_app.config.get("ANTHROPIC_API_KEY")
    if not api_key or not text_summary:
        return None
    try:
        lines = [f"Class: {session_row['course_title']} (Trainer: {session_row['trainer_name'] or 'TBC'})", ""]
        if combined_ratings:
            lines.append("Already-computed rating summaries (exact, not your job to recompute, just use "
                         "them as context if relevant to the overall takeaway):")
            for combined in combined_ratings:
                lines.append(f"  - {combined['summary_text']}")
            lines.append("")
        for q in text_summary:
            lines.append(f"Question: {q['question']}")
            for i, ans in enumerate(q["answers"][:MAX_ANSWERS_PER_QUESTION], 1):
                lines.append(f"  {i}. {ans[:MAX_CHARS_PER_ANSWER]}")
            lines.append("")
        transcript = "\n".join(lines)
        prompt = (
            "Below are open-ended post-training evaluation answers from participants of a corporate "
            "training class, grouped by question. Summarize them honestly and specifically, group "
            "recurring themes, note both praise and criticism, and don't invent anything not actually "
            "said. If a question has few or contradictory answers, say so plainly rather than "
            "overstating a pattern. Reply with ONLY a JSON object, nothing else, no markdown, no "
            "explanation. Shape: {\"overall\": \"2-4 sentence overall takeaway across all questions\", "
            "\"by_question\": [{\"question\": \"<question text>\", \"summary\": \"2-4 sentence summary "
            "of themes for this question specifically\"}, ...]}\n\n" + transcript
        )
        parsed = _call_claude_json(prompt)
        if not isinstance(parsed, dict) or "overall" not in parsed:
            return None
        return parsed
    except Exception:  # noqa: BLE001 - a bad response must never break the report
        current_app.logger.exception("AI evaluation-feedback summary failed for session %s", session_row["id"])
        return None


def _summarize_ratings(session_row, numeric_summary, combined_ratings):
    """Best-effort AI narrative of the rating/multiple-choice questions —
    a plain-English readout of numbers that are already exact (see
    _describe_combined_ratings and the plain-arithmetic aggregation in
    build_report()); this only turns them into sentences, it never
    recomputes or alters any of them. Requested directly: the deterministic
    combined-ratings line alone gets unreadable once a template has a
    dozen-plus rated criteria (a long parenthetical list of question
    titles), so this calls out the strongest/weakest by name in prose
    instead. Never raises — returns None if unconfigured, there's nothing
    rated, or the call/parse fails; the exact numbers are shown either way,
    this is purely additive."""
    api_key = current_app.config.get("ANTHROPIC_API_KEY")
    rated = [q for q in numeric_summary if "average" in q]
    if not api_key or not rated:
        return None
    try:
        lines = [f"Class: {session_row['course_title']} (Trainer: {session_row['trainer_name'] or 'TBC'})", ""]
        if combined_ratings:
            lines.append("Combined rating groups (already computed exactly. Every criterion sharing one "
                         "rating scale, pooled together):")
            for combined in combined_ratings:
                lines.append(f"  - {', '.join(combined['criteria'])}: {combined['summary_text']}")
            lines.append("")
        lines.append("Individual rating questions (already computed exactly):")
        for q in rated:
            scale_max = q.get("scale_max", 5)
            lines.append(f"  - {q['question']}: average {q['average']}/{scale_max} ({q['count']} responses)")
        transcript = "\n".join(lines)
        prompt = (
            "Below is exact, already-computed rating data from a post-training evaluation, averages and "
            "response counts. Do not recompute or alter any of these numbers, and never invent a number "
            "not given below. Write a clear, honest 3-5 sentence narrative summary a training manager "
            "could read at a glance: name the specific criteria that scored strongest and weakest, and "
            "say plainly how positive the overall picture is. Reply with ONLY a JSON object, nothing "
            "else - no markdown, no explanation. Shape: {\"summary\": \"...\"}\n\n" + transcript
        )
        parsed = _call_claude_json(prompt, max_tokens=500)
        if not isinstance(parsed, dict) or not parsed.get("summary"):
            return None
        return parsed["summary"]
    except Exception:  # noqa: BLE001 - a bad response must never break the report
        current_app.logger.exception("AI ratings summary failed for session %s", session_row["id"])
        return None


# ---- Fix210: "Needs attention" (internal, never in the client report) ----------
# Low ratings by plain arithmetic, and the comments that criticise something
# or ask for an improvement - picked by AI word for word when the report is
# built (keyword fallback otherwise). Shown on the staff Training Report page
# only; full_reports.py reads named ai_summary keys and never this one.

LOW_RATING_OUT_OF_5 = 3.75  # an average at or below this (scaled to the question's own scale)


def low_ratings(report):
    """Rated questions whose average is at or below 3.75 out of 5 (same share
    of a 10-point scale etc.), lowest first, each with how many people gave
    one of the two lowest scores."""
    out = []
    for q in (report or {}).get("numeric_summary", []):
        avg = q.get("average")
        if avg is None or q.get("kind") not in ("scale", "choice_numeric", "choice_ordinal"):
            continue
        top = q.get("scale_max") or 5
        try:
            norm = float(avg) / float(top) * 5
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if norm > LOW_RATING_OUT_OF_5 + 1e-9:
            continue
        low_votes, low_labels = 0, ""
        if q.get("kind") == "choice_ordinal" and q.get("scale"):
            low = [lbl.lower() for lbl in q["scale"][:2]]
            low_votes = sum(d["count"] for d in q.get("distribution", []) if str(d["option"]).strip().lower() in low)
            low_labels = " / ".join(lbl.title() for lbl in q["scale"][:2])
        else:
            cutoff = float(top) * 0.4
            for d in q.get("distribution", []):
                try:
                    if float(d["option"]) <= cutoff:
                        low_votes += d["count"]
                except (TypeError, ValueError):
                    pass
            low_labels = f"{_clean_num(round(cutoff))} or below"
        out.append({"question": q["question"], "average": avg, "out_of": _clean_num(top),
                    "norm": norm, "count": q.get("count"), "low_votes": low_votes, "low_labels": low_labels})
    out.sort(key=lambda x: x["norm"])
    return out


_EMPTY_ANSWERS = {"", "-", "--", "n/a", "na", "nil", "none", "no", "nothing", "no comment", "no comments",
                  "tiada", "tak ada", "takde", "ok", "okay", "good", "all good", "nope"}
# Fix211/Fix212: the wording-cue fallback (used only until a report gets its
# AI pass). Whole-word patterns, so "hot" never matches "hotel's".
_CONTRAST = r"\b(however|but|although|though|except|tapi|tetapi|cuma)\b"
_REQUEST = (r"\bwould (help|be better|be good|be great|be nice|make)\b|\bbetter if\b|"
            r"\b(can|could|should|need|needs|must|to) be improved\b|\bneeds? (to )?improv|\bimprovement|"
            r"\bsuggest|\bwish\b|\brefin(e|ing)\b|\bimproving the\b|\brecommend (that|refining|improving|adding|more|less)|"
            r"\b(more|less|extra|additional) (time|examples?|exercises?|hands-on|practical|practice|interactive|"
            r"activities|breaks?|days?|sessions?|case studies|videos?)\b|"
            r"\bneeds? (more|less|an? |extra|half|another|additional|to)|\bcould (be|have|include|add|use)\b")
_COMPLAINT = (r"\b(not|cannot|unable|blocked|lack|lacking|rushed?|rushing|boring|difficult|hard to|confus\w*|"
              r"unclear|disappoint\w*|slow|cold|noisy|late|crowded|cramped|kurang|lambat|susah|sejuk|bosan|"
              r"problems?|issues?)\b|n't\b|\btoo (much|many|long|short|fast|slow|cold|hot|noisy|difficult|"
              r"technical|basic|rushed|packed|early|late)\b|\bshould\b")
# Questions asking for positives or recommendations ("most valuable
# takeaways", "who else might benefit"): only a contrast or a clear request
# counts there - "able to improve myself", "solving problems", "every
# department should take part" are not criticism.
_POSITIVE_QUESTION_WORDS = ("takeaway", "valuable", "like most", "liked", "enjoy", "learn", "gained", "benefit",
                            "best", "useful", "strength", "highlight", "recommend", "department", "colleague",
                            "who else")


def _flag_answer(answer, positive_question):
    import re as _re
    low = answer.lower()
    if _re.search(_CONTRAST, low) or _re.search(_REQUEST, low):
        return True
    return not positive_question and bool(_re.search(_COMPLAINT, low))


def _keyword_attention(text_summary):
    """Fallback when the AI can't be used: answers that read like criticism or
    a suggestion, by simple wording cues. Marked as such on the page."""
    out = []
    for q in text_summary or []:
        positive_q = any(w in (q.get("question") or "").lower() for w in _POSITIVE_QUESTION_WORDS)
        for ans in q.get("answers", []):
            a = (ans or "").strip()
            if a.lower().strip(" .!") in _EMPTY_ANSWERS:
                continue
            if _flag_answer(a, positive_q):
                out.append({"quote": a, "question": q["question"], "category": ""})
    return out


def _attention_comments(session_row, text_summary):
    """AI pass: every open-text answer that criticises something, shows
    dissatisfaction or asks for an improvement, quoted exactly and tagged
    Trainer / Content / Venue & Logistics / Other. Each quote is checked
    against the real answers, so nothing reworded or invented gets through.
    Returns None if the AI isn't available (callers fall back to keywords)."""
    if not current_app.config.get("ANTHROPIC_API_KEY") or not text_summary:
        return None
    try:
        lines = []
        for q in text_summary:
            lines.append(f"Question: {q['question']}")
            for i, ans in enumerate(q["answers"][:MAX_ANSWERS_PER_QUESTION], 1):
                lines.append(f"  {i}. {ans[:MAX_CHARS_PER_ANSWER]}")
            lines.append("")
        prompt = (
            "Below are open-ended answers from a post-training evaluation, grouped by question. Pick out EVERY "
            "answer that criticises something, shows dissatisfaction, describes a problem or setback, or asks "
            "or suggests that something be improved or done differently (even politely, e.g. 'more examples "
            "would help'). Leave out pure praise and empty answers such as 'none', 'nil', 'no comment'. A takeaway or "
            "something learned is praise, not a request: answers like 'learned more about Copilot', 'we get to know "
            "more about what Copilot can do' or 'more understanding of X and how to apply it' must be left out. So must "
            "praise that mentions improving or problems in a positive way ('able to improve myself at work', 'learnt "
            "things to improve our service', 'learnt how to solve customer problems') and recommendations of who else "
            "should attend ('every department should take part'). "
            "Copy "
            "each picked answer EXACTLY as written (the whole answer, or the critical sentence of a long one), "
            "never reword. Tag each with one category: Trainer, Content, Venue & Logistics, or Other. Reply with "
            "ONLY a JSON object, no markdown: {\"items\": [{\"question\": \"<question text>\", \"quote\": "
            "\"<exact words>\", \"category\": \"<category>\"}]}. Use {\"items\": []} if there are none.\n\n"
            + "\n".join(lines)
        )
        parsed = _call_claude_json(prompt, max_tokens=2000)
        items = parsed.get("items") if isinstance(parsed, dict) else None
        if not isinstance(items, list):
            return None
        answers = [a for q in text_summary for a in q.get("answers", [])]
        lowered = [a.lower() for a in answers]
        out = []
        for item in items:
            quote = str((item or {}).get("quote") or "").strip().strip('"')
            if not quote or not any(quote.lower() in a for a in lowered):
                continue  # not in the real answers: never show it
            cat = str(item.get("category") or "").strip().lower()
            category = ("Trainer" if cat.startswith(("trainer", "facilitator", "speaker")) else
                        "Content" if cat.startswith(("content", "material", "module", "course")) else
                        "Venue & Logistics" if cat.startswith(("venue", "logistic", "facilit", "food", "room")) else
                        "Other")
            out.append({"quote": quote, "question": str(item.get("question") or ""), "category": category})
        return out
    except Exception:  # noqa: BLE001 - a bad response must never break the report
        current_app.logger.exception("AI attention-comments pass failed for session %s", session_row["id"])
        return None


def attention(report):
    """Everything the "Needs attention" card shows."""
    report = report or {}
    ai = report.get("ai_summary") or {}
    comments = ai.get("attention")
    by_ai = comments is not None
    if not by_ai:
        comments = _keyword_attention(report.get("text_summary"))
    return {"low": low_ratings(report), "comments": comments, "by_ai": by_ai}


def get_report(session_id):
    """The last-generated Training Report for a class, or None if one has
    never been built."""
    row = db.query("SELECT * FROM training_reports WHERE session_id = ?", (session_id,), one=True)
    if row is None:
        return None
    raw_numeric = json.loads(row["numeric_summary_json"] or "[]")
    # numeric_summary_json holds {"questions": [...], "combined": [...]} —
    # a plain list is only possible from a report saved before combined
    # ratings existed, read as "no combined summary yet" rather than an error.
    if isinstance(raw_numeric, list):
        numeric_summary, combined_ratings = raw_numeric, []
    else:
        numeric_summary = raw_numeric.get("questions", [])
        combined_ratings = raw_numeric.get("combined", [])
    return {
        "response_count": row["response_count"],
        "numeric_summary": numeric_summary,
        "combined_ratings": combined_ratings,
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
            "No Evaluation Form has been generated for this class yet. Generate one from the class "
            "page first."
        )
    access_token = evaluation_forms.get_valid_access_token()
    if not access_token:
        raise TrainingReportError(
            "Couldn't get a valid Google access token. The connection under Settings may need to be "
            "reconnected."
        )

    structure = evaluation_forms.get_form_structure(form_id, access_token)
    responses = evaluation_forms.list_form_responses(form_id, access_token)

    numeric_summary = []
    text_summary = []
    # Every choice_ordinal question sharing one worded scale (the common
    # case: every row of one "rate the following" grid) gets pooled here so
    # they can be combined into one overall picture below, not just read
    # off one row at a time — see _describe_combined_ratings.
    ordinal_groups = {}
    for question_id, meta in structure.items():
        kind = meta["kind"]
        if kind not in ("scale", "choice_numeric", "choice_ordinal", "choice_text", "text"):
            continue
        if _is_identity_question(meta.get("title")):
            continue
        values = []
        for response in responses:
            values.extend(_collect_answer_values(response, question_id))
        if not values:
            continue
        form_group = meta.get("group")
        if kind in ("scale", "choice_numeric"):
            agg = _aggregate_numeric(values)
            if agg:
                # A distribution (exact vote counts per value) alongside the
                # average/min/max — full_reports.py's PDF charts a question
                # the same way regardless of kind, so every kind that can
                # have values needs one, not just the worded-scale kinds.
                agg["distribution"] = _aggregate_categorical(values)["distribution"]
                # The question's own declared ceiling (a linear scale's
                # "high", or the largest numeric option on a numbers-as-
                # options question), so anything reading this back knows
                # whether a 4.2 is 4.2-out-of-5 or 4.2-out-of-10 —
                # trainer_scores.py needs it to compare classes whose
                # forms use different scales. None when it can't be
                # determined; never guessed at.
                agg["scale_max"] = _declared_scale_max(kind, meta)
                numeric_summary.append({"question": meta["title"], "kind": kind, "group": form_group, **agg})
        elif kind == "choice_ordinal":
            scale = meta.get("scale") or []
            agg = _aggregate_categorical(values)
            scores = _score_ordinal_values(values, scale)
            if scores:
                agg["average"] = _clean_num(round(sum(scores) / len(scores), 2))
                agg["scale_max"] = len(scale)
            numeric_summary.append({"question": meta["title"], "kind": kind, "scale": scale, "group": form_group,
                                     **agg})
            if scale:
                group = ordinal_groups.setdefault(tuple(scale), {"titles": [], "values": []})
                group["titles"].append(meta["title"])
                group["values"].extend(values)
        elif kind == "choice_text":
            numeric_summary.append({"question": meta["title"], "kind": kind, "group": form_group,
                                     "widget": meta.get("widget"), **_aggregate_categorical(values)})
        elif kind == "text":
            text_summary.append({"question": meta["title"], "answers": values, "group": form_group})

    combined_ratings = []
    for scale, group in ordinal_groups.items():
        if len(group["titles"]) < 2:
            continue  # only one question on this scale — its own row above already shows this
        combined = _describe_combined_ratings(scale, group["titles"], group["values"])
        if combined:
            combined_ratings.append(combined)

    ratings_summary = _summarize_ratings(session_row, numeric_summary, combined_ratings)
    text_ai_summary = _summarize_open_text(session_row, text_summary, combined_ratings)
    attention_items = _attention_comments(session_row, text_summary)  # Fix210, internal only
    ai_summary = None
    if ratings_summary or text_ai_summary or attention_items is not None:
        ai_summary = {}
        if attention_items is not None:
            ai_summary["attention"] = attention_items
        if ratings_summary:
            ai_summary["ratings_summary"] = ratings_summary
        if text_ai_summary:
            ai_summary["overall"] = text_ai_summary["overall"]
            ai_summary["by_question"] = text_ai_summary["by_question"]

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
        (session_id, len(responses),
         json.dumps({"questions": numeric_summary, "combined": combined_ratings}),
         json.dumps(text_summary),
         json.dumps(ai_summary) if ai_summary else None, user_id),
    )
    return get_report(session_id)


@bp.route("/")
@login_required
def index():
    rows = db.query(
        """SELECT cs.id, c.title AS course_title, t.name AS trainer_name, cs.start_date, cs.end_date,
                  cs.evaluation_form_link, tr.response_count, tr.generated_at,
                  cs.evaluation_sent_at, cs.evaluation_sent_to, cs.status
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

    report = get_report(session_id)
    if report is None and session_row["evaluation_form_id"]:
        # First-ever visit to this class's Training Report: build it once
        # automatically instead of landing on an empty "click Generate
        # Report" page. Erik's own expectation was that the report would
        # "just appear" once participants had filled the Form; the manual
        # Refresh Report button (see the module docstring's "deliberately a
        # cache" note) still governs every visit AFTER this one — there's
        # nothing to overwrite on a page that's never been generated
        # before, so building it once here costs nothing a first manual
        # click wouldn't have. Never raises to the page — a real failure
        # (e.g. the Google connection needs reconnecting) is flashed and
        # falls back to the same empty state Generate Report would have
        # left it in.
        try:
            build_report(session_id, user_id=g.user["id"] if g.user else None)
            report = get_report(session_id)
        except (TrainingReportError, evaluation_forms.EvaluationFormError) as exc:
            flash(str(exc), "danger")

    # Local import to avoid a circular import — full_reports.py imports this
    # module at the top level (it reuses _call_claude_json/get_report), so
    # this module can't import full_reports back at the top level too.
    from . import full_reports
    return render_template(
        "training_reports/view.html",
        s=session_row,
        report=report,
        ai_configured=is_ai_configured(),
        full_report=full_reports.get_full_report(session_id),
        attention=attention(report),
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
