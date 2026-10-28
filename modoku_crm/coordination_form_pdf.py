"""Coordination Form PDF (Fix231, flowing layout Fix233): the look of
Modoku's own Canva form - orange page border, logo, big title, the boxed
details table, then "( ) / (✓)" options in small letter-spaced capitals.

Drawn with ReportLab (points from the top of an A4 page), so it looks the
same on every machine. It doesn't go through wkhtmltopdf, so the VPS's ~18%
scaling (see CLAUDE.md) doesn't apply.

Sections follow each other in the original order and a section that doesn't
fit starts a new page (never split). Sections switched off for the form are
left out, so a short form is 1-2 pages. The notes box is sized to the notes.
Every page has a small footer: CF number, who signed and confirmed, and when.
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


TOP_FIRST = 172       # page 1: the details table starts here
TOP_NEXT = 185        # later pages: the first section heading
BOTTOM = 812          # nothing below this (the footer sits under it)


def _frame(p, number, total, title, footer):
    c = p.c
    c.setStrokeColorRGB(*ORANGE)
    c.setLineWidth(9)
    c.rect(4.5, 4.5, W - 9, H - 9, stroke=1, fill=0)
    p.text(RIGHT + 5, 31, f"PAGE {number}/{total}", "Poppins-Medium", 8.5, (0.80, 0.80, 0.80), align="right")
    try:
        logo = ImageReader(_LOGO)
        lw = 98
        lh = lw * logo.getSize()[1] / logo.getSize()[0]
        c.drawImage(logo, (W - lw) / 2, p.y(42 + lh), lw, lh, mask="auto")
    except Exception:  # noqa: BLE001
        pass
    p.text(W / 2, 135, title, "Poppins-Bold", 23, INK, align="center")
    if footer:
        p.text(LEFT, 828, footer, "Poppins", 6.5, (0.62, 0.62, 0.62))


def render(form, answers, sections, design_images, other_files, design_other_files,
           client_sig_path=None, staff_sig_path=None, staff_name=None, staff_position=None,
           client_name=None, client_date=None, staff_date=None):
    """Returns the PDF bytes. answers/sections as in coordination_forms;
    design_images: (path, name) of T-shirt design images; other_files /
    design_other_files: names of the other attached files."""
    from .coordination_forms import CONSENT_TEXT, DIETS
    _register_fonts()
    a = answers
    on = set(sections)
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(W, H))
    c.setTitle(f"Coordination Form - {form['title']}")
    title = "COORDINATION FORM"
    measure = _Page(c)  # for wrapping text while measuring

    # ---- each block: (height, draw(p, top)). top = the heading's baseline
    # (for "details": the table's top edge). Heights include the gap after.

    def details(p, top):
        mid = 297
        edges = [top]
        for r in (33, 33, 33, 54):
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
        addr = p.wrap(a.get("address") or "", "Helvetica", 9, RIGHT - mid - 90)[:3] if a.get("address") else []
        addr_top = edges[3] + 14 if len(addr) > 1 else (edges[3] + edges[4]) / 2 + 2.6
        w = p.label(mid + 7, addr_top, "Address:", size=8)
        for i, line in enumerate(addr):
            p.text(mid + 12 + w, addr_top + i * 11, line, "Helvetica", 9)

    def room_setup(p, top):
        p.heading(top, "Room Setup Preferences")
        rs = a.get("room_setup")
        p.option(36, top + 34, "Classroom", rs == "Classroom")
        p.option(158, top + 34, "Theater", rs == "Theater")
        p.option(280, top + 34, "Other:", rs == "Other")
        p.hline(342, 440, top + 36)
        if rs == "Other":
            p.value(345, top + 33, a.get("room_setup_other") or "", size=8.5, max_width=94)
        p.option(36, top + 68, "U-Shape", rs == "U-Shape")
        p.option(158, top + 68, "Banquet", rs == "Banquet")

    def accommodation(p, top):
        p.heading(top, "Accommodation Information")
        occ = a.get("occupancy") or []
        p.label(36, top + 33, "Room occupancy")
        p.label(172, top + 33, "Breakfast")
        p.label(348, top + 33, "Special assistance (disabled, etc)")
        p.option(50, top + 64, "Single", "Single" in occ)
        p.option(50, top + 97, "Double", "Double" in occ)
        p.option(186, top + 64, "With breakfast", a.get("breakfast") == "With breakfast")
        p.option(186, top + 97, "Without breakfast", a.get("breakfast") == "Without breakfast")
        sa = a.get("special_assistance")
        p.option(362, top + 64, "Yes", sa == "Yes")
        p.option(362, top + 97, "No", sa == "No")
        if sa == "Yes" and a.get("special_assistance_details"):
            p.value(408, top + 64, a["special_assistance_details"], size=8.5, max_width=152)

    def fnb(p, top):
        p.heading(top, "Food & Beverage Requirements")
        diet = a.get("diet") or []
        p.label(36, top + 34, "Dietary preferences (please select all that apply):")
        p.label(348, top + 34, "Allergies")
        c.setStrokeColorRGB(0.85, 0.85, 0.85)
        c.setLineWidth(0.5)
        c.line(175, p.y(top + 57), 175, p.y(top + 141))
        for i, d in enumerate(DIETS):
            row = top + 61 + i * 12
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
        p.option(362, top + 66, "None", al == "None")
        p.option(362, top + 99, "Yes, please specify:", al == "Yes")
        p.hline(364, 540, top + 132)
        if al == "Yes" and a.get("allergies_details"):
            p.value(366, top + 129, a["allergies_details"], size=8.5, max_width=172)

    transport_lines = measure.wrap(a.get("transport_details") or "", "Helvetica", 7.2, 330)[:4]

    def transport(p, top):
        p.heading(top, "Transportation Details")
        tr, tt = a.get("transport_required"), a.get("transport_type")
        p.label(36, top + 31, "Transportation required")
        p.label(209, top + 31, "Type of transportation")
        p.option(50, top + 63, "Yes", tr == "Yes")
        p.option(50, top + 97, "No", tr == "No")
        p.option(223, top + 63, "Van", tt == "Van")
        p.option(223, top + 97, "Bus", tt == "Bus")
        p.option(314, top + 63, "Flight", tt == "Flight")
        p.option(314, top + 97, "Other:", tt == "Other")
        p.hline(376, 464, top + 99)
        if tt == "Other" and a.get("transport_other"):
            p.value(378, top + 96, a["transport_other"], size=8, max_width=84)
        for i, line in enumerate(transport_lines):
            p.text(226, top + 112 + i * 8.2, line, "Helvetica", 7.2)
        pax_top = top + max(140, 118 + len(transport_lines) * 8.2 + 12)
        w = p.label(36, pax_top, "Number of participants using this transportation:")
        p.hline(41 + w, 41 + w + 54, pax_top + 3)
        if a.get("transport_pax"):
            p.text(41 + w + 27, pax_top, str(a["transport_pax"]), "Helvetica", 9.5, align="center")

    transport_h = max(140, 118 + len(transport_lines) * 8.2 + 12) + 47

    shirt_lines = measure.wrap(a.get("tshirt_design") or "", "Helvetica", 9, 360)
    photos = design_images[:4]
    extra_files = design_other_files + [name for _path, name in design_images[4:]]
    photo_h = 180 if len(photos) <= 2 else 96
    photos_h = 0 if not photos else (photo_h if len(photos) <= 2 else 2 * photo_h + 6)

    def design(p, top):
        p.heading(top, "Design Specifications")
        w = p.label(36, top + 33, "T-shirt design (if any):")
        for i, line in enumerate(shirt_lines[:3]):
            p.text(42 + w, top + 33 + i * 11.5, line, "Helvetica", 9)
        box_top = top + 33 + max(1, min(3, len(shirt_lines))) * 11.5 + 8
        for i, (path, _name) in enumerate(photos):
            x = 35 if i % 2 == 0 else 292
            top_i = box_top + (i // 2) * (photo_h + 6)
            p.rect(x, top_i, 207, photo_h)
            p.image(path, x + 5, top_i + 5, 197, photo_h - 10)
        if extra_files:
            p.text(36, box_top + photos_h + (14 if photos else 4), "Design files: " + ", ".join(extra_files),
                   "Helvetica", 7.5)

    design_h = 33 + max(1, min(3, len(shirt_lines))) * 11.5 + 8 + photos_h + (22 if extra_files else 0) + 40

    def technical(p, top):
        p.heading(top, "Technical Requirements")
        lr = a.get("laptop_rental")
        p.label(36, top + 35, "Laptop rental")
        p.option(50, top + 67, "Yes:", lr == "Yes")
        p.hline(96, 149, top + 71)
        if lr == "Yes" and a.get("laptop_pcs"):
            p.text(122, top + 67, str(a["laptop_pcs"]), "Helvetica", 9.5, align="center")
        p.label(154, top + 67, "pcs")
        p.option(50, top + 100, "No", lr == "No")

    # Notes: the box fits the notes (10pt, at most ~half a page; smaller text if longer).
    notes = a.get("notes") or ""
    size = 10
    while True:
        note_lines = measure.wrap(notes, "Helvetica", size, 500) if notes else []
        if len(note_lines) * size * 1.95 <= 400 or size <= 6.5:
            break
        size -= 0.5
    notes_box = min(424, max(90, len(note_lines) * size * 1.95 + 30))

    def notes_block(p, top):
        p.heading(top, "Additional Notes/Requests")
        box_top = top + 17
        p.rect(LEFT, box_top, RIGHT - LEFT, notes_box)
        for i, line in enumerate(note_lines):
            y = box_top + 22 + i * size * 1.95
            if y > box_top + notes_box - 8:
                break
            p.text(46, y, line, "Helvetica", size)

    def attachments(p, top):
        p.text(LEFT, top - 26, "Attachments: " + ", ".join(other_files), "Helvetica", 7.5)  # just under the notes

    def consent(p, top):
        p.heading(top, "Consent & Agreement")
        for i, line in enumerate(CONSENT_TEXT):
            p.text(LEFT, top + 18 + i * 11.5, line, "Poppins", 7.8, (0.25, 0.25, 0.25))

        def sign_box(btop, lbl, sig_path, name, when):
            h, split = 84, 298
            p.rect(LEFT, btop, RIGHT - LEFT, h)
            c.setStrokeColorRGB(*LINE)
            c.line(split, p.y(btop), split, p.y(btop + h))
            p.hline(split, RIGHT, btop + h * 0.47)
            p.label(36, btop + 15, lbl, size=7.8)
            if sig_path and os.path.exists(sig_path):
                p.image(sig_path, 44, btop + 22, 230, 56)
            w = p.label(split + 8, btop + 15, "Name", size=7.8)
            p.value(split + 14 + w, btop + 15, name or "", size=9.5, max_width=RIGHT - split - w - 20)
            w = p.label(split + 8, btop + h * 0.47 + 26, "Date:", size=7.8)
            p.value(split + 14 + w, btop + h * 0.47 + 26, when or "", size=9.5)

        sign_box(top + 62, "Client's signature:", client_sig_path, client_name, client_date)
        staff = f"{staff_name}, {staff_position}" if staff_name and staff_position else staff_name
        sign_box(top + 156, "Confirmed by Modoku:", staff_sig_path, staff, staff_date)

    blocks = [("details", 194, details)]
    for key, h, fn in (("room_setup", 118, room_setup), ("accommodation", 147, accommodation),
                       ("fnb", 168, fnb), ("transport", transport_h, transport), ("design", design_h, design),
                       ("technical", 128, technical), ("notes", 17 + notes_box + 42, notes_block)):
        if key in on:
            blocks.append((key, h, fn))
    if other_files:
        blocks.append(("attachments", 14, attachments))
    blocks.append(("consent", 245, consent))

    # Pagination first (a section never splits), so "PAGE n/N" is known.
    pages, cur, y = [], [], TOP_FIRST
    for key, h, fn in blocks:
        if cur and y + h - 30 > BOTTOM:  # h includes ~30-40pt of space after the section
            pages.append(cur)
            cur, y = [], TOP_NEXT
        cur.append((y, fn))
        y += h
    pages.append(cur)

    footer = f"{form['form_no'] or ''}"
    if client_name:
        footer += f"  ·  Signed by {client_name}" + (f" on {client_date}" if client_date else "")
    if staff_name:
        footer += f"  ·  Confirmed by {staff_name}" + (f" on {staff_date}" if staff_date else "")
    for n, page in enumerate(pages, 1):
        p = _Page(c)
        _frame(p, n, len(pages), title, footer)
        for top, fn in page:
            fn(p, top)
        c.showPage()
    c.save()
    return buf.getvalue()
