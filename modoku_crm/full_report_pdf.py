"""Renders the branded, client-facing "Course Training Report" PDF for
full_reports.py — the automated version of the report Modoku Tech has
always compiled by hand after a class ends (foreword, objective,
participant list, a training-performance-details table, one chart per
rating question grouped by the evaluation Form's own section headings,
open-text answer tables, and a conclusion).

Built from two PDFs merged with pypdf:
  1. the cover page — full-bleed Modoku Navy — composited as a Pillow
     image and saved straight to a one-page PDF, exactly like
     pdfgen.py's certificate generation (see that module's comment above
     _build_certificate_image): this environment's wkhtmltopdf (0.12.6,
     unpatched Qt) has a documented width-mis-scaling bug on a full-bleed,
     no-margin page, so the one page here that needs to be full-bleed
     bypasses wkhtmltopdf entirely rather than fight the same bug again.
  2. every other page — built as HTML and rendered through wkhtmltopdf
     (which handles ordinary margined, multi-page flowing content fine —
     every other multi-page document in this app already goes through it
     this way), with the Modoku logo repeated top-left (--header-html) and
     the address/phone/email/website footer repeated bottom (--footer-
     html) on every page automatically.
Kept as its own module rather than added to pdfgen.py because of the
extra chart-drawing and image/HTML-merge code a document this long needs,
that no other document here does.

Charts are hand-built inline SVG (bar / horizontal-bar / pie) — no
charting library needed, and wkhtmltopdf's WebKit renders plain SVG
without issue. Colours: rating charts (Poor..Excellent, or any other
worded/numeric scale) use the same fixed per-position colours as Google
Forms' own response-summary charts (blue/red/orange/green/purple —
_RATING_PALETTE below, sampled from a real Forms chart legend), keyed to
each option's position in the scale, so the report's bars match what
staff already see when they open the Form itself. Plain categorical
choice questions (Yes/No/Not sure, a checkbox multi-select, ...) use
Modoku's own secondary palette instead, since there's no inherent order
to colour by.
"""
import io
import math
import os
import subprocess
import tempfile

from markupsafe import escape
from PIL import Image, ImageDraw, ImageFont

from . import pdfgen

NAVY = "#293884"
GOLD = "#FCB016"
INK = "#181B2E"
MUTED = "#5B6088"
SURFACE = "#F1EEE6"
RULE = "#E4E2DC"

# Secondary/tag palette from the Modoku brand guide, reused here as a
# qualitative (non-ordered) chart palette for plain categorical questions.
CATEGORICAL_PALETTE = [NAVY, GOLD, "#247EFF", "#F43FC1", MUTED, "#0090D0"]

# Fixed per-position colours for a rating scale's bars — Poor/Uncertain/
# Fair/Good/Excellent (or however many points a given scale actually has),
# always assigned worst-to-best by POSITION, never by which options
# happened to receive votes. Sampled directly from a real Google Forms
# response-summary chart's own legend (blue/red/orange/green/purple), per
# a direct request to match that familiar look rather than an invented
# "bad to good" gradient. A scale longer than 5 points (rare for a rating
# question) wraps rather than erroring.
_RATING_PALETTE = ["#426ac3", "#cd4a2b", "#f29c3c", "#499634", "#8c2594"]

_FONT_DIR = os.path.join(os.path.dirname(__file__), "static", "fonts")


def _rating_color(index, count):  # noqa: ARG001 - count kept for call-site symmetry/future use
    """The fixed palette colour for position `index` of a rating scale (0 =
    worst, count-1 = best) — see _RATING_PALETTE above."""
    return _RATING_PALETTE[index % len(_RATING_PALETTE)]


def _truncate(text, max_len=42):
    text = text or ""
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


# --- Chart builders (return an inline <svg>...</svg> string) ---------------

def svg_bar_chart(categories, counts, colors, width=500, height=190):
    """A small clustered vertical bar chart for one question — categories
    are always short here (a rating scale's own labels: Poor/Fair/Good/...,
    or 1/2/3/4/5), so no label wrapping/truncation is needed."""
    n = len(categories)
    if n == 0:
        return ""
    max_count = max(counts) if counts and max(counts) > 0 else 1
    margin_left, margin_right, margin_top, margin_bottom = 8, 8, 22, 30
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom
    bar_gap = 14
    bar_w = max((plot_w - bar_gap * (n - 1)) / n, 4)
    parts = []
    baseline_y = margin_top + plot_h
    parts.append(f'<line x1="{margin_left}" y1="{baseline_y:.1f}" x2="{width - margin_right}" '
                 f'y2="{baseline_y:.1f}" stroke="{RULE}" stroke-width="1"/>')
    for i, (cat, count) in enumerate(zip(categories, counts)):
        bar_h = (count / max_count) * plot_h if max_count else 0
        x = margin_left + i * (bar_w + bar_gap)
        y = margin_top + (plot_h - bar_h)
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{max(bar_h, 0):.1f}" '
                     f'fill="{colors[i % len(colors)]}" rx="3"/>')
        if count:
            parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{y - 6:.1f}" font-size="11" fill="{INK}" '
                         f'text-anchor="middle" font-family="Poppins,Arial,sans-serif">{count}</text>')
        parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{height - margin_bottom + 16:.1f}" font-size="10" '
                     f'fill="{MUTED}" text-anchor="middle" font-family="Poppins,Arial,sans-serif">'
                     f'{escape(cat)}</text>')
    return (f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'xmlns="http://www.w3.org/2000/svg">' + "".join(parts) + "</svg>")


def svg_hbar_chart(categories, counts, total_responses, width=500, row_h=22):
    """A horizontal bar per category, count + percentage-of-responses
    labeled at the bar's end — for a pick-many (checkbox) question, or a
    single-choice question with too many options for a pie chart to stay
    readable. Percentages can add to more than 100% for a checkbox
    question (a respondent can pick several), by design."""
    n = len(categories)
    if n == 0:
        return ""
    max_count = max(counts) if counts and max(counts) > 0 else 1
    margin_left, margin_right = 210, 70
    plot_w = width - margin_left - margin_right
    height = n * row_h + 8
    parts = []
    for i, (cat, count) in enumerate(zip(categories, counts)):
        y = 4 + i * row_h
        bar_w = (count / max_count) * plot_w if max_count else 0
        pct = round(count / total_responses * 100) if total_responses else 0
        parts.append(f'<text x="{margin_left - 8}" y="{y + row_h / 2 + 4:.1f}" font-size="10.5" fill="{INK}" '
                     f'text-anchor="end" font-family="Poppins,Arial,sans-serif">{escape(_truncate(cat))}</text>')
        parts.append(f'<rect x="{margin_left}" y="{y:.1f}" width="{max(bar_w, 0):.1f}" height="{row_h - 7}" '
                     f'fill="{NAVY}" rx="2"/>')
        parts.append(f'<text x="{margin_left + bar_w + 8:.1f}" y="{y + row_h / 2 + 4:.1f}" font-size="10.5" '
                     f'fill="{MUTED}" font-family="Poppins,Arial,sans-serif">{count} ({pct}%)</text>')
    return (f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'xmlns="http://www.w3.org/2000/svg">' + "".join(parts) + "</svg>")


def svg_pie_chart(categories, counts, width=420, size=150):
    """A pie chart with a right-hand legend (label — count (pct%)) — used
    for a single-choice question with few enough options to stay readable
    as wedges (e.g. Yes/No/Not sure)."""
    n = len(categories)
    total = sum(counts) or 1
    cx = cy = size / 2
    r = size / 2 - 4
    parts = []
    angle = -90.0
    for i, count in enumerate(counts):
        if count <= 0:
            continue
        color = CATEGORICAL_PALETTE[i % len(CATEGORICAL_PALETTE)]
        frac = count / total
        if frac >= 0.999:
            parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}"/>')
            break
        end_angle = angle + frac * 360
        x1, y1 = cx + r * math.cos(math.radians(angle)), cy + r * math.sin(math.radians(angle))
        x2, y2 = cx + r * math.cos(math.radians(end_angle)), cy + r * math.sin(math.radians(end_angle))
        large_arc = 1 if (end_angle - angle) > 180 else 0
        parts.append(f'<path d="M{cx},{cy} L{x1:.2f},{y1:.2f} A{r},{r} 0 {large_arc},1 {x2:.2f},{y2:.2f} Z" '
                     f'fill="{color}"/>')
        angle = end_angle
    legend = []
    for i, (cat, count) in enumerate(zip(categories, counts)):
        pct = round(count / total * 100)
        ly = 10 + i * 20
        color = CATEGORICAL_PALETTE[i % len(CATEGORICAL_PALETTE)]
        legend.append(f'<rect x="0" y="{ly}" width="11" height="11" fill="{color}" rx="2"/>'
                       f'<text x="17" y="{ly + 10}" font-size="11" fill="{INK}" '
                       f'font-family="Poppins,Arial,sans-serif">{escape(cat)} — {count} ({pct}%)</text>')
    height = max(size, 10 + n * 20)
    return (f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f'<g>{"".join(parts)}</g><g transform="translate({size + 24},0)">{"".join(legend)}</g></svg>')


def _chart_for_question(q):
    """Picks a chart type for one numeric_summary entry (training_reports.
    build_report's per-question dict) and returns its SVG, or "" if there's
    nothing sensible to chart (e.g. zero responses)."""
    dist = q.get("distribution") or []
    if not dist:
        return ""
    if q["kind"] in ("choice_ordinal", "scale", "choice_numeric"):
        scale = q.get("scale")
        if scale:
            # Colour by each option's position in the FULL worded scale
            # (0..n-1), not its position among only the options that
            # actually got a vote — otherwise a class where nobody picked
            # "Poor"/"Uncertain" would show its lowest actual rating
            # ("Fair") as the reddest bar, which misrepresents it.
            n = len(scale)
            order = {label.lower(): i for i, label in enumerate(scale)}
            ordered = sorted(dist, key=lambda d: order.get(d["option"].strip().lower(), 999))
            colors = [_rating_color(order.get(d["option"].strip().lower(), 0), n) for d in ordered]
        else:
            # No worded scale (a plain numeric 1-5 "scale" question, or a
            # choice_numeric question) — order low to high by value, and
            # colour by each value's position within the full numeric
            # range actually seen (a reasonable approximation without the
            # question's own declared low/high bounds on hand here).
            def _num(d):
                try:
                    return float(d["option"])
                except ValueError:
                    return 0
            ordered = sorted(dist, key=_num)
            colors = [_rating_color(i, len(ordered)) for i in range(len(ordered))]
        categories = [d["option"] for d in ordered]
        counts = [d["count"] for d in ordered]
        return svg_bar_chart(categories, counts, colors)
    # choice_text: a checkbox (pick-many) question, or any single-choice
    # question with too many options for a pie chart to stay legible.
    if q.get("widget") == "CHECKBOX" or len(dist) > 6:
        return svg_hbar_chart([d["option"] for d in dist], [d["count"] for d in dist], q.get("count") or 0)
    return svg_pie_chart([d["option"] for d in dist], [d["count"] for d in dist])


# --- HTML building -----------------------------------------------------

def _font_face_css():
    faces = [
        ("Poppins", 300, "normal", "Poppins-Light.ttf"),
        ("Poppins", 400, "normal", "Poppins-Regular.ttf"),
        ("Poppins", 500, "normal", "Poppins-Medium.ttf"),
        ("Poppins", 700, "normal", "Poppins-Bold.ttf"),
        ("Poppins", 400, "italic", "Poppins-Italic.ttf"),
    ]
    rules = []
    for family, weight, style, filename in faces:
        uri = pdfgen._data_uri(os.path.join(_FONT_DIR, filename))
        if uri:
            rules.append(
                f"@font-face {{ font-family:'{family}'; src:url({uri}) format('truetype'); "
                f"font-weight:{weight}; font-style:{style}; }}"
            )
    return "\n".join(rules)


_COVER_DPI = 200
_COVER_W_MM, _COVER_H_MM = 210.0, 297.0
_COVER_PX_PER_MM = _COVER_DPI / 25.4
_COVER_W_PX = round(_COVER_W_MM * _COVER_PX_PER_MM)
_COVER_H_PX = round(_COVER_H_MM * _COVER_PX_PER_MM)

_FONT_LIGHT = os.path.join(_FONT_DIR, "Poppins-Light.ttf")
_FONT_REGULAR = os.path.join(_FONT_DIR, "Poppins-Regular.ttf")
_FONT_ITALIC = os.path.join(_FONT_DIR, "Poppins-Italic.ttf")
_FONT_BOLD = os.path.join(_FONT_DIR, "Poppins-Bold.ttf")
# Georgia itself is a proprietary Microsoft font with no redistributable
# file available to bundle here, so the cover's italic "for" uses DejaVu
# Serif Italic instead — a real, freely-licensed serif italic (already
# used nowhere else in this app) that reads the same way Georgia Italic
# would in this one-word spot. Swap in an actual Georgia Italic .ttf here
# if one becomes available.
_FONT_GEORGIA_ITALIC = os.path.join(_FONT_DIR, "DejaVuSerif-Italic.ttf")


def _hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def _cover_mm(value):
    return value * _COVER_PX_PER_MM


def _cover_font(path, size_pt):
    try:
        return ImageFont.truetype(path, round(size_pt * _COVER_DPI / 72))
    except OSError:
        return ImageFont.load_default()


def _cover_wrap(draw, text, font, max_width):
    words = (text or "").split()
    if not words:
        return [""]
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
    return lines or [""]


def _cover_center(draw, y_mm, text, font, fill, max_width_mm=None):
    """Draws `text` horizontally centered at y_mm (wrapped to max_width_mm
    if given), returning the y (in mm) just below the last line drawn."""
    max_width_px = _cover_mm(max_width_mm) if max_width_mm else None
    lines = _cover_wrap(draw, text, font, max_width_px) if max_width_px else [text]
    ascent, descent = font.getmetrics()
    line_height_mm = (ascent + descent) / _COVER_PX_PER_MM
    y = y_mm
    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text(((_COVER_W_PX - w) / 2, _cover_mm(y)), line, font=font, fill=fill)
        y += line_height_mm
    return y


def _build_cover_pdf(course_title, date_range, client_name):
    """The cover page, composited with Pillow (see the module docstring for
    why) and returned as one-page PDF bytes."""
    img = Image.new("RGB", (_COVER_W_PX, _COVER_H_PX), _hex_to_rgb(NAVY))
    draw = ImageDraw.Draw(img)

    title_font = _cover_font(_FONT_BOLD, 22)
    date_font = _cover_font(_FONT_LIGHT, 12)
    label_font = _cover_font(_FONT_REGULAR, 14)
    for_font = _cover_font(_FONT_GEORGIA_ITALIC, 11)
    client_font = _cover_font(_FONT_BOLD, 16)
    company_font = _cover_font(_FONT_REGULAR, 10)

    y = _cover_center(draw, 38, course_title, title_font, "white", max_width_mm=155)
    _cover_center(draw, y + 4, date_range, date_font, (222, 227, 245))

    y = _cover_center(draw, 128, "Course Training Report", label_font, "white")
    y = _cover_center(draw, y + 2, "for", for_font, (222, 227, 245))
    if client_name:
        _cover_center(draw, y + 3, client_name, client_font, "white", max_width_mm=160)

    # Logo sized 20% larger than before and moved down to sit close above
    # "Modoku Tech Sdn Bhd" (rather than floating alone with a lot of empty
    # space beneath it), per request.
    logo_path = os.path.join(os.path.dirname(__file__), "static", "img", "logo.png")
    try:
        logo = Image.open(logo_path).convert("RGBA")
        logo_w_px = round(_cover_mm(24 * 1.2))
        logo_h_px = round(logo.height * (logo_w_px / logo.width))
        logo = logo.resize((logo_w_px, logo_h_px))
        img.paste(logo, (round((_COVER_W_PX - logo_w_px) / 2), round(_cover_mm(241))), logo)
    except OSError:
        pass
    _cover_center(draw, 262, "Modoku Tech Sdn Bhd", company_font, (222, 227, 245))

    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=_COVER_DPI)
    return buf.getvalue()


def _build_page_overlay_pdf():
    """A one-page A4 PDF with just the Modoku logo (top-left) and the
    address/phone/email/website footer (bottom) — merged onto every page of
    the rendered content PDF in build_full_report_pdf() below.

    This exists only because this environment's wkhtmltopdf is an
    "unpatched Qt" build, which silently ignores EVERY header/footer
    switch it has (confirmed: even the plain-text --header-center/
    --footer-left/--footer-right flags, not just the HTML-page-based
    --header-html/--footer-html one) — there is no way to ask wkhtmltopdf
    itself to repeat anything on every page of a multi-page render in this
    build. Stamping a logo+footer overlay onto each page afterwards with
    pypdf, via a single small reportlab-drawn PDF page reused for all of
    them, sidesteps that limitation entirely and needs nothing from
    wkhtmltopdf at all. The content PDF's own top/bottom margins (see
    build_full_report_pdf) are sized to leave exactly this much room
    blank, so the stamp never overlaps real content."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas as rl_canvas

    try:
        pdfmetrics.registerFont(TTFont("Poppins", os.path.join(_FONT_DIR, "Poppins-Regular.ttf")))
        footer_font = "Poppins"
    except Exception:  # noqa: BLE001 - a missing/bad font file must never break PDF generation
        footer_font = "Helvetica"

    buf = io.BytesIO()
    page_w, page_h = A4
    c = rl_canvas.Canvas(buf, pagesize=A4)

    logo_path = os.path.join(os.path.dirname(__file__), "static", "img", "logo.png")
    try:
        with Image.open(logo_path) as logo_img:
            aspect = logo_img.height / logo_img.width
        logo_w = 18 * mm
        logo_h = logo_w * aspect
        c.drawImage(logo_path, 18 * mm, page_h - 14 * mm - logo_h, width=logo_w, height=logo_h,
                    mask="auto", preserveAspectRatio=True)
    except (OSError, ValueError):
        pass

    navy_rgb = tuple(v / 255 for v in _hex_to_rgb(NAVY))
    c.setFillColorRGB(*navy_rgb)
    c.setFont(footer_font, 8)
    left_lines = ["Level 30, Menara Prestige", "1, Jalan Pinang", "50450 Kuala Lumpur"]
    right_lines = ["+603 2728 1035", "hello@modoku.tech", "www.modoku.tech"]
    # Lifted further off the physical bottom edge than the logo's own top
    # gap (was flush enough to look cramped) — the content pages' own
    # margin-bottom (see build_full_report_pdf) leaves the matching room
    # above this block.
    base_y = 20 * mm
    line_gap = 4 * mm
    for i, (left, right) in enumerate(zip(left_lines, right_lines)):
        y = base_y - i * line_gap
        c.drawString(18 * mm, y, left)
        c.drawRightString(page_w - 18 * mm, y, right)

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.getvalue()


def _section_heading(text):
    return f'<div class="section-heading">{escape(text)}</div>'


def _paragraphs_html(text):
    return "".join(f"<p>{escape(p)}</p>" for p in (text or "").strip().split("\n") if p.strip())


def _answer_table_html(title, answers, respondent_names=None):
    rows = []
    for i, answer in enumerate(answers, 1):
        name_cell = f"<td class='resp-name'>{escape(respondent_names[i - 1])}</td>" if respondent_names else ""
        rows.append(f"<tr><td class='num'>{i}</td>{name_cell}<td>{escape(answer)}</td></tr>")
    colspan = 3 if respondent_names else 2
    return (f'<table class="answers"><thead><tr><th colspan="{colspan}">{escape(title)}</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def _build_content_html(ctx):
    """ctx keys: course_title, date_range, client_name, trainer_name, venue,
    total_participants, total_responded, participants (list of names),
    foreword_text, objective_text, conclusion_text, numeric_summary (list,
    each already carrying 'group'), text_summary (list)."""
    performance_rows = [
        ("Trainer Name", ctx["trainer_name"] or "TBC"),
        ("Program", ctx["course_title"]),
        ("Client", ctx["client_name"] or "—"),
        ("Date", ctx["date_range"]),
        ("Venue", ctx["venue"] or "—"),
        ("Total Participants", str(ctx["total_participants"])),
        ("Total Responded", str(ctx["total_responded"])),
    ]
    performance_table = "".join(
        f"<tr><th>{escape(label)}</th><td>{escape(str(value))}</td></tr>" for label, value in performance_rows
    )

    participant_rows = "".join(
        f"<tr><td class='num'>{i}</td><td>{escape(name)}</td></tr>"
        for i, name in enumerate(ctx["participants"], 1)
    )
    participant_section = (
        _section_heading("Participant List")
        + f"<p>Total of {len(ctx['participants'])} participant{'' if len(ctx['participants']) == 1 else 's'}.</p>"
        + (f'<table class="plain"><thead><tr><th class="num">No</th><th>Participants Name</th></tr></thead>'
           f'<tbody>{participant_rows}</tbody></table>' if ctx["participants"] else
           "<p class='muted'>No participants enrolled on this class yet.</p>")
    )

    # One chart block per question, grouped under its own Form section
    # heading (falling back to the question's own title as its own,
    # single-question "section" when it wasn't under a Form section) —
    # laid out in the Form's own question order, matching how the sheet is
    # actually filled in. Groups flow freely across pages (only
    # page-break-inside:avoid per chart) rather than one forced page per
    # group, since a short group and a long one next to each other should
    # still share a page when they fit — exactly how the original hand-made
    # reports are laid out.
    seen_groups = []
    grouped = {}
    for q in ctx["numeric_summary"]:
        label = q.get("group") or q["question"]
        if label not in grouped:
            grouped[label] = []
            seen_groups.append(label)
        grouped[label].append(q)

    chart_sections = []
    for label in seen_groups:
        questions = grouped[label]
        blocks = []
        for q in questions:
            svg = _chart_for_question(q)
            if not svg:
                continue
            sub_title = q["question"] if q["question"] != label else None
            blocks.append(
                '<div class="chart-block">'
                + (f'<div class="chart-question">{escape(sub_title)}</div>' if sub_title else "")
                + svg + "</div>"
            )
        if blocks:
            chart_sections.append(f'<div class="chart-group">{_section_heading(label)}{"".join(blocks)}</div>')

    text_sections = []
    for q in ctx["text_summary"]:
        if q["answers"]:
            text_sections.append(_answer_table_html(q["question"], q["answers"]))

    # Interleave charts and open-text tables in the Form's own overall
    # order isn't reconstructable perfectly post-aggregation, but showing
    # every chart group first and then every open-text table (the same
    # broad ordering the original reports use — ratings first, verbatim
    # comments after) keeps it simple and readable.
    body = (
        _section_heading("Foreword")
        + _paragraphs_html(ctx["foreword_text"])
        + '<div style="page-break-before:always">' + _section_heading("Objective") + _paragraphs_html(ctx["objective_text"]) + '</div>'
        + '<div style="page-break-before:always">' + participant_section + '</div>'
        + '<div style="page-break-before:always">'
        + _section_heading("Training Performance Details — Overall Evaluation")
        + f'<table class="plain performance">{performance_table}</table>'
        + "".join(chart_sections)
        + "</div>"
        + "".join(text_sections)
        + '<div style="page-break-before:always">' + _section_heading("Conclusion") + _paragraphs_html(ctx["conclusion_text"]) + '</div>'
    )

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
{_font_face_css()}
* {{ box-sizing:border-box; }}
body {{ font-family:'Poppins',Arial,sans-serif; font-size:13.8px; color:{INK}; margin:0; }}
.section-heading {{ color:{NAVY}; font-size:15.3px; font-weight:700; text-transform:uppercase;
  letter-spacing:.02em; margin:0 0 10px; }}
p {{ line-height:1.2; margin:0 0 10px; }}
.muted {{ color:{MUTED}; }}
table.plain {{ width:100%; border-collapse:collapse; margin-bottom:14px; page-break-inside:avoid; }}
table.plain th, table.plain td {{ border:1px solid #cfcdc6; padding:6px 10px; text-align:left; font-size:11.5px; }}
table.plain thead th {{ background:#333; color:#fff; font-weight:600; }}
table.performance th {{ width:35%; background:#fff; color:{INK}; font-weight:700; }}
td.num, th.num {{ width:34px; text-align:center; }}
table.answers {{ width:100%; border-collapse:collapse; margin:14px 0; page-break-inside:avoid; }}
table.answers th {{ background:#333; color:#fff; font-weight:600; text-align:center; padding:7px 10px; font-size:11px; }}
table.answers td {{ border:1px solid #cfcdc6; padding:6px 10px; font-size:11.5px; }}
table.answers td.num {{ width:34px; text-align:center; }}
table.answers td.resp-name {{ width:32%; font-weight:600; }}
.chart-group {{ margin:0 0 18px; page-break-inside:avoid; }}
.chart-block {{ margin:0 0 14px; page-break-inside:avoid; }}
.chart-question {{ font-size:12px; font-weight:600; margin-bottom:4px; }}
</style></head>
<body>{body}</body></html>"""


# --- Rendering / merge ---------------------------------------------------

def _render_pdf(html, extra_args=None):
    tmp_paths = []
    try:
        with tempfile.NamedTemporaryFile(suffix=".html", mode="w", encoding="utf-8", delete=False) as f:
            f.write(html)
            html_path = f.name
        tmp_paths.append(html_path)
        pdf_path = html_path.replace(".html", ".pdf")
        tmp_paths.append(pdf_path)

        args = ["wkhtmltopdf", "--quiet", "--page-size", "A4"] + (extra_args or []) + [html_path, pdf_path]
        subprocess.run(args, check=True, timeout=45, capture_output=True)
        with open(pdf_path, "rb") as f:
            return f.read()
    finally:
        for path in tmp_paths:
            try:
                os.remove(path)
            except OSError:
                pass


def build_full_report_pdf(ctx):
    """Returns the finished, multi-page PDF (bytes) for the given context
    dict (see _build_content_html for the exact keys expected)."""
    from pypdf import PdfReader, PdfWriter

    cover_pdf = _build_cover_pdf(ctx["course_title"], ctx["date_range"], ctx["client_name"])
    # Margins sized to leave enough blank room for the logo+footer overlay
    # stamped onto every content page below (see _build_page_overlay_pdf
    # for why it's a post-process stamp rather than a wkhtmltopdf
    # --header-html/--footer-html render) PLUS a bit of extra breathing
    # room past the logo/footer themselves, so a heading like "Foreword"
    # never sits flush against the logo and the footer never sits flush
    # against the last line of content.
    content_pdf = _render_pdf(
        _build_content_html(ctx),
        extra_args=["--margin-top", "34mm", "--margin-bottom", "28mm",
                    "--margin-left", "18mm", "--margin-right", "18mm"],
    )
    overlay_pdf = _build_page_overlay_pdf()
    overlay_page_template = PdfReader(io.BytesIO(overlay_pdf)).pages[0]

    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(cover_pdf)).pages:
        writer.add_page(page)
    for page in PdfReader(io.BytesIO(content_pdf)).pages:
        page.merge_page(overlay_page_template)
        writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
