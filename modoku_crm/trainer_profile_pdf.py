"""Trainer Profile PDF generator.

Builds the multi-page, brand-styled "Trainer Profile" brochure Erik
previously had to put together by hand in an external tool and upload
through trainers.profile_file (see DOCUMENT_FIELDS in trainers.py) - the
same slot a class's HRDCorp Grant Documents pack already pulls in
automatically (sessions.py), and the same slot the class page links as
"Trainer Profile". Structured content lives in the trainer_profile_*
tables (db.py, one row per trainer plus several repeatable child lists);
this module turns that data into the actual document.

Pillow-composited onto a plain image canvas per page, the same approach
certificates.py and poster.py already use for this app's other
pixel-precise, heavily-decorated brand documents, rather than HTML run
through wkhtmltopdf like most of the rest of this app. That's a deliberate
choice here too: this profile's left-hand colour sidebar has to repaint,
full page height, on every page while the right-hand column's text flows
and paginates on its own - and a spike test in this environment
(wkhtmltopdf 0.12.6) showed a `position: fixed; height: 100%` sidebar
paints correctly on a page that's fully used, but gets clipped short on
a page whose flowed content doesn't fill it (exactly the kind of page a
short trailing certificate list produces). Rather than fight that, this
module runs its own simple text-flow paginator over the right column and
draws the sidebar itself, once per page, at a size it controls exactly.

Colours, the sidebar's width, and its decorative triangles/icon were
measured directly off Erik's two reference PDFs (rendered at 300dpi and
pixel-sampled) so the layout matches "as exactly as possible", per his
request - down to reusing this app's existing certificate colour
constants (pdfgen._CERT_NAVY / _CERT_ORANGE), since they came back an
almost exact pixel match for this document's navy/gold too. The one thing
deliberately NOT reverse-engineered pixel-by-pixel is the very faint
full-bleed diagonal watermark visible behind some reference body text -
a low-emphasis flourish that isn't worth the fragility of trying to trace
it exactly; everything structural (sidebar ratio, triangle vertices,
photo/pill/logo position, section order, fonts) is matched from real
measurements.

Two typefaces, matching what Erik specified: Morion (attached by him -
Morion Beta Regular / Morion Bold) for every section heading and every
paragraph/entry of body content on the right-hand column, the credentials
line under the name, and the sidebar's own Areas of Expertise / Companies
Trained list content - and Poppins (already Modoku's brand face elsewhere
in this app) for just the trainer's name itself and the sidebar's pill
labels ("AREAS OF EXPERTISE" / "COMPANIES TRAINED").
"""
import os
from io import BytesIO

from flask import current_app
from PIL import Image, ImageDraw, ImageFont

from . import pdfgen

# --- Canvas -----------------------------------------------------------
PAGE_W_MM = 210.0
PAGE_H_MM = 297.0
DPI = 200
_PX_PER_MM = DPI / 25.4
_PX_PER_PT = DPI / 72
PAGE_W_PX = round(PAGE_W_MM * _PX_PER_MM)
PAGE_H_PX = round(PAGE_H_MM * _PX_PER_MM)

SIDEBAR_W_MM = 72.5   # measured off the reference PDFs - see module docstring
SIDEBAR_W_PX = round(SIDEBAR_W_MM * _PX_PER_MM)

CONTENT_LEFT_MM = SIDEBAR_W_MM + 10.0
CONTENT_RIGHT_MARGIN_MM = 13.0
CONTENT_TOP_MM = 18.0
CONTENT_BOTTOM_MM = 18.0

# --- Colours - reusing this app's existing certificate brand constants,
# confirmed by pixel-sampling the reference Trainer Profile PDFs to be an
# almost exact match for this document's navy/gold too.
NAVY = pdfgen._CERT_NAVY
ORANGE = pdfgen._CERT_ORANGE
DARK = pdfgen._CERT_DARK
MID = pdfgen._CERT_GRAY
WHITE = (255, 255, 255)
PURPLE_ACCENT = (109, 76, 148)
TRIANGLE_ICON_FILL = (243, 244, 249)

# --- Fonts --------------------------------------------------------------
_FONT_DIR = os.path.join(os.path.dirname(__file__), "static", "fonts")
_MORION_REGULAR = os.path.join(_FONT_DIR, "Morion-Regular.ttf")
_MORION_BOLD = os.path.join(_FONT_DIR, "Morion-Bold.ttf")
_POPPINS_REGULAR = os.path.join(_FONT_DIR, "Poppins-Regular.ttf")
_POPPINS_MEDIUM = os.path.join(_FONT_DIR, "Poppins-Medium.ttf")
_POPPINS_BOLD = os.path.join(_FONT_DIR, "Poppins-Bold.ttf")

_font_cache = {}


def _font(path, size_pt):
    key = (path, size_pt)
    f = _font_cache.get(key)
    if f is None:
        try:
            f = ImageFont.truetype(path, round(size_pt * _PX_PER_PT))
        except OSError:
            f = ImageFont.load_default()
        _font_cache[key] = f
    return f


def mm(v):
    return v * _PX_PER_MM


# A throwaway Draw used purely for text measurement before any page canvas
# exists - Pillow's textlength/textbbox don't need to be bound to the
# canvas actually being painted.
_MEASURE = ImageDraw.Draw(Image.new("RGB", (10, 10)))


def _wrap(text, font, max_width):
    words = (text or "").split()
    if not words:
        return [""]
    lines, line = [], ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if _MEASURE.textlength(candidate, font=font) <= max_width:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines or [""]


def _line_height(font, leading=1.28):
    ascent, descent = font.getmetrics()
    return round((ascent + descent) * leading)


# --- Right-column content blocks ----------------------------------------
# Each block is a small dict: {"kind": ..., plus whatever draw() needs}.
# measure() returns the pixel height it will occupy at a given width;
# draw() paints it and returns the height actually used (same value,
# kept separate only so a block could special-case drawing later).

_H_FONT = lambda: _font(_MORION_BOLD, 15.5)
_BODY_FONT = lambda: _font(_MORION_REGULAR, 11)
_ENTRY_TITLE_FONT = lambda: _font(_MORION_BOLD, 11.3)
_ENTRY_DETAIL_FONT = lambda: _font(_MORION_REGULAR, 10)
_BULLET_FONT = lambda: _font(_MORION_REGULAR, 10.8)

_SECTION_GAP = round(mm(7))    # space before a new section heading
_PARA_GAP = round(mm(3.2))     # space between paragraphs / entries
_HEADING_GAP = round(mm(2.6))  # space between a heading and its first line


def _heading_block(title):
    font = _H_FONT()
    lh = _line_height(font, 1.15)
    return {
        "kind": "heading", "title": title, "font": font,
        "height": lh, "before": _SECTION_GAP,
    }


def _paragraph_block(text, width):
    font = _BODY_FONT()
    lines = []
    for para in (text or "").split("\n\n"):
        para = para.strip()
        if not para:
            continue
        lines.extend(_wrap(para, font, width))
        lines.append("")  # blank line between paragraphs
    while lines and lines[-1] == "":
        lines.pop()
    lh = _line_height(font)
    return {
        "kind": "paragraph", "lines": lines, "font": font,
        "height": lh * max(1, len(lines)), "line_height": lh, "before": 0,
    }


def _entry_block(title, detail, width, before=_PARA_GAP):
    tfont = _ENTRY_TITLE_FONT()
    dfont = _ENTRY_DETAIL_FONT()
    title_lines = _wrap(title, tfont, width) if title else []
    detail_lines = _wrap(detail, dfont, width) if detail else []
    tlh = _line_height(tfont, 1.2)
    dlh = _line_height(dfont, 1.25)
    height = tlh * len(title_lines) + (round(mm(0.8)) if title_lines and detail_lines else 0) + dlh * len(detail_lines)
    return {
        "kind": "entry", "title_lines": title_lines, "detail_lines": detail_lines,
        "tfont": tfont, "dfont": dfont, "tlh": tlh, "dlh": dlh,
        "height": height, "before": before,
    }


def _oneline_entry_block(text, width, before=round(mm(2.4))):
    font = _ENTRY_DETAIL_FONT()
    lines = _wrap(text, font, width)
    lh = _line_height(font, 1.25)
    return {
        "kind": "oneline", "lines": lines, "font": font, "lh": lh,
        "height": lh * len(lines), "before": before,
    }


def _bullet_block(text, width, before=round(mm(1.6))):
    font = _BULLET_FONT()
    indent = round(mm(4.5))
    lines = _wrap(text, font, width - indent)
    lh = _line_height(font, 1.25)
    return {
        "kind": "bullet", "lines": lines, "font": font, "lh": lh, "indent": indent,
        "height": lh * len(lines), "before": before,
    }


def _draw_block(canvas, draw, block, x, y, width):
    """Draws one block at (x, y); returns the y position just below it
    (i.e. y + block['before'] + block['height'])."""
    y += block.get("before", 0)
    kind = block["kind"]
    if kind == "heading":
        draw.text((x, y), block["title"], font=block["font"], fill=NAVY)
        y += block["height"]
    elif kind == "paragraph":
        for line in block["lines"]:
            if line:
                draw.text((x, y), line, font=block["font"], fill=DARK)
            y += block["line_height"]
    elif kind == "entry":
        for line in block["title_lines"]:
            draw.text((x, y), line, font=block["tfont"], fill=NAVY)
            y += block["tlh"]
        if block["title_lines"] and block["detail_lines"]:
            y += round(mm(0.8))
        for line in block["detail_lines"]:
            draw.text((x, y), line, font=block["dfont"], fill=MID)
            y += block["dlh"]
    elif kind == "oneline":
        for line in block["lines"]:
            draw.text((x, y), line, font=block["font"], fill=DARK)
            y += block["lh"]
    elif kind == "bullet":
        first = True
        for line in block["lines"]:
            prefix_x = x
            text_x = x + block["indent"]
            if first:
                # Morion (this block's font) has no middle-dot glyph
                # (U+00B7) - confirmed via its cmap - so this uses the
                # bullet glyph (U+2022) instead, which it does have.
                draw.text((prefix_x, y), "•", font=block["font"], fill=NAVY)
            draw.text((text_x, y), line, font=block["font"], fill=DARK)
            y += block["lh"]
            first = False
    return y


def _joined(*parts):
    return " | ".join(p for p in parts if p)


def _build_right_blocks(profile, academic, certifications, experience, sections, width):
    """Returns the ordered list of content blocks that flow down the right
    column, starting from Background. The name header and the "ABOUT THE
    TRAINER" pill are handled separately by the pager (header repeats every
    page; the pill only appears once, before this list's first block)."""
    blocks = []

    if profile.get("background_text"):
        blocks.append(_heading_block("Background"))
        blocks.append(_paragraph_block(profile["background_text"], width))

    if profile.get("experience_text"):
        blocks.append(_heading_block("Experience"))
        blocks.append(_paragraph_block(profile["experience_text"], width))

    if academic:
        blocks.append(_heading_block("Academic Qualifications"))
        for i, row in enumerate(academic):
            detail = _joined(row.get("institution"), row.get("year"), row.get("location"))
            blocks.append(_entry_block(row["qualification"], detail, width,
                                        before=0 if i == 0 else round(mm(4))))

    if certifications:
        blocks.append(_heading_block("Professional Certifications"))
        for i, row in enumerate(certifications):
            text = _joined(row["title"], row.get("issuer"))
            blocks.append(_oneline_entry_block(text, width, before=0 if i == 0 else round(mm(2.4))))

    if experience:
        blocks.append(_heading_block("Professional Experience"))
        for i, row in enumerate(experience):
            title = _joined(row["company"], f"({row['period']})" if row.get("period") else None)
            blocks.append(_entry_block(title, row.get("role"), width,
                                        before=0 if i == 0 else round(mm(4))))

    for section in sections:
        bullets = section.get("bullets") or []
        if not bullets:
            continue
        blocks.append(_heading_block(section["title"]))
        for i, item in enumerate(bullets):
            blocks.append(_bullet_block(item, width, before=0 if i == 0 else round(mm(1.6))))

    return blocks


def _paginate_right(blocks, top_y, bottom_y, first_page_extra_top=0):
    """Splits blocks across pages of the right column. Returns a list of
    (page_blocks, start_y) - start_y is always top_y; first_page_extra_top
    (the "ABOUT THE TRAINER" pill's height) is only consumed on page 0 by
    the caller, not here. A section heading is never left as the last
    thing on a page - if there isn't room for it plus at least its first
    entry/line, both defer to the next page together."""
    pages = []
    i = 0
    n = len(blocks)
    usable = bottom_y - top_y
    while i < n or not pages:
        page = []
        y = 0
        budget = usable - (first_page_extra_top if not pages else 0)
        while i < n:
            b = blocks[i]
            h = b.get("before", 0) + b["height"]
            is_heading = b["kind"] == "heading"
            lookahead_h = 0
            if is_heading and i + 1 < n:
                nb = blocks[i + 1]
                lookahead_h = nb.get("before", 0) + nb["height"]
            needed = h + (lookahead_h if is_heading else 0)
            if y + needed > budget and page:
                break
            if y + h > budget and not page:
                # a single block taller than a whole page - place what we
                # can rather than looping forever; simplest safe fallback
                # is to just place it (it will visually overflow slightly
                # rather than vanish or infinite-loop).
                pass
            page.append(b)
            y += h
            i += 1
        pages.append(page)
        if i >= n:
            break
    return pages


# --- Sidebar --------------------------------------------------------------

def _draw_sidebar(canvas, draw, pill_label, list_items, photo_img):
    draw.rectangle([0, 0, SIDEBAR_W_PX, PAGE_H_PX], fill=NAVY)

    # Bottom-right decorative arrow, vertices measured off the reference
    # (see module docstring).
    draw.polygon(
        [
            (mm(SIDEBAR_W_MM), mm(202.4)),
            (mm(8.3), mm(271.1)),
            (mm(SIDEBAR_W_MM), mm(277.4)),
        ],
        fill=ORANGE,
    )

    # Small top-left corner icon: a purple diamond sliver behind a white
    # "play" triangle, echoing the cover page's triangle motif.
    _corner_diamond(canvas, size_mm=15, top_mm=-9, left_mm=-9, color=PURPLE_ACCENT)
    draw.polygon(
        [(mm(6.5), mm(2.7)), (mm(6.5), mm(18.7)), (mm(19.7), mm(10.2))],
        fill=TRIANGLE_ICON_FILL,
    )

    # modoku wordmark, bottom-left.
    try:
        logo = Image.open(os.path.join(current_app.root_path, "static", "img", "logo.png")).convert("RGBA")
        logo_w = round(mm(26))
        logo_h = round(logo_w * logo.height / logo.width)
        logo = logo.resize((logo_w, logo_h), Image.LANCZOS)
        canvas.paste(logo, (round(mm(7)), round(mm(281))), logo)
    except Exception:  # noqa: BLE001 - a missing logo shouldn't break the whole page
        current_app.logger.exception("Failed to composite logo onto trainer profile sidebar")

    # Circular photo - contain-fit, not cropped: the whole photo stays
    # visible, scaled down (preserving aspect ratio) to fit within the
    # circle and centered, rather than center-cropped to fill it (which
    # could cut off the top of a head or the sides of a wider shot). Any
    # gap around the photo within the circle is filled with the sidebar's
    # own navy, so it reads as the photo floating on the sidebar rather
    # than sitting in a visible box.
    photo_d = round(mm(40))
    photo_cx = SIDEBAR_W_PX / 2
    photo_top = mm(16)
    if photo_img is not None:
        photo = photo_img.copy().convert("RGB")
        scale = photo_d / max(photo.size)
        fit_w = max(1, round(photo.width * scale))
        fit_h = max(1, round(photo.height * scale))
        photo = photo.resize((fit_w, fit_h), Image.LANCZOS)
        square = Image.new("RGB", (photo_d, photo_d), NAVY)
        square.paste(photo, ((photo_d - fit_w) // 2, (photo_d - fit_h) // 2))
        mask = Image.new("L", (photo_d, photo_d), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, photo_d, photo_d], fill=255)
        canvas.paste(square, (round(photo_cx - photo_d / 2), round(photo_top)), mask)
    # A single ring, drawn straddling the photo circle's own edge (not
    # offset outward with a gap) so it reads as one clean boundary.
    ring_w = 4
    ring_bbox = [photo_cx - photo_d / 2 - ring_w / 2, photo_top - ring_w / 2,
                 photo_cx + photo_d / 2 + ring_w / 2, photo_top + photo_d + ring_w / 2]
    draw.ellipse(ring_bbox, outline=ORANGE, width=ring_w)

    # Pill label + list.
    pill_font = _font(_POPPINS_BOLD, 9.5)
    pill_text = pill_label.upper()
    pad_x, pad_y = round(mm(3)), round(mm(1.6))
    tw = draw.textlength(pill_text, font=pill_font)
    th = _line_height(pill_font, 1.0)
    pill_x, pill_y = round(mm(6)), round(mm(70))
    draw.rectangle([pill_x, pill_y, pill_x + tw + 2 * pad_x, pill_y + th + 2 * pad_y], fill=ORANGE)
    draw.text((pill_x + pad_x, pill_y + pad_y), pill_text, font=pill_font, fill=NAVY)

    # Content is Morion (the pill label above stays Poppins) - per Erik's
    # request. Bullet char is U+2022, not U+00B7: Morion has no middle-dot
    # glyph (same issue already found and fixed for the right column's own
    # bullet lists - see _draw_block's "bullet" case).
    list_font = _font(_MORION_REGULAR, 10.2)
    list_lh = _line_height(list_font, 1.35)
    list_x = round(mm(6))
    list_width = SIDEBAR_W_PX - round(mm(10))
    y = pill_y + th + 2 * pad_y + round(mm(4))
    triangle_top = round(mm(202.4))
    max_y = triangle_top - round(mm(4))
    for item in list_items:
        for line in _wrap(f"• {item}", list_font, list_width):
            if y + list_lh > max_y:
                return
            draw.text((list_x, y), line, font=list_font, fill=WHITE)
            y += list_lh


def _corner_diamond(canvas, size_mm, top_mm, left_mm, color):
    """A 45°-rotated square, mostly off-page, leaving a triangular sliver
    visible in the top-left corner - same technique as pdfgen._cert_shape,
    reimplemented locally since that helper is wired to the certificate's
    own page-size globals."""
    size_px = round(mm(size_mm))
    top_px = round(mm(top_mm))
    left_px = round(mm(left_mm))
    square = Image.new("RGBA", (size_px, size_px), color + (255,))
    rotated = square.rotate(45, expand=True, resample=Image.BICUBIC)
    canvas.paste(rotated, (left_px, top_px), rotated)


# --- Name header (repeats on every content page) --------------------------

def _draw_name_header(canvas, draw, x, top_y, width, full_name, credentials_line):
    name_font = _font(_POPPINS_BOLD, 33.75)  # 27 * 1.25, per Erik's request
    parts = (full_name or "").split()
    if len(parts) > 1:
        line1, line2 = " ".join(parts[:-1]).upper(), parts[-1].upper()
    else:
        line1, line2 = (full_name or "").upper(), ""
    lh = _line_height(name_font, 0.9)  # tightened from 1.05 - lines sat too far apart
    y = top_y
    for line in _wrap(line1, name_font, width):
        draw.text((x, y), line, font=name_font, fill=NAVY)
        y += lh
    for line in _wrap(line2, name_font, width) if line2 else []:
        draw.text((x, y), line, font=name_font, fill=ORANGE)
        y += lh
    if credentials_line:
        cred_font = _font(_MORION_REGULAR, 12)  # was Poppins - Erik wants Morion here too
        y += round(mm(2))
        for line in _wrap(credentials_line, cred_font, width):
            draw.text((x, y), line, font=cred_font, fill=MID)
            y += _line_height(cred_font, 1.3)
    return y


def _draw_about_pill(draw, x, y):
    font = _font(_POPPINS_BOLD, 10.5)
    text = "ABOUT THE TRAINER"
    pad_x, pad_y = round(mm(3.2)), round(mm(1.8))
    tw = draw.textlength(text, font=font)
    th = _line_height(font, 1.0)
    draw.rectangle([x, y, x + tw + 2 * pad_x, y + th + 2 * pad_y], fill=ORANGE)
    draw.text((x + pad_x, y + pad_y), text, font=font, fill=NAVY)
    return y + th + 2 * pad_y


# --- Cover page -------------------------------------------------------

def _build_cover_page():
    canvas = Image.new("RGB", (PAGE_W_PX, PAGE_H_PX), NAVY)
    draw = ImageDraw.Draw(canvas)

    try:
        logo = Image.open(os.path.join(current_app.root_path, "static", "img", "logo.png")).convert("RGBA")
        logo_w = round(mm(46))
        logo_h = round(logo_w * logo.height / logo.width)
        logo = logo.resize((logo_w, logo_h), Image.LANCZOS)
        canvas.paste(logo, (round(mm(18)), round(mm(20))), logo)
        tag_font = _font(_POPPINS_REGULAR, 10)
        draw.text((round(mm(18)), round(mm(20)) + logo_h + round(mm(1))),
                   "Journey To Excellence", font=tag_font, fill=WHITE)
    except Exception:  # noqa: BLE001 - a missing logo shouldn't block the cover page
        current_app.logger.exception("Failed to composite logo onto trainer profile cover")

    # Decorative triangle cluster, bottom-right - a simplified but
    # faithful echo of the reference cover's four-triangle motif.
    tri_color_sets = [
        ([(mm(140), mm(202)), (mm(140), mm(240)), (mm(176), mm(221))], (255, 255, 255)),
        ([(mm(140), mm(202)), (mm(176), mm(221)), (mm(176), mm(183))], PURPLE_ACCENT),
        ([(mm(133), mm(240)), (mm(133), mm(272)), (mm(163), mm(256))], ORANGE),
        ([(mm(163), mm(256)), (mm(163), mm(288)), (mm(193), mm(272))], (26, 41, 105)),
        ([(mm(103), mm(272)), (mm(103), mm(304)), (mm(133), mm(288))], (61, 82, 168)),
    ]
    for points, color in tri_color_sets:
        draw.polygon(points, fill=color)

    title_font = _font(_POPPINS_BOLD, 44)
    subtitle_font = _font(_POPPINS_REGULAR, 44)
    y = mm(250)
    draw.text((mm(18), y), "Trainer", font=title_font, fill=WHITE)
    y += _line_height(title_font, 1.05)
    draw.text((mm(18), y), "Profile", font=subtitle_font, fill=WHITE)

    site_font = _font(_POPPINS_REGULAR, 11)
    site_text = "modoku.tech"
    tw = draw.textlength(site_text, font=site_font)
    draw.text((PAGE_W_PX - mm(18) - tw, mm(283)), site_text, font=site_font, fill=WHITE)

    return canvas


# --- Public entry point --------------------------------------------------

def build_trainer_profile_pages(subject, profile, expertise, companies,
                                 academic, certifications, experience, sections,
                                 photo_path=None):
    """Returns a list of Pillow RGB Image pages (cover + content pages)."""
    # Normalized to plain dicts up front (db.query rows are sqlite3.Row,
    # which doesn't support .get()) so every helper below can use .get()
    # freely regardless of whether the caller passed rows or plain dicts.
    profile = dict(profile or {})
    expertise = [dict(r) for r in expertise]
    companies = [dict(r) for r in companies]
    academic = [dict(r) for r in academic]
    certifications = [dict(r) for r in certifications]
    experience = [dict(r) for r in experience]
    sections = [dict(s) for s in sections]

    content_width = PAGE_W_PX - round(mm(CONTENT_LEFT_MM)) - round(mm(CONTENT_RIGHT_MARGIN_MM))
    content_x = round(mm(CONTENT_LEFT_MM))
    top_y = round(mm(CONTENT_TOP_MM))
    bottom_y = PAGE_H_PX - round(mm(CONTENT_BOTTOM_MM))

    blocks = _build_right_blocks(profile, academic, certifications, experience, sections, content_width)

    # Name header height is the same on every page - measure it once by
    # drawing onto a scratch canvas, so pagination knows how much of the
    # first page's vertical budget is already spoken for.
    scratch = Image.new("RGB", (PAGE_W_PX, PAGE_H_PX))
    scratch_draw = ImageDraw.Draw(scratch)
    header_bottom = _draw_name_header(scratch, scratch_draw, content_x, top_y, content_width,
                                       subject["name"], profile.get("credentials_line"))
    header_height = header_bottom - top_y + round(mm(8))

    about_pill_height = round(mm(9.5)) + round(mm(3))  # pill box + gap before Background

    pages_blocks = _paginate_right(
        blocks,
        top_y=top_y + header_height,
        bottom_y=bottom_y,
        first_page_extra_top=about_pill_height,
    )

    photo_img = None
    if photo_path and os.path.exists(photo_path):
        try:
            photo_img = Image.open(photo_path)
        except Exception:  # noqa: BLE001 - render without a photo rather than fail the whole document
            current_app.logger.exception("Failed to open trainer photo %s for profile PDF", photo_path)

    pages = [_build_cover_page()]

    for page_index, page_blocks in enumerate(pages_blocks):
        canvas = Image.new("RGB", (PAGE_W_PX, PAGE_H_PX), WHITE)
        draw = ImageDraw.Draw(canvas)

        if page_index == 0 or not companies:
            # No Companies Trained list at all (Erik may leave it empty for
            # a trainer whose client list isn't for publishing) - every
            # page just keeps showing Areas of Expertise instead of an
            # empty "Companies Trained" box.
            pill_label, list_items = "AREAS OF EXPERTISE", [e["label"] for e in expertise]
        else:
            pill_label = "COMPANIES TRAINED"
            list_items = _companies_for_page(companies, page_index)
        _draw_sidebar(canvas, draw, pill_label, list_items, photo_img)

        y = _draw_name_header(canvas, draw, content_x, top_y, content_width,
                               subject["name"], profile.get("credentials_line"))
        y += round(mm(8))
        if page_index == 0:
            y = _draw_about_pill(draw, content_x, y)
            y += round(mm(3))

        for block in page_blocks:
            y = _draw_block(canvas, draw, block, content_x, y, content_width)

        pages.append(canvas)

    return pages


_COMPANY_CHUNK_CACHE_KEY = "_chunks"


def _companies_for_page(companies, page_index):
    """page_index counts from 0 at the first CONTENT page (the cover isn't
    included); Companies Trained starts on page_index==1. If the full list
    fits on one sidebar page it's simply repeated on every subsequent page
    (matches the reference: a short list appears identically on every
    trailing page). A list too long for one page is split into
    continuation chunks instead ("append" - Erik's own suggestion for a
    long list); once every chunk has been shown once, later pages repeat
    the full list again from the top rather than ever showing an empty box."""
    names = [c["name"] for c in companies]
    if not names:
        return []
    chunks = _chunk_companies(names)
    slot = page_index - 1
    if slot < len(chunks):
        return chunks[slot]
    return names


def _chunk_companies(names, max_per_page=None):
    font = _font(_POPPINS_REGULAR, 10.2)
    lh = _line_height(font, 1.35)
    list_width = SIDEBAR_W_PX - round(mm(10))
    available = round(mm(202.4)) - round(mm(4)) - (round(mm(70)) + round(mm(9.5)) + round(mm(4)))
    if max_per_page is None:
        # how many single-line items fit, conservatively (each name may
        # itself wrap onto 2 lines, but company names are normally short)
        max_per_page = max(1, available // lh)
    if len(names) <= max_per_page:
        return [names]
    return [names[i:i + max_per_page] for i in range(0, len(names), max_per_page)]


def generate_trainer_profile_pdf(subject, profile, expertise, companies,
                                  academic, certifications, experience, sections,
                                  photo_path=None):
    """Returns the finished PDF as bytes."""
    pages = build_trainer_profile_pages(subject, profile, expertise, companies,
                                         academic, certifications, experience, sections,
                                         photo_path=photo_path)
    buf = BytesIO()
    pages[0].save(buf, format="PDF", resolution=DPI, save_all=True, append_images=pages[1:])
    return buf.getvalue()
