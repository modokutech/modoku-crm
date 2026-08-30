"""Public, no-login "How to Get Your e-Certificate" page.

Meant to be projected on the training room screen (or printed) at the end
of a class so participants know how to claim their e-Certificate afterwards
— a single page, deliberately kept short enough to need no scrolling, with
a QR code that jumps straight to the /cert lookup page.

The QR image is generated on the fly by the same styled-QR renderer used
for the evaluation-form poster (see poster.py) rather than duplicating
that logic, so both stay visually consistent and both keep working if the
site's own URL ever changes.
"""
import os
from io import BytesIO

from flask import Blueprint, Response, current_app, render_template, url_for

from .poster import _draw_qr

bp = Blueprint("tutorial", __name__, url_prefix="/how")


@bp.route("/")
def index():
    return render_template("tutorial/index.html")


@bp.route("/qr.png")
def qr_image():
    cert_url = url_for("certificates.lookup", _external=True)
    logo_path = os.path.join(current_app.root_path, "static", "img", "logo.png")
    img = _draw_qr(cert_url, box_size=520, logo_path=logo_path)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), mimetype="image/png",
                     headers={"Cache-Control": "public, max-age=3600"})
