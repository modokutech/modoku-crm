"""Coordination Form PDF (Fix231): the same 3-page design as Modoku's own
Canva form - orange page border, logo, big title, the boxed details table,
then "( ) / (✓)" options in small letter-spaced capitals.

Drawn with ReportLab at fixed positions (points from the top of an A4 page),
so it looks the same on every machine. It doesn't go through wkhtmltopdf, so
the VPS's ~18% scaling (see CLAUDE.md) doesn't apply.

Page 1: details, room setup, accommodation, food & beverage.
Page 2: transportation, design (T-shirt photos), technical.
Page 3: additional notes, consent, the client's and Modoku's signatures.
A section switched off for the form keeps its place, marked "Not applicable".
"""
import io
import os
from datetime import date, datetime

from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas

W, H = 595.28, 841.89
ORANGE = (0.984, 0.686, 0.090)       # #fbaf17
INK = (0.13, 0.13, 0.13)
HEAD = (0.20, 0.20, 0.25)            # section headings
MUTED = (0.62, 0.62, 0.62)
LINE = (0.35, 0.35, 0.35)
LEFT, RIGHT = 29, 566

_FONT_DIR = os.path.join(os.path.dirname(__file__), "static", "fonts")
_LOGO = os.path.join(os.path.dirname(__file__), "static", "img", "logo.png")
_fonts_ready = False


def _register_fonts():
    global _fonts_ready
    if _fonts_ready:
        return
    for name, file in (("Poppins", "Poppins-Regular.ttf"), ("Poppins-Medium", "Poppins-Medium.ttf"),
                       ("Poppins-Bold", "Poppins-Bold.ttf")):
        pdfmetrics.registerFont(TTFont(name, os.path.join(_FONT_DIR, file)))
    _fonts_ready = True


def _fmt_dt(d, t):
    """'2026-11-24', '09:00' -> '24/11/2026 9:00' (the original form's style)."""
    out = ""
    try:
        dd = date.fromisoformat(d)
        out = f"{dd.day}/{dd.month}/{dd.year}"
    except (TypeError, ValueError):
        out = d or ""
    if t:
        try:
            tt = datetime.strptime(t, "%H:%M")
            out = f"{out} {tt.hour}:{tt.minute:02d}".strip()
        except ValueError:
            out = f"{out} {t}".strip()
    return out


class _Page:
    def __init__(self, c):
        self.c = c

    def y(self, top):
        return H - top

    def text(self, x, top, s, font="Poppins", size=7.2, color=INK, space=0.0, align="left"):
        c = self.c
        c.setFillColorRGB(*color)
        t = c.beginText()
        t.setFont(font, size)
        t.setCharSpace(space)
        width = pdfmetrics.stringWidth(s, font, size) + space * max(0, len(s) - 1)
        if align == "center":
            x -= width / 2
        elif align == "right":
            x -= width
        t.setTextOrigin(x, self.y(top))
        t.textOut(s)
        c.drawText(t)
        return width

    def label(self, x, top, s, size=8.6, color=INK):
        """Small letter-spaced capitals, like the original's labels."""
        return self.text(x, top, s.upper(), "Poppins", size, color, space=1.0)

    def value(self, x, top, s, size=10, max_width=None, font="Helvetica"):
        s = s or ""
        if max_width:
            while size > 6.5 and pdfmetrics.stringWidth(s, font, size) > max_width:
                size -= 0.25
            while s and pdfmetrics.stringWidth(s, font, size) > max_width:
                s = s[:-2] + "…"
        return self.text(x, top, s, font, size)

    def wrap(self, s, font, size, width):
        lines = []
        for para in (s or "").splitlines() or [""]:
            words, line = para.split(), ""
            for w in words:
                cand = f"{line} {w}".strip()
                if pdfmetrics.stringWidth(cand, font, size) <= width:
                    line = cand
                else:
                    if line:
                        lines.append(line)
                    line = w
            lines.append(line)
        return lines

    def heading(self, top, s, off=False):
        self.text(LEFT, top, s.upper(), "Poppins", 13.5, HEAD)
        if off:
            self.label(LEFT + 4, top + 22, "Not applicable", color=MUTED)

    def hline(self, x1, x2, top, width=0.8, color=LINE):
        c = self.c
        c.setStrokeColorRGB(*color)
        c.setLineWidth(width)
        c.line(x1, self.y(top), x2, self.y(top))

    def rect(self, x, top, w, h, width=0.8, color=LINE):
        c = self.c
        c.setStrokeColorRGB(*color)
        c.setLineWidth(width)
        c.rect(x, self.y(top + h), w, h, stroke=1, fill=0)

    def option(self, x, top, label, checked, extra=None):
        """'( ) LABEL' or '(✓) LABEL'. extra: text put inside the brackets
        instead of a tick (e.g. a pax count)."""
        self.text(x, top, "(", "Poppins", 9.5)
        self.text(x + 12, top, ")", "Poppins", 9.5)
        if extra:
            self.text(x + 7.2, top - 0.3, str(extra), "Helvetica", 7.5, align="center")
        elif checked:
            c = self.c
            base = self.y(top)
            c.setStrokeColorRGB(*INK)
            c.setLineWidth(0.9)
            path = c.beginPath()
            path.moveTo(x + 3.2, base + 3.0)
            path.lineTo(x + 5.3, base + 0.6)
            path.lineTo(x + 9.8, base + 6.4)
            c.drawPath(path, stroke=1, fill=0)
        if label:
            self.label(x + 20, top, label)

    def box(self, x, top, checked):
        """The small square of the dietary list."""
        c = self.c
        c.setStrokeColorRGB(*INK)
        c.setLineWidth(0.5)
        c.rect(x, self.y(top), 5, 5, stroke=1, fill=0)
        if checked:
            p = c.beginPath()
            p.moveTo(x + 1, self.y(top) + 2.6)
            p.lineTo(x + 2.2, self.y(top) + 1.2)
            p.lineTo(x + 4.2, self.y(top) + 4.4)
            c.setLineWidth(0.7)
            c.drawPath(p, stroke=1, fill=0)

    def image(self, path, x, top, w, h):
        """An image fitted (and centred) inside the w x h box."""
        try:
            img = ImageReader(path)
        except Exception:  # noqa: BLE001 - a broken file just leaves the box empty
            return False
        iw, ih = img.getSize()
        scale = min(w / iw, h / ih)
        dw, dh = iw * scale, ih * scale
        self.c.drawImage(img, x + (w - dw) / 2, self.y(top + (h + dh) / 2), dw, dh, mask="auto")
        return True


def _frame(p, number, title):
    c = p.c
    c.setStrokeColorRGB(*ORANGE)
    c.setLineWidth(9)
    c.rect(4.5, 4.5, W - 9, H - 9, stroke=1, fill=0)
    p.text(RIGHT + 5, 31, f"PAGE {number}/3", "Poppins-Medium", 8.5, (0.80, 0.80, 0.80), align="right")
    try:
        logo = ImageReader(_LOGO)
        lw = 98
        lh = lw * logo.getSize()[1] / logo.getSize()[0]
        c.drawImage(logo, (W - lw) / 2, p.y(42 + lh), lw, lh, mask="auto")
    except Exception:  # noqa: BLE001
        pass
    p.text(W / 2, 135, title, "Poppins-Bold", 23, INK, align="center")


def render(form, answers, sections, design_images, other_files, design_other_files,
           client_sig_path=None, staff_sig_path=None, staff_name=None, staff_position=None,
           client_name=None, client_date=None, staff_date=None):
    """Returns the PDF bytes. answers/sections as in coordination_forms;
    design_images: (path, name) of T-shirt design images; other_files /
    design_other_files: names of the other attached files."""
    _register_fonts()
    a = answers
    on = set(sections)
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(W, H))
    c.setTitle(f"Coordination Form - {form['title']}")
    title = "COORDINATION FORM"

    # ---------- page 1 ----------
    p = _Page(c)
    _frame(p, 1, title)
    top, mid = 172, 297
    rows = [33, 33, 33, 54]
    edges = [top]
    for r in rows:
        edges.append(edges[-1] + r)
    p.rect(LEFT, top, RIGHT - LEFT, edges[-1] - top)
    for e in edges[1:-1]:
        p.hline(LEFT, RIGHT, e)
    c.setLineWidth(0.8)
    c.line(mid, p.y(edges[1]), mid, p.y(edges[-1]))

    def cell(x, row, lbl, val, right_edge):
        mid_top = (edges[row] + edges[row + 1]) / 2 + 2.6
        w = p.label(x + 7, mid_top, lbl + ":", size=8)
        p.value(x + 7 + w + 5, mid_top, val, max_width=right_edge - (x + 7 + w + 5) - 6)

    cell(LEFT, 0, "Programme name", a.get("programme") or form["title"], RIGHT)
    cell(LEFT, 1, "Start date & time", _fmt_dt(a.get("start_date"), a.get("start_time")), mid)
    cell(mid, 1, "End date & time", _fmt_dt(a.get("end_date"), a.get("end_time")), RIGHT)
    cell(LEFT, 2, "Company", a.get("company") or "", mid)
    cell(mid, 2, "No of pax", str(a.get("pax") or ""), RIGHT)
    cell(LEFT, 3, "Venue", a.get("venue") or "", mid)
    # Address can be long: up to 3 lines in its cell
    addr = p.wrap(a.get("address") or "", "Helvetica", 9, RIGHT - mid - 90)[:3] if a.get("address") else []
    addr_top = edges[3] + 14 if len(addr) > 1 else (edges[3] + edges[4]) / 2 + 2.6
    w = p.label(mid + 7, addr_top, "Address:", size=8)
    for i, line in enumerate(addr):
        p.text(mid + 12 + w, addr_top + i * 11, line, "Helvetica", 9)

    # Room setup
    p.heading(366, "Room Setup Preferences", off="room_setup" not in on)
    if "room_setup" in on:
        rs = a.get("room_setup")
        p.option(36, 400, "Classroom", rs == "Classroom")
        p.option(158, 400, "Theater", rs == "Theater")
        p.option(280, 400, "Other:", rs == "Other")
        p.hline(342, 430, 402)
        if rs == "Other":
            p.value(345, 399, a.get("room_setup_other") or "", size=8, max_width=84)
        p.option(36, 434, "U-Shape", rs == "U-Shape")
        p.option(158, 434, "Banquet", rs == "Banquet")

    # Accommodation
    p.heading(484, "Accommodation Information", off="accommodation" not in on)
    if "accommodation" in on:
        occ = a.get("occupancy") or []
        p.label(36, 517, "Room occupancy")
        p.label(172, 517, "Breakfast")
        p.label(348, 517, "Special assistance (disabled, etc)")
        p.option(50, 548, "Single", "Single" in occ)
        p.option(50, 581, "Double", "Double" in occ)
        p.option(186, 548, "With breakfast", a.get("breakfast") == "With breakfast")
        p.option(186, 581, "Without breakfast", a.get("breakfast") == "Without breakfast")
        sa = a.get("special_assistance")
        p.option(362, 548, "Yes", sa == "Yes")
        p.option(362, 581, "No", sa == "No")
        if sa == "Yes" and a.get("special_assistance_details"):
            p.value(400, 548, a["special_assistance_details"], size=8, max_width=160)

    # Food & beverage
    p.heading(631, "Food & Beverage Requirements", off="fnb" not in on)
    if "fnb" in on:
        from .coordination_forms import DIETS
        diet = a.get("diet") or []
        p.label(36, 665, "Dietary preferences (please select all that apply):")
        p.label(348, 665, "Allergies")
        c.setStrokeColorRGB(0.85, 0.85, 0.85)
        c.setLineWidth(0.5)
        c.line(175, p.y(688), 175, p.y(772))
        for i, d in enumerate(DIETS):
            row = 692 + i * 12
            p.box(50, row + 4.8, d in diet)
            p.label(59, row + 4.6, d, size=7.6)
            if d == "Other":
                p.hline(95, 163, row + 6)
                if a.get("diet_other"):
                    p.value(97, row + 4.4, a["diet_other"], size=7, max_width=64)
            if d != "No restrictions":
                pax = a.get(f"diet_pax_{i}")
                p.option(183, row + 4.6, None, False, extra=pax if pax else None)
        al = a.get("allergies")
        p.option(362, 697, "None", al == "None")
        p.option(362, 730, "Yes, please specify:", al == "Yes")
        p.hline(364, 540, 763)
        if al == "Yes" and a.get("allergies_details"):
            p.value(366, 760, a["allergies_details"], size=8.5, max_width=172)
    c.showPage()

    # ---------- page 2 ----------
    p = _Page(c)
    _frame(p, 2, title)
    p.heading(185, "Transportation Details", off="transport" not in on)
    if "transport" in on:
        tr, tt = a.get("transport_required"), a.get("transport_type")
        p.label(36, 216, "Transportation required")
        p.label(209, 216, "Type of transportation")
        p.option(50, 248, "Yes", tr == "Yes")
        p.option(50, 282, "No", tr == "No")
        p.option(223, 248, "Van", tt == "Van")
        p.option(223, 282, "Bus", tt == "Bus")
        p.option(314, 248, "Flight", tt == "Flight")
        p.option(314, 282, "Other:", tt == "Other")
        p.hline(376, 464, 284)
        if tt == "Other" and a.get("transport_other"):
            p.value(378, 281, a["transport_other"], size=8, max_width=84)
        for i, line in enumerate(p.wrap(a.get("transport_details") or "", "Helvetica", 7.2, 330)[:4]):
            p.text(226, 297 + i * 8.2, line, "Helvetica", 7.2)
        w = p.label(36, 325, "Number of participants using this transportation:")
        p.hline(41 + w, 41 + w + 54, 328)
        if a.get("transport_pax"):
            p.text(41 + w + 27, 325, str(a["transport_pax"]), "Helvetica", 9.5, align="center")

    p.heading(372, "Design Specifications", off="design" not in on)
    if "design" in on:
        w = p.label(36, 405, "T-shirt design (if any):")
        lines = p.wrap(a.get("tshirt_design") or "", "Helvetica", 8.5, RIGHT - 46 - w)
        for i, line in enumerate(lines[:2]):
            p.text(42 + w, 405 + i * 11, line, "Helvetica", 8.5)
        box_top = 422 if len(lines) <= 1 else 432
        # Up to 4 photos (Fix232): one row of two big boxes, or two rows of smaller ones.
        photos = design_images[:4]
        box_h = 180 if len(photos) <= 2 else 96
        for i, (path, _name) in enumerate(photos):
            x = 35 if i % 2 == 0 else 292
            top_i = box_top + (i // 2) * (box_h + 6)
            p.rect(x, top_i, 207, box_h)
            p.image(path, x + 5, top_i + 5, 197, box_h - 10)
        extra = design_other_files + [name for _path, name in design_images[4:]]
        if extra:
            rows_h = 0 if not photos else (box_h if len(photos) <= 2 else 2 * box_h + 6)
            p.text(36, box_top + rows_h + (10 if photos else 6), "Design files: " + ", ".join(extra), "Helvetica", 7)

    p.heading(650, "Technical Requirements", off="technical" not in on)
    if "technical" in on:
        lr = a.get("laptop_rental")
        p.label(36, 685, "Laptop rental")
        p.option(50, 717, "Yes:", lr == "Yes")
        p.hline(96, 149, 721)
        if lr == "Yes" and a.get("laptop_pcs"):
            p.text(122, 717, str(a["laptop_pcs"]), "Helvetica", 9.5, align="center")
        p.label(154, 717, "pcs")
        p.option(50, 750, "No", lr == "No")
    c.showPage()

    # ---------- page 3 ----------
    p = _Page(c)
    _frame(p, 3, title)
    p.heading(185, "Additional Notes/Requests", off="notes" not in on)
    box_top, box_bottom = 202, 528
    p.rect(LEFT, box_top, RIGHT - LEFT, box_bottom - box_top)
    if "notes" in on and a.get("notes"):
        size = 10
        while True:
            lines = p.wrap(a["notes"], "Helvetica", size, 500)
            if len(lines) * size * 1.95 <= box_bottom - box_top - 24 or size <= 6.5:
                break
            size -= 0.5
        for i, line in enumerate(lines):
            y = box_top + 22 + i * size * 1.95
            if y > box_bottom - 8:
                break
            p.text(46, y, line, "Helvetica", size)
    if other_files:
        p.text(LEFT, box_bottom + 13, "Attachments: " + ", ".join(other_files), "Helvetica", 7)

    p.heading(562, "Consent & Agreement")
    from .coordination_forms import CONSENT_TEXT
    for i, line in enumerate(CONSENT_TEXT):
        p.text(LEFT, 580 + i * 11.5, line, "Poppins", 7.8, (0.25, 0.25, 0.25))

    def sign_box(top, lbl, sig_path, name, when):
        h, split = 84, 298
        p.rect(LEFT, top, RIGHT - LEFT, h)
        c.setStrokeColorRGB(*LINE)
        c.line(split, p.y(top), split, p.y(top + h))
        p.hline(split, RIGHT, top + h * 0.47)
        p.label(36, top + 15, lbl, size=7.8)
        if sig_path and os.path.exists(sig_path):
            p.image(sig_path, 44, top + 22, 230, 56)
        w = p.label(split + 8, top + 15, "Name", size=7.8)
        p.value(split + 14 + w, top + 15, name or "", size=9.5, max_width=RIGHT - split - w - 20)
        w = p.label(split + 8, top + h * 0.47 + 26, "Date:", size=7.8)
        p.value(split + 14 + w, top + h * 0.47 + 26, when or "", size=9.5)

    sign_box(624, "Client's signature:", client_sig_path, client_name, client_date)
    staff_label = "Confirmed by Modoku:"
    sign_box(718, staff_label, staff_sig_path,
             f"{staff_name}, {staff_position}" if staff_name and staff_position else staff_name, staff_date)
    c.showPage()
    c.save()
    return buf.getvalue()
