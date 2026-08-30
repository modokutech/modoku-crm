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

FONT_DIR = "/usr/share/fonts/truetype/google-fonts"
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
    W, H = 1200, 1500
    try:
        canvas = _fill_resize(Image.open(BG_IMAGE_PATH).convert("RGB"), W, H)
    except Exception:
        canvas = Image.new("RGB", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    title_font = _font(FONT_BOLD, 72)
    course_font = _font(FONT_BOLD, 46)
    date_font = _font(FONT_MEDIUM, 30)

    # Title
    title = "Training Evaluation"
    tw = draw.textlength(title, font=title_font)
    draw.text(((W - tw) / 2, 90), title, font=title_font, fill=TITLE_COLOR)

    # QR box
    box_size = 900
    box_x = (W - box_size) // 2
    box_y = 250
    qr_img = _draw_qr(form_link, box_size, logo_path=logo_path)
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
