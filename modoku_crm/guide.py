"""Staff-facing "How to Use Modoku Hub" reference guide.

A single static page (no forms, no database reads beyond the login check)
documenting the order-of-operations staff should follow when taking a class
from first contact through to invoicing: Client (Company) -> Leads ->
Trainer (+ their documents) -> Course -> Class -> Quotation -> Purchase
Order -> Invoice. Login required since this is for internal staff, not the
client/trainer/vendor-facing public pages elsewhere in the app.

Lives at /tutorial — the path freed up when the old participant-facing
"Get Your e-Certificate" page (tutorial.py) moved to /how.
"""
from flask import Blueprint, render_template

from .auth import login_required

bp = Blueprint("guide", __name__, url_prefix="/tutorial")


@bp.route("/")
@login_required
def index():
    return render_template("guide/index.html")
