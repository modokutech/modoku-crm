import re
from datetime import date, datetime, timedelta

from flask import Flask, g, redirect, render_template, url_for
from markupsafe import Markup, escape

from . import db as db_module


def linelist(text, ordered=False):
    """Renders free text as a proper <ul>/<ol> list — one <li> per line.
    Accepts newline-separated text (the current format); for backward
    compatibility with older records saved with manual '<br>' separators
    and '•'/'1.' prefixes, falls back to splitting on '<br>' and strips any
    leading bullet/number the user typed manually."""
    if not text:
        return ""
    raw_lines = text.split("\n") if "\n" in text else re.split(r"<br\s*/?>", text)
    lines = []
    for line in raw_lines:
        line = line.strip()
        line = re.sub(r"^[•\-\*]\s*", "", line)
        line = re.sub(r"^\d+[\.\)]\s*", "", line)
        if line:
            lines.append(line)
    if not lines:
        return ""
    tag = "ol" if ordered else "ul"
    items = "".join(f"<li>{escape(l)}</li>" for l in lines)
    return Markup(f"<{tag} class='doc-list'>{items}</{tag}>")


def fmtdate(value, with_time=False):
    """Render an ISO date/datetime string (or date object) as '23 Aug 2026'
    (or '23 Aug 2026, 3:45 PM' if with_time)."""
    if not value:
        return "-"
    if isinstance(value, (date, datetime)):
        dt = value
    else:
        text = str(value).strip()
        try:
            # Handles 'YYYY-MM-DD' and 'YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DDTHH:MM:SS'
            dt = datetime.fromisoformat(text.replace(" ", "T", 1) if "T" not in text and len(text) > 10 else text)
        except ValueError:
            try:
                dt = datetime.strptime(text[:10], "%Y-%m-%d")
            except ValueError:
                return text
    if with_time and isinstance(dt, datetime) and (dt.hour or dt.minute):
        return dt.strftime("%-d %b %Y, %-I:%M %p")
    return dt.strftime("%-d %b %Y")


def _parse_date_loose(value):
    if not value:
        return None
    if isinstance(value, (date, datetime)):
        return value.date() if isinstance(value, datetime) else value
    text = str(value).strip()
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def fmtdaterange(start_value, end_value=None):
    """Renders a class's start/end date as a single human-readable string —
    a plain date for a one-day class, otherwise the full span so a 2-day
    class reads '10 & 11 Aug 2026' rather than just the start date."""
    start = _parse_date_loose(start_value)
    if start is None:
        return fmtdate(start_value)
    end = _parse_date_loose(end_value)
    if end is None or end == start:
        return fmtdate(start)

    same_year = start.year == end.year
    same_month = same_year and start.month == end.month
    num_days = (end - start).days + 1

    if same_month:
        start_part = f"{start.day}"
    elif same_year:
        start_part = start.strftime("%-d %b")
    else:
        start_part = start.strftime("%-d %b %Y")
    end_part = fmtdate(end)

    joiner = " & " if num_days == 2 else " – "
    return f"{start_part}{joiner}{end_part}"


def fmtmoney(value):
    """Renders a price/amount with a thousand-separator comma — '1750' or
    '1750.0' both become '1,750'. A whole-number amount drops the trailing
    '.00' entirely ('21000.00' -> '21,000'); an amount with real cents
    keeps them ('21000.10' -> '21,000.10'). None/blank is treated as 0 so
    callers don't need an `or 0` guard everywhere.

    This only affects on-screen HTML pages — the PDF generator (pdfgen.py)
    formats its own amounts directly and never calls this filter, so
    invoices/quotations/POs always show the full '.00' regardless."""
    try:
        amount = float(value) if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        return str(value)
    text = f"{amount:,.2f}"
    if text.endswith(".00"):
        text = text[:-3]
    return text


def fmtdays(value):
    """Renders a course duration as '1 day' / '2.5 days' — singular only
    for exactly 1, trailing '.0' dropped ('1.0' -> '1', '2.0' -> '2 days'),
    a fractional value like 1.5 kept as-is."""
    try:
        amount = float(value) if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        return f"{value} day(s)"
    text = f"{amount:g}"  # '1.0' -> '1', '2.5' -> '2.5'
    label = "day" if amount == 1 else "days"
    return f"{text} {label}"


def create_app(config_object="config.Config"):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object)
    app.permanent_session_lifetime = timedelta(hours=24)

    if app.config.get("SECRET_KEY") == "change-this-secret-key-in-production" and not app.testing:
        app.logger.warning(
            "SECRET_KEY is still the default placeholder — sessions are not secure. "
            "Set a random SECRET_KEY environment variable before going live."
        )

    db_module.register(app)
    db_module.init_db(app)

    app.jinja_env.filters["fmtdate"] = fmtdate
    app.jinja_env.filters["fmtdatetime"] = lambda v: fmtdate(v, with_time=True)
    app.jinja_env.filters["fmtdaterange"] = fmtdaterange
    app.jinja_env.filters["linelist"] = linelist
    app.jinja_env.filters["fmtmoney"] = fmtmoney
    app.jinja_env.filters["fmtdays"] = fmtdays

    from .sessions import split_training_time as _split_training_time
    app.jinja_env.filters["_split_training_time"] = _split_training_time

    from . import auth
    from . import dashboard
    from . import leads
    from . import companies
    from . import hotels
    from . import vendors
    from . import trainers
    from . import trainer_utilization
    from . import courses
    from . import sessions
    from . import enrollments
    from . import invoices
    from . import users
    from . import reports
    from . import purchase_orders
    from . import quotations
    from . import t3
    from . import profile
    from . import admin_logs
    from . import attendance_return
    from . import quotation_return
    from . import t3_public
    from . import certificates
    from . import cert_admin
    from . import po_confirm
    from . import vendor_purchase_orders
    from . import vendor_po_confirm
    from . import vendor_invoice
    from . import hrdcorp_grant
    from . import calendar_integration
    from . import settings as settings_module
    from . import security
    from . import trainer_invoice
    from . import training_costs
    from . import sales_performance
    from . import notifications
    from . import tutorial
    from . import export
    from . import heatmap
    from . import claims
    from . import payment_receipts
    from . import library
    from . import jd14_return
    from . import guide

    app.register_blueprint(auth.bp)
    app.register_blueprint(dashboard.bp)
    app.register_blueprint(leads.bp)
    app.register_blueprint(companies.bp)
    app.register_blueprint(hotels.bp)
    app.register_blueprint(vendors.bp)
    app.register_blueprint(trainers.bp)
    app.register_blueprint(trainer_utilization.bp)
    app.register_blueprint(courses.bp)
    app.register_blueprint(sessions.bp)
    app.register_blueprint(enrollments.bp)
    app.register_blueprint(invoices.bp)
    app.register_blueprint(users.bp)
    app.register_blueprint(reports.bp)
    app.register_blueprint(purchase_orders.bp)
    app.register_blueprint(quotations.bp)
    app.register_blueprint(t3.bp)
    app.register_blueprint(profile.bp)
    app.register_blueprint(admin_logs.bp)
    app.register_blueprint(attendance_return.bp)
    app.register_blueprint(quotation_return.bp)
    app.register_blueprint(t3_public.bp)
    app.register_blueprint(certificates.bp)
    app.register_blueprint(cert_admin.bp)
    app.register_blueprint(po_confirm.bp)
    app.register_blueprint(vendor_purchase_orders.bp)
    app.register_blueprint(vendor_po_confirm.bp)
    app.register_blueprint(vendor_invoice.bp)
    app.register_blueprint(hrdcorp_grant.bp)
    app.register_blueprint(jd14_return.bp)
    app.register_blueprint(guide.bp)
    app.register_blueprint(calendar_integration.bp)
    app.register_blueprint(settings_module.bp)
    app.register_blueprint(trainer_invoice.bp)
    app.register_blueprint(training_costs.bp)
    app.register_blueprint(sales_performance.bp)
    app.register_blueprint(notifications.bp)
    app.register_blueprint(tutorial.bp)
    app.register_blueprint(export.bp)
    app.register_blueprint(heatmap.bp)
    app.register_blueprint(claims.bp)
    app.register_blueprint(payment_receipts.bp)
    app.register_blueprint(library.bp)

    security.init_app(app)

    # Global error pages — every uncaught 400/403/404/413/500 renders a
    # friendly branded page (matching the sidebar layout when the visitor is
    # logged in, a plain centered page otherwise) instead of a raw
    # Werkzeug/Flask traceback or default error page. 500s are logged with
    # a full traceback so real bugs are still visible in the server logs.
    def _error_page(code, heading, message):
        return render_template("errors/error.html", code=code, heading=heading, message=message), code

    @app.errorhandler(400)
    def _handle_400(e):
        return _error_page(400, "Bad Request",
                            getattr(e, "description", None)
                            or "We couldn't process that request. Please go back and try again.")

    @app.errorhandler(403)
    def _handle_403(e):
        return _error_page(403, "Access Denied", "You don't have permission to view this page.")

    @app.errorhandler(404)
    def _handle_404(e):
        return _error_page(404, "Page Not Found",
                            "The page you're looking for doesn't exist or may have been moved.")

    @app.errorhandler(413)
    def _handle_413(e):
        return _error_page(413, "File Too Large",
                            "The file (or files) you tried to upload exceed the maximum allowed size.")

    @app.errorhandler(500)
    def _handle_500(e):
        app.logger.exception("Unhandled server error")
        return _error_page(500, "Something Went Wrong",
                            "An unexpected error occurred on our end. It's been logged — please try "
                            "again, and let us know if it keeps happening.")

    @app.route("/")
    def root():
        return redirect(url_for("dashboard.index"))

    @app.route("/sw.js")
    def service_worker():
        # Served from the root (not /static/sw.js) so its default scope is
        # "/" and it can control real app pages, not just /static/* assets.
        # See static/sw.js for what it actually caches.
        from flask import send_from_directory
        response = send_from_directory(app.static_folder, "sw.js")
        response.headers["Service-Worker-Allowed"] = "/"
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.before_request
    def load_module_flags():
        g.modules = settings_module.get_module_flags()

    @app.context_processor
    def inject_globals():
        from . import notifications as notifications_module
        unread = notifications_module.unread_count(g.user["id"]) if getattr(g, "user", None) else 0
        return {
            "company_name": app.config.get("COMPANY_NAME", "Modoku Tech"),
            "now_date": date.today().isoformat(),
            "modules": getattr(g, "modules", {}),
            "unread_notifications": unread,
        }

    return app
