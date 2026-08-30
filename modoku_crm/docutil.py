"""Small shared helper for the document-download routes (Purchase Orders,
Quotations, Invoices).
"""
import re
import unicodedata
from urllib.parse import quote


def content_disposition(filename, as_attachment=True):
    """Builds a Content-Disposition header value that's safe for filenames
    containing non-ASCII characters (e.g. quotation titles use an em dash,
    "—") — a raw header must be latin-1 encodable, so a plain
    f'attachment; filename="{filename}"' with such characters crashes the
    response mid-stream. Sends both a plain ASCII fallback (filename=) and
    the full UTF-8 name (filename*=), per RFC 6266/5987, so browsers still
    show the correct name."""
    disposition = "attachment" if as_attachment else "inline"
    ascii_fallback = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii")
    ascii_fallback = re.sub(r'[\\"]', "_", ascii_fallback).strip() or "download.pdf"
    return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"
