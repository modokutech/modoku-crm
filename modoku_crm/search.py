"""Global record search behind the command palette (Ctrl+/ or Cmd+/).

The palette in base.html has always searched the sidebar *pages*; this
module adds the other half — the actual records — so typing "Petronas"
jumps straight to that client, and "INV-0042" straight to that invoice,
instead of landing on the list page and searching again.

Deliberately one small read-only JSON endpoint rather than a full search
page: the palette is the only consumer, results are capped tight (a
handful per type), and every query is a plain LIKE against columns that
are already indexed or small enough not to care. No AI, no ranking model
— matches that START with what you typed sort above matches that merely
contain it, and that's the whole ranking story.

Respects the same visibility rules as the rest of the app: a record type
whose module is switched off under Settings (Invoices, Purchase Orders,
Quotations) is skipped entirely, so the palette never offers a link to a
page that would just bounce the user back to the dashboard.
"""
from flask import Blueprint, g, jsonify, request, url_for

from . import db
from .auth import login_required

bp = Blueprint("search", __name__, url_prefix="/search")

# Per-type and overall caps. The palette shows a short list — anything
# longer is faster to find from the type's own list page with its filters.
PER_TYPE_LIMIT = 5
TOTAL_LIMIT = 20
# Below this, a query is too broad to be a useful record search ("a"
# matches half the database); the palette still shows page matches.
MIN_QUERY_LENGTH = 2


def _like(term):
    """Escapes LIKE wildcards in user input so a literal % or _ typed into
    the palette searches for that character instead of matching everything.
    Pair with ESCAPE '\\' in the SQL."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _digits(value):
    return "".join(ch for ch in (value or "") if ch.isdigit())


def _rank(label, query):
    """0 for a label that starts with the query, 1 for one that merely
    contains it — so exact-ish matches float to the top of the merged
    list regardless of which type they came from."""
    return 0 if (label or "").lower().startswith(query.lower()) else 1


def _companies(term):
    rows = db.query(
        "SELECT id, name, city FROM companies WHERE name LIKE ? ESCAPE '\\' "
        "ORDER BY name COLLATE NOCASE LIMIT ?",
        (_like(term), PER_TYPE_LIMIT),
    )
    return [{
        "type": "Client", "icon": "bi-building", "label": r["name"],
        "sub": r["city"] or "", "href": url_for("companies.view", company_id=r["id"]),
    } for r in rows]


def _leads(term):
    rows = db.query(
        """SELECT l.id, l.name, l.status, l.email, co.name AS company_name
           FROM leads l LEFT JOIN companies co ON co.id = l.company_id
           WHERE l.name LIKE ? ESCAPE '\\' OR l.email LIKE ? ESCAPE '\\'
              OR co.name LIKE ? ESCAPE '\\'
           ORDER BY l.name COLLATE NOCASE LIMIT ?""",
        (_like(term), _like(term), _like(term), PER_TYPE_LIMIT),
    )
    return [{
        "type": "Lead", "icon": "bi-person-lines-fill", "label": r["name"],
        "sub": " · ".join(p for p in (r["company_name"], r["status"]) if p),
        "href": url_for("leads.view", lead_id=r["id"]),
    } for r in rows]


def _courses(term):
    rows = db.query(
        "SELECT id, title, code, category FROM courses "
        "WHERE title LIKE ? ESCAPE '\\' OR code LIKE ? ESCAPE '\\' "
        "ORDER BY title COLLATE NOCASE LIMIT ?",
        (_like(term), _like(term), PER_TYPE_LIMIT),
    )
    return [{
        "type": "Course", "icon": "bi-journal-text", "label": r["title"],
        "sub": " · ".join(p for p in (r["code"], r["category"]) if p),
        "href": url_for("courses.view", course_id=r["id"]),
    } for r in rows]


def _trainers(term):
    rows = db.query(
        "SELECT id, name, email, specialization FROM trainers "
        "WHERE name LIKE ? ESCAPE '\\' OR email LIKE ? ESCAPE '\\' "
        "OR specialization LIKE ? ESCAPE '\\' "
        "ORDER BY name COLLATE NOCASE LIMIT ?",
        (_like(term), _like(term), _like(term), PER_TYPE_LIMIT),
    )
    return [{
        "type": "Trainer", "icon": "bi-person-badge", "label": r["name"],
        "sub": r["specialization"] or r["email"] or "",
        "href": url_for("trainers.view", trainer_id=r["id"]),
    } for r in rows]


def _sessions(term):
    rows = db.query(
        """SELECT cs.id, cs.start_date, cs.venue, cs.status, cs.session_code,
                  c.title AS course_title
           FROM course_sessions cs JOIN courses c ON c.id = cs.course_id
           WHERE c.title LIKE ? ESCAPE '\\' OR cs.venue LIKE ? ESCAPE '\\'
              OR cs.session_code LIKE ? ESCAPE '\\'
           ORDER BY cs.start_date DESC LIMIT ?""",
        (_like(term), _like(term), _like(term), PER_TYPE_LIMIT),
    )
    return [{
        "type": "Class", "icon": "bi-calendar-event", "label": r["course_title"],
        "sub": " · ".join(p for p in (r["start_date"], r["venue"], r["status"]) if p),
        "href": url_for("sessions.view", session_id=r["id"]),
    } for r in rows]


def _quotations(term):
    rows = db.query(
        """SELECT q.id, q.quote_no, q.status, q.course_title,
                  COALESCE(co.name, q.company_name_override) AS client
           FROM quotations q LEFT JOIN companies co ON co.id = q.client_company_id
           WHERE q.quote_no LIKE ? ESCAPE '\\' OR q.course_title LIKE ? ESCAPE '\\'
              OR co.name LIKE ? ESCAPE '\\' OR q.company_name_override LIKE ? ESCAPE '\\'
           ORDER BY q.quote_date DESC LIMIT ?""",
        (_like(term), _like(term), _like(term), _like(term), PER_TYPE_LIMIT),
    )
    return [{
        "type": "Quotation", "icon": "bi-file-earmark-ruled", "label": r["quote_no"],
        "sub": " · ".join(p for p in (r["client"], r["status"]) if p),
        "href": url_for("quotations.view", quotation_id=r["id"]),
    } for r in rows]


def _invoices(term):
    rows = db.query(
        """SELECT i.id, i.invoice_no, i.status, i.total,
                  COALESCE(co.name, i.bill_to_name) AS client
           FROM invoices i LEFT JOIN companies co ON co.id = i.company_id
           WHERE i.invoice_no LIKE ? ESCAPE '\\' OR co.name LIKE ? ESCAPE '\\'
              OR i.bill_to_name LIKE ? ESCAPE '\\'
           ORDER BY i.invoice_date DESC LIMIT ?""",
        (_like(term), _like(term), _like(term), PER_TYPE_LIMIT),
    )
    return [{
        "type": "Invoice", "icon": "bi-receipt", "label": r["invoice_no"],
        "sub": " · ".join(p for p in (r["client"], r["status"]) if p),
        "href": url_for("invoices.view", invoice_id=r["id"]),
    } for r in rows]


def _purchase_orders(term):
    rows = db.query(
        """SELECT po.id, po.po_no, po.status, t.name AS trainer_name
           FROM purchase_orders po LEFT JOIN trainers t ON t.id = po.trainer_id
           WHERE po.po_no LIKE ? ESCAPE '\\' OR t.name LIKE ? ESCAPE '\\'
           ORDER BY po.issue_date DESC LIMIT ?""",
        (_like(term), _like(term), PER_TYPE_LIMIT),
    )
    return [{
        "type": "Purchase Order", "icon": "bi-cart-check", "label": r["po_no"],
        "sub": " · ".join(p for p in (r["trainer_name"], r["status"]) if p),
        "href": url_for("purchase_orders.view", po_id=r["id"]),
    } for r in rows]


def _participants(term):
    """Participants are searched by name AND by IC — the IC digits-only so
    an IC typed with or without dashes finds the same person either way
    (the same normalization certificates.py uses for the public claim
    flow). A participant has no page of their own, so each result opens
    the T3 participant list of the class they're on, which is where you'd
    act on them."""
    digits = _digits(term)
    args = [_like(term)]
    ic_clause = ""
    if len(digits) >= 4:
        ic_clause = (
            " OR REPLACE(REPLACE(REPLACE(COALESCE(e.ic_no, ''), '-', ''), ' ', ''), '.', '') "
            "LIKE ? ESCAPE '\\'"
        )
        args.append(_like(digits))
    args.append(PER_TYPE_LIMIT)
    rows = db.query(
        f"""SELECT e.id, e.participant_name, e.ic_no, e.session_id,
                   c.title AS course_title, cs.start_date
            FROM enrollments e
            JOIN course_sessions cs ON cs.id = e.session_id
            JOIN courses c ON c.id = cs.course_id
            WHERE e.participant_name LIKE ? ESCAPE '\\'{ic_clause}
            ORDER BY cs.start_date DESC LIMIT ?""",
        args,
    )
    return [{
        "type": "Participant", "icon": "bi-people", "label": r["participant_name"],
        "sub": " · ".join(p for p in (r["course_title"], r["start_date"]) if p),
        "href": url_for("t3.manage", session_id=r["session_id"]),
    } for r in rows]


def collect(term):
    """Every matching record across the app, best matches first, capped.
    Module-gated types are skipped when their module is switched off, so
    the palette never links somewhere the user would just get bounced
    from."""
    modules = getattr(g, "modules", {}) or {}
    groups = [_companies, _leads, _courses, _trainers, _sessions, _participants]
    if modules.get("quotations", True):
        groups.append(_quotations)
    if modules.get("invoices", True):
        groups.append(_invoices)
    if modules.get("purchase_orders", True):
        groups.append(_purchase_orders)

    results = []
    for fn in groups:
        try:
            results.extend(fn(term))
        except Exception:  # noqa: BLE001 - one bad table must never break the whole palette
            continue
    results.sort(key=lambda item: (_rank(item["label"], term), item["label"].lower()))
    return results[:TOTAL_LIMIT]


@bp.route("/records")
@login_required
def records():
    term = (request.args.get("q") or "").strip()
    if len(term) < MIN_QUERY_LENGTH:
        return jsonify({"results": []})
    return jsonify({"results": collect(term)})
