"""Lightweight fetch of recent corporate-training / HRD Corp news headlines
for the dashboard, using Google News' public RSS search feed (no API key
needed). Results are cached in memory for an hour so the dashboard doesn't
hit the network on every page load, and any failure (no internet access,
timeout, etc.) degrades gracefully — the dashboard just shows a quiet
"unavailable" note instead of breaking.
"""

import logging
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

_logger = logging.getLogger(__name__)

_CACHE = {"ts": 0, "items": []}
_CACHE_TTL_SECONDS = 3600
_QUERY = "corporate training Malaysia OR HRD Corp OR HRDF"
_FEED_URL = "https://news.google.com/rss/search?q={q}&hl=en-MY&gl=MY&ceid=MY:en"


def _fetch(limit):
    url = _FEED_URL.format(q=urllib.parse.quote(_QUERY))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ModokuCRM)"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = resp.read()
    root = ET.fromstring(data)
    items = []
    for item in root.findall("./channel/item")[:limit]:
        source_el = item.find("source")
        raw_pub = (item.findtext("pubDate") or "").strip()
        pub_iso = ""
        if raw_pub:
            try:
                pub_iso = parsedate_to_datetime(raw_pub).date().isoformat()
            except (TypeError, ValueError):
                pub_iso = ""
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "pub_date": pub_iso,
            "source": source_el.text.strip() if source_el is not None and source_el.text else "",
        })
    return items


def get_training_news(limit=5):
    """Returns (items, error). items is a list of dicts (title/link/pub_date/source);
    error is None on success, or a short message if the fetch failed and there
    was nothing cached to fall back on."""
    now = time.time()
    if _CACHE["items"] and (now - _CACHE["ts"] < _CACHE_TTL_SECONDS):
        return _CACHE["items"], None
    try:
        items = _fetch(limit)
        _CACHE["items"] = items
        _CACHE["ts"] = now
        return items, None
    except Exception as exc:  # noqa: BLE001 - any fetch failure degrades to the cached/empty state below
        # Logged (not just swallowed) so the actual cause — no internet, a
        # firewall/proxy blocking news.google.com, DNS failure, etc. — is
        # visible in the server log rather than only a generic message on
        # the dashboard.
        _logger.warning("Training news fetch failed: %s: %s", type(exc).__name__, exc)
        if _CACHE["items"]:
            return _CACHE["items"], None
        return [], "Couldn't load news right now — this needs internet access from the server."
