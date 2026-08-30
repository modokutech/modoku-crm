from flask import Blueprint, render_template, request

from . import db
from .auth import login_required
from . import trainer_utilization

bp = Blueprint("reports", __name__, url_prefix="/reports")


@bp.route("/")
@login_required
def index():
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")
    date_filter = ""
    args = []
    if date_from:
        date_filter += " AND date(a.created_at) >= date(?)"
        args.append(date_from)
    if date_to:
        date_filter += " AND date(a.created_at) <= date(?)"
        args.append(date_to)

    # Pipeline by status
    pipeline = db.query("SELECT status, COUNT(*) c FROM leads GROUP BY status")

    # Per-salesperson performance: leads owned, won, lost, calls made, emails/proposals sent
    salesperson_rows = db.query(
        f"""SELECT u.id, u.name,
                COUNT(DISTINCT l.id) AS total_leads,
                SUM(CASE WHEN l.status = 'Deal Closed' THEN 1 ELSE 0 END) AS won,
                SUM(CASE WHEN l.status = 'Lost' THEN 1 ELSE 0 END) AS lost,
                SUM(CASE WHEN l.status NOT IN ('Deal Closed','Lost') THEN 1 ELSE 0 END) AS open_leads,
                (SELECT COUNT(*) FROM lead_activities a
                    JOIN leads l2 ON l2.id = a.lead_id
                    WHERE l2.assigned_to = u.id AND a.activity_type = 'Call' {date_filter}) AS calls_made,
                (SELECT COUNT(*) FROM lead_activities a
                    JOIN leads l2 ON l2.id = a.lead_id
                    WHERE l2.assigned_to = u.id AND a.activity_type = 'Email' {date_filter}) AS emails_sent
           FROM users u
           LEFT JOIN leads l ON l.assigned_to = u.id
           WHERE u.active = 1
           GROUP BY u.id, u.name
           ORDER BY total_leads DESC""",
        args + args,
    )

    unassigned_open = db.query(
        "SELECT COUNT(*) c FROM leads WHERE assigned_to IS NULL AND status NOT IN ('Deal Closed','Lost')",
        one=True,
    )["c"]

    # Revenue / invoicing snapshot
    revenue = db.query(
        """SELECT status, COUNT(*) c, COALESCE(SUM(total),0) amt FROM invoices GROUP BY status"""
    )
    revenue_by_month = db.query(
        """SELECT strftime('%Y-%m', invoice_date) ym, COALESCE(SUM(total),0) amt
           FROM invoices GROUP BY ym ORDER BY ym DESC LIMIT 6"""
    )

    # Course popularity
    course_popularity = db.query(
        """SELECT c.title, COUNT(e.id) enrolled
           FROM courses c
           LEFT JOIN course_sessions cs ON cs.course_id = c.id
           LEFT JOIN enrollments e ON e.session_id = cs.id
           GROUP BY c.id, c.title ORDER BY enrolled DESC LIMIT 8"""
    )

    total_leads = db.query("SELECT COUNT(*) c FROM leads", one=True)["c"]
    total_won = db.query("SELECT COUNT(*) c FROM leads WHERE status='Deal Closed'", one=True)["c"]
    conversion_rate = round((total_won / total_leads * 100), 1) if total_leads else 0

    # Trainer utilization snapshot (top 5 by amount paid) — full breakdown
    # and per-trainer detail live under the Trainer Utilization module;
    # this is just a quick pointer from the general Reports page.
    top_trainers = trainer_utilization._aggregate_by_trainer(trainer_utilization._confirmed_po_rows(), "amount", "desc")[:5]

    return render_template(
        "reports/index.html",
        pipeline={row["status"]: row["c"] for row in pipeline},
        salesperson_rows=salesperson_rows,
        unassigned_open=unassigned_open,
        revenue=revenue,
        revenue_by_month=revenue_by_month,
        course_popularity=course_popularity,
        total_leads=total_leads,
        total_won=total_won,
        conversion_rate=conversion_rate,
        date_from=date_from,
        date_to=date_to,
        top_trainers=top_trainers,
    )
