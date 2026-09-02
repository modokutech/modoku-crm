"""Shared upload validation — every file-upload route in Modoku Hub should
run incoming files through validate_upload() before saving them to disk.
Rejects files over the size cap and files whose extension isn't on the
allowlist for that kind of upload, so an oversized or unexpected file type
never reaches the filesystem.
"""

import os

MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB per file — the default cap everywhere

# Trainer documents (Trainer Profile, Accredited Certificate, TTT
# Certificate) get a stricter 1 MB cap — these are the ones most often
# attached together onto outgoing emails (see the HRDCorp Grant Documents
# email in sessions.py), so keeping them small matters more here than
# elsewhere to stay under mailer.MAX_TOTAL_ATTACHMENT_BYTES.
TRAINER_DOCUMENT_MAX_BYTES = 1 * 1024 * 1024  # 1 MB per file

# Lead Proposal Decks are often multi-slide PPT/PPTX exports with embedded
# images, which routinely blow past the default 2 MB cap — bumped just for
# this one field rather than raising the cap everywhere.
PROPOSAL_DECK_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file

DOCUMENT_EXTENSIONS = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "txt"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
CSV_EXTENSIONS = {"csv"}
# Most document-upload fields (signed POs/quotations, trainer invoices,
# certificates, banners, name cards...) accept a mix of documents and
# images since scanned signed copies are often photographed rather than
# scanned properly.
DEFAULT_EXTENSIONS = DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS


def _extension(filename):
    return (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()


def validate_upload(file_storage, allowed_extensions=None, max_bytes=MAX_UPLOAD_BYTES):
    """Returns an error message (str) if file_storage should be rejected, or
    None if it's fine to save. Checks: has a filename, extension is in the
    allowlist, isn't empty, and doesn't exceed max_bytes.

    Safe to call on an empty/unsubmitted FileStorage (what
    request.files.get(...) returns when the field wasn't filled in) — that
    case returns None (not an error); it's up to the caller to flash a
    "please choose a file" message if the field is required.
    """
    if allowed_extensions is None:
        allowed_extensions = DEFAULT_EXTENSIONS
    if not file_storage or not file_storage.filename:
        return None
    ext = _extension(file_storage.filename)
    if allowed_extensions and ext not in allowed_extensions:
        allowed = ", ".join(sorted(allowed_extensions))
        return f"\".{ext}\" files aren't allowed here. Accepted types: {allowed}."
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size == 0:
        return "That file is empty."
    if size > max_bytes:
        return f"File is too large ({size / (1024 * 1024):.1f} MB) — the maximum is {max_bytes // (1024 * 1024)} MB."
    return None
