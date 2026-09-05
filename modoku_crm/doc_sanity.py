"""AI-assisted sanity-checking on document uploads.

When staff upload the signed JD14 form or the HRDCorp grant quotation on a
class page, this module gives Claude's vision API a quick look at the file
and flags anything that looks obviously wrong — e.g. a JD14 upload that's
actually a blank template or an unrelated screenshot, or a "quotation" that
doesn't look like a quotation/invoice at all.

This never blocks the upload. It's a best-effort, additive check only: on
any failure (feature not configured, unsupported file type, network error,
unparseable response) it silently returns None and the upload proceeds
exactly as it always has. Same "never raises" contract as ai_match.py's
analyze_attendance_photo — see that module for the pattern this follows.
"""
import base64
import json
import mimetypes

import requests
from flask import current_app

from .ai_match import ANTHROPIC_API_URL, ANTHROPIC_API_VERSION

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

_GENERIC_INSTRUCTIONS = (
    "Reply with ONLY a JSON object, nothing else — no markdown, no "
    "explanation. Format: {\"looks_right\": true|false, \"reason\": \"<one "
    "short sentence, only when looks_right is false>\"}. Only set "
    "looks_right to false if you're fairly confident this is the wrong kind "
    "of document. If it's unclear, low quality, an unusual template, or "
    "you're simply not sure, set looks_right to true — this check should "
    "only catch obvious mistakes, never second-guess a real document."
)

_PROMPTS = {
    "jd14": (
        "This file was uploaded as a signed HRDCorp Joint Declaration Form "
        "(PSMB/SBL-KHAS/JD/14) — a short government form declaring that "
        "training was conducted, normally bearing signatures and/or company "
        "stamps from both the training provider and the client. Look at the "
        "file and decide whether it plausibly IS such a signed declaration "
        "form (e.g. it would be wrong if this were instead a blank/unsigned "
        "template, a completely unrelated photo or screenshot, or a "
        "different form entirely). " + _GENERIC_INSTRUCTIONS
    ),
    "grant_quotation": (
        "This file was uploaded as a training quotation for an HRDCorp "
        "grant claim pack — normally a document showing a company "
        "letterhead, a quotation/invoice number, itemized fees, and a total "
        "amount. Look at the file and decide whether it plausibly IS a "
        "quotation document (e.g. it would be wrong if this were instead an "
        "unrelated photo, a completely different form, or a blank page). "
        + _GENERIC_INSTRUCTIONS
    ),
    "signed_quotation": (
        "This file was uploaded as a client's SIGNED copy of a training "
        "quotation — expect to see quotation content (a course/programme "
        "name, itemized fees, a total amount) together with some sign of "
        "acceptance such as a signature, initials, or a company stamp. Look "
        "at the file and decide whether it plausibly IS a signed quotation "
        "(e.g. it would be wrong if this were instead an unrelated photo, a "
        "completely different document, or a blank page). "
        + _GENERIC_INSTRUCTIONS
    ),
    "t3_attendance": (
        "This file was uploaded as a photo or scan of a signed HRDCorp "
        "training attendance sign-in sheet (form PSMB/SBL-KHAS/T3/01) — "
        "expect to see a course title, training date(s), and a list of "
        "participant names with signatures or initials next to them. Look "
        "at the file and decide whether it plausibly IS such an attendance "
        "sheet (e.g. it would be wrong if this were instead a completely "
        "unrelated photo, a different form entirely, or a blank/unsigned "
        "sheet with no one signed in). " + _GENERIC_INSTRUCTIONS
    ),
    "evaluation_report": (
        "This file was uploaded as a training evaluation report — expect to "
        "see a summary or compilation of participant feedback/ratings for a "
        "completed training. Look at the file and decide whether it "
        "plausibly IS such a report (e.g. it would be wrong if this were "
        "instead an unrelated photo, a completely different kind of "
        "document, or a blank page). " + _GENERIC_INSTRUCTIONS
    ),
    "trainer_credential": (
        "This file was uploaded as a trainer's profile document or "
        "accreditation/certification certificate — expect to see either a "
        "trainer's professional profile/résumé content, or a certificate "
        "bearing a name, an issuing body, and typically a certificate "
        "number or date. Look at the file and decide whether it plausibly "
        "IS one of those (e.g. it would be wrong if this were instead an "
        "unrelated photo, a completely different kind of document, or a "
        "blank page). " + _GENERIC_INSTRUCTIONS
    ),
    "financial_document": (
        "This file was uploaded as a financial document — an invoice, "
        "purchase order, payment receipt, or similar claim/expense "
        "supporting document — expect to see a company/vendor name, an "
        "amount, and typically a date and/or reference number. Look at the "
        "file and decide whether it plausibly IS such a financial document "
        "(e.g. it would be wrong if this were instead an unrelated photo, a "
        "completely different kind of document, or a blank page). "
        + _GENERIC_INSTRUCTIONS
    ),
}


def is_configured():
    return bool(current_app.config.get("ANTHROPIC_API_KEY"))


def _content_block(file_path):
    """Returns a Claude API content block for a file type it can visually
    inspect (PDF or common image formats), or None for one it can't (Word,
    Excel, PowerPoint, CSV, plain text, or anything without an extension) —
    those are silently skipped rather than checked."""
    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
    if ext == "pdf":
        with open(file_path, "rb") as fh:
            data = base64.b64encode(fh.read()).decode("ascii")
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
    if ext in IMAGE_EXTENSIONS:
        mime, _ = mimetypes.guess_type(file_path)
        if not mime or not mime.startswith("image/"):
            mime = "image/jpeg"
        with open(file_path, "rb") as fh:
            data = base64.b64encode(fh.read()).decode("ascii")
        return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
    return None


def check_document(file_path, kind):
    """Best-effort sanity check of an uploaded document against what it's
    supposed to be ("jd14" or "grant_quotation"). Returns a short warning
    string to flash to the uploader, or None when there's nothing to flag —
    because the feature isn't configured, the file type can't be visually
    inspected, the request failed, or the document looks fine.

    Never raises — a bad file or a flaky API call must never block or break
    the upload it's checking.
    """
    prompt = _PROMPTS.get(kind)
    if not prompt:
        return None
    api_key = current_app.config.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        block = _content_block(file_path)
        if block is None:
            return None
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
                "max_tokens": 256,
                "messages": [{
                    "role": "user",
                    "content": [block, {"type": "text", "text": prompt}],
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
            return None
        if parsed.get("looks_right", True):
            return None
        reason = parsed.get("reason")
        reason = reason.strip() if isinstance(reason, str) and reason.strip() else None
        base_msg = "The uploaded file doesn't look like the expected document — please double-check it's correct."
        return f"{base_msg} ({reason})" if reason else base_msg
    except Exception:  # noqa: BLE001 - a bad file/response must never break the upload
        current_app.logger.exception("AI document sanity-check failed for %s (kind=%s)", file_path, kind)
        return None
