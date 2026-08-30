import hashlib
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import (Blueprint, current_app, flash, g, redirect, render_template,
                    request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import activity, db
from .mailer import MailNotConfigured, MailSendError, send_email

bp = Blueprint("auth", __name__)

# Brute-force protection: after this many wrong passwords in a row, the
# account is locked out for LOCKOUT_MINUTES. Counters reset on a successful
# login. This is enforced server-side regardless of what the client sends.
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15

# Email one-time-code (2FA) settings for the second login step.
OTP_LENGTH = 6
OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5

ALLOWED_REGISTRATION_DOMAIN = "modoku.tech"


@bp.before_app_request
def load_logged_in_user():
    user_id = session.get("user_id")
    g.user = None
    if user_id is not None:
        g.user = db.query("SELECT * FROM users WHERE id = ? AND active = 1", (user_id,), one=True)


def _safe_redirect_target(candidate):
    """Only accept a same-site relative path (e.g. '/sessions/12') as a
    post-login redirect target — never an absolute URL to another host.

    Without this check, a crafted link like
    '/login?next=https://evil.example/phish' would send someone who just
    correctly authenticated straight to an external site right after
    login/OTP — a classic open-redirect phishing vector, since the app
    itself vouches for the link right up to the moment of redirect."""
    if not candidate or not isinstance(candidate, str):
        return None
    if not candidate.startswith("/") or candidate.startswith("//") or candidate.startswith("/\\"):
        return None
    return candidate


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.path))
        if g.user["role"] != "admin":
            flash("You need administrator access for that.", "danger")
            return redirect(url_for("dashboard.index"))
        return view(*args, **kwargs)
    return wrapped


def is_allowed_registration_email(email):
    """New staff accounts must use a company @modoku.tech address — keeps
    account creation from ever being pointed at an outside mailbox."""
    email = (email or "").strip().lower()
    return email.endswith("@" + ALLOWED_REGISTRATION_DOMAIN)


def _hash_otp(code):
    # Codes are short-lived and single-use, but we still avoid storing them
    # in the session in plain text.
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _finish_login(user, remember_next=True):
    session.clear()
    session["user_id"] = user["id"]
    session.permanent = True
    g.user = user
    activity.log("login", "user", user["id"], f"{user['name']} logged in")


def _start_password_step(user):
    """Called right after a correct email+password — resets the account's
    failed-attempt counter and decides whether an email OTP step is needed."""
    db.execute(
        "UPDATE users SET failed_login_count = 0, locked_until = NULL WHERE id = ?",
        (user["id"],),
    )
    code = "".join(secrets.choice("0123456789") for _ in range(OTP_LENGTH))
    try:
        send_email(
            user["email"],
            "Your Modoku Hub login code",
            (
                f"Hi {user['name']},\n\n"
                f"Your one-time login code is: {code}\n\n"
                f"It expires in {OTP_TTL_MINUTES} minutes. If this wasn't you, "
                f"please let an admin know.\n"
            ),
            related_type="user", related_id=user["id"],
        )
    except (MailNotConfigured, MailSendError):
        # Email isn't set up on this deployment yet — don't lock staff out of
        # the app over it, just skip straight to a normal login and let an
        # admin know via the flash message so it gets fixed.
        current_app.logger.warning("Skipping login OTP for user %s — email not available", user["id"])
        _finish_login(user)
        flash("Signed in. (Email verification was skipped because outgoing email isn't configured yet.)", "warning")
        return None

    session["otp_pending_user_id"] = user["id"]
    session["otp_hash"] = _hash_otp(code)
    session["otp_expires_at"] = (datetime.utcnow() + timedelta(minutes=OTP_TTL_MINUTES)).isoformat()
    session["otp_attempts"] = 0
    session["otp_next_url"] = _safe_redirect_target(request.args.get("next")) or url_for("dashboard.index")
    return redirect(url_for("auth.verify"))


@bp.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = db.query("SELECT * FROM users WHERE lower(email) = ?", (email,), one=True)

        error = None
        now = datetime.utcnow()
        locked_until = None
        if user and user["locked_until"]:
            try:
                locked_until = datetime.fromisoformat(user["locked_until"])
            except ValueError:
                locked_until = None

        if locked_until and now < locked_until:
            minutes_left = max(1, int((locked_until - now).total_seconds() // 60) + 1)
            error = f"Too many failed attempts. Try again in about {minutes_left} minute(s)."
        elif user is None or not check_password_hash(user["password_hash"], password):
            error = "Incorrect email or password."
            if user is not None:
                new_count = (user["failed_login_count"] or 0) + 1
                lock_value = None
                if new_count >= MAX_FAILED_LOGINS:
                    lock_value = (now + timedelta(minutes=LOCKOUT_MINUTES)).isoformat()
                db.execute(
                    "UPDATE users SET failed_login_count = ?, locked_until = ? WHERE id = ?",
                    (new_count, lock_value, user["id"]),
                )
                if lock_value:
                    error = f"Too many failed attempts. This account is locked for {LOCKOUT_MINUTES} minutes."
        elif not user["active"]:
            error = "This account has been deactivated."

        if error is None:
            resp = _start_password_step(user)
            if resp is not None:
                return resp
            return redirect(session.pop("otp_next_url", None) or url_for("dashboard.index"))
        flash(error, "danger")

    return render_template("login.html")


@bp.route("/login/verify", methods=("GET", "POST"))
def verify():
    pending_user_id = session.get("otp_pending_user_id")
    if not pending_user_id:
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        expires_at = session.get("otp_expires_at")
        expired = True
        if expires_at:
            try:
                expired = datetime.utcnow() > datetime.fromisoformat(expires_at)
            except ValueError:
                expired = True

        attempts = session.get("otp_attempts", 0)
        code = request.form.get("code", "").strip()

        if expired:
            session.clear()
            flash("That code expired. Please log in again.", "danger")
            return redirect(url_for("auth.login"))
        if attempts >= OTP_MAX_ATTEMPTS:
            session.clear()
            flash("Too many incorrect codes. Please log in again.", "danger")
            return redirect(url_for("auth.login"))
        if not code or _hash_otp(code) != session.get("otp_hash"):
            session["otp_attempts"] = attempts + 1
            flash("Incorrect code. Please try again.", "danger")
        else:
            user = db.query("SELECT * FROM users WHERE id = ? AND active = 1", (pending_user_id,), one=True)
            next_url = session.get("otp_next_url") or url_for("dashboard.index")
            if user is None:
                session.clear()
                flash("This account is no longer active.", "danger")
                return redirect(url_for("auth.login"))
            _finish_login(user)
            return redirect(next_url)

    return render_template("login_verify.html")


@bp.route("/login/verify/resend", methods=("POST",))
def resend_code():
    pending_user_id = session.get("otp_pending_user_id")
    if not pending_user_id:
        return redirect(url_for("auth.login"))
    user = db.query("SELECT * FROM users WHERE id = ? AND active = 1", (pending_user_id,), one=True)
    if user is None:
        session.clear()
        return redirect(url_for("auth.login"))
    next_url = session.get("otp_next_url")
    resp = _start_password_step(user)
    if resp is None:
        # Email wasn't available and _start_password_step already logged the
        # user in directly.
        return redirect(next_url or url_for("dashboard.index"))
    session["otp_next_url"] = next_url or url_for("dashboard.index")
    flash("A new code has been sent.", "success")
    return redirect(url_for("auth.verify"))


@bp.route("/logout")
def logout():
    if g.user:
        activity.log("logout", "user", g.user["id"], f"{g.user['name']} logged out")
    session.clear()
    return redirect(url_for("auth.login"))
