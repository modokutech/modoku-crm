""""Scan mode" for photos submitted through the public Return Attendance
Form (see attendance_return.py) — a trainer's phone photo of the signed T3
sheet is often at an angle, dim, or a little soft, so this turns it into a
flat, contrast-enhanced, crisper version that's easier to read back in the
office. Everything here is deterministic image processing (OpenCV) run
entirely on our own server — no external API, no account, no per-use cost,
and nothing generative: it straightens and sharpens what's actually in the
photo, it never invents detail that wasn't there.

Deliberately NEVER touches or replaces the original upload. This module
only ever writes a SEPARATE file; the caller decides what to do with it
(attendance_return.py saves it as attendance_returns.enhanced_filename,
alongside the untouched original in `filename`). Best-effort throughout —
any failure (corrupt image, no document-shaped region found, missing
OpenCV) falls back to producing nothing rather than raising, so a bad
photo can never block a trainer's submission.
"""
import logging

logger = logging.getLogger(__name__)

# Cap the working resolution before processing - a modern phone photo can be
# 4000px+ on the long side, which makes contour detection and filtering slow
# for no real benefit: the output here is for on-screen viewing, not print,
# so anything beyond this is downscaled first (aspect ratio kept).
_MAX_DIMENSION = 2000

# A candidate 4-point contour has to cover at least this fraction of the
# photo's area to be trusted as "the document" rather than some other
# rectangular thing in frame (a table edge, a clipboard, a shadow). Below
# this, perspective correction is skipped and the enhancement steps run on
# the photo as-is - a safe, visible-improvement fallback rather than a risky
# guess at the document's corners.
_MIN_DOCUMENT_AREA_FRACTION = 0.20

JPEG_QUALITY = 92


def is_available():
    """True if OpenCV is importable - the caller checks this before trying
    to enhance anything, so a deployment that hasn't installed the optional
    opencv-python-headless dependency yet just skips enhancement entirely
    (same optional-feature pattern as ai_match.is_configured())."""
    try:
        import cv2  # noqa: F401
    except ImportError:
        return False
    return True


def _order_corners(pts):
    """Given 4 (x, y) points in any order, returns them as
    [top-left, top-right, bottom-right, bottom-left] - the order
    cv2.getPerspectiveTransform needs. Standard sum/diff trick: top-left
    has the smallest x+y, bottom-right the largest; top-right has the
    smallest y-x, bottom-left the largest."""
    import numpy as np

    pts = pts.reshape(4, 2)
    ordered = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    ordered[0] = pts[np.argmin(s)]
    ordered[2] = pts[np.argmax(s)]
    diff = np.diff(pts, axis=1)
    ordered[1] = pts[np.argmin(diff)]
    ordered[3] = pts[np.argmax(diff)]
    return ordered


def _find_document_corners(gray):
    """Best-effort search for a 4-cornered document-shaped region in a
    grayscale image. Returns an ordered (4, 2) float32 array of corners, or
    None if nothing confident enough was found - callers treat None as
    "skip perspective correction, enhance the full photo instead"."""
    import cv2
    import numpy as np

    h, w = gray.shape[:2]
    image_area = h * w

    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)[:5]

    for contour in contours:
        area = cv2.contourArea(contour)
        if area < image_area * _MIN_DOCUMENT_AREA_FRACTION:
            continue
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approx) == 4:
            return _order_corners(approx.astype("float32"))
    return None


def _warp_to_corners(image, corners):
    """Perspective-correct `image` so the quadrilateral `corners` becomes a
    flat, filled rectangle - the actual "straighten a photographed page"
    step. Output size is derived from the corners' own measured
    width/height so text isn't stretched or squashed."""
    import cv2
    import numpy as np

    (tl, tr, br, bl) = corners
    width_a = np.linalg.norm(br - bl)
    width_b = np.linalg.norm(tr - tl)
    max_width = max(int(width_a), int(width_b))

    height_a = np.linalg.norm(tr - br)
    height_b = np.linalg.norm(tl - bl)
    max_height = max(int(height_a), int(height_b))

    if max_width < 10 or max_height < 10:
        return None

    dst = np.array(
        [[0, 0], [max_width - 1, 0], [max_width - 1, max_height - 1], [0, max_height - 1]],
        dtype="float32",
    )
    matrix = cv2.getPerspectiveTransform(corners, dst)
    return cv2.warpPerspective(image, matrix, (max_width, max_height))


def enhance_to_scan(src_path, dst_path):
    """Reads the image at src_path, straightens it if a confident
    document-shaped region is found, contrast-enhances and sharpens it, and
    writes the result to dst_path as a JPEG. Returns True on success, False
    on any failure (bad/corrupt file, OpenCV not installed, nothing
    readable) - never raises, so a bad submission can never break the
    trainer's upload. src_path is opened read-only and is never written
    to or deleted."""
    if not is_available():
        return False
    try:
        import cv2
        import numpy as np

        image = cv2.imread(src_path, cv2.IMREAD_COLOR)
        if image is None:
            return False

        h, w = image.shape[:2]
        if max(h, w) > _MAX_DIMENSION:
            scale = _MAX_DIMENSION / max(h, w)
            image = cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        corners = _find_document_corners(gray)
        working = image
        if corners is not None:
            warped = _warp_to_corners(image, corners)
            if warped is not None:
                working = warped

        gray = cv2.cvtColor(working, cv2.COLOR_BGR2GRAY)
        # Light denoise before sharpening, so contrast/sharpening doesn't
        # also amplify JPEG/sensor noise into visible speckling.
        denoised = cv2.fastNlMeansDenoising(gray, h=7)
        # CLAHE: local (not global) contrast boost - brings out faint pen
        # strokes/print without blowing out already-bright paper the way a
        # flat contrast stretch would.
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        contrasted = clahe.apply(denoised)
        # Unsharp mask: blur a copy, then push the original further away
        # from that blur - the standard "make edges crisper" sharpening
        # technique, applied lightly so text doesn't get a harsh halo.
        blurred = cv2.GaussianBlur(contrasted, (0, 0), sigmaX=3)
        sharpened = cv2.addWeighted(contrasted, 1.5, blurred, -0.5, 0)

        result = cv2.cvtColor(sharpened, cv2.COLOR_GRAY2BGR)
        ok = cv2.imwrite(dst_path, result, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        return bool(ok)
    except Exception:  # noqa: BLE001 - enhancement must never break the upload it's enhancing
        logger.exception("Scan-mode enhancement failed for %s", src_path)
        return False
