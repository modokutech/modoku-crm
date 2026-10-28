"""Generates the "Training Evaluation" QR poster (JPEG) for a session —
a QR code linking to the evaluation form, styled to match Modoku's poster
template, with the course title and date underneath. No external QR
library is available in this environment, so the QR matrix itself is
built with reportlab's bundled pure-Python encoder
(reportlab.graphics.barcode.qr) and rendered/styled here with Pillow.
"""
import os

from PIL import Image, ImageDraw, ImageFont
from reportlab.graphics.barcode import qr

# Bundled inside the repo (not a system font package) so rendering doesn't
# depend on fonts happening to be installed on whatever server runs this —
# same self-contained-asset approach as the background image below.
FONT_DIR = os.path.join(os.path.dirname(__file__), "static", "fonts")
FONT_BOLD = os.path.join(FONT_DIR, "Poppins-Bold.ttf")
FONT_MEDIUM = os.path.join(FONT_DIR, "Poppins-Medium.ttf")
FONT_REGULAR = os.path.join(FONT_DIR, "Poppins-Regular.ttf")

# Same navy/indigo gradient background used across the front-end/guest pages
# (see .auth-wrap in style.css) — applied here too so the poster matches.
BG_IMAGE_PATH = os.path.join(os.path.dirname(__file__), "static", "img", "modoku-bg-gradient.jpg")
BG_COLOR = (46, 49, 146)       # deep indigo fallback, used if the image is ever missing
TITLE_COLOR = (255, 255, 255)
COURSE_COLOR = (255, 255, 255)
DATE_COLOR = (210, 213, 235)
QR_DARK = (17, 17, 17)


def _font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _qr_matrix(value):
    widget = qr.QrCodeWidget(value, barLevel="H")
    inner = widget.qr
    inner.make()
    n = inner.getModuleCount()
    return [[inner.isDark(r, c) for c in range(n)] for r in range(n)], n


def _draw_qr(value, box_size, logo_path=None):
    """Renders a styled (rounded-module) QR code onto a white square of
    box_size pixels, with the Modoku logo watermarked in the center."""
    matrix, n = _qr_matrix(value)
    quiet = 2  # modules of white margin inside the box
    cell = box_size / (n + quiet * 2)

    img = Image.new("RGB", (box_size, box_size), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    def is_finder(r, c):
        size = 7
        corners = [(0, 0), (0, n - size), (n - size, 0)]
        return any(cr <= r < cr + size and cc <= c < cc + size for cr, cc in corners)

    for r in range(n):
        for c in range(n):
            if not matrix[r][c]:
                continue
            x0 = (c + quiet) * cell
            y0 = (r + quiet) * cell
            x1 = x0 + cell
            y1 = y0 + cell
            pad = cell * 0.12
            radius = cell * 0.45 if not is_finder(r, c) else cell * 0.28
            draw.rounded_rectangle(
                [x0 + pad, y0 + pad, x1 - pad, y1 - pad], radius=radius, fill=QR_DARK
            )

    if logo_path and os.path.exists(logo_path):
        try:
            logo = Image.open(logo_path).convert("RGBA")
        except Exception:
            logo = None
        if logo:
            logo_w = int(box_size * 0.30)
            logo_h = int(logo_w * logo.height / logo.width)
            logo = logo.resize((logo_w, logo_h), Image.LANCZOS)
            pad_x, pad_y = int(logo_w * 0.18), int(logo_h * 0.28)
            plate = Image.new(
                "RGB", (logo_w + pad_x * 2, logo_h + pad_y * 2), (255, 255, 255)
            )
            plate.paste(logo, (pad_x, pad_y), logo)
            img.paste(
                plate,
                ((box_size - plate.width) // 2, (box_size - plate.height) // 2),
            )

    return img


def _wrap_text(draw, text, font, max_width):
    words = text.split()
    lines, line = [], ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _fill_resize(img, target_w, target_h):
    """Stretches an image to exactly fill target_w x target_h, ignoring its
    original aspect ratio. Used instead of a center-crop "cover" resize
    because the source gradient (1920x1080, landscape) is far wider than
    this poster's canvas (1200x1500, portrait) — cropping-to-cover a shape
    that different would slice off most of each side, right where the
    gradient's two corner accents live, leaving what looks like a plain
    flat color. This is a soft, blurred gradient with no straight lines or
    text in it, so stretching it non-uniformly to fit isn't visible —
    and it keeps both corner accents fully in frame, which is the point."""
    return img.resize((target_w, target_h), Image.LANCZOS)


def generate_evaluation_poster(course_title, date_text, form_link, logo_path=None):
    """Returns JPEG bytes for the Training Evaluation QR poster."""
    return generate_qr_poster("Training Evaluation", course_title, date_text, form_link,
                              logo_path=logo_path, square_qr=False)


def _fit_font(draw, text, path, size, max_width, min_size=40):
    """The largest font (from `size` down) that fits `text` on one line."""
    while size > min_size:
        font = _font(path, size)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 4
    return _font(path, min_size)


def generate_qr_poster(heading, course_title, date_text, link, logo_path=None, square_qr=True, instruction=None):
    """Returns JPEG bytes for a QR poster in the Training Evaluation poster's
    layout: heading at the top, the QR code in a white box, course title and
    date underneath. Fix179: shared by the QR Posters module (Pre/Post-
    Assessment Test etc.), which uses square_qr=True - the classic square
    modules phone cameras read most reliably - and an optional one-line
    `instruction` under the heading."""
    W, H = 1200, 1500
    try:
        canvas = _fill_resize(Image.open(BG_IMAGE_PATH).convert("RGB"), W, H)
    except Exception:
        canvas = Image.new("RGB", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    course_font = _font(FONT_BOLD, 46)
    date_font = _font(FONT_MEDIUM, 30)

    # Heading (shrinks to fit one line, for longer custom headings)
    title_font = _fit_font(draw, heading, FONT_BOLD, 72, W - 120)
    tw = draw.textlength(heading, font=title_font)
    draw.text(((W - tw) / 2, 90 if not instruction else 70), heading, font=title_font, fill=TITLE_COLOR)
    if instruction:
        ins_font = _font(FONT_REGULAR, 28)
        iw = draw.textlength(instruction, font=ins_font)
        draw.text(((W - iw) / 2, 172), instruction, font=ins_font, fill=DATE_COLOR)

    # QR box
    box_size = 900
    box_x = (W - box_size) // 2
    box_y = 250
    if square_qr:
        import io
        qr_img = Image.open(io.BytesIO(qr_png(link, box_size=box_size, logo_path=logo_path))).convert("RGB")
        if qr_img.size != (box_size, box_size):
            # qr_png sizes in whole pixels per module; centre it on a white box of exactly box_size.
            box = Image.new("RGB", (box_size, box_size), (255, 255, 255))
            qr_img.thumbnail((box_size, box_size), Image.NEAREST)
            box.paste(qr_img, ((box_size - qr_img.width) // 2, (box_size - qr_img.height) // 2))
            qr_img = box
    else:
        qr_img = _draw_qr(link, box_size, logo_path=logo_path)
    canvas.paste(qr_img, (box_x, box_y))

    # Course title + date below the box
    y = box_y + box_size + 60
    course_lines = _wrap_text(draw, course_title or "", course_font, W - 160)
    for line in course_lines[:2]:
        lw = draw.textlength(line, font=course_font)
        draw.text(((W - lw) / 2, y), line, font=course_font, fill=COURSE_COLOR)
        y += 58

    y += 10
    if date_text:
        dw = draw.textlength(date_text, font=date_font)
        draw.text(((W - dw) / 2, y), date_text, font=date_font, fill=DATE_COLOR)

    from io import BytesIO
    buf = BytesIO()
    canvas.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def qr_scans_to(jpeg_or_png_bytes, expected):
    """Fix179: True if a QR in the image decodes to `expected` (OpenCV's
    detector, as a stand-in for a phone camera). None when OpenCV isn't
    installed, so callers can skip the check rather than warn wrongly."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    img = cv2.imdecode(np.frombuffer(jpeg_or_png_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return False
    detector = cv2.QRCodeDetector()
    for scale in (1.0, 0.5):
        probe = img if scale == 1.0 else cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        data, _, _ = detector.detectAndDecode(probe)
        if data:
            return data == expected
    return False


def qr_png(value, box_size=900, logo_path=None):
    """Fix155: the T3 e-signature sign-in QR as PNG bytes. Classic square
    modules with a 4-module white margin (the rounded, gapped style of the
    evaluation poster didn't decode in testing), error correction H, and the
    Modoku logo on a white plate in the centre (well within what level H
    can recover)."""
    import io
    matrix, n = _qr_matrix(value)
    quiet = 4
    cell = max(1, box_size // (n + quiet * 2))
    size = cell * (n + quiet * 2)
    img = Image.new("RGB", (size, size), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for r in range(n):
        for c in range(n):
            if matrix[r][c]:
                x0, y0 = (c + quiet) * cell, (r + quiet) * cell
                draw.rectangle([x0, y0, x0 + cell - 1, y0 + cell - 1], fill=QR_DARK)
    if logo_path and os.path.exists(logo_path):
        try:
            logo = Image.open(logo_path).convert("RGBA")
            w = int(size * 0.22)
            h = int(logo.height * w / logo.width)
            logo = logo.resize((w, h))
            pad = int(size * 0.02)
            x, y = (size - w) // 2, (size - h) // 2
            draw.rounded_rectangle([x - pad, y - pad, x + w + pad, y + h + pad], radius=pad, fill=(255, 255, 255))
            img.paste(logo, (x, y), logo)
        except Exception:  # noqa: BLE001 - a missing/broken logo just means a plain QR
            pass
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def sign_in_poster_png(course_title, date_text, link, code=None, logo_path=None,
                       scan_text="Scan to sign your attendance",
                       hint_text="Find your name on the list and tap Sign."):
    """Fix223: the whole e-signature sign-in screen as one landscape PNG
    (2400x1350, 16:9 like the training-room screen): course title, date,
    the QR, "Scan to sign your attendance" and, in Code Attendance mode,
    today's code. Same layout as templates/t3/sign_in_qr.html."""
    import io
    W, H = 2400, 1350
    try:
        canvas = _fill_resize(Image.open(BG_IMAGE_PATH).convert("RGB"), W, H)
    except Exception:  # noqa: BLE001 - plain indigo if the background image is missing
        canvas = Image.new("RGB", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    def centred(text, font, y, fill):
        draw.text(((W - draw.textlength(text, font=font)) / 2, y), text, font=font, fill=fill)

    # Title: one line, shrinking to fit; a very long one wraps to two.
    title = course_title or ""
    title_font = _fit_font(draw, title, FONT_BOLD, 64, W - 300, min_size=48)
    title_lines = _wrap_text(draw, title, title_font, W - 300)[:2]
    title_lh = round(title_font.size * 1.3)
    date_font, scan_font, hint_font, code_font = (_font(FONT_REGULAR, 34), _font(FONT_BOLD, 54),
                                                  _font(FONT_REGULAR, 30), _font(FONT_BOLD, 46))
    card, pad = 760, 50

    # Everything stacked and centred on the page as one block, like the screen.
    total = (len(title_lines) * title_lh + (60 if date_text else 0) + 36 + card + 44 + 84 + 46
             + (74 if code else 0))
    y = max(30, (H - total) // 2)
    for line in title_lines:
        centred(line, title_font, y, TITLE_COLOR)
        y += title_lh
    if date_text:
        centred(date_text, date_font, y + 8, DATE_COLOR)
        y += 60
    y += 36

    # QR on a white rounded card with a soft shadow.
    from PIL import ImageFilter
    card_x, card_y = (W - card) // 2, y
    shadow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shadow).rounded_rectangle([card_x, card_y + 16, card_x + card, card_y + card + 16],
                                             radius=30, fill=90)
    shadow = shadow.filter(ImageFilter.GaussianBlur(28))
    canvas.paste(Image.new("RGB", (W, H), (0, 0, 0)), (0, 0), shadow)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle([card_x, card_y, card_x + card, card_y + card], radius=30, fill=(255, 255, 255))
    qr_img = Image.open(io.BytesIO(qr_png(link, box_size=card - 2 * pad, logo_path=logo_path))).convert("RGB")
    canvas.paste(qr_img, (card_x + (card - qr_img.width) // 2, card_y + (card - qr_img.height) // 2))

    y = card_y + card + 44
    centred(scan_text, scan_font, y, TITLE_COLOR)
    y += 84
    centred(hint_text, hint_font, y, DATE_COLOR)
    if code:
        y += 74
        lead = "Can't scan? Open the attendance link and enter today's code:"
        spacing = 12
        code_w = sum(draw.textlength(ch, font=code_font) for ch in code) + spacing * (len(code) - 1)
        lead_w = draw.textlength(lead, font=hint_font)
        x = (W - (lead_w + 24 + code_w)) / 2
        draw.text((x, y + 10), lead, font=hint_font, fill=DATE_COLOR)
        x += lead_w + 24
        for ch in code:
            draw.text((x, y - 2), ch, font=code_font, fill=TITLE_COLOR)
            x += draw.textlength(ch, font=code_font) + spacing

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
