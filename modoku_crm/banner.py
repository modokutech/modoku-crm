"""Generates the training banner image (PNG) for a class/session — the
Modoku wordmark on the right, an optional client logo on the left of a thin
divider, and the class title / time / date / venue underneath, styled to
match Modoku's reference banner template (deep indigo background with
colored corner shapes). Built with Pillow, same approach as poster.py.
"""
import os
from datetime import datetime
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

# Bundled inside the repo (not a system font package) so rendering doesn't
# depend on fonts happening to be installed on whatever server runs this —
# same self-contained-asset approach as the logo/background image.
FONT_DIR = os.path.join(os.path.dirname(__file__), "static", "fonts")
FONT_BOLD = os.path.join(FONT_DIR, "Poppins-Bold.ttf")
FONT_MEDIUM = os.path.join(FONT_DIR, "Poppins-Medium.ttf")
FONT_REGULAR = os.path.join(FONT_DIR, "Poppins-Regular.ttf")

W, H = 1920, 1080
BG_COLOR = (42, 54, 133)         # sampled from the reference banner
SHAPE_TAN = (161, 118, 86)
SHAPE_TEAL = (74, 131, 132)
SHAPE_BLUE = (73, 114, 164)
SHAPE_TERRACOTTA = (155, 74, 64)
TITLE_COLOR = (255, 255, 255)
SUBTITLE_COLOR = (255, 255, 255)
VENUE_COLOR = (214, 218, 240)
DIVIDER_COLOR = (255, 255, 255)


def _font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _wrap_text(draw, text, font, max_width, max_lines=2):
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
    return lines[:max_lines]


def _title_lines(draw, title, font, max_width):
    """Splits the title into up to two display lines. A colon in the title
    (e.g. "Away Day: Step Up and Stand Out 2023") is treated as the natural
    break, matching the reference banner's two-line layout; otherwise the
    title is word-wrapped."""
    title = (title or "").strip()
    if not title:
        return []
    if ":" in title:
        head, _, tail = title.partition(":")
        return [f"{head.strip()}:", tail.strip()]
    return _wrap_text(draw, title, font, max_width, max_lines=2)


def _draw_shapes(canvas):
    draw = ImageDraw.Draw(canvas, "RGBA")

    # Top-left circle
    r = 100
    cx, cy = 140, 140
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=SHAPE_TAN)

    # Top-right diamond (a square rotated 45°, cropped by the canvas edges)
    diamond_size = 260
    diamond = Image.new("RGBA", (diamond_size, diamond_size), (0, 0, 0, 0))
    dd = ImageDraw.Draw(diamond)
    dd.polygon(
        [(diamond_size / 2, 0), (diamond_size, diamond_size / 2),
         (diamond_size / 2, diamond_size), (0, diamond_size / 2)],
        fill=SHAPE_TEAL,
    )
    canvas.paste(diamond, (W - int(diamond_size * 0.72), -int(diamond_size * 0.32)), diamond)

    # Bottom-left rectangle, cropped by the bottom edge
    rect_w, rect_top = 145, 790
    draw.rectangle([85, rect_top, 85 + rect_w, H + 60], fill=SHAPE_BLUE)

    # Bottom-right circle, cropped by the corner
    r2 = 220
    cx2, cy2 = W - 70, H - 60
    draw.ellipse([cx2 - r2, cy2 - r2, cx2 + r2, cy2 + r2], fill=SHAPE_TERRACOTTA)


def _fmt_date_range(start_date, end_date):
    def parse(d):
        try:
            return datetime.strptime(d, "%Y-%m-%d")
        except (ValueError, TypeError):
            return None

    start = parse(start_date)
    if start is None:
        return start_date or ""
    end = parse(end_date) if end_date else None
    if end is None or end.date() == start.date():
        return start.strftime("%-d %B %Y")
    if end.year == start.year and end.month == start.month:
        return f"{start.day} - {end.strftime('%-d %B %Y')}"
    return f"{start.strftime('%-d %B %Y')} - {end.strftime('%-d %B %Y')}"


def generate_banner(title, training_time, start_date, end_date, venue,
                     modoku_logo_path=None, client_logo_path=None):
    """Returns PNG bytes for the training banner."""
    canvas = Image.new("RGB", (W, H), BG_COLOR)
    _draw_shapes(canvas)
    draw = ImageDraw.Draw(canvas)

    # --- Logo lockup: client logo | divider | Modoku logo, centered as a
    # group. If there's no client logo yet, just center the Modoku logo. ---
    logo_box_h = 170
    gap = 40

    def _load_logo(path, max_h):
        if not path or not os.path.exists(path):
            return None
        try:
            img = Image.open(path).convert("RGBA")
        except Exception:
            return None
        ratio = min(max_h / img.height, 420 / img.width)
        return img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))), Image.LANCZOS)

    client_logo = _load_logo(client_logo_path, logo_box_h)
    modoku_logo = _load_logo(modoku_logo_path, logo_box_h)

    pieces = []
    if client_logo is not None:
        pieces.append(("logo", client_logo))
        pieces.append(("divider", None))
    if modoku_logo is not None:
        pieces.append(("logo", modoku_logo))

    if pieces:
        divider_w = 3
        total_w = 0
        for kind, obj in pieces:
            total_w += obj.width if kind == "logo" else divider_w
            total_w += gap
        total_w -= gap

        x = (W - total_w) // 2
        lockup_y = 300
        for kind, obj in pieces:
            if kind == "logo":
                y = lockup_y + (logo_box_h - obj.height) // 2
                canvas.paste(obj, (x, y), obj)
                x += obj.width + gap
            else:
                draw.line([(x, lockup_y), (x, lockup_y + logo_box_h)], fill=DIVIDER_COLOR, width=divider_w)
                x += divider_w + gap

    # --- Title ---
    title_font = _font(FONT_BOLD, 84)
    lines = _title_lines(draw, title, title_font, W - 400)
    y = 560
    for line in lines:
        lw = draw.textlength(line, font=title_font)
        draw.text(((W - lw) / 2, y), line, font=title_font, fill=TITLE_COLOR)
        y += 96

    # --- Time / date ---
    y += 30
    date_text = _fmt_date_range(start_date, end_date)
    line1 = ", ".join(p for p in [training_time, date_text] if p)
    if line1:
        sub_font = _font(FONT_MEDIUM, 46)
        lw = draw.textlength(line1, font=sub_font)
        draw.text(((W - lw) / 2, y), line1, font=sub_font, fill=SUBTITLE_COLOR)
        y += 62

    # --- Venue ---
    if venue:
        venue_font = _font(FONT_MEDIUM, 34)
        line2 = f"@ {venue}"
        lw = draw.textlength(line2, font=venue_font)
        draw.text(((W - lw) / 2, y), line2, font=venue_font, fill=VENUE_COLOR)

    buf = BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()
