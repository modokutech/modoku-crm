"""Toggleable modules — lets an admin switch off parts of Modoku Hub the
business isn't using yet (e.g. Invoices, Purchase Orders) without touching
any data. A disabled module's sidebar link disappears and its routes
redirect back to the dashboard with a flash message, for every user
(including admins — re-enable it from Settings to get back in).
"""

from functools import wraps

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from . import db, mailer
from .auth import admin_required, login_required

bp = Blueprint("settings", __name__, url_prefix="/settings")

# key -> (display label, default enabled)
MODULES = {
    "invoices": {"label": "Invoices", "default": True},
    "purchase_orders": {"label": "Purchase Orders", "default": True},
    "quotations": {"label": "Quotations", "default": True},
}

NOTIFICATION_EMAILS_KEY = "notification_emails"
# The addresses Modoku Hub notified before this was configurable — kept as
# the default so an admin who never visits this page keeps getting the same
# notifications they always have.
DEFAULT_NOTIFICATION_EMAILS = "eriktajudin@modoku.tech, hello@modoku.tech"

# Document numbering — prefix/suffix are persistent settings; the "next
# number" override is a one-time reset (consumed the moment it's used to
# generate a number, then normal auto-increment off existing rows resumes).
PO_PREFIX_KEY = "po_number_prefix"
PO_SUFFIX_KEY = "po_number_suffix"
PO_OVERRIDE_KEY = "po_number_next_override"
INVOICE_PREFIX_KEY = "invoice_number_prefix"
INVOICE_SUFFIX_KEY = "invoice_number_suffix"
INVOICE_OVERRIDE_KEY = "invoice_number_next_override"
DEFAULT_PO_PREFIX = "PO"
DEFAULT_INVOICE_PREFIX = "INV"


def get_po_number_prefix():
    return db.get_setting(PO_PREFIX_KEY, DEFAULT_PO_PREFIX) or DEFAULT_PO_PREFIX


def get_po_number_suffix():
    return db.get_setting(PO_SUFFIX_KEY, "") or ""


def get_invoice_number_prefix():
    return db.get_setting(INVOICE_PREFIX_KEY, DEFAULT_INVOICE_PREFIX) or DEFAULT_INVOICE_PREFIX


def get_invoice_number_suffix():
    return db.get_setting(INVOICE_SUFFIX_KEY, "") or ""


def _consume_override(key):
    raw = db.get_setting(key, "")
    if not raw:
        return None
    db.set_setting(key, "")
    try:
        return int(raw)
    except ValueError:
        return None


def consume_po_number_override():
    """The admin-set 'reset next PO number to' value, as an int — reading it
    clears it, so it only affects the very next PO generated."""
    return _consume_override(PO_OVERRIDE_KEY)


def consume_invoice_number_override():
    """Same as consume_po_number_override, for Invoices."""
    return _consume_override(INVOICE_OVERRIDE_KEY)


def _setting_key(module_key):
    return f"module_enabled:{module_key}"


def get_module_flags():
    return {
        key: (db.get_setting(_setting_key(key), "1" if cfg["default"] else "0") == "1")
        for key, cfg in MODULES.items()
    }


def get_notification_emails_raw():
    """The admin-editable notification-emails setting, as the raw
    comma-separated string (for pre-filling the Settings form input)."""
    return db.get_setting(NOTIFICATION_EMAILS_KEY, DEFAULT_NOTIFICATION_EMAILS)


def get_notification_emails():
    """Office-wide notification addresses (signed documents received,
    trainer confirmed/rejected a PO, attendance form returned, etc.),
    admin-configurable under Settings — used everywhere the app used to
    hard-code eriktajudin@modoku.tech / hello@modoku.tech. Returns a list of
    email addresses, split on commas/whitespace/newlines, empty entries
    dropped."""
    raw = get_notification_emails_raw()
    return [addr.strip() for addr in raw.replace("\n", ",").split(",") if addr.strip()]


def module_required(module_key):
    """Route decorator: redirects to the dashboard if this module is disabled."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not g.modules.get(module_key, True):
                label = MODULES.get(module_key, {}).get("label", module_key)
                flash(f"The {label} module is currently disabled. Ask an admin to re-enable it under Settings.", "warning")
                return redirect(url_for("dashboard.index"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


@bp.route("/", methods=("GET", "POST"))
@login_required
@admin_required
def index():
    if request.method == "POST":
        for key in MODULES:
            enabled = "1" if request.form.get(key) == "on" else "0"
            db.set_setting(_setting_key(key), enabled)
        notify_emails = (request.form.get("notification_emails") or "").strip()
        db.set_setting(NOTIFICATION_EMAILS_KEY, notify_emails or DEFAULT_NOTIFICATION_EMAILS)
        db.set_setting(PO_PREFIX_KEY, (request.form.get("po_number_prefix") or "").strip() or DEFAULT_PO_PREFIX)
        db.set_setting(PO_SUFFIX_KEY, (request.form.get("po_number_suffix") or "").strip())
        db.set_setting(INVOICE_PREFIX_KEY,
                        (request.form.get("invoice_number_prefix") or "").strip() or DEFAULT_INVOICE_PREFIX)
        db.set_setting(INVOICE_SUFFIX_KEY, (request.form.get("invoice_number_suffix") or "").strip())
        flash("Settings updated.", "success")
        return redirect(url_for("settings.index"))

    flags = get_module_flags()
    return render_template("settings/index.html", modules=MODULES, flags=flags,
                            mail_configured=mailer.is_configured(),
                            notification_emails=get_notification_emails_raw(),
                            po_number_prefix=get_po_number_prefix(), po_number_suffix=get_po_number_suffix(),
                            invoice_number_prefix=get_invoice_number_prefix(),
                            invoice_number_suffix=get_invoice_number_suffix())


@bp.route("/reset-numbering", methods=("POST",))
@login_required
@admin_required
def reset_numbering():
    po_next = (request.form.get("po_number_reset") or "").strip()
    invoice_next = (request.form.get("invoice_number_reset") or "").strip()
    if po_next:
        db.set_setting(PO_OVERRIDE_KEY, po_next)
    if invoice_next:
        db.set_setting(INVOICE_OVERRIDE_KEY, invoice_next)
    if po_next or invoice_next:
        flash("Numbering reset — the next document(s) generated will use the number you set.", "success")
    else:
        flash("Enter a number to reset to.", "danger")
    return redirect(url_for("settings.index"))


@bp.route("/test-email", methods=("POST",))
@login_required
@admin_required
def test_email():
    to_email = request.form.get("to_email", "").strip()
    if not to_email:
        flash("Enter an email address to send the test to.", "danger")
        return redirect(url_for("settings.index"))

    try:
        mailer.send_email(
            to_email,
            "Modoku Hub — test email",
            "This is a test email from Modoku Hub. If you're reading this, your "
            "outgoing email settings are working correctly.",
        )
    except mailer.MailNotConfigured as exc:
        flash(str(exc), "danger")
    except mailer.MailSendError as exc:
        flash(f"Test email failed to send: {exc}", "danger")
    else:
        flash(f"Test email sent to {to_email} — check the inbox (and spam folder).", "success")
    return redirect(url_for("settings.index"))
