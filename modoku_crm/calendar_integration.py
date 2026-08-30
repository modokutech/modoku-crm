"""Per-staff Google / Outlook calendar integration.

Each staff member connects their OWN calendar account from their Profile
page (OAuth — nothing shared org-wide, unlike the single SMTP mailbox
mailer.py sends from). The moment a class's status becomes 'Scheduled' —
whether set directly, or automatically when a signed quotation comes back
(see quotations._handle_quotation_signed) — a blocking event is created on
EVERY staff member's connected calendar (Google and/or Outlook), not just
one particular person's (see block_calendar_for_session, called from
sessions.py). A user with no calendar connected simply doesn't get one;
this never blocks the rest of the class-scheduling flow (best-effort,
exceptions are always caught by the caller).

Requires GOOGLE_OAUTH_CLIENT_ID/SECRET and/or MS_OAUTH_CLIENT_ID/SECRET to
be set (see README "Setting up calendar integration") — until then,
is_google_configured()/is_microsoft_configured() are False and the Connect
buttons on Profile explain that setup is needed first, exactly like the
existing mailer.is_configured() pattern for email.
"""
import secrets
from datetime import datetime, timedelta

import requests
from flask import Blueprint, current_app, flash, g, redirect, request, session, url_for

from . import activity, db
from .auth import login_required
from .sessions import split_training_time

bp = Blueprint("calendar_integration", __name__, url_prefix="/calendar")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GOOGLE_SCOPES = "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/userinfo.email"

MS_EVENTS_URL = "https://graph.microsoft.com/v1.0/me/events"
MS_USERINFO_URL = "https://graph.microsoft.com/v1.0/me"
MS_SCOPES = "offline_access Calendars.ReadWrite User.Read"

# Malaysia is UTC+8, no daylight saving — same convention as sessions.py's
# .ics builder, so calendar events land at the correct local time.
_MY_UTC_OFFSET_HOURS = 8


def is_google_configured():
    cfg = current_app.config
    return bool(cfg.get("GOOGLE_OAUTH_CLIENT_ID") and cfg.get("GOOGLE_OAUTH_CLIENT_SECRET"))


def is_microsoft_configured():
    cfg = current_app.config
    return bool(cfg.get("MS_OAUTH_CLIENT_ID") and cfg.get("MS_OAUTH_CLIENT_SECRET"))


def _ms_authority():
    return f"https://login.microsoftonline.com/{current_app.config.get('MS_OAUTH_TENANT') or 'common'}"


def get_connection(user_id, provider):
    return db.query(
        "SELECT * FROM calendar_connections WHERE user_id = ? AND provider = ?",
        (user_id, provider), one=True,
    )


def get_connections_for_user(user_id):
    return db.query("SELECT * FROM calendar_connections WHERE user_id = ?", (user_id,))


def disconnect(user_id, provider):
    db.execute("DELETE FROM calendar_connections WHERE user_id = ? AND provider = ?", (user_id, provider))


def _store_connection(user_id, provider, access_token, refresh_token, expires_in, calendar_email):
    expiry = (datetime.utcnow() + timedelta(seconds=int(expires_in or 3600) - 60)).isoformat()
    existing = get_connection(user_id, provider)
    if existing and not refresh_token:
        # Some providers only issue a refresh_token on the very first
        # consent — keep the one we already have rather than clobbering it.
        refresh_token = existing["refresh_token"]
    db.execute(
        """INSERT INTO calendar_connections (user_id, provider, access_token, refresh_token, token_expiry, calendar_email)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(user_id, provider) DO UPDATE SET access_token=excluded.access_token,
               refresh_token=excluded.refresh_token, token_expiry=excluded.token_expiry,
               calendar_email=excluded.calendar_email""",
        (user_id, provider, access_token, refresh_token, expiry, calendar_email),
    )


def _refresh_google(conn):
    resp = requests.post(GOOGLE_TOKEN_URL, data={
        "client_id": current_app.config["GOOGLE_OAUTH_CLIENT_ID"],
        "client_secret": current_app.config["GOOGLE_OAUTH_CLIENT_SECRET"],
        "refresh_token": conn["refresh_token"],
        "grant_type": "refresh_token",
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    _store_connection(conn["user_id"], "google", data["access_token"], conn["refresh_token"],
                       data.get("expires_in"), conn["calendar_email"])
    return data["access_token"]


def _refresh_microsoft(conn):
    resp = requests.post(f"{_ms_authority()}/oauth2/v2.0/token", data={
        "client_id": current_app.config["MS_OAUTH_CLIENT_ID"],
        "client_secret": current_app.config["MS_OAUTH_CLIENT_SECRET"],
        "refresh_token": conn["refresh_token"],
        "grant_type": "refresh_token",
        "scope": MS_SCOPES,
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    _store_connection(conn["user_id"], "microsoft", data["access_token"],
                       data.get("refresh_token") or conn["refresh_token"],
                       data.get("expires_in"), conn["calendar_email"])
    return data["access_token"]


def get_valid_access_token(user_id, provider):
    """Returns a live access token for this user's connection, refreshing
    it first if it's expired (or about to). Returns None if not connected,
    or if the refresh itself fails (e.g. the user revoked access on the
    provider's side) — callers treat that as 'nothing to block against'."""
    conn = get_connection(user_id, provider)
    if conn is None:
        return None
    try:
        expiry = datetime.fromisoformat(conn["token_expiry"]) if conn["token_expiry"] else None
    except ValueError:
        expiry = None
    if expiry and expiry > datetime.utcnow():
        return conn["access_token"]
    try:
        if provider == "google":
            return _refresh_google(conn)
        if provider == "microsoft":
            return _refresh_microsoft(conn)
    except requests.RequestException:
        current_app.logger.exception("Failed to refresh %s calendar token for user %s", provider, user_id)
    return None


def _event_datetimes(start_date, end_date, training_time):
    """Local (Malaysia) start/end datetimes for the class, from the same
    training_time string sessions.py stores — falls back to a 9am-5pm
    all-day-ish span if a class somehow has no time on file."""
    last_date = end_date or start_date
    start_hhmm, end_hhmm = split_training_time(training_time) if training_time else ("", "")
    if not (start_hhmm and end_hhmm):
        start_hhmm, end_hhmm = "09:00", "17:00"
    start_dt = datetime.strptime(f"{start_date} {start_hhmm}", "%Y-%m-%d %H:%M")
    end_dt = datetime.strptime(f"{last_date} {end_hhmm}", "%Y-%m-%d %H:%M")
    return start_dt, end_dt


def _create_event_google(access_token, summary, description, location, start_dt, end_dt):
    body = {
        "summary": summary,
        "description": description,
        "location": location or "",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Kuala_Lumpur"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Kuala_Lumpur"},
    }
    resp = requests.post(
        GOOGLE_EVENTS_URL, json=body, timeout=15,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    return resp.json().get("id")


def _create_event_microsoft(access_token, summary, description, location, start_dt, end_dt):
    body = {
        "subject": summary,
        "body": {"contentType": "Text", "content": description},
        "location": {"displayName": location or ""},
        "start": {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Malay Peninsula Standard Time"},
        "end": {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Malay Peninsula Standard Time"},
    }
    resp = requests.post(
        MS_EVENTS_URL, json=body, timeout=15,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    return resp.json().get("id")


def block_calendar_for_session(session_row):
    """Best-effort: create a blocking event on EVERY staff member's
    connected calendar (any user with a row in calendar_connections),
    not just one particular person's. Safe to call more than once for the
    same class (e.g. status flips to Scheduled, then edited again) — skips
    silently once course_sessions.calendar_blocked_at is already set, so a
    class is never double-booked with duplicate events. Never raises —
    every call site treats this as fire-and-forget."""
    if session_row["calendar_blocked_at"]:
        return
    course_title = session_row["course_title"] if "course_title" in session_row.keys() else "Training"
    summary = f"{course_title} — Training (blocked)"
    description = "Auto-blocked by Modoku Hub once this class was confirmed/scheduled."
    location = session_row["venue"] if "venue" in session_row.keys() else None
    start_dt, end_dt = _event_datetimes(
        session_row["start_date"], session_row["end_date"], session_row["training_time"],
    )

    connected_user_ids = [row["user_id"] for row in db.query(
        "SELECT DISTINCT user_id FROM calendar_connections"
    )]
    if not connected_user_ids:
        return

    blocked_any = False
    for user_id in connected_user_ids:
        for provider, create_fn in (("google", _create_event_google), ("microsoft", _create_event_microsoft)):
            access_token = get_valid_access_token(user_id, provider)
            if not access_token:
                continue
            try:
                create_fn(access_token, summary, description, location, start_dt, end_dt)
                activity.log("update", "session", session_row["id"],
                             f"Blocked {provider.title()} calendar for user {user_id}")
                blocked_any = True
                break  # one connected calendar is enough for this particular user
            except requests.RequestException:
                current_app.logger.exception(
                    "Failed to create %s calendar event for session %s (user %s)",
                    provider, session_row["id"], user_id)

    if blocked_any:
        db.execute("UPDATE course_sessions SET calendar_blocked_at = datetime('now') WHERE id = ?",
                   (session_row["id"],))


# ---------------------------------------------------------------------------
# OAuth connect / callback / disconnect routes


@bp.route("/connect/google")
@login_required
def connect_google():
    if not is_google_configured():
        flash("Google Calendar integration isn't set up yet — an admin needs to configure "
              "GOOGLE_OAUTH_CLIENT_ID/SECRET first (see README).", "danger")
        return redirect(url_for("profile.edit"))
    state = secrets.token_urlsafe(16)
    session["calendar_oauth_state"] = state
    params = {
        "client_id": current_app.config["GOOGLE_OAUTH_CLIENT_ID"],
        "redirect_uri": url_for("calendar_integration.google_callback", _external=True),
        "response_type": "code",
        "scope": GOOGLE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return redirect(GOOGLE_AUTH_URL + "?" + requests.compat.urlencode(params))


@bp.route("/oauth/google/callback")
@login_required
def google_callback():
    if request.args.get("state") != session.pop("calendar_oauth_state", None):
        flash("Calendar connection failed — the request expired, please try again.", "danger")
        return redirect(url_for("profile.edit"))
    code = request.args.get("code")
    if not code:
        flash("Google didn't grant access — connection cancelled.", "warning")
        return redirect(url_for("profile.edit"))
    try:
        resp = requests.post(GOOGLE_TOKEN_URL, data={
            "client_id": current_app.config["GOOGLE_OAUTH_CLIENT_ID"],
            "client_secret": current_app.config["GOOGLE_OAUTH_CLIENT_SECRET"],
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": url_for("calendar_integration.google_callback", _external=True),
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        email = ""
        try:
            info = requests.get(GOOGLE_USERINFO_URL, timeout=10,
                                 headers={"Authorization": f"Bearer {data['access_token']}"})
            email = info.json().get("email", "") if info.ok else ""
        except requests.RequestException:
            pass
        _store_connection(g.user["id"], "google", data["access_token"], data.get("refresh_token"),
                           data.get("expires_in"), email)
        activity.log("update", "user", g.user["id"], "Connected Google Calendar")
        flash(f"Google Calendar connected{' as ' + email if email else ''}.", "success")
    except requests.RequestException:
        current_app.logger.exception("Google Calendar OAuth exchange failed")
        flash("Couldn't connect Google Calendar — please try again.", "danger")
    return redirect(url_for("profile.edit"))


@bp.route("/connect/microsoft")
@login_required
def connect_microsoft():
    if not is_microsoft_configured():
        flash("Outlook Calendar integration isn't set up yet — an admin needs to configure "
              "MS_OAUTH_CLIENT_ID/SECRET first (see README).", "danger")
        return redirect(url_for("profile.edit"))
    state = secrets.token_urlsafe(16)
    session["calendar_oauth_state"] = state
    params = {
        "client_id": current_app.config["MS_OAUTH_CLIENT_ID"],
        "redirect_uri": url_for("calendar_integration.microsoft_callback", _external=True),
        "response_type": "code",
        "response_mode": "query",
        "scope": MS_SCOPES,
        "state": state,
    }
    return redirect(f"{_ms_authority()}/oauth2/v2.0/authorize?" + requests.compat.urlencode(params))


@bp.route("/oauth/microsoft/callback")
@login_required
def microsoft_callback():
    if request.args.get("state") != session.pop("calendar_oauth_state", None):
        flash("Calendar connection failed — the request expired, please try again.", "danger")
        return redirect(url_for("profile.edit"))
    code = request.args.get("code")
    if not code:
        flash("Microsoft didn't grant access — connection cancelled.", "warning")
        return redirect(url_for("profile.edit"))
    try:
        resp = requests.post(f"{_ms_authority()}/oauth2/v2.0/token", data={
            "client_id": current_app.config["MS_OAUTH_CLIENT_ID"],
            "client_secret": current_app.config["MS_OAUTH_CLIENT_SECRET"],
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": url_for("calendar_integration.microsoft_callback", _external=True),
            "scope": MS_SCOPES,
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        email = ""
        try:
            info = requests.get(MS_USERINFO_URL, timeout=10,
                                 headers={"Authorization": f"Bearer {data['access_token']}"})
            if info.ok:
                info_json = info.json()
                email = info_json.get("mail") or info_json.get("userPrincipalName") or ""
        except requests.RequestException:
            pass
        _store_connection(g.user["id"], "microsoft", data["access_token"], data.get("refresh_token"),
                           data.get("expires_in"), email)
        activity.log("update", "user", g.user["id"], "Connected Outlook Calendar")
        flash(f"Outlook Calendar connected{' as ' + email if email else ''}.", "success")
    except requests.RequestException:
        current_app.logger.exception("Microsoft Calendar OAuth exchange failed")
        flash("Couldn't connect Outlook Calendar — please try again.", "danger")
    return redirect(url_for("profile.edit"))


@bp.route("/disconnect/<provider>", methods=("POST",))
@login_required
def disconnect_route(provider):
    if provider not in ("google", "microsoft"):
        flash("Unknown calendar provider.", "danger")
        return redirect(url_for("profile.edit"))
    disconnect(g.user["id"], provider)
    activity.log("update", "user", g.user["id"], f"Disconnected {provider.title()} Calendar")
    flash(f"{provider.title()} Calendar disconnected.", "success")
    return redirect(url_for("profile.edit"))
