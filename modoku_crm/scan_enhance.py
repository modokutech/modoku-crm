""""Scan mode" for photos submitted through the public Return Attendance
Form (see attendance_return.py) — a trainer's phone photo of the signed T3
sheet is often at an angle, dim, or a little soft, so this turns it into a
flat, clean colour "scan" (white paper, no shadows, crisp ink - the
CamScanner-style look, Fix159) that's easier to read back in the office. Everything here is deterministic image processing (OpenCV) run
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
_MAX_DIMENSION = 3000

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
    "skip perspective correction, enhance the full photo instead".

    Fix159: tries a few edge/threshold maps (a plain Canny misses pages on
    a light desk or with a soft shadow across the edge) and keeps the
    largest convex 4-sided shape that's big enough to be the page."""
    import cv2
    import numpy as np

    h, w = gray.shape[:2]
    image_area = h * w
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)

    candidates = []
    canny = cv2.Canny(blurred, 50, 150)
    candidates.append(cv2.dilate(canny, np.ones((3, 3), np.uint8), iterations=1))
    canny_soft = cv2.Canny(blurred, 20, 80)
    candidates.append(cv2.morphologyEx(canny_soft, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8), iterations=2))
    _, otsu = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    candidates.append(cv2.morphologyEx(otsu, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8)))

    best, best_area = None, 0
    for edge_map in candidates:
        contours, _ = cv2.findContours(edge_map, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:8]:
            hull = cv2.convexHull(contour)
            area = cv2.contourArea(hull)
            if area < image_area * _MIN_DOCUMENT_AREA_FRACTION or area <= best_area:
                continue
            # A page that fills (almost) the whole photo is the photo edge, not the page.
            if area > image_area * 0.985:
                continue
            perimeter = cv2.arcLength(hull, True)
            for eps in (0.02, 0.03, 0.04):
                approx = cv2.approxPolyDP(hull, eps * perimeter, True)
                if len(approx) == 4 and cv2.isContourConvex(approx):
                    best, best_area = approx, area
                    break
    return _order_corners(best.astype("float32")) if best is not None else None


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


def _ink_mask(gray, corners):
    """Dark strokes (print, pen, stamps) that sit on paper-bright background,
    with single-pixel noise removed. Used to tell "the page" from "a box
    printed on the page" (Fix160). The brightness gate is what keeps a
    wood-grain or patterned desk from counting as writing: its local
    background is well below the paper's (measured inside `corners`)."""
    import cv2
    import numpy as np

    block = max(15, (min(gray.shape[:2]) // 40) | 1)
    ink = cv2.adaptiveThreshold(cv2.GaussianBlur(gray, (3, 3), 0), 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                cv2.THRESH_BINARY_INV, block, 15)
    ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))

    inside = np.zeros(gray.shape[:2], np.uint8)
    cv2.fillConvexPoly(inside, corners.astype(np.int32), 255)
    paper = float(np.percentile(gray[inside > 0], 75))
    k = block * 2 + 1
    local_bg = cv2.GaussianBlur(cv2.dilate(gray, np.ones((k, k), np.uint8)), (0, 0), block)
    ink[local_bg < paper * 0.8] = 0
    return ink


def _has_content_outside(ink, corners):
    """Fix160: True if there's real writing outside the quad - meaning the
    quad is the printed attendance table (or another box ON the page), not
    the paper itself. Happens when the paper edge doesn't show (white table,
    photo zoomed in on the page). Cropping to it would cut off names written
    under the table and the certification/signature block, so the caller
    keeps that content instead. A thin band just outside the quad and along
    the photo's own border is ignored (paper edge / desk edge, not writing)."""
    import cv2
    import numpy as np

    h, w = ink.shape[:2]
    band = max(3, int(min(h, w) * 0.02))
    inside = np.zeros((h, w), np.uint8)
    cv2.fillConvexPoly(inside, corners.astype(np.int32), 255)
    near = cv2.dilate(inside, np.ones((band * 2 + 1, band * 2 + 1), np.uint8))
    outside = near == 0
    outside[:band, :] = outside[-band:, :] = False
    outside[:, :band] = outside[:, -band:] = False
    outside_area = int(outside.sum())
    if outside_area < h * w * 0.03:
        return False
    out_ink = int((ink[outside] > 0).sum())
    # A plain or wood desk scores ~0 here; a page with writing round the
    # table scores 3-8%. 0.5% (and a minimum absolute amount) sits well clear
    # of both.
    return out_ink > outside_area * 0.005 and out_ink > h * w * 0.001


def _warp_keeping_content(image, corners, ink, det_scale):
    """Fix160: straighten using the quad's perspective (the table's ruled
    lines are a reliable rectangle), but keep everything that's written
    around it: the output is the bounding box of all ink once straightened,
    plus a margin - not just the quad."""
    import cv2
    import numpy as np

    (tl, tr, br, bl) = corners
    qw = max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl))
    qh = max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl))
    if qw < 10 or qh < 10:
        return None
    rect = np.array([[0, 0], [qw - 1, 0], [qw - 1, qh - 1], [0, qh - 1]], dtype="float32")
    matrix = cv2.getPerspectiveTransform(corners.astype("float32"), rect)

    # Where the whole photo lands once straightened (capped, so a strong
    # perspective can't stretch the far side to an enormous canvas).
    h, w = image.shape[:2]
    frame = cv2.perspectiveTransform(np.float32([[[0, 0]], [[w, 0]], [[w, h]], [[0, h]]]), matrix).reshape(4, 2)
    x0 = max(frame[:, 0].min(), -1.0 * qw)
    y0 = max(frame[:, 1].min(), -1.0 * qh)
    x1 = min(frame[:, 0].max(), 2.0 * qw)
    y1 = min(frame[:, 1].max(), 2.0 * qh)
    shift = np.array([[1, 0, -x0], [0, 1, -y0], [0, 0, 1]], dtype=np.float64)
    full = shift @ matrix
    out_w, out_h = int(x1 - x0), int(y1 - y0)

    # Find the content's extent on the small ink mask, warped the same way.
    down = np.diag([det_scale, det_scale, 1.0])
    up = np.diag([1.0 / det_scale, 1.0 / det_scale, 1.0])
    ink_w = cv2.warpPerspective(ink, down @ full @ up,
                                (max(1, int(out_w * det_scale)), max(1, int(out_h * det_scale))))
    # Rows/columns with a meaningful amount of ink - stray specks don't count.
    rows = np.where((ink_w > 0).sum(axis=1) > max(3, ink_w.shape[1] * 0.004))[0]
    cols = np.where((ink_w > 0).sum(axis=0) > max(3, ink_w.shape[0] * 0.004))[0]
    if len(rows) == 0 or len(cols) == 0:
        return None
    margin = 0.03 * max(qw, qh)
    cx0 = max(0, int(cols[0] / det_scale - margin))
    cy0 = max(0, int(rows[0] / det_scale - margin))
    cx1 = min(out_w, int(cols[-1] / det_scale + margin))
    cy1 = min(out_h, int(rows[-1] / det_scale + margin))
    crop = np.array([[1, 0, -cx0], [0, 1, -cy0], [0, 0, 1]], dtype=np.float64)
    return cv2.warpPerspective(image, crop @ full, (cx1 - cx0, cy1 - cy0),
                               flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def _flatten_lighting(image):
    """Fix159: the step that makes a phone photo look scanned. Estimates the
    paper's own brightness across the page (ink removed by a max-filter,
    then smoothed) and divides it out of each colour channel - shadows,
    vignetting and warm/dim light all become clean white paper, while ink
    keeps its colour (blue signatures stay blue)."""
    import cv2
    import numpy as np

    h, w = image.shape[:2]
    small_scale = 600.0 / max(h, w)
    small = cv2.resize(image, (max(1, int(w * small_scale)), max(1, int(h * small_scale))), interpolation=cv2.INTER_AREA)
    k = max(7, int(round(max(small.shape[:2]) / 45)) | 1)
    background = cv2.dilate(small, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    background = cv2.medianBlur(background, k if k <= 255 else 255)
    background = cv2.GaussianBlur(background, (0, 0), k / 2)
    background = cv2.resize(background, (w, h), interpolation=cv2.INTER_CUBIC)
    return cv2.divide(image, np.maximum(background, 1), scale=255)


def _scan_levels(flat):
    """Paper to pure white, ink deep and solid: a white point just under
    the paper, a black point from the darkest ink, and a gentle curve so
    faint pen strokes darken without the page turning harsh black-and-white."""
    import cv2
    import numpy as np

    sample = flat[::4, ::4].mean(axis=2)
    white = max(float(np.percentile(sample, 60)) * 0.97, 200.0)
    black = min(float(np.percentile(sample, 0.5)), 120.0)
    # One lookup table instead of float maths on every pixel.
    x = np.arange(256, dtype=np.float32)
    lut = (np.clip((x - black) / max(white - black, 1.0), 0, 1) ** 1.6 * 255.0).astype(np.uint8)
    return cv2.LUT(flat.astype(np.uint8), lut)


def enhance_to_scan(src_path, dst_path):
    """Reads the image at src_path, straightens it if a confident
    document-shaped region is found, turns it into a clean colour "scan"
    (white paper, no shadows, crisp ink) and writes the result to dst_path
    as a JPEG. Returns True on success, False on any failure (bad/corrupt
    file, OpenCV not installed, nothing readable) - never raises, so a bad
    submission can never break the trainer's upload. src_path is opened
    read-only and is never written to or deleted."""
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

        # Find the page on a smaller copy (faster, steadier), warp the full one.
        h, w = image.shape[:2]
        det_scale = 1000.0 / max(h, w) if max(h, w) > 1000 else 1.0
        det = cv2.resize(image, (int(w * det_scale), int(h * det_scale)), interpolation=cv2.INTER_AREA) if det_scale < 1 else image
        det_gray = cv2.cvtColor(det, cv2.COLOR_BGR2GRAY)
        corners = _find_document_corners(det_gray)
        working = image
        ink = _ink_mask(det_gray, corners) if corners is not None else None
        if corners is not None and _has_content_outside(ink, corners):
            # Fix160: that's the printed table, not the page - straighten by
            # it but keep names written below it, the signature block, etc.
            warped = _warp_keeping_content(image, corners / det_scale, ink, det_scale)
            if warped is not None:
                working = warped
        elif corners is not None:
            warped = _warp_to_corners(image, corners / det_scale)
            if warped is not None:
                # Trim a sliver off each edge so no desk shows along the border.
                th, tw = warped.shape[:2]
                m = max(2, int(min(th, tw) * 0.006))
                working = warped[m:th - m, m:tw - m]

        # Light, edge-keeping denoise first so the level stretch doesn't turn
        # sensor/JPEG noise into speckles (the white point then clears what's
        # left on the paper). A 3x3 median, not NL-means: same result here at
        # a fraction of the time, which matters as it runs during the upload.
        working = cv2.medianBlur(working, 3)
        result = _scan_levels(_flatten_lighting(working))
        # Gentle unsharp mask for crisp strokes (small radius, so no halo).
        soft = cv2.GaussianBlur(result, (0, 0), sigmaX=1.0)
        result = cv2.addWeighted(result, 1.6, soft, -0.6, 0)

        ok = cv2.imwrite(dst_path, result, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        return bool(ok)
    except Exception:  # noqa: BLE001 - enhancement must never break the upload it's enhancing
        logger.exception("Scan-mode enhancement failed for %s", src_path)
        return False
