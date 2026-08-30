from datetime import date

from flask import Blueprint, render_template, url_for

from . import db, fmtmoney
from . import sessions as _sessions
from .auth import login_required

bp = Blueprint("dashboard", __name__)


def _days_between(earlier, later):
    """later - earlier, in whole calendar days, from ISO date/datetime
    strings (only the date part matters — a bare date and a full
    'YYYY-MM-DD HH:MM:SS' timestamp both work). Computed in Python rather
    than SQL's julianday(), which mixes a fractional time-of-day into
    'now' and quietly off-by-ones a same-day comparison — this matches the
    plain date.today() arithmetic sessions.py's own nudge functions use, so
    the two never disagree."""
    return (date.fromisoformat(later[:10]) - date.fromisoformat(earlier[:10])).days


def _attention_items():
    """The "Needs Your Attention" panel — a handful of concrete, itemized
    things across the app that are genuinely due for a look right now,
    pulled straight from data already in the database. Deliberately reuses
    the exact same signals (and constants) that already drive the
    automatic Notification nudges — quotations._auto_advance_quotation_statuses,
    invoices._auto_mark_overdue_invoices, sessions._notify_pending_grant_docs,
    sessions._notify_overdue_evaluation_reports — so this list and your
    notifications inbox never disagree with each other. No AI involved:
    pure SQL + date arithmetic against data you already store. Each
    section is capped to a handful of items (most urgent first); returns
    [] entirely once nothing needs attention."""
    today_iso = date.today().isoformat()
    sections = []

    followups = db.query(
        "SELECT id, quote_no, sent_at FROM quotations WHERE status = 'Follow-up' "
        "ORDER BY sent_at ASC LIMIT 5"
    )
    if followups:
        sections.append({
            "label": "Quotations gone quiet", "icon": "bi-file-earmark-ruled", "tone": "warning",
            "entries": [{
                "text": f"{q['quote_no']} — sent {_days_between(q['sent_at'], today_iso)} day(s) ago, no response yet",
                "link": url_for("quotations.view", quotation_id=q["id"]),
            } for q in followups],
        })

    overdue_invoices = db.query(
        "SELECT id, invoice_no, total, due_date FROM invoices WHERE status = 'Overdue' "
        "ORDER BY due_date ASC LIMIT 5"
    )
    if overdue_invoices:
        sections.append({
            "label": "Overdue invoices", "icon": "bi-receipt", "tone": "danger",
            "entries": [{
                "text": (f"{inv['invoice_no']} — RM {fmtmoney(inv['total'])}, "
                         f"{_days_between(inv['due_date'], today_iso)} day(s) overdue"),
                "link": url_for("invoices.view", invoice_id=inv["id"]),
            } for inv in overdue_invoices],
        })

    grant_docs_cutoff = date.today().toordinal() + _sessions.GRANT_DOCS_LEAD_DAYS
    grant_docs_rows = [
        row for row in db.query(
            """SELECT cs.id, c.title AS course_title, cs.start_date
               FROM course_sessions cs JOIN courses c ON c.id = cs.course_id
               WHERE cs.status != 'Cancelled' AND (cs.grant_docs_sent_at IS NULL OR cs.grant_docs_sent_at = '')
                 AND cs.start_date IS NOT NULL AND cs.start_date != ''
               ORDER BY cs.start_date ASC"""
        )
        if date.fromisoformat(row["start_date"][:10]).toordinal() <= grant_docs_cutoff
    ][:5]
    if grant_docs_rows:
        sections.append({
            "label": "Grant Documents not sent", "icon": "bi-award", "tone": "danger",
            "entries": [{
                "text": (lambda days_left: (
                    f"{row['course_title']} — training in {days_left} day(s)" if days_left >= 0
                    else f"{row['course_title']} — training already started, still not sent"
                ))(_days_between(today_iso, row["start_date"])),
                "link": url_for("sessions.view", session_id=row["id"]),
            } for row in grant_docs_rows],
        })

    eval_cutoff_days = _sessions.EVALUATION_REPORT_REMINDER_AFTER_DAYS
    eval_rows = [
        row for row in db.query(
            """SELECT cs.id, c.title AS course_title, COALESCE(cs.end_date, cs.start_date) AS done_date
               FROM course_sessions cs JOIN courses c ON c.id = cs.course_id
               WHERE cs.status = 'Completed' AND (cs.evaluation_report_file IS NULL OR cs.evaluation_report_file = '')
               ORDER BY cs.end_date ASC"""
        )
        if row["done_date"] and _days_between(row["done_date"], today_iso) >= eval_cutoff_days
    ][:5]
    if eval_rows:
        sections.append({
            "label": "Evaluation reports overdue", "icon": "bi-patch-check", "tone": "warning",
            "entries": [{
                "text": (f"{row['course_title']} — completed {_days_between(row['done_date'], today_iso)} "
                         f"day(s) ago, report still missing"),
                "link": url_for("sessions.view", session_id=row["id"]),
            } for row in eval_rows],
        })

    return sections


@bp.route("/dashboard")
@login_required
def index():
    lead_counts = db.query(
        "SELECT status, COUNT(*) c FROM leads GROUP BY status"
    )
    total_leads = db.query("SELECT COUNT(*) c FROM leads", one=True)["c"]
    open_leads = db.query(
        "SELECT COUNT(*) c FROM leads WHERE status NOT IN ('Deal Closed','Lost')", one=True
    )["c"]

    upcoming_sessions = db.query(
        """SELECT cs.*, c.title AS course_title, c.code AS course_code,
                  t.name AS trainer_name,
                  (SELECT COUNT(*) FROM enrollments e WHERE e.session_id = cs.id) AS enrolled_count
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN trainers t ON t.id = cs.trainer_id
           WHERE date(cs.start_date) >= date('now') AND cs.status != 'Cancelled'
           ORDER BY cs.start_date ASC LIMIT 6"""
    )

    outstanding = db.query(
        """SELECT COALESCE(SUM(total),0) AS amt, COUNT(*) AS cnt FROM invoices
           WHERE status IN ('Sent','Overdue')""", one=True
    )
    # Invoices auto-flip to 'Overdue' once their due date passes (see
    # invoices._auto_mark_overdue_invoices), so this is now a direct status
    # count rather than a manual date comparison.
    overdue = db.query("SELECT COUNT(*) c FROM invoices WHERE status = 'Overdue'", one=True)["c"]

    pending_quotations = db.query(
        "SELECT COUNT(*) c FROM quotations WHERE status IN ('Sent', 'Follow-up')", one=True
    )["c"]

    recent_leads = db.query(
        """SELECT l.*, co.name AS company_name FROM leads l
           LEFT JOIN companies co ON co.id = l.company_id
           ORDER BY l.created_at DESC LIMIT 6"""
    )

    attention_sections = _attention_items()

    return render_template(
        "dashboard.html",
        lead_counts={row["status"]: row["c"] for row in lead_counts},
        total_leads=total_leads,
        attention_sections=attention_sections,
        open_leads=open_leads,
        upcoming_sessions=upcoming_sessions,
        outstanding=outstanding,
        overdue=overdue,
        pending_quotations=pending_quotations,
        recent_leads=recent_leads,
    )
