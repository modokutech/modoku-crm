"""Minimal outgoing-email helper built on Python's standard library
(smtplib + email), so sending mail needs no extra packages installed.

Used for emailing Purchase Orders/Quotations/notifications from Modoku Hub.
Every email sent through send_email() gets the same standard footer
(company name, address, website) appended automatically — callers don't
need to add it themselves.
"""
import os
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid
from html import escape

from flask import current_app, g


class MailNotConfigured(Exception):
    pass


class MailSendError(Exception):
    pass


# Most SMTP providers (especially the budget/shared-hosting ones smaller
# outfits tend to use) reject or silently drop a message somewhere around
# 10-25MB once headers and base64 attachment encoding (~37% larger than the
# raw file) are added in — and when that happens mid-transaction, smtplib
# doesn't raise a clean "too big" error, it raises SMTPServerDisconnected
# ("Server not connected" / "Connection unexpectedly closed"), which looks
# like a config problem even though mail is set up correctly. Checking the
# raw attachment total against a conservative cap *before* ever opening the
# SMTP connection turns that confusing failure into a clear one. 8MB raw is
# comfortably under even a strict ~10MB provider limit once encoded.
MAX_TOTAL_ATTACHMENT_BYTES = 8 * 1024 * 1024


# Shown on every outgoing email, right after the body's own sign-off (e.g.
# "Cheers!") and before the company signature block — callers never need to
# add this themselves.
AUTOMATED_DISCLAIMER_TEXT = "This is an automated email. Please do not reply to this message."

FOOTER_TEXT = (
    f"\n\n{AUTOMATED_DISCLAIMER_TEXT}\n\n--\n"
    "Modoku Tech Sdn Bhd (1390352-H)\n"
    "Level 30, Menara Prestige\n"
    "1, Jalan Pinang\n"
    "50450 Kuala Lumpur\n"
    "modoku.tech"
)


def _disclaimer_html():
    return (
        '<div style="font-family:\'Courier New\',Courier,monospace;font-size:11px;'
        f'color:#767171;margin-top:16px;">{escape(AUTOMATED_DISCLAIMER_TEXT)}</div>'
    )


def _footer_html(logo_cid):
    logo_img = f'<img src="cid:{logo_cid}" width="64" alt="Modoku" style="display:block;margin-bottom:8px;">' if logo_cid else ""
    return f"""
<table cellpadding="0" cellspacing="0" border="0" style="margin-top:18px;padding-top:14px;border-top:1px solid #e2e2e2;font-family:Arial,Helvetica,sans-serif;">
  <tr><td>
    {logo_img}
    <div style="color:#0c45a6;font-weight:bold;font-size:12px;">Modoku Tech Sdn Bhd</div>
    <div style="color:#767171;font-size:10px;">(1390352-H)</div>
    <div style="color:#767171;font-size:10px;margin-top:4px;">Level 30, Menara Prestige<br>1, Jalan Pinang<br>50450 Kuala Lumpur</div>
    <div style="margin-top:4px;"><a href="https://modoku.tech/" style="color:#0c45a6;font-size:10px;text-decoration:none;font-weight:bold;">modoku.tech</a></div>
  </td></tr>
</table>
"""


def is_configured():
    cfg = current_app.config
    return bool(cfg.get("MAIL_USERNAME") and cfg.get("MAIL_PASSWORD"))


def _log_attempt(to_email, subject, status, error=None, related_type=None, related_id=None, cc_email=None):
    try:
        from . import db
        sent_by = g.user["id"] if getattr(g, "user", None) else None
        db.execute(
            "INSERT INTO mail_log (to_email, subject, status, error, related_type, related_id, sent_by, cc_email) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (to_email, subject, status, error, related_type, related_id, sent_by, cc_email or None),
        )
    except Exception:  # noqa: BLE001 - logging must never break the send
        pass


def send_email(to_email, subject, body_text, attachments=None, related_type=None, related_id=None,
                cc_email=None):
    """Send a plain-text email, optionally with attachments.

    attachments: list of (filename, bytes, mimetype) tuples.
    cc_email: optional string — one address, or several comma-separated —
    CC'd on top of to_email. Blank/None means no CC.
    related_type/related_id: what this email is about (e.g. 'purchase_order', 42) —
    recorded in the Mail Log so admins can trace every email sent from the app.
    Raises MailNotConfigured if no SMTP credentials are set, or
    MailSendError if the SMTP server rejects the send.
    """
    cfg = current_app.config
    if not is_configured():
        _log_attempt(to_email, subject, "not_configured", "Email not configured", related_type, related_id, cc_email)
        raise MailNotConfigured(
            "Email isn't set up yet. An admin needs to set MAIL_USERNAME and "
            "MAIL_PASSWORD (see README) before Modoku Hub can send emails."
        )

    total_attachment_bytes = sum(len(data) for _, data, _ in (attachments or []))
    if total_attachment_bytes > MAX_TOTAL_ATTACHMENT_BYTES:
        error = (
            f"These attachments total {total_attachment_bytes / (1024 * 1024):.1f} MB, which is too "
            f"large to email reliably (over {MAX_TOTAL_ATTACHMENT_BYTES // (1024 * 1024)} MB) — most mail "
            "servers will refuse or drop it. Try a smaller file, or share a link instead of attaching."
        )
        _log_attempt(to_email, subject, "failed", error, related_type, related_id, cc_email)
        raise MailSendError(error)

    from_address = cfg.get("MAIL_FROM_ADDRESS") or cfg["MAIL_USERNAME"]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{cfg.get('MAIL_FROM_NAME', 'Modoku Tech')} <{from_address}>"
    msg["To"] = to_email
    cc_email = (cc_email or "").strip()
    if cc_email:
        msg["Cc"] = cc_email
    msg.set_content(body_text + FOOTER_TEXT)

    # HTML alternative with the same standard footer, logo embedded inline
    # (cid: reference) rather than linked to an external host, so it shows
    # correctly even for recipients who block remote images.
    logo_path = None
    try:
        logo_path = os.path.join(current_app.root_path, "static", "img", "logo.png")
        with open(logo_path, "rb") as f:
            logo_bytes = f.read()
    except OSError:
        logo_bytes = None

    body_html_escaped = escape(body_text).replace("\n", "<br>")
    logo_cid = make_msgid()[1:-1] if logo_bytes else None
    html_body = (
        f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#1a1a1a;">'
        f"{body_html_escaped}{_disclaimer_html()}{_footer_html(logo_cid)}</div>"
    )
    msg.add_alternative(html_body, subtype="html")
    if logo_bytes:
        msg.get_payload()[-1].add_related(logo_bytes, maintype="image", subtype="png", cid=f"<{logo_cid}>")

    for filename, data, mimetype in (attachments or []):
        maintype, _, subtype = (mimetype or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream",
                            filename=filename)

    try:
        with smtplib.SMTP(cfg["MAIL_SERVER"], cfg["MAIL_PORT"], timeout=20) as server:
            if cfg.get("MAIL_USE_TLS", True):
                server.starttls()
            server.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
            server.send_message(msg)
    except Exception as exc:  # noqa: BLE001 - surface any SMTP failure to the caller
        _log_attempt(to_email, subject, "failed", str(exc), related_type, related_id, cc_email)
        raise MailSendError(str(exc)) from exc

    _log_attempt(to_email, subject, "sent", None, related_type, related_id, cc_email)
