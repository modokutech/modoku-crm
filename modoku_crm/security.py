"""Site-wide request hardening: CSRF protection for every state-changing
form submission, plus a few standard security response headers.

Kept deliberately dependency-free (no Flask-WTF) so it drops into the
existing raw-form templates with a single hidden field each.
"""
import hmac
import secrets

from flask import abort, request, session

# Endpoints that legitimately receive a POST from outside our own pages and
# therefore can't carry our session-bound CSRF token. Keep this list short
# and explicit — anything else with a form must include {{ csrf_field() }}.
CSRF_EXEMPT_ENDPOINTS = {
    # The public "Return Attendance Form" flow is reached via a printed
    # class-code link, not a session we control end-to-end, but its own
    # submit form still carries the token when the visitor loads the page
    # first (session cookies are set on the initial GET), so it is NOT
    # exempted — only truly external callers (webhooks, none currently)
    # would go here.
}


def _get_or_create_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(32)
        session["csrf_token"] = token
    return token


def csrf_field():
    """Use in templates as {{ csrf_field()|safe }} inside every <form method=post>."""
    token = _get_or_create_csrf_token()
    return f'<input type="hidden" name="csrf_token" value="{token}">'


def init_app(app):
    app.jinja_env.globals["csrf_token"] = _get_or_create_csrf_token
    app.jinja_env.globals["csrf_field"] = csrf_field

    @app.before_request
    def _check_csrf():
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return None
        if request.endpoint in CSRF_EXEMPT_ENDPOINTS:
            return None
        expected = session.get("csrf_token")
        submitted = request.form.get("csrf_token") or request.headers.get("X-CSRFToken")
        if not expected or not submitted or not hmac.compare_digest(expected, submitted):
            abort(400, description="Your session expired or this form was submitted from an unexpected "
                                    "source. Please go back, refresh the page, and try again.")
        return None

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=(self)")
        return response
