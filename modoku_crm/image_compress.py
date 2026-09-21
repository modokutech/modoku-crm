"""Shared photo compression for upload fields where a modern phone's
30-50 MP camera routinely produces an 8-20 MB JPEG for what is, on screen,
an ordinary photo: the Return Attendance Form (attendance_return.py), the
public Claim submission page (claims.py), and the Name Card upload
(leads.py). Resizes down to a resolution far past what any of these are
ever read at, then re-encodes as JPEG at a quality setting few people
could tell apart from the original — shrinking the file, not visibly
degrading it.

Runs in memory, on the raw uploaded bytes, BEFORE the field's own size cap
is checked (see maybe_compress()) — so an oversized raw phone photo that
would have been rejected outright gets a chance to shrink under the cap
first, instead of every field needing an ever-larger cap to keep up with
newer phone cameras.

Optional/best-effort, same pattern as scan_enhance.py and ai_match.py: if
Pillow can't read the file for any reason (corrupt image, unsupported
format, a decompression-bomb-sized input), the original bytes are used
untouched and the caller's normal validate_upload() decides its fate —
compression failing never blocks an upload by itself.
"""
import io
import logging
import os

from . import uploadutil

logger = logging.getLogger(__name__)

MAX_DIMENSION = 2000  # long edge, px — matches scan_enhance.py's own cap
JPEG_QUALITY = 85

# A raw upload past this size is left alone (no compression attempted) and
# falls through to the caller's own size check, which will reject it with
# its usual message — guards against spending real work (a full decode) on
# something absurd. Comfortably above what any real phone photo produces
# (a 48 MP iPhone JPEG is typically 8-15 MB).
RAW_INPUT_CEILING_BYTES = 25 * 1024 * 1024  # 25 MB


def is_available():
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def compress_photo(raw_bytes, filename):
    """Returns (bytes, filename) — a resized, re-encoded JPEG when Pillow
    can process the image and doing so actually shrinks it, or the
    original bytes and filename unchanged otherwise (not an image Pillow
    can read, an image already small/simple enough that re-encoding
    wouldn't help, or Pillow unavailable). Never raises.
    """
    if not raw_bytes or len(raw_bytes) > RAW_INPUT_CEILING_BYTES or not is_available():
        return raw_bytes, filename
    try:
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(raw_bytes))
        img = ImageOps.exif_transpose(img)  # respect the phone's camera orientation before it's lost
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > MAX_DIMENSION:
            scale = MAX_DIMENSION / max(w, h)
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        compressed = buf.getvalue()
        if len(compressed) >= len(raw_bytes):
            # Rare (a small/simple image, or an already heavily-compressed
            # source) — the original was already smaller than our re-encode.
            return raw_bytes, filename
        base = os.path.splitext(filename)[0] if filename else "photo"
        return compressed, f"{base}.jpg"
    except Exception:
        logger.exception("Photo compression failed for %s — using the original file", filename)
        return raw_bytes, filename


def maybe_compress(file_storage):
    """Drop-in replacement step for a file-upload loop: pass the raw
    FileStorage in, get back either the same object (not an image
    extension, or compression made no worthwhile difference) or a new
    in-memory FileStorage carrying the compressed JPEG bytes — ready to
    hand to uploadutil.validate_upload() exactly as before. Only files
    whose extension is already in uploadutil.IMAGE_EXTENSIONS are touched;
    anything else (a PDF, a Word doc, ...) passes through unchanged.
    """
    filename = file_storage.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in uploadutil.IMAGE_EXTENSIONS:
        return file_storage
    file_storage.stream.seek(0)
    raw_bytes = file_storage.stream.read()
    file_storage.stream.seek(0)
    compressed, new_filename = compress_photo(raw_bytes, filename)
    if compressed is raw_bytes:
        return file_storage
    from werkzeug.datastructures import FileStorage
    return FileStorage(stream=io.BytesIO(compressed), filename=new_filename, content_type="image/jpeg")
