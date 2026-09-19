"""Toggleable modules — lets an admin switch off parts of Modoku Hub the
business isn't using yet (e.g. Invoices, Purchase Orders) without touching
any data. A disabled module's sidebar link disappears and its routes
redirect back to the dashboard with a flash message, for every user
(including admins — re-enable it from Settings to get back in).
"""

import os
import uuid
from functools import wraps

from flask import (Blueprint, current_app, flash, g, redirect, render_template,
                    request, send_from_directory, url_for)
from werkzeug.utils import secure_filename

from . import db, evaluation_forms, mailer, uploadutil
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

# The company's rubber-stamp image, uploaded once here and reused on every
# JD14 Form an admin signs (jd14.py / pdfgen.generate_jd14_pdf) — a global
# setting, not per-user, since the stamp belongs to the company, not to
# whichever admin happens to sign.
COMPANY_STAMP_KEY = "company_stamp_file"


def get_company_stamp_file():
    return db.get_setting(COMPANY_STAMP_KEY, "")


def _global_upload_dir():
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "global")
    os.makedirs(path, exist_ok=True)
    return path


def _handle_company_stamp_upload():
    file_storage = request.files.get("company_stamp_file")
    if not file_storage or not file_storage.filename:
        return
    error = uploadutil.validate_upload(file_storage, allowed_extensions=uploadutil.IMAGE_EXTENSIONS)
    if error:
        flash(error, "danger")
        return
    safe_name = secure_filename(file_storage.filename)
    stored_name = f"stamp_{uuid.uuid4().hex[:8]}_{safe_name}"
    file_storage.save(os.path.join(_global_upload_dir(), stored_name))
    db.set_setting(COMPANY_STAMP_KEY, stored_name)


def get_po_number_prefix():
    return db.get_setting(PO_PREFIX_KEY, DEFAULT_PO_PREFIX) or DEFAULT_PO_PREFIX


def get_po_number_suffix():
    return db.get_setting(PO_SUFFIX_KEY, "") or ""


def get_invoice_number_prefix():
    return db.get_setting(INVOICE_PREFIX_KEY, DEFAULT_INVOICE_PREFIX) or DEFAULT_INVOICE_PREFIX


def get_invoice_number_suffix():
    return db.get_setting(INVOICE_SUFFIX_KEY, "") or ""


def _peek_override(key):
    """The pending override as an int, WITHOUT clearing it. For anywhere that
    only needs to show what the next number will be (e.g. the preview on the
    New Invoice form) — see _consume_override for why that distinction
    matters."""
    raw = db.get_setting(key, "")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _consume_override(key):
    """The pending override as an int, clearing it as a side effect so it
    only ever applies once.

    Only call this at the moment a number is actually being assigned to a
    saved record. Calling it to render a preview silently burns the override:
    the preview gets the right number, and the save that follows falls back to
    the running sequence — which is exactly the bug where an admin set 'next
    invoice number = 297', opened the form, and got INV-2026-0001."""
    value = _peek_override(key)
    if value is None:
        return None
    db.set_setting(key, "")
    return value


def consume_po_number_override():
    """The admin-set 'reset next PO number to' value, as an int — reading it
    clears it, so it only affects the very next PO generated."""
    return _consume_override(PO_OVERRIDE_KEY)


def peek_po_number_override():
    """The pending PO override without consuming it (preview only)."""
    return _peek_override(PO_OVERRIDE_KEY)


def consume_invoice_number_override():
    """Same as consume_po_number_override, for Invoices."""
    return _consume_override(INVOICE_OVERRIDE_KEY)


def peek_invoice_number_override():
    """The pending invoice override without consuming it (preview only)."""
    return _peek_override(INVOICE_OVERRIDE_KEY)


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
                            invoice_number_suffix=get_invoice_number_suffix(),
                            eval_forms_google_configured=evaluation_forms.is_configured(),
                            eval_forms_connected=evaluation_forms.is_connected(),
                            eval_forms_connected_email=evaluation_forms.connected_email(),
                            eval_forms_template_id=evaluation_forms.get_template_id(),
                            company_stamp_file=get_company_stamp_file())


@bp.route("/company-stamp", methods=("POST",))
@login_required
@admin_required
def upload_company_stamp():
    """Uploads the global company rubber-stamp image used on signed JD14
    Forms (see jd14.py / pdfgen.generate_jd14_pdf). Kept as its own route
    (rather than folded into index()'s POST) so submitting this one small
    file form never touches the Modules checkboxes living in a separate
    <form> on the same page."""
    file_storage = request.files.get("company_stamp_file")
    if not file_storage or not file_storage.filename:
        flash("Choose an image file first.", "danger")
        return redirect(url_for("settings.index"))
    _handle_company_stamp_upload()
    if get_company_stamp_file():
        flash("Company stamp updated.", "success")
    return redirect(url_for("settings.index"))


@bp.route("/company-stamp")
@login_required
def company_stamp():
    """Serves the current global company stamp image — embedded into the
    JD14 Form PDF/preview (pdfgen._company_stamp_data_uri) and shown as a
    preview here on the Settings page. Any logged-in user can view it (it's
    not sensitive), only an admin can replace it."""
    stamp_file = get_company_stamp_file()
    if not stamp_file:
        flash("No company stamp uploaded yet.", "danger")
        return redirect(url_for("settings.index"))
    return send_from_directory(_global_upload_dir(), stamp_file, as_attachment=False)


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
        flash("Numbering reset. The next document(s) generated will use the number you set.", "success")
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
            "Modoku Hub, test email",
            "This is a test email from Modoku Hub. If you're reading this, your "
            "outgoing email settings are working correctly.",
        )
    except mailer.MailNotConfigured as exc:
        flash(str(exc), "danger")
    except mailer.MailSendError as exc:
        flash(f"Test email failed to send: {exc}", "danger")
    else:
        flash(f"Test email sent to {to_email}. Check the inbox (and spam folder).", "success")
    return redirect(url_for("settings.index"))
