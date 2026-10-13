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

Fix165: each section's "Rephrase" menu reworks the current text (same
facts, new wording: shorter, more detailed, more formal, warmer, or the
user's own instruction) or writes a fresh draft from the data. Every AI
call is shown the same section from this client's earlier reports and told
not to reuse their wording, so repeat clients don't get the same report
every time. A report can be marked as Batch N and compared with an earlier
class: the comparison numbers are computed here (batch_comparison), and the
AI only words them (a "Compared with Batch 1" Key Findings point, the
trend in the Conclusion, "the second batch" in the Foreword).

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


@bp.before_request
def _block_cancelled_class():
    from .sessions import block_cancelled_class
    return block_cancelled_class("training_reports.index", "training report")

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


def _generate_foreword(session_row, extra=""):
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
        "JSON object, nothing else: {\"text\": \"...\"}" + extra
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


def _generate_objective(session_row, extra=""):
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
            "actually mention. " + _OBJECTIVE_STYLE_RULES + extra
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
        f"actually mention): {description_line}\n\n" + _OBJECTIVE_STYLE_RULES + extra
    )
    parsed = training_reports._call_claude_json(prompt, max_tokens=700)
    text = parsed.get("text", "").strip() if isinstance(parsed, dict) else ""
    return text or None


def _generate_conclusion(session_row, report, extra=""):
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
        + extra
    )
    parsed = training_reports._call_claude_json(prompt, max_tokens=500)
    text = parsed.get("text", "").strip() if isinstance(parsed, dict) else ""
    return text or None


def _has_feedback(report):
    return bool(report and (report.get("numeric_summary") or report.get("text_summary")))


# --- Fix165: batches, batch-vs-batch comparison, and varied wording ----------

_ORDINALS = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh",
             8: "eighth", 9: "ninth", 10: "tenth"}
MAX_BATCH = 10

# "Rewrite with AI" options. "fresh" writes the section again from the class
# data; the rest rework the CURRENT text (same facts, different wording).
REWRITE_STYLES = {
    "fresh": ("Fresh draft", None),
    "rephrase": ("Rephrase, same meaning", "Keep the same length and tone, just say it differently."),
    "shorter": ("Shorter", "Make it noticeably shorter and tighter, keeping the key points."),
    "detailed": ("More detailed", "Make it a little fuller and more specific, without padding or "
                                  "inventing anything."),
    "formal": ("More formal", "Make the tone more formal and corporate."),
    "warmer": ("Warmer", "Make the tone warmer and more personal, still professional."),
    "custom": ("Your own instruction", None),
}


def batch_ordinal(batch_no):
    return _ORDINALS.get(batch_no, f"no. {batch_no}")


def _earlier_classes(session_row):
    """This client's other classes that ran before this one (not cancelled),
    newest first, each flagged with whether it's the same course and has
    evaluation data to compare against."""
    if not session_row["client_company_id"]:
        return []
    rows = db.query(
        """SELECT cs.id, cs.start_date, cs.end_date, cs.course_id, c.title AS course_title,
                  tr.id AS report_id, ftr.batch_no
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN training_reports tr ON tr.session_id = cs.id
           LEFT JOIN full_training_reports ftr ON ftr.session_id = cs.id
           WHERE cs.client_company_id = ? AND cs.id != ? AND cs.status != 'Cancelled'
             AND cs.start_date < ?
           ORDER BY cs.start_date DESC""",
        (session_row["client_company_id"], session_row["id"], session_row["start_date"]),
    )
    return [dict(r, same_course=(r["course_id"] == session_row["course_id"]), has_data=bool(r["report_id"]))
            for r in rows]


def batch_suggestion(session_row):
    """(suggested batch_no or None, suggested compare_session_id or None,
    reason text). Batch N = this is the client's Nth class of the same
    course; a lone class (no earlier or later run of the same course for
    this client) isn't a batch at all."""
    earlier_same = [r for r in _earlier_classes(session_row) if r["same_course"]]
    if earlier_same:
        last_batch = earlier_same[0]["batch_no"]
        batch = (last_batch + 1) if last_batch else len(earlier_same) + 1
        compare = next((r["id"] for r in earlier_same if r["has_data"]), None)
        return (min(batch, MAX_BATCH), compare,
                f"this client's {batch_ordinal(batch)} run of this course")
    if session_row["client_company_id"]:
        later = db.query(
            """SELECT 1 FROM course_sessions WHERE client_company_id = ? AND course_id = ? AND id != ?
               AND status != 'Cancelled' AND start_date > ? LIMIT 1""",
            (session_row["client_company_id"], session_row["course_id"], session_row["id"],
             session_row["start_date"]), one=True,
        )
        if later:
            return 1, None, "more runs of this course are booked for this client"
    return None, None, ""


def compare_options(session_row):
    """Earlier classes for this client that have evaluation data - the
    "Compare with" dropdown. Same course first."""
    rows = [r for r in _earlier_classes(session_row) if r["has_data"]]
    return sorted(rows, key=lambda r: (not r["same_course"], r["start_date"]), reverse=False)


def _overall(report):
    combined = (report or {}).get("combined_ratings") or []
    return combined[0] if combined else None


def batch_comparison(session_id, compare_session_id):
    """Exact, code-computed comparison between this class's evaluation and
    an earlier one's (no AI): overall average, share of positive ratings,
    responses, and the criteria that moved most. None if either side has no
    data. The AI only ever words these numbers, it never computes them."""
    if not compare_session_id:
        return None
    cur = training_reports.get_report(session_id)
    prev = training_reports.get_report(compare_session_id)
    if not _has_feedback(cur) or not _has_feedback(prev):
        return None
    out = {"rows": [], "criteria": []}
    c_all, p_all = _overall(cur), _overall(prev)
    if c_all and p_all and c_all.get("scale_max") == p_all.get("scale_max"):
        out["rows"].append({"label": "Average rating", "prev": f"{p_all['average']}/{p_all['scale_max']}",
                            "cur": f"{c_all['average']}/{c_all['scale_max']}",
                            "delta": round(float(c_all["average"]) - float(p_all["average"]), 2), "unit": ""})
        out["rows"].append({"label": "Positive ratings", "prev": f"{p_all['pct_positive']}%",
                            "cur": f"{c_all['pct_positive']}%",
                            "delta": c_all["pct_positive"] - p_all["pct_positive"], "unit": " pts"})
    out["rows"].append({"label": "Responses", "prev": str(prev["response_count"]), "cur": str(cur["response_count"]),
                        "delta": None, "unit": ""})
    prev_avg = {q["question"].strip().lower(): q for q in prev["numeric_summary"] if "average" in q}
    for q in cur["numeric_summary"]:
        if "average" not in q:
            continue
        pq = prev_avg.get(q["question"].strip().lower())
        if pq is None or pq.get("scale_max", 5) != q.get("scale_max", 5):
            continue
        delta = round(float(q["average"]) - float(pq["average"]), 2)
        out["criteria"].append({"question": q["question"], "prev": pq["average"], "cur": q["average"],
                                "scale_max": q.get("scale_max", 5), "delta": delta})
    out["criteria"].sort(key=lambda r: abs(r["delta"]), reverse=True)
    if not out["rows"] and not out["criteria"]:
        return None
    return out


def _comparison_lines(comparison, prev_label, cur_label):
    lines = []
    for r in comparison["rows"]:
        delta = "" if r["delta"] is None else f" (change {r['delta']:+g}{r['unit']})"
        lines.append(f"- {r['label']}: {prev_label} {r['prev']} -> {cur_label} {r['cur']}{delta}")
    moved = [c for c in comparison["criteria"] if abs(c["delta"]) >= 0.1][:4]
    for c in moved:
        lines.append(f"- \"{c['question']}\": {prev_label} {c['prev']}/{c['scale_max']} -> "
                     f"{cur_label} {c['cur']}/{c['scale_max']} (change {c['delta']:+g})")
    return lines


def _earlier_texts_for_client(session_row, column):
    """The same section from up to 3 other reports for this client (sent
    ones first) - shown to the AI so it doesn't keep landing on the same
    wording for a client who gets a report from us every few weeks."""
    if not session_row["client_company_id"]:
        return []
    rows = db.query(
        f"""SELECT ftr.{column} AS text FROM full_training_reports ftr
            JOIN course_sessions cs ON cs.id = ftr.session_id
            WHERE cs.client_company_id = ? AND ftr.session_id != ?
              AND ftr.{column} IS NOT NULL AND ftr.{column} != ''
            ORDER BY ftr.approved_at IS NULL, ftr.approved_at DESC, ftr.id DESC LIMIT 3""",
        (session_row["client_company_id"], session_row["id"]),
    )
    return [r["text"][:900] for r in rows]


def earlier_report_count(session_row):
    if not session_row["client_company_id"]:
        return 0
    row = db.query(
        """SELECT COUNT(*) AS n FROM full_training_reports ftr JOIN course_sessions cs ON cs.id = ftr.session_id
           WHERE cs.client_company_id = ? AND ftr.session_id != ? AND ftr.approved_at IS NOT NULL""",
        (session_row["client_company_id"], session_row["id"]), one=True,
    )
    return row["n"] if row else 0


def _writing_context(session_row, section, batch_no=None, compare_session_id=None):
    """Extra prompt text for one section: the batch, the batch-vs-batch
    numbers (Key Findings / Conclusion only), and this client's earlier
    wording to steer away from. Empty string when there's nothing to add."""
    parts = []
    if batch_no:
        ordinal = batch_ordinal(batch_no)
        if section == "foreword":
            parts.append(f"This report is for the {ordinal} batch (Batch {batch_no}) of this training for "
                         f"this client. Mention that naturally, e.g. \"the {ordinal} batch of ...\".")
        elif section == "objective":
            parts.append(f"This is Batch {batch_no} of this training. You may refer to \"this batch\" once "
                         "if it reads naturally, but keep the focus on what the course equips participants "
                         "to do.")
        else:
            parts.append(f"This report is for Batch {batch_no} (the {ordinal} batch) of this training for "
                         "this client.")
    comparison = batch_comparison(session_row["id"], compare_session_id) if section in (
        "key_findings", "conclusion") else None
    if comparison:
        prev = db.query("SELECT start_date, end_date FROM course_sessions WHERE id = ?",
                        (compare_session_id,), one=True)
        prev_batch = db.query("SELECT batch_no FROM full_training_reports WHERE session_id = ?",
                              (compare_session_id,), one=True)
        prev_label = (f"Batch {prev_batch['batch_no']}" if prev_batch and prev_batch["batch_no"]
                      else f"the earlier class ({fmtdaterange(prev['start_date'], prev['end_date'])})")
        cur_label = f"Batch {batch_no}" if batch_no else "this class"
        lines = _comparison_lines(comparison, prev_label, cur_label)
        if section == "key_findings":
            parts.append(
                f"Exact comparison with {prev_label} (computed, already correct):\n" + "\n".join(lines) + "\n"
                f"Add ONE point labelled \"Compared with {prev_label}\" summarising the most meaningful "
                "change using only these numbers. Be honest: if results dipped, say so constructively. If "
                "the change is tiny (under 0.1 on the average), say results were consistent."
            )
        else:
            parts.append(
                f"For context, compared with {prev_label}:\n" + "\n".join(lines) + "\n"
                "Reflect the overall direction in one natural phrase (e.g. building on the earlier batch, "
                "or results holding steady). No numbers or percentages."
            )
    earlier = _earlier_texts_for_client(session_row, f"{section}_text")
    if earlier:
        quoted = "\n\n".join(f"[Earlier report {i}]\n{t}" for i, t in enumerate(earlier, 1))
        parts.append(
            "This client has already received reports from us with the wording below for this same "
            "section. Write noticeably different wording: a different opening, different sentence "
            "structure, and none of their distinctive phrases. Same professional tone.\n\n" + quoted
        )
    if not parts:
        return ""
    return "\n\nAdditional context:\n" + "\n\n".join(parts) + "\n\nStill reply with ONLY the JSON object."


def _rephrase(session_row, section, current_text, style, instruction, batch_no, compare_session_id):
    """Reworks the section's CURRENT text: same facts, numbers and meaning,
    new wording (plus the chosen style, or the user's own instruction)."""
    label = SECTION_LABELS[section]
    directive = REWRITE_STYLES[style][1] if style != "custom" else (
        f"Follow this instruction from our staff: \"{instruction}\". It can change tone, length, emphasis "
        "or what's mentioned, but never add a fact, number or claim that isn't in the text or context.")
    fmt = ("Keep the format: one point per line, each starting with a short label and a colon. No bullets "
           "or numbering characters." if section == "key_findings" else
           "Keep it as plain paragraphs separated by a blank line.")
    prompt = (
        f"Below is the \"{label}\" section of Modoku Tech Sdn Bhd's post-training evaluation report for "
        f"the course \"{session_row['course_title']}\", which goes to the client company.\n\n"
        f"Current text:\n{current_text}\n\n"
        "Rewrite it. Keep every fact, number, name and the overall meaning exactly as they are. "
        f"{directive} Make it read noticeably different from the current text (different opening and "
        f"sentence structure). {fmt} Plain, professional English; avoid bombastic or obviously "
        "AI-generated language (no \"delve\", \"unparalleled\", \"game-changing\"). "
        "Reply with ONLY a JSON object, nothing else: {\"text\": \"...\"}"
        + _writing_context(session_row, section, batch_no, compare_session_id)
    )
    parsed = training_reports._call_claude_json(prompt, max_tokens=900)
    text = parsed.get("text", "").strip() if isinstance(parsed, dict) else ""
    if section == "key_findings" and text:
        points = [" ".join(p.split()).lstrip("-•· ").strip() for p in text.splitlines()]
        text = "\n".join(p for p in points if p)
    return text or None


def _generate_key_findings(session_row, report, extra=""):
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
        + extra.replace("Still reply with ONLY the JSON object.", "Still reply with ONLY the {\"points\": [...]} JSON object.")
    )
    parsed = training_reports._call_claude_json(prompt, max_tokens=700)
    points = parsed.get("points") if isinstance(parsed, dict) else None
    if not isinstance(points, list):
        return None
    points = [" ".join(str(p).split()).lstrip("-•· ").strip() for p in points]
    points = [p for p in points if p]
    return "\n".join(points) or None


def build_or_refresh_draft(session_id, user_id=None, batch_no=None, compare_session_id=None):
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
    # Fix165: batch/comparison picked on the Generate screen (first draft), or
    # whatever's already saved on the draft.
    if existing is not None and batch_no is None and compare_session_id is None:
        batch_no, compare_session_id = existing["batch_no"], existing["compare_session_id"]

    def ctx(section):
        return _writing_context(session_row, section, batch_no, compare_session_id)

    current = {
        "foreword_text": existing["foreword_text"] if existing else None,
        "objective_text": existing["objective_text"] if existing else None,
        "key_findings_text": existing["key_findings_text"] if existing else None,
        "conclusion_text": existing["conclusion_text"] if existing else None,
    }
    errors = []
    if not current["foreword_text"]:
        try:
            current["foreword_text"] = _generate_foreword(session_row, ctx("foreword"))
        except Exception:  # noqa: BLE001
            current_app.logger.exception("AI foreword generation failed for session %s", session_id)
            errors.append("Foreword")
    if not current["objective_text"]:
        try:
            current["objective_text"] = _generate_objective(session_row, ctx("objective"))
        except Exception:  # noqa: BLE001
            current_app.logger.exception("AI objective generation failed for session %s", session_id)
            errors.append("Objective")
    # Key Findings only when there's feedback to summarize - a class with no
    # evaluation data yet just leaves it empty (not an error; the PDF omits
    # the section), and a later Generate fills it in once data arrives.
    if not current["key_findings_text"] and _has_feedback(report):
        try:
            current["key_findings_text"] = _generate_key_findings(session_row, report, ctx("key_findings"))
            if not current["key_findings_text"]:
                errors.append("Key Findings")
        except Exception:  # noqa: BLE001
            current_app.logger.exception("AI key findings generation failed for session %s", session_id)
            errors.append("Key Findings")
    if not current["conclusion_text"]:
        try:
            current["conclusion_text"] = _generate_conclusion(session_row, report, ctx("conclusion"))
        except Exception:  # noqa: BLE001
            current_app.logger.exception("AI conclusion generation failed for session %s", session_id)
            errors.append("Conclusion")

    db.execute(
        """INSERT INTO full_training_reports (session_id, foreword_text, objective_text, key_findings_text,
                                               conclusion_text, generated_at, generated_by, batch_no,
                                               compare_session_id)
           VALUES (?, ?, ?, ?, ?, datetime('now'), ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
               foreword_text = excluded.foreword_text,
               objective_text = excluded.objective_text,
               key_findings_text = excluded.key_findings_text,
               conclusion_text = excluded.conclusion_text,
               generated_at = datetime('now'),
               generated_by = excluded.generated_by,
               batch_no = excluded.batch_no,
               compare_session_id = excluded.compare_session_id""",
        (session_id, current["foreword_text"], current["objective_text"], current["key_findings_text"],
         current["conclusion_text"], user_id, batch_no, compare_session_id),
    )
    if errors:
        raise FullReportError(
            f"Draft saved, but AI couldn't write the {', '.join(errors)} section"
            f"{'s' if len(errors) > 1 else ''} just now. Edit {'them' if len(errors) > 1 else 'it'} in "
            "by hand below, or try Rephrase > Fresh draft again in a moment."
        )
    return get_full_report(session_id)


def rewrite_section(session_id, section, user_id=None, style="fresh", instruction=None):
    """Regenerates exactly one of the prose sections via AI, discarding
    only that section's current text — the others are left untouched.
    Fix165: `style` "fresh" writes it again from the class data; any other
    REWRITE_STYLES key reworks the current text (same facts, new wording),
    "custom" following the user's own `instruction`. Raises FullReportError
    if locked, unconfigured, or the call fails."""
    if section not in DRAFT_SECTIONS:
        raise FullReportError("Unknown report section.")
    if style not in REWRITE_STYLES:
        style = "fresh"
    instruction = " ".join((instruction or "").split())[:300]
    if style == "custom" and not instruction:
        raise FullReportError("Type an instruction first, e.g. \"mention the hands-on exercises\".")
    if not training_reports.is_ai_configured():
        raise FullReportError("AI drafting isn't set up (ANTHROPIC_API_KEY isn't configured).")
    session_row = _session_context(session_id)
    if session_row is None:
        raise FullReportError("Class not found.")
    existing = get_full_report(session_id)
    if is_locked(existing):
        raise FullReportError("This class's report has already been sent. It can no longer be edited.")

    batch_no = existing["batch_no"] if existing else None
    compare_id = existing["compare_session_id"] if existing else None
    current_text = ((existing[f"{section}_text"] if existing else None) or "").strip()
    if style != "fresh" and not current_text:
        style = "fresh"  # nothing to rework yet - write it from the data instead
    extra = _writing_context(session_row, section, batch_no, compare_id)
    try:
        if style != "fresh":
            text = _rephrase(session_row, section, current_text, style, instruction, batch_no, compare_id)
        elif section == "foreword":
            text = _generate_foreword(session_row, extra)
        elif section == "objective":
            text = _generate_objective(session_row, extra)
        elif section == "key_findings":
            report = training_reports.get_report(session_id)
            if not _has_feedback(report):
                raise FullReportError("No evaluation feedback for this class yet, so there's nothing to "
                                      "summarize into Key Findings. Refresh the Training Report first.")
            text = _generate_key_findings(session_row, report, extra)
        else:
            text = _generate_conclusion(session_row, training_reports.get_report(session_id), extra)
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
        f"Dear {greeting_name or 'Sir/Madam'},\n\n"
        f"Thank you for giving us the opportunity to host the {session_row['course_title']} training for "
        "your team.\n\n"
        "We'd like to share the training evaluation report with you. It includes valuable feedback from "
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
        "batch_no": existing["batch_no"],
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


def _batch_from_form():
    """(batch_no, compare_session_id) from the Batch & Comparison fields."""
    try:
        batch_no = int(request.form.get("batch_no") or 0)
    except ValueError:
        batch_no = 0
    batch_no = batch_no if 1 <= batch_no <= MAX_BATCH else None
    try:
        compare_id = int(request.form.get("compare_session_id") or 0) or None
    except ValueError:
        compare_id = None
    return batch_no, compare_id


def _save_draft_from_form(session_id):
    """Saves all four text boxes exactly as currently on screen (and,
    Fix165, the Batch & Comparison picks)."""
    save_draft(session_id, request.form.get("foreword_text", ""), request.form.get("objective_text", ""),
               request.form.get("conclusion_text", ""),
               key_findings_text=request.form.get("key_findings_text", ""))
    if "batch_no" in request.form:
        batch_no, compare_id = _batch_from_form()
        db.execute("UPDATE full_training_reports SET batch_no = ?, compare_session_id = ? WHERE session_id = ?",
                   (batch_no, compare_id, session_id))


# --- Routes --------------------------------------------------------------

@bp.route("/<int:session_id>/full")
@login_required
def edit(session_id):
    session_row = _session_context(session_id)
    if session_row is None:
        flash("Class not found.", "danger")
        return redirect(url_for("training_reports.index"))
    full_report = get_full_report(session_id)
    suggested_batch, suggested_compare, batch_reason = batch_suggestion(session_row)
    if full_report is not None:
        batch_no, compare_id = full_report["batch_no"], full_report["compare_session_id"]
    else:
        batch_no, compare_id = suggested_batch, suggested_compare
    return render_template(
        "full_reports/edit.html", s=session_row, full_report=full_report, locked=is_locked(full_report),
        batch_no=batch_no, compare_id=compare_id, suggested_batch=suggested_batch, batch_reason=batch_reason,
        compare_options=compare_options(session_row), comparison=batch_comparison(session_id, compare_id),
        earlier_reports=earlier_report_count(session_row), max_batch=MAX_BATCH,
        rewrite_styles=REWRITE_STYLES, batch_ordinal=batch_ordinal,
        ai_configured=training_reports.is_ai_configured(),
        default_email_subject=_default_full_report_email_subject(session_row),
        default_email_body=_default_full_report_email_body(session_row),
    )


@bp.route("/<int:session_id>/full/generate", methods=("POST",))
@login_required
def generate(session_id):
    try:
        batch_no, compare_id = _batch_from_form() if "batch_no" in request.form else (None, None)
        build_or_refresh_draft(session_id, user_id=g.user["id"] if g.user else None,
                               batch_no=batch_no, compare_session_id=compare_id)
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
        style = request.args.get("style", "fresh")
        rewrite_section(session_id, section, user_id=g.user["id"] if g.user else None, style=style,
                        instruction=request.form.get(f"instruction_{section}"))
    except FullReportError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("full_reports.edit", session_id=session_id, _anchor=f"sec-{section}"))
    how = REWRITE_STYLES.get(request.args.get("style", "fresh"), REWRITE_STYLES["fresh"])[0].lower()
    flash(f"{SECTION_LABELS.get(section, section.title())} rewritten by AI ({how}).", "success")
    return redirect(url_for("full_reports.edit", session_id=session_id, _anchor=f"sec-{section}"))


@bp.route("/<int:session_id>/full/rewrite-all", methods=("POST",))
@login_required
def rewrite_all(session_id):
    """Fix165: after changing the Batch / Compare with picks, write every
    section again with that context (one click instead of four)."""
    try:
        _save_draft_from_form(session_id)
    except FullReportError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("full_reports.edit", session_id=session_id))
    failed = []
    has_feedback = _has_feedback(training_reports.get_report(session_id))
    for section in DRAFT_SECTIONS:
        if section == "key_findings" and not has_feedback:
            continue  # nothing to summarise yet - the PDF leaves the section out
        try:
            rewrite_section(session_id, section, user_id=g.user["id"] if g.user else None)
        except FullReportError:
            failed.append(SECTION_LABELS[section])
    if failed:
        flash(f"Rewrote the rest, but couldn't rewrite {', '.join(failed)} just now. Try again in a moment.",
              "warning")
    else:
        flash("All sections rewritten with the batch details.", "success")
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
        "batch_no": full_report["batch_no"],
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
