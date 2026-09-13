"""AI namecard reading — snap a business card, get the lead form filled in.

Leads have always stored the business card image (leads.namecard_file), but
nothing ever read it: whoever met the person still typed the name, role,
email, phone and company in by hand off the card they'd just photographed.
This module reads it with Claude's vision API instead and hands the fields
back to the form as a suggestion the user can correct before saving.

Same shape and same guarantees as ai_match.py, deliberately: entirely
optional (no ANTHROPIC_API_KEY -> is_configured() is False and the button
never appears), never raises, and never writes anything itself — it only
proposes values into an unsaved form, so a bad read costs a correction,
never a bad record. Nothing here is load-bearing: the manual flow is
untouched and still works exactly as it always has.

The company is returned as the plain text printed on the card plus, when
it's close enough to a client already on file, that company's id — so an
existing client is picked in the dropdown rather than silently duplicated.
Matching is the same conservative difflib approach quotations.py already
uses for course pricing; below the threshold we hand back the text alone
and let the user decide.
"""
import base64
import difflib
import json
import mimetypes
import re

import requests
from flask import current_app

from . import db

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"

# A card's company name has to read this close to a saved client before we
# preselect that client. Deliberately strict — quietly attaching a lead to
# the wrong company is worse than making someone pick from the dropdown.
COMPANY_MATCH_THRESHOLD = 0.82

EXTRACTION_PROMPT = (
    "This is a photo of a business card. Read the contact details printed on it and reply with "
    "ONLY a JSON object, nothing else — no markdown, no explanation. Use these exact keys, with "
    "null for anything not printed on the card or not legible: "
    "\"name\" (the person's full name, without honorifics or qualifications), "
    "\"role\" (their job title), "
    "\"company\" (the organisation name, without the tagline), "
    "\"email\", \"phone\" (their direct mobile/line — prefer a mobile number over a switchboard, "
    "keep the country code if printed), "
    "\"linkedin_url\" (only if a LinkedIn address is actually printed). "
    "Do not invent, complete or correct anything that isn't legibly on the card. "
    "Example: {\"name\": \"Nurul Aisyah binti Rahman\", \"role\": \"Head of Learning & Development\", "
    "\"company\": \"Petronas Digital Sdn Bhd\", \"email\": \"nurul.aisyah@petronas.com\", "
    "\"phone\": \"+60 12-345 6789\", \"linkedin_url\": null}"
)

_FIELDS = ("name", "role", "company", "email", "phone", "linkedin_url")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_configured():
    return bool(current_app.config.get("ANTHROPIC_API_KEY"))


def _encode_image(path):
    mime, _ = mimetypes.guess_type(path)
    if not mime or not mime.startswith("image/"):
        mime = "image/jpeg"
    with open(path, "rb") as fh:
        data = base64.b64encode(fh.read()).decode("ascii")
    return mime, data


def _clean(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    # A model that can't read a field is told to send null, but "null"/"n/a"
    # as literal text still turns up occasionally — treat those as empty
    # rather than typing them into someone's contact record.
    if not value or value.lower() in {"null", "none", "n/a", "na", "-"}:
        return None
    return value


def _match_company(company_name):
    """The saved client whose name reads closest to what's on the card, if
    it's close enough to be worth preselecting. Returns (company_id,
    company_name) or (None, None)."""
    if not company_name:
        return None, None
    rows = db.query("SELECT id, name FROM companies")
    if not rows:
        return None, None
    target = company_name.lower()
    best_id, best_name, best_score = None, None, 0.0
    for row in rows:
        score = difflib.SequenceMatcher(None, target, (row["name"] or "").lower()).ratio()
        if score > best_score:
            best_id, best_name, best_score = row["id"], row["name"], score
    if best_score >= COMPANY_MATCH_THRESHOLD:
        return best_id, best_name
    return None, None


def analyze_namecard(image_path):
    """Reads one business card photo and returns
    {"name", "role", "company", "email", "phone", "linkedin_url",
     "company_id", "matched_company_name"} — every value None when it
    isn't on the card or couldn't be read.

    Best-effort in exactly the way ai_match.analyze_attendance_photo is:
    returns all-None if the feature isn't configured, the request fails,
    or the reply isn't parseable, and never raises. Callers should treat
    an empty result as "nothing to suggest" and leave the form alone."""
    empty = {field: None for field in _FIELDS}
    empty.update({"company_id": None, "matched_company_name": None})
    api_key = current_app.config.get("ANTHROPIC_API_KEY")
    if not api_key:
        return empty
    try:
        mime, b64_data = _encode_image(image_path)
        response = requests.post(
            ANTHROPIC_API_URL,
            timeout=45,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_API_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": current_app.config.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
                "max_tokens": 512,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64_data}},
                        {"type": "text", "text": EXTRACTION_PROMPT},
                    ],
                }],
            },
        )
        response.raise_for_status()
        text = response.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.strip("`")
            if "\n" in text:
                text = text.split("\n", 1)[1]
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            return empty
        result = {field: _clean(parsed.get(field)) for field in _FIELDS}
        # An unparseable email is worse than no email — it lands in a field
        # the app later tries to send mail to.
        if result["email"] and not _EMAIL_RE.match(result["email"]):
            result["email"] = None
        company_id, matched_name = _match_company(result["company"])
        result["company_id"] = company_id
        result["matched_company_name"] = matched_name
        return result
    except Exception:  # noqa: BLE001 - a bad photo/response must never break the lead form
        current_app.logger.exception("AI namecard read failed for %s", image_path)
        return empty
