"""The "Generate Full Report" feature — the automated version of the
branded "Course Training Report" PDF Modoku Tech has always compiled by
hand once a class wraps up and its evaluation feedback is in.

Workflow: click "Generate Full Report" (next to Training Report's own
"Refresh Report", training_reports.py) -> an editable DRAFT is created,
with its three prose sections (Foreword / Objective / Conclusion)
AI-prefilled in Modoku's own established style (see _generate_foreword/
_generate_objective/_generate_conclusion) from the class's real data —
course description, trainer, dates, and (for the Conclusion) the exact
numbers training_reports.py already computed. Everything else in the
finished PDF (participant list, performance-details table, every rating
chart, every open-text table) is pulled fresh from the data at render
time, never something AI writes.

The draft is reviewed/edited on its own page (templates/full_reports/
edit.html) before being sent — "Save Draft" just saves the three text
boxes as typed, no AI involved; "Rewrite with AI" regenerates one section
fresh, discarding only that section's current text. Regenerating the
whole draft (clicking "Generate Full Report" again) never overwrites text
that's already there in any of the three columns — it only fills in
whichever section(s), if any, are still empty (a failed earlier AI call,
say) — so an edit you've made always sticks; see build_or_refresh_draft.

Once "Approve & Send" is clicked: the final PDF is rendered (full_report_
pdf.build_full_report_pdf) from the CURRENT saved draft text and the
freshest data, saved into course_sessions.evaluation_report_file (the
very same slot a manually-uploaded report already uses — so the class
page's "Training Report" section, sessions.py, shows either one the same
way and manual upload keeps working exactly as it always has, by design),
emailed to the client, and the draft row is locked (approved_at set) —
regenerating after that is refused; a real correction goes through that
same manual-upload button instead. See the module docstring in
full_report_pdf.py for how the PDF itself is built.
"""
import json
import os
import uuid

import requests
from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for, Response

from . import activity, courses, db, doc_sanity, full_report_pdf, mailer, training_reports
from . import fmtdaterange
from .ai_match import ANTHROPIC_API_URL, ANTHROPIC_API_VERSION
from .auth import login_required

bp = Blueprint("full_reports", __name__, url_prefix="/training-report")

DRAFT_SECTIONS = ("foreword", "objective", "key_findings", "conclusion")
SECTION_LABELS = {"foreword": "Foreword", "objective": "Objective",
                  "key_findings": "Key Findings", "conclusion": "Conclusion"}


class FullReportError(Exception):
    """Raised for any failure building or sending the full report PDF.
    Always written to be directly flashable."""


def _attendance_dir(session_id):
    """Same folder sessions.py's own uploads use (kept as a tiny local
    copy rather than importing sessions.py here, which already imports
    this module's sibling evaluation_forms/training_reports at module
    level — importing back would be circular)."""
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "sessions", str(session_id))
    os.makedirs(path, exist_ok=True)
    return path


def _session_context(session_id):
    return db.query(
        """SELECT cs.*, c.title AS course_title, c.description AS course_description,
                  c.duration_days AS course_duration_days, c.outline_file AS course_outline_file,
                  t.name AS trainer_name, cl.name AS client_name, cl.email AS client_email,
                  pic.name AS pic_name, pic.email AS pic_email
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN trainers t ON t.id = cs.trainer_id
           LEFT JOIN companies cl ON cl.id = cs.client_company_id
           LEFT JOIN leads pic ON pic.id = cs.pic_lead_id
           WHERE cs.id = ?""",
        (session_id,), one=True,
    )


def _participant_names(session_id):
    # The Attendance Form roster (t3_participants — the printed T3 list a
    # class's actual attendees sign), not `enrollments` — enrollments drives
    # invoicing/HRDF-claim counts and can include people who registered but
    # never actually showed up.
    #
    # Restricted to attended = 1 (per Erik: the report's participant list
    # should only be people who actually attended, as reflected on the
    # signed T3 attendance form) — someone added to the class roster but
    # never marked attended (a no-show, a duplicate entry, a registration
    # that didn't pan out) has no business appearing in a report that's
    # meant to describe who was actually in the room. Same `attended` flag
    # certificates.py already gates certificate eligibility on.
    rows = db.query(
        "SELECT name FROM t3_participants WHERE session_id = ? AND attended = 1 ORDER BY id", (session_id,)
    )
    return [r["name"] for r in rows]


def get_full_report(session_id):
    return db.query("SELECT * FROM full_training_reports WHERE session_id = ?", (session_id,), one=True)


def is_locked(full_report_row):
    return bool(full_report_row and full_report_row["approved_at"])


# --- AI drafting (each best-effort in the sense of never raising a bare
# exception, but callers here — unlike training_reports.py's purely-
# additive summaries — DO treat a None/failed result as an error to flash,
# since these three paragraphs are the actual deliverable content a class
# without one can't do without) ---------------------------------------

def _duration_text(session_row):
    days = session_row["course_duration_days"] if "course_duration_days" in session_row.keys() else None
    try:
        days = int(days) if days and float(days) == int(days) else days
    except (TypeError, ValueError):
        days = None
    if days:
        return f"{days} day(s)"
    # Fall back to the actual date span if the course has no declared
    # duration on file.
    try:
        from datetime import date
        start = date.fromisoformat(session_row["start_date"][:10])
        end = date.fromisoformat((session_row["end_date"] or session_row["start_date"])[:10])
        return f"{(end - start).days + 1} day(s)"
    except (ValueError, TypeError):
        return "unknown duration"


def _generate_foreword(session_row):
    client_name = session_row["client_name"] if "client_name" in session_row.keys() else None
    client_line = (f'Client: {client_name}' if client_name else
                   'Client: none on file for this class, write generically about "all participants" '
                   'rather than naming a specific client company.')
    prompt = (
        "You write short \"Foreword\" paragraphs for Modoku Tech Sdn Bhd's post-training evaluation "
        "reports, sent to corporate clients after a class wraps up. Two real examples Modoku Tech has "
        "used before, purely as a style/tone/length reference (do not reuse their specific wording or "
        "facts):\n\n"
        "Example 1: \"Modoku Tech congratulates all YTL Cement participants who attended the 2-day "
        "Office 365 PowerUser course. This evaluation report provides a summary of the feedback and "
        "outcomes from the training, giving YTL Cement insights and indicators on its effectiveness and "
        "satisfaction rates.\"\n\n"
        "Example 2: \"Modoku Tech congratulates all Shell participants who attended the highly demanded "
        "three-day PL-300: Microsoft Power BI Data Analyst course. This evaluation report provides a "
        "comprehensive overview of the feedback and outcomes from the training, offering Shell valuable "
        "insights and indicators of its effectiveness and participant satisfaction.\"\n\n"
        "Now write a new Foreword for this training:\n"
        f"Course: {session_row['course_title']}\n"
        f"{client_line}\n"
        f"Duration: {_duration_text(session_row)}\n\n"
        "Vary your sentence structure and wording naturally. Do not reuse the examples' exact sentence "
        "template every time this is generated. Keep it warm, plain, and professional; avoid bombastic, "
        "overly enthusiastic, or obviously AI-generated language (no \"unparalleled\", \"game-changing\", "
        "\"delve\", excessive exclamation marks). One short paragraph, 2-4 sentences. Reply with ONLY a "
        "JSON object, nothing else: {\"text\": \"...\"}"
    )
    parsed = training_reports._call_claude_json(prompt, max_tokens=400)
    text = parsed.get("text", "").strip() if isinstance(parsed, dict) else ""
    return text or None


_OBJECTIVE_EXAMPLE = (
    "\"The Office 365 PowerUser training program equips participants with advanced skills to "
    "maximize the use of Microsoft 365 applications for greater productivity and collaboration. "
    "Through practical, hands-on exercises, participants will learn to integrate and leverage tools "
    "such as Outlook, Teams, OneDrive, SharePoint, and Excel to streamline workflows and enhance "
    "workplace efficiency.\n\nThis comprehensive program is designed for professionals who already "
    "have a basic understanding of Microsoft Office and wish to elevate their capabilities to a more "
    "strategic, power-user level.\n\nBy the end of the course, attendees will be able to confidently "
    "apply their new skills to manage tasks, collaborate effectively across departments, and "
    "optimize daily operations.\""
)
_OBJECTIVE_STYLE_RULES = (
    "Vary your wording and structure naturally rather than reusing a fixed template every time. "
    "Avoid bombastic or obviously AI-generated language. 2-3 short paragraphs. Reply with ONLY a "
    "JSON object, nothing else: {\"text\": \"paragraph one\\n\\nparagraph two\"}"
)


def _call_claude_json_with_document(prompt, content_block, max_tokens=700):
    """Like training_reports._call_claude_json, but the message also hands
    Claude a document to actually read (a base64 PDF/image content block —
    see doc_sanity._content_block, reused here) rather than relying purely
    on text already typed into the database. Only _generate_objective uses
    this, when the course has an outline file Claude can read; raises on
    any failure exactly like _call_claude_json, so callers keep the same
    try/except-and-fall-back-to-plain-text pattern."""
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
            "messages": [{"role": "user", "content": [content_block, {"type": "text", "text": prompt}]}],
        },
    )
    response.raise_for_status()
    text = response.json()["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    return json.loads(text)


def _outline_content_block(session_row):
    """A Claude document/image content block for this course's uploaded
    outline file (courses.outline_file), or None when there isn't one, it's
    not a file Claude can visually read (e.g. Word/Excel — only PDF/image
    are supported, same limitation as doc_sanity's upload sanity-checks),
    or it's missing on disk. Never raises."""
    outline_file = session_row["course_outline_file"] if "course_outline_file" in session_row.keys() else None
    if not outline_file:
        return None
    try:
        path = os.path.join(courses._course_upload_dir(session_row["course_id"]), outline_file)
        if not os.path.isfile(path):
            return None
        return doc_sanity._content_block(path)
    except Exception:  # noqa: BLE001 - a bad/unreadable outline file must never break drafting
        current_app.logger.exception("Couldn't read course outline file for course %s", session_row["course_id"])
        return None


def _generate_objective(session_row):
    """Writes the Objective section — grounded in the course's actual
    uploaded outline document when Claude can read one (PDF/image), so it
    aligns with what the outline really covers rather than the shorter
    courses.description field; falls back to that description (or, absent
    even that, the bare course title) when there's no outline file, it's a
    format Claude can't read (e.g. Word), or the outline-based call fails
    for any reason."""
    description = (session_row["course_description"] or "").strip() if\
        "course_description" in session_row.keys() else ""
    description_line = description or ("(no description on file for this course, write a brief, "
                                         "plausible objective from the course title alone, without "
                                         "inventing specific tools/techniques the course may not cover)")

    outline_block = _outline_content_block(session_row)
    if outline_block is not None:
        outline_prompt = (
            "You write the \"Objective\" section of Modoku Tech Sdn Bhd's post-training evaluation "
            "reports, 2-3 short paragraphs describing what the course equips participants to do. A "
            "real example, purely as a style/length reference (a different course. Do not reuse its "
            "specific content):\n\n" + _OBJECTIVE_EXAMPLE + "\n\n"
            "Attached is this course's actual outline/syllabus document. Read it and write the "
            "Objective section for this course:\n"
            f"Course: {session_row['course_title']}\n\n"
            "Base the content on what the attached outline actually covers. Its real topics, skills, "
            "and target audience, never invent a capability, tool, or audience the outline doesn't "
            "actually mention. " + _OBJECTIVE_STYLE_RULES
        )
        try:
            parsed = _call_claude_json_with_document(outline_prompt, outline_block, max_tokens=700)
            text = parsed.get("text", "").strip() if isinstance(parsed, dict) else ""
            if text:
                return text
        except Exception:  # noqa: BLE001 - fall back to the plain-text prompt below
            current_app.logger.exception(
                "Outline-based Objective generation failed for course %s, falling back to description",
                session_row["course_id"],
            )

    prompt = (
        "You write the \"Objective\" section of Modoku Tech Sdn Bhd's post-training evaluation reports - "
        "2-3 short paragraphs describing what the course equips participants to do. A real example, "
        "purely as a style/length reference (a different course. Do not reuse its specific content):\n\n"
        + _OBJECTIVE_EXAMPLE + "\n\n"
        "Now write the Objective section for this course:\n"
        f"Course: {session_row['course_title']}\n"
        f"Course description (base your content on this, never invent a capability it doesn't "
        f"actually mention): {description_line}\n\n" + _OBJECTIVE_STYLE_RULES
    )
    parsed = training_reports._call_claude_json(prompt, max_tokens=700)
    text = parsed.get("text", "").strip() if isinstance(parsed, dict) else ""
    return text or None


def _generate_conclusion(session_row, report):
    lines = []
    if report:
        for combined in (report.get("combined_ratings") or []):
            lines.append(f"- {combined['summary_text']}")
        ai_summary = report.get("ai_summary") or {}
        if ai_summary.get("ratings_summary"):
            lines.append(f"- Ratings narrative: {ai_summary['ratings_summary']}")
        if ai_summary.get("overall"):
            lines.append(f"- Open-text feedback takeaway: {ai_summary['overall']}")
    data_block = "\n".join(lines) if lines else "(no evaluation feedback recorded yet for this class)"
    prompt = (
        "You write the \"Conclusion\" section of Modoku Tech Sdn Bhd's post-training evaluation reports "
        "a short closing paragraph. Two real examples, purely as a style/tone/length reference (do not"
        "reuse their specific wording or facts):\n\n"
        "Example 1: \"In conclusion, we are truly pleased with the significant and positive impact this "
        "training has made on the participants. It is encouraging to see how the program has enhanced "
        "their knowledge, engagement, and confidence in applying what they have learned to their "
        "respective roles. Moving forward, we remain committed to supporting their continuous "
        "professional growth and development. We greatly value the feedback shared by the participants "
        "and will take it into careful consideration when designing future sessions.\"\n\n"
        "Example 2: \"In conclusion, we are delighted with the positive impact the PL-300 training has "
        "had on the participants and look forward to supporting their continued professional growth. We "
        "will thoughtfully consider their feedback for future sessions. The participants displayed "
        "strong engagement and demonstrated substantial learning, while also expressing interest in "
        "further training.\"\n\n"
        f"Course: {session_row['course_title']}\n\n"
        "Actual computed feedback for THIS training (already exact, reflect it honestly, never invent "
        "a number or claim not supported here; if it says there's no feedback yet, write a more general "
        "closing paragraph without specific results claims):\n" + data_block + "\n\n"
        "Write a new Conclusion, 3-5 sentences, in a similar warm/professional style to the examples "
        "but reflecting this training's own actual results/sentiment above. The report already has a "
        "Key Findings section that lists the exact scores, percentages, strongest/weakest criteria and "
        "specific participant concerns, so do NOT repeat any number, percentage or criterion name here. "
        "Refer to the results only in general terms (e.g. \"strongly positive feedback\"). Vary your wording and "
        "structure naturally rather than reusing a fixed template every time. Avoid bombastic or "
        "obviously AI-generated language. Reply with ONLY a JSON object, nothing else: {\"text\": \"...\"}"
    )
    parsed = training_reports._call_claude_json(prompt, max_tokens=500)
    text = parsed.get("text", "").strip() if isinstance(parsed, dict) else ""
    return text or None


def _has_feedback(report):
    return bool(report and (report.get("numeric_summary") or report.get("text_summary")))


def _generate_key_findings(session_row, report):
    """Fix90: the one-glance "Key Findings" list that sits under the
    Training Performance Details table. Built from what the Training Report
    page already shows (exact combined ratings + per-criterion averages,
    the AI ratings narrative, the AI open-text overall and per-question
    summaries) but condensed into a handful of short, non-overlapping
    points. Those source summaries repeat each other (e.g. both quote the
    same "93% Good or Excellent"), so the prompt makes each fact appear
    exactly once. Returns newline-separated points, or None when the class
    has no feedback yet."""
    if not _has_feedback(report):
        return None
    lines = []
    for combined in (report.get("combined_ratings") or []):
        lines.append(f"- Combined: {combined['summary_text']}")
    rated = [q for q in (report.get("numeric_summary") or []) if "average" in q]
    if rated:
        lines.append("- Per-criterion averages (exact):")
        for q in sorted(rated, key=lambda q: q["average"], reverse=True):
            lines.append(f"    {q['question']}: {q['average']}/{q.get('scale_max', 5)}")
    ai_summary = report.get("ai_summary") or {}
    if ai_summary.get("ratings_summary"):
        lines.append(f"- Ratings narrative: {ai_summary['ratings_summary']}")
    if ai_summary.get("overall"):
        lines.append(f"- Open-text overall: {ai_summary['overall']}")
    for item in (ai_summary.get("by_question") or []):
        if item.get("summary"):
            lines.append(f"- Open-text, \"{item.get('question', '')}\": {item['summary']}")
    if not ai_summary:
        # No AI summaries cached - fall back to a sample of the raw answers.
        for q in (report.get("text_summary") or []):
            answers = [a for a in q.get("answers", []) if a and a.strip()][:15]
            if answers:
                lines.append(f"- Answers to \"{q['question']}\": " + " | ".join(a[:200] for a in answers))
    prompt = (
        "You write the \"Key Findings\" section of Modoku Tech Sdn Bhd's post-training evaluation report, "
        "which is sent to the CLIENT company (the employer who paid for the training). Busy managers "
        "read only this part, so it must be short and scannable.\n\n"
        f"Course: {session_row['course_title']}\nTrainer: {session_row['trainer_name'] or 'TBC'}\n\n"
        "Evaluation data (numbers are exact; the narratives are earlier AI summaries of the same data "
        "and overlap each other heavily):\n" + "\n".join(lines) + "\n\n"
        "Rules:\n"
        "- 4 to 6 points. Each point is ONE sentence, max 25 words, starting with a short label and a "
        "colon. Use labels such as \"Overall\", \"Strengths\", \"Areas to improve\", \"Participant "
        "feedback\", \"Recommendation\" (only the ones the data supports).\n"
        "- Every fact appears ONCE. Never repeat a number, percentage or criterion across points.\n"
        "- Only use numbers given above; never invent or recompute one. Don't restate the participant "
        "or response counts (already shown in the table above this section).\n"
        "- Include concrete, actionable items the client can act on (e.g. a tool/licence gap, duration "
        "requests, departments that would benefit), phrased constructively and professionally. Never "
        "name individual participants.\n"
        "- Plain, professional English. No fluff, no marketing language, no \"overall this was a highly "
        "successful\" style filler.\n"
        "Reply with ONLY a JSON object, nothing else: {\"points\": [\"Label: sentence\", ...]}"
    )
    parsed = training_reports._call_claude_json(prompt, max_tokens=700)
    points = parsed.get("points") if isinstance(parsed, dict) else None
    if not isinstance(points, list):
        return None
    points = [" ".join(str(p).split()).lstrip("-•· ").strip() for p in points]
    points = [p for p in points if p]
    return "\n".join(points) or None


def build_or_refresh_draft(session_id, user_id=None):
    """Creates the draft row if it doesn't exist yet, then AI-fills
    whichever of the three text columns is still empty — never touches a
    column that already has text in it (an edit you made always sticks;
    see the module docstring). Raises FullReportError if the draft is
    already approved/sent (locked), or if every still-empty section's AI
    call fails outright. Returns the (possibly updated) row."""
    if not training_reports.is_ai_configured():
        raise FullReportError(
            "AI drafting isn't set up (ANTHROPIC_API_KEY isn't configured). An admin needs to set "
            "that before Full Reports can be generated."
        )
    session_row = _session_context(session_id)
    if session_row is None:
        raise FullReportError("Class not found.")

    existing = get_full_report(session_id)
    if is_locked(existing):
        raise FullReportError(
            "This class's report has already been approved and sent. Regenerating it is disabled so a "
            "document the client already received is never silently replaced. Use the manual upload "
            "below if you need to send a corrected one."
        )

    report = training_reports.get_report(session_id)
    current = {
        "foreword_text": existing["foreword_text"] if existing else None,
        "objective_text": existing["objective_text"] if existing else None,
        "key_findings_text": existing["key_findings_text"] if existing else None,
        "conclusion_text": existing["conclusion_text"] if existing else None,
    }
    errors = []
    if not current["foreword_text"]:
        try:
            current["foreword_text"] = _generate_foreword(session_row)
        except Exception:  # noqa: BLE001
            current_app.logger.exception("AI foreword generation failed for session %s", session_id)
            errors.append("Foreword")
    if not current["objective_text"]:
        try:
            current["objective_text"] = _generate_objective(session_row)
        except Exception:  # noqa: BLE001
            current_app.logger.exception("AI objective generation failed for session %s", session_id)
            errors.append("Objective")
    # Key Findings only when there's feedback to summarize - a class with no
    # evaluation data yet just leaves it empty (not an error; the PDF omits
    # the section), and a later Generate fills it in once data arrives.
    if not current["key_findings_text"] and _has_feedback(report):
        try:
            current["key_findings_text"] = _generate_key_findings(session_row, report)
            if not current["key_findings_text"]:
                errors.append("Key Findings")
        except Exception:  # noqa: BLE001
            current_app.logger.exception("AI key findings generation failed for session %s", session_id)
            errors.append("Key Findings")
    if not current["conclusion_text"]:
        try:
            current["conclusion_text"] = _generate_conclusion(session_row, report)
        except Exception:  # noqa: BLE001
            current_app.logger.exception("AI conclusion generation failed for session %s", session_id)
            errors.append("Conclusion")

    db.execute(
        """INSERT INTO full_training_reports (session_id, foreword_text, objective_text, key_findings_text,
                                               conclusion_text, generated_at, generated_by)
           VALUES (?, ?, ?, ?, ?, datetime('now'), ?)
           ON CONFLICT(session_id) DO UPDATE SET
               foreword_text = excluded.foreword_text,
               objective_text = excluded.objective_text,
               key_findings_text = excluded.key_findings_text,
               conclusion_text = excluded.conclusion_text,
               generated_at = datetime('now'),
               generated_by = excluded.generated_by""",
        (session_id, current["foreword_text"], current["objective_text"], current["key_findings_text"],
         current["conclusion_text"], user_id),
    )
    if errors:
        raise FullReportError(
            f"Draft saved, but AI couldn't write the {', '.join(errors)} section"
            f"{'s' if len(errors) > 1 else ''} just now. Edit {'them' if len(errors) > 1 else 'it'} in "
            "by hand below, or try Rewrite with AI again in a moment."
        )
    return get_full_report(session_id)


def rewrite_section(session_id, section, user_id=None):
    """Regenerates exactly one of the three prose sections via AI,
    discarding only that section's current text — the other two are left
    untouched. Raises FullReportError if locked, unconfigured, or the call
    fails."""
    if section not in DRAFT_SECTIONS:
        raise FullReportError("Unknown report section.")
    if not training_reports.is_ai_configured():
        raise FullReportError("AI drafting isn't set up (ANTHROPIC_API_KEY isn't configured).")
    session_row = _session_context(session_id)
    if session_row is None:
        raise FullReportError("Class not found.")
    existing = get_full_report(session_id)
    if is_locked(existing):
        raise FullReportError("This class's report has already been sent. It can no longer be edited.")

    try:
        if section == "foreword":
            text = _generate_foreword(session_row)
        elif section == "objective":
            text = _generate_objective(session_row)
        elif section == "key_findings":
            report = training_reports.get_report(session_id)
            if not _has_feedback(report):
                raise FullReportError("No evaluation feedback for this class yet, so there's nothing to "
                                      "summarize into Key Findings. Refresh the Training Report first.")
            text = _generate_key_findings(session_row, report)
        else:
            text = _generate_conclusion(session_row, training_reports.get_report(session_id))
    except FullReportError:
        raise
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("AI rewrite (%s) failed for session %s", section, session_id)
        raise FullReportError("AI couldn't write a new draft just now, try again in a moment.") from exc
    if not text:
        raise FullReportError("AI couldn't write a new draft just now, try again in a moment.")

    column = f"{section}_text"
    db.execute(
        f"""INSERT INTO full_training_reports (session_id, {column}, generated_at, generated_by)
            VALUES (?, ?, datetime('now'), ?)
            ON CONFLICT(session_id) DO UPDATE SET {column} = excluded.{column},
                generated_at = datetime('now'), generated_by = excluded.generated_by""",
        (session_id, text, user_id),
    )
    return get_full_report(session_id)


def save_draft(session_id, foreword_text, objective_text, conclusion_text, key_findings_text=""):
    """Saves the three text boxes verbatim, no AI involved — always
    allowed pre-approval (both the Save Draft button and, as its first
    step, Approve & Send, so whatever's currently in the textareas is what
    gets used regardless of which button was actually clicked). Raises
    FullReportError if locked or no draft exists yet."""
    existing = get_full_report(session_id)
    if existing is None:
        raise FullReportError("No draft to save yet. Click Generate Full Report first.")
    if is_locked(existing):
        raise FullReportError("This class's report has already been sent. It can no longer be edited.")
    db.execute(
        "UPDATE full_training_reports SET foreword_text = ?, objective_text = ?, key_findings_text = ?, "
        "conclusion_text = ? WHERE session_id = ?",
        (foreword_text, objective_text, key_findings_text, conclusion_text, session_id),
    )


def _default_full_report_email_subject(session_row):
    date_range = fmtdaterange(session_row["start_date"], session_row["end_date"])
    return f"Training Evaluation Report - {session_row['course_title']} ({date_range})"


def _default_full_report_email_body(session_row):
    greeting_name = session_row["pic_name"] if "pic_name" in session_row.keys() and session_row["pic_name"] else None
    return (
        f"Hi {greeting_name or 'there'},\n\n"
        f"Thank you for giving us the opportunity to host the {session_row['course_title']} training for "
        "your team.\n\n"
        "I'd like to share the training evaluation report with you. It includes valuable feedback from "
        "the participants.\n\n"
        "Feel free to review the report and share it with your team. If you have any questions or need "
        "further information, please don't hesitate to reach out. We're happy to help.\n\n"
        "Thank you again for choosing us for your training needs. We look forward to working with you "
        "again in the future."
    )


def _build_pdf_or_raise(ctx, session_id, action):
    """Wraps full_report_pdf.build_full_report_pdf with error handling
    shared by approve_and_send and the preview route — same PDF, same
    failure modes. Gives a specific, actionable message for the one
    failure most likely right after this feature is first deployed
    (pypdf — a brand-new dependency this feature introduced — not yet
    installed on the server), and a generic one otherwise; either way the
    real traceback is always logged so a persistent failure can be
    diagnosed from the server log."""
    try:
        return full_report_pdf.build_full_report_pdf(ctx)
    except ModuleNotFoundError as exc:
        current_app.logger.exception("Full Report PDF %s failed for session %s (missing dependency)",
                                      action, session_id)
        raise FullReportError(
            f"Couldn't build the report PDF. The server is missing a required Python package "
            f"({exc.name or 'pypdf'}). An admin needs to run `pip install -r requirements.txt` "
            "(and restart the app) to pick up the newest dependencies, then try again."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("Full Report PDF %s failed for session %s", action, session_id)
        raise FullReportError("Couldn't build the report PDF. Try again, or contact support if it keeps failing.") from exc


def approve_and_send(session_id, to_email, subject, body, cc_email=None, user_id=None):
    """Renders the final PDF from the current saved draft + freshest data,
    saves it into course_sessions.evaluation_report_file (the same slot
    manual upload uses), emails it, and locks the draft. Raises
    FullReportError on any failure — nothing is saved/sent/locked in that
    case."""
    existing = get_full_report(session_id)
    if existing is None:
        raise FullReportError("No draft to send yet. Click Generate Full Report first.")
    if is_locked(existing):
        raise FullReportError("This class's report has already been sent.")
    if not to_email:
        raise FullReportError(
            "No client email on file for this class. Add one, or type an address to send to."
        )

    session_row = _session_context(session_id)
    if session_row is None:
        raise FullReportError("Class not found.")
    report = training_reports.get_report(session_id) or {
        "response_count": 0, "numeric_summary": [], "text_summary": [],
    }
    participants = _participant_names(session_id)
    client_name = session_row["client_name"] if "client_name" in session_row.keys() else None

    ctx = {
        "course_title": session_row["course_title"],
        "date_range": fmtdaterange(session_row["start_date"], session_row["end_date"]),
        "client_name": client_name,
        "trainer_name": session_row["trainer_name"],
        "venue": session_row["venue"],
        "total_participants": len(participants),
        "total_responded": report["response_count"],
        "participants": participants,
        "foreword_text": existing["foreword_text"] or "",
        "objective_text": existing["objective_text"] or "",
        "key_findings_text": existing["key_findings_text"] or "",
        "conclusion_text": existing["conclusion_text"] or "",
        "numeric_summary": report["numeric_summary"],
        "text_summary": report["text_summary"],
    }
    pdf_bytes = _build_pdf_or_raise(ctx, session_id, "generation")

    stored_name = f"training_report_{uuid.uuid4().hex[:8]}.pdf"
    saved_path = os.path.join(_attendance_dir(session_id), stored_name)
    with open(saved_path, "wb") as f:
        f.write(pdf_bytes)

    try:
        mailer.send_email(
            to_email, subject, body,
            attachments=[(stored_name, pdf_bytes, "application/pdf")],
            related_type="course_session", related_id=session_id, cc_email=cc_email,
        )
    except (mailer.MailNotConfigured, mailer.MailSendError) as exc:
        raise FullReportError(str(exc)) from exc

    db.execute(
        "UPDATE course_sessions SET evaluation_report_file = ?, evaluation_sent_at = datetime('now'), "
        "evaluation_sent_to = ? WHERE id = ?",
        (stored_name, to_email, session_id),
    )
    db.execute(
        "UPDATE full_training_reports SET approved_at = datetime('now'), approved_by = ?, sent_to = ? "
        "WHERE session_id = ?",
        (user_id, to_email, session_id),
    )
    activity.log("send_email", "session", session_id, f"Approved and emailed Full Training Report to {to_email}")


def _save_draft_from_form(session_id):
    """Saves all four text boxes exactly as currently on screen."""
    save_draft(session_id, request.form.get("foreword_text", ""), request.form.get("objective_text", ""),
               request.form.get("conclusion_text", ""),
               key_findings_text=request.form.get("key_findings_text", ""))


# --- Routes --------------------------------------------------------------

@bp.route("/<int:session_id>/full")
@login_required
def edit(session_id):
    session_row = _session_context(session_id)
    if session_row is None:
        flash("Class not found.", "danger")
        return redirect(url_for("training_reports.index"))
    full_report = get_full_report(session_id)
    return render_template(
        "full_reports/edit.html", s=session_row, full_report=full_report, locked=is_locked(full_report),
        ai_configured=training_reports.is_ai_configured(),
        default_email_subject=_default_full_report_email_subject(session_row),
        default_email_body=_default_full_report_email_body(session_row),
    )


@bp.route("/<int:session_id>/full/generate", methods=("POST",))
@login_required
def generate(session_id):
    try:
        build_or_refresh_draft(session_id, user_id=g.user["id"] if g.user else None)
    except FullReportError as exc:
        flash(str(exc), "danger" if get_full_report(session_id) is None else "warning")
        return redirect(url_for("training_reports.view", session_id=session_id))
    activity.log("update", "session", session_id, "Generated Full Report draft")
    flash("Full Report draft generated, review and edit below before sending.", "success")
    return redirect(url_for("full_reports.edit", session_id=session_id))


@bp.route("/<int:session_id>/full/save", methods=("POST",))
@login_required
def save(session_id):
    try:
        _save_draft_from_form(session_id)
    except FullReportError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("full_reports.edit", session_id=session_id))
    flash("Draft saved.", "success")
    return redirect(url_for("full_reports.edit", session_id=session_id))


@bp.route("/<int:session_id>/full/rewrite/<section>", methods=("POST",))
@login_required
def rewrite(session_id, section):
    # Whatever's currently in the OTHER two textareas must survive this
    # click too, even though this route only touches one column server-
    # side — save them first from the submitted form (same "save whatever
    # is on screen" rule as save()/approve() below).
    try:
        _save_draft_from_form(session_id)
        rewrite_section(session_id, section, user_id=g.user["id"] if g.user else None)
    except FullReportError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("full_reports.edit", session_id=session_id))
    flash(f"{SECTION_LABELS.get(section, section.title())} rewritten by AI.", "success")
    return redirect(url_for("full_reports.edit", session_id=session_id))


@bp.route("/<int:session_id>/full/preview", methods=("GET", "POST"))
@login_required
def preview(session_id):
    session_row = _session_context(session_id)
    full_report = get_full_report(session_id)
    if session_row is None or full_report is None:
        flash("No draft to preview yet.", "danger")
        return redirect(url_for("training_reports.view", session_id=session_id))
    if request.method == "POST" and not is_locked(full_report):
        # The Preview button lives inside the same <form> as the three
        # textareas — save whatever's currently typed first (same "save
        # whatever's on screen" rule Rewrite/Approve use below) so the
        # preview always matches what's on screen, not just the last
        # explicit Save Draft click.
        try:
            _save_draft_from_form(session_id)
            full_report = get_full_report(session_id)
        except FullReportError:
            pass  # e.g. locked by another tab just now — preview whatever's already on file instead
    report = training_reports.get_report(session_id) or {"response_count": 0, "numeric_summary": [], "text_summary": []}
    participants = _participant_names(session_id)
    client_name = session_row["client_name"] if "client_name" in session_row.keys() else None
    ctx = {
        "course_title": session_row["course_title"],
        "date_range": fmtdaterange(session_row["start_date"], session_row["end_date"]),
        "client_name": client_name,
        "trainer_name": session_row["trainer_name"],
        "venue": session_row["venue"],
        "total_participants": len(participants),
        "total_responded": report["response_count"],
        "participants": participants,
        "foreword_text": full_report["foreword_text"] or "",
        "objective_text": full_report["objective_text"] or "",
        "key_findings_text": full_report["key_findings_text"] or "",
        "conclusion_text": full_report["conclusion_text"] or "",
        "numeric_summary": report["numeric_summary"],
        "text_summary": report["text_summary"],
    }
    try:
        pdf_bytes = _build_pdf_or_raise(ctx, session_id, "preview")
    except FullReportError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("full_reports.edit", session_id=session_id))
    return Response(pdf_bytes, mimetype="application/pdf",
                     headers={"Content-Disposition": "inline; filename=training-report-preview.pdf"})


@bp.route("/<int:session_id>/full/approve", methods=("POST",))
@login_required
def approve(session_id):
    session_row = _session_context(session_id)
    if session_row is None:
        flash("Class not found.", "danger")
        return redirect(url_for("training_reports.index"))
    try:
        _save_draft_from_form(session_id)
        to_email = (request.form.get("to_email") or session_row["pic_email"] or session_row["client_email"] or "").strip()
        subject = (request.form.get("subject") or "").strip() or _default_full_report_email_subject(session_row)
        body = (request.form.get("body") or "").strip() or _default_full_report_email_body(session_row)
        cc_email = (request.form.get("cc_email") or "").strip() or None
        approve_and_send(session_id, to_email, subject, body, cc_email=cc_email,
                          user_id=g.user["id"] if g.user else None)
    except FullReportError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("full_reports.edit", session_id=session_id))
    flash(f"Full Report approved and emailed to {request.form.get('to_email') or 'the client'}.", "success")
    return redirect(url_for("sessions.view", session_id=session_id))
