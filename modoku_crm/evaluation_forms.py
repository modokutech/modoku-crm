"""Google Forms automation for post-training evaluation forms.

Modoku Hub keeps ONE master evaluation Form template (a Google Form Erik
maintains by hand, containing all the real questions) and, per class,
makes a fresh independent copy of it — swapping in that class's course
title, trainer name, and date, explicitly publishing it so it's actually
open to responses, then reads back its live link — instead of someone
duplicating/editing/publishing the Form by hand in Google Drive every
time a class finishes. A freshly-copied Google Form inherits Google's
newer "unpublished" draft state and will silently refuse responses until
something calls forms.setPublishSettings, so that call is part of the
sequence below, not optional.

The moment a Form is generated, this module also builds that class's QR
poster automatically (see sessions._build_evaluation_qr_poster) — there's
no separate "Generate Poster" click needed once Google Forms automation
is connected; the class page only falls back to the manual poster button
when it isn't.

This is deliberately a SINGLE, admin-configured Google connection (see
connect()/google_callback() below), not a per-staff-member one like
calendar_integration.py's calendar connections — whoever clicks "Generate
Evaluation Form" on a class page, the Form is always created under the one
Google account that owns the master template, so every generated Form
lives in the same place and behaves consistently. Stored as a handful of
rows in the existing settings key/value table (see db.get_setting/
set_setting) rather than a dedicated table, since there's only ever one of
these.

Reuses the same GOOGLE_OAUTH_CLIENT_ID/SECRET as the calendar integration
(same Google Cloud project) — it just needs the Forms API and Drive API
enabled on that project, and this module's own scopes granted
(forms.body, forms.responses.readonly, drive) alongside whatever the
calendar integration already asks for. The OAuth redirect URI this module
registers (evaluation_forms.google_callback) needs to be added to that
same OAuth client's "Authorized redirect URIs" in Google Cloud Console —
see the README section on Evaluation Forms setup.

Best-effort throughout: is_connected()/get_template_id() gate every entry
point, and a failed API call always raises EvaluationFormError with a
clear, already-flashable message rather than a bare exception — an
existing evaluation_form_link on the class is left untouched on failure.
"""
import secrets
from datetime import datetime, timedelta

import requests
from flask import Blueprint, current_app, flash, redirect, request, session, url_for

from . import activity, db
from . import fmtdaterange
from .auth import admin_required, login_required

bp = Blueprint("evaluation_forms", __name__, url_prefix="/evaluation-forms")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
DRIVE_API_URL = "https://www.googleapis.com/drive/v3"
FORMS_API_URL = "https://forms.googleapis.com/v1"
GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/forms.body "
    "https://www.googleapis.com/auth/forms.responses.readonly "
    "https://www.googleapis.com/auth/drive "
    "https://www.googleapis.com/auth/userinfo.email"
)

# settings table keys — a single, shared connection (not one row per staff
# member the way calendar_connections works), so plain key/value rows are
# enough; no dedicated table needed.
_ACCESS_TOKEN_KEY = "eval_forms_access_token"
_REFRESH_TOKEN_KEY = "eval_forms_refresh_token"
_TOKEN_EXPIRY_KEY = "eval_forms_token_expiry"
_CONNECTED_EMAIL_KEY = "eval_forms_connected_email"
_TEMPLATE_ID_KEY = "eval_forms_template_id"


def is_configured():
    cfg = current_app.config
    return bool(cfg.get("GOOGLE_OAUTH_CLIENT_ID") and cfg.get("GOOGLE_OAUTH_CLIENT_SECRET"))


def is_connected():
    return bool(db.get_setting(_REFRESH_TOKEN_KEY))


def connected_email():
    return db.get_setting(_CONNECTED_EMAIL_KEY)


def get_template_id():
    return db.get_setting(_TEMPLATE_ID_KEY)


def set_template_id(file_id):
    db.set_setting(_TEMPLATE_ID_KEY, (file_id or "").strip())


def disconnect():
    for key in (_ACCESS_TOKEN_KEY, _REFRESH_TOKEN_KEY, _TOKEN_EXPIRY_KEY, _CONNECTED_EMAIL_KEY):
        db.set_setting(key, "")


def _store_tokens(access_token, refresh_token, expires_in, email):
    expiry = (datetime.utcnow() + timedelta(seconds=int(expires_in or 3600) - 60)).isoformat()
    db.set_setting(_ACCESS_TOKEN_KEY, access_token)
    if refresh_token:  # Google only issues this on the very first consent — keep the existing one otherwise
        db.set_setting(_REFRESH_TOKEN_KEY, refresh_token)
    db.set_setting(_TOKEN_EXPIRY_KEY, expiry)
    if email:
        db.set_setting(_CONNECTED_EMAIL_KEY, email)


def _refresh_access_token():
    refresh_token = db.get_setting(_REFRESH_TOKEN_KEY)
    if not refresh_token:
        return None
    resp = requests.post(GOOGLE_TOKEN_URL, data={
        "client_id": current_app.config["GOOGLE_OAUTH_CLIENT_ID"],
        "client_secret": current_app.config["GOOGLE_OAUTH_CLIENT_SECRET"],
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    _store_tokens(data["access_token"], refresh_token, data.get("expires_in"), None)
    return data["access_token"]


def get_valid_access_token():
    """Returns a live access token, refreshing first if it's expired (or
    about to be). None if not connected, or if the refresh itself fails
    (e.g. access was revoked on Google's side) — callers treat that as
    'can't generate right now', never as a reason to crash."""
    if not is_connected():
        return None
    expiry_raw = db.get_setting(_TOKEN_EXPIRY_KEY)
    try:
        expiry = datetime.fromisoformat(expiry_raw) if expiry_raw else None
    except ValueError:
        expiry = None
    if expiry and expiry > datetime.utcnow():
        return db.get_setting(_ACCESS_TOKEN_KEY)
    try:
        return _refresh_access_token()
    except requests.RequestException:
        current_app.logger.exception("Failed to refresh evaluation-forms Google token")
        return None


class EvaluationFormError(Exception):
    """Raised for any failure generating a Form. Always written to be
    directly flashable — callers catch this and flash str(exc) as-is."""


def generate_form_for_session(session_row):
    """Copies the master template, retitles it for this class, and returns
    (form_id, responder_uri). Raises EvaluationFormError (with a clear,
    already user-facing message) on any failure — never a bare/unclear
    exception, and never partially updates the class's own DB row itself
    (the caller does that once this returns successfully)."""
    if not is_connected():
        raise EvaluationFormError(
            "No Google account connected for Evaluation Forms yet — connect one under Settings first.")
    template_id = get_template_id()
    if not template_id:
        raise EvaluationFormError(
            "No master evaluation Form template is set yet — set one under Settings first.")
    access_token = get_valid_access_token()
    if not access_token:
        raise EvaluationFormError(
            "Couldn't get a valid Google access token — the connection under Settings may need to be "
            "reconnected.")

    course_title = session_row["course_title"]
    trainer_name = session_row["trainer_name"] if "trainer_name" in session_row.keys() else None
    date_text = fmtdaterange(session_row["start_date"], session_row["end_date"])
    file_name = f"{course_title} Training Evaluation — {trainer_name or 'TBC'} — {date_text}"
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        copy_resp = requests.post(
            f"{DRIVE_API_URL}/files/{template_id}/copy",
            json={"name": file_name}, headers=headers, timeout=20,
        )
        copy_resp.raise_for_status()
        new_form_id = copy_resp.json()["id"]
    except requests.RequestException as exc:
        current_app.logger.exception("Drive copy failed for evaluation form template %s", template_id)
        raise EvaluationFormError(
            "Couldn't duplicate the evaluation Form template — check the template ID under Settings is "
            "correct and the connected Google account can still access it."
        ) from exc

    section_title = f"{course_title} Training Evaluation"
    section_description = f"Trainer: {trainer_name or 'TBC'}\nDate: {date_text}"
    try:
        update_resp = requests.post(
            f"{FORMS_API_URL}/forms/{new_form_id}:batchUpdate",
            json={"requests": [{
                "updateFormInfo": {
                    "info": {"title": section_title, "description": section_description},
                    "updateMask": "title,description",
                },
            }]},
            headers=headers, timeout=20,
        )
        update_resp.raise_for_status()
    except requests.RequestException as exc:
        current_app.logger.exception("Forms batchUpdate failed for new evaluation form %s", new_form_id)
        raise EvaluationFormError(
            "The Form was duplicated, but updating its title/trainer/date failed — you can still edit it "
            "by hand in Google Forms (it's already in your Drive), or try generating again."
        ) from exc

    # A copy of a Form starts in Google's "unpublished" draft state and
    # rejects responses until this is called — without it, the Form looks
    # fine but nobody can actually submit feedback until someone opens it
    # in Google Forms and clicks Publish by hand.
    try:
        publish_resp = requests.post(
            f"{FORMS_API_URL}/forms/{new_form_id}:setPublishSettings",
            json={"publishSettings": {"publishState": {"isPublished": True, "isAcceptingResponses": True}}},
            headers=headers, timeout=20,
        )
        publish_resp.raise_for_status()
    except requests.RequestException as exc:
        current_app.logger.exception("Forms setPublishSettings failed for new evaluation form %s", new_form_id)
        raise EvaluationFormError(
            "The Form was created and updated, but publishing it so it can accept responses failed — open "
            "it in Google Forms and click Publish by hand, or try generating again."
        ) from exc

    try:
        get_resp = requests.get(f"{FORMS_API_URL}/forms/{new_form_id}", headers=headers, timeout=20)
        get_resp.raise_for_status()
        responder_uri = get_resp.json().get("responderUri")
    except requests.RequestException as exc:
        current_app.logger.exception("Forms get failed for new evaluation form %s", new_form_id)
        raise EvaluationFormError(
            "The Form was created and updated, but I couldn't retrieve its link — find it in Google "
            "Drive/Forms and paste the link in manually below."
        ) from exc

    return new_form_id, responder_uri


def _is_number(text):
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


def get_form_structure(form_id, access_token):
    """Reads back a generated Form's questions, classifying each one for
    training_reports.py: 'scale' (a 1-5 style rating), 'choice_numeric' (a
    multiple-choice question whose options are themselves numbers — treated
    like a scale), 'choice_text' (multiple-choice with non-numeric options,
    e.g. Excellent/Good/Fair/Poor — tallied as a distribution), 'text' (an
    open-ended question — fed to the AI summary), or 'other' (date/time/file
    upload — not aggregated at all). Returns {questionId: {...}}.

    Handles "Multiple choice grid" questions too (Google's API calls these
    a questionGroupItem) — the classic layout for a training evaluation
    ("Trainer knowledge / Course content / Venue, ..." as rows, sharing one
    Poor/Uncertain/Fair/Good/Excellent scale across the top) — since a
    grid's rows don't show up as a questionItem at all; each row is its
    own question sharing the grid's columns as its options, and is
    classified exactly like a standalone multiple-choice question above.

    Raises EvaluationFormError on any API failure."""
    try:
        resp = requests.get(f"{FORMS_API_URL}/forms/{form_id}",
                             headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        current_app.logger.exception("Forms get (structure) failed for form %s", form_id)
        raise EvaluationFormError(
            "Couldn't read the Form's questions from Google — try again in a moment."
        ) from exc

    questions = {}
    for item in data.get("items", []):
        group = item.get("questionGroupItem")
        if group:
            grid_options = [opt.get("value", "") for opt in (group.get("grid") or {}).get("columns", {}).get("options", [])
                             if opt.get("value")]
            numeric = bool(grid_options) and all(_is_number(opt) for opt in grid_options)
            kind = "choice_numeric" if numeric else "choice_text"
            group_title = item.get("title") or ""
            for row in group.get("questions", []):
                row_question_id = row.get("questionId")
                if not row_question_id:
                    continue
                row_title = (row.get("rowQuestion") or {}).get("title") or "(untitled row)"
                title = f"{group_title} — {row_title}" if group_title else row_title
                questions[row_question_id] = {"title": title, "kind": kind, "options": grid_options}
            continue

        question_item = item.get("questionItem") or {}
        question = question_item.get("question")
        question_id = question.get("questionId") if question else None
        if not question_id:
            continue  # section headers, images, page breaks — nothing to aggregate
        title = item.get("title") or "(untitled question)"
        if "scaleQuestion" in question:
            sq = question["scaleQuestion"]
            questions[question_id] = {"title": title, "kind": "scale", "low": sq.get("low"), "high": sq.get("high")}
        elif "choiceQuestion" in question:
            options = [opt.get("value", "") for opt in question["choiceQuestion"].get("options", [])
                       if opt.get("value")]
            numeric = bool(options) and all(_is_number(opt) for opt in options)
            questions[question_id] = {
                "title": title, "kind": "choice_numeric" if numeric else "choice_text", "options": options,
            }
        elif "textQuestion" in question:
            questions[question_id] = {"title": title, "kind": "text"}
        else:
            questions[question_id] = {"title": title, "kind": "other"}
    return questions


def list_form_responses(form_id, access_token):
    """Every response submitted to a generated Form so far, paginating
    through Google's pageToken until exhausted. Raises EvaluationFormError
    on any API failure. Each item is a raw Forms API FormResponse dict —
    training_reports.py pulls out the answers it needs by questionId."""
    headers = {"Authorization": f"Bearer {access_token}"}
    responses = []
    page_token = None
    try:
        while True:
            params = {"pageSize": 200}
            if page_token:
                params["pageToken"] = page_token
            resp = requests.get(f"{FORMS_API_URL}/forms/{form_id}/responses",
                                 headers=headers, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            responses.extend(data.get("responses", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    except requests.RequestException as exc:
        current_app.logger.exception("Forms responses.list failed for form %s", form_id)
        raise EvaluationFormError(
            "Couldn't read responses from Google Forms — try again in a moment."
        ) from exc
    return responses


# ---------------------------------------------------------------------------
# OAuth connect flow (admin-only) — mirrors calendar_integration.py's Google
# flow, but this is a single shared connection rather than one per staff
# member, so it's admin-gated and lives under Settings rather than Profile.
# ---------------------------------------------------------------------------

@bp.route("/connect")
@admin_required
def connect():
    if not is_configured():
        flash("Google OAuth isn't set up yet — GOOGLE_OAUTH_CLIENT_ID/SECRET need to be configured first "
              "(see README).", "danger")
        return redirect(url_for("settings.index"))
    state = secrets.token_urlsafe(16)
    session["eval_forms_oauth_state"] = state
    params = {
        "client_id": current_app.config["GOOGLE_OAUTH_CLIENT_ID"],
        "redirect_uri": url_for("evaluation_forms.google_callback", _external=True),
        "response_type": "code",
        "scope": GOOGLE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return redirect(GOOGLE_AUTH_URL + "?" + requests.compat.urlencode(params))


@bp.route("/oauth/google/callback")
@admin_required
def google_callback():
    if request.args.get("state") != session.pop("eval_forms_oauth_state", None):
        flash("Connection failed — the request expired, please try again.", "danger")
        return redirect(url_for("settings.index"))
    code = request.args.get("code")
    if not code:
        flash("Google didn't grant access — connection cancelled.", "warning")
        return redirect(url_for("settings.index"))
    try:
        resp = requests.post(GOOGLE_TOKEN_URL, data={
            "client_id": current_app.config["GOOGLE_OAUTH_CLIENT_ID"],
            "client_secret": current_app.config["GOOGLE_OAUTH_CLIENT_SECRET"],
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": url_for("evaluation_forms.google_callback", _external=True),
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
        _store_tokens(data["access_token"], data.get("refresh_token"), data.get("expires_in"), email)
        activity.log("update", "settings", None,
                     f"Connected Google account for Evaluation Forms{' (' + email + ')' if email else ''}")
        flash(f"Google account connected for Evaluation Forms{' as ' + email if email else ''}.", "success")
    except requests.RequestException:
        current_app.logger.exception("Evaluation-forms Google OAuth exchange failed")
        flash("Couldn't connect the Google account — please try again.", "danger")
    return redirect(url_for("settings.index"))


@bp.route("/disconnect", methods=("POST",))
@admin_required
def disconnect_route():
    disconnect()
    activity.log("update", "settings", None, "Disconnected Google account for Evaluation Forms")
    flash("Google account disconnected for Evaluation Forms.", "success")
    return redirect(url_for("settings.index"))


@bp.route("/template", methods=("POST",))
@admin_required
def set_template():
    file_id = (request.form.get("template_file_id") or "").strip()
    set_template_id(file_id)
    flash("Master evaluation Form template saved." if file_id else "Master evaluation Form template cleared.",
          "success")
    return redirect(url_for("settings.index"))


@bp.route("/<int:session_id>/generate", methods=("POST",))
@login_required
def generate(session_id):
    session_row = db.query(
        """SELECT cs.*, c.title AS course_title, t.name AS trainer_name
           FROM course_sessions cs
           JOIN courses c ON c.id = cs.course_id
           LEFT JOIN trainers t ON t.id = cs.trainer_id
           WHERE cs.id = ?""",
        (session_id,), one=True,
    )
    if session_row is None:
        flash("Session not found.", "danger")
        return redirect(url_for("sessions.index"))
    try:
        form_id, responder_uri = generate_form_for_session(session_row)
    except EvaluationFormError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("sessions.view", session_id=session_id))
    db.execute(
        "UPDATE course_sessions SET evaluation_form_id = ?, evaluation_form_link = ?, "
        "evaluation_form_generated_at = datetime('now') WHERE id = ?",
        (form_id, responder_uri, session_id),
    )
    activity.log("update", "session", session_id, "Generated Evaluation Form from template")

    # One click does both jobs: the Form now exists (published) and linked,
    # so immediately build its QR poster too — no separate manual "Generate
    # Poster" step. A local-import here (rather than at module load) avoids
    # a circular import, since sessions.py already imports this module.
    poster_failed = False
    try:
        from . import sessions as sessions_module
        sessions_module._build_evaluation_qr_poster(
            session_id, session_row["course_title"], session_row["start_date"], session_row["end_date"],
            responder_uri,
        )
    except Exception:
        current_app.logger.exception("Auto QR poster generation failed for session %s", session_id)
        poster_failed = True

    if poster_failed:
        flash("Evaluation Form generated, published, and linked — but the QR poster couldn't be "
              "auto-generated. Try again, or check the poster settings.", "warning")
    else:
        flash("Evaluation Form generated, published, linked, and its QR poster is ready below.", "success")
    return redirect(url_for("sessions.view", session_id=session_id))
