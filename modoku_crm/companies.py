from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from . import activity, db
from .auth import admin_required, login_required
from .csvutil import csv_response

bp = Blueprint("companies", __name__, url_prefix="/companies")

MY_STATES = [
    "Johor", "Kedah", "Kelantan", "Melaka", "Negeri Sembilan", "Pahang",
    "Perak", "Perlis", "Pulau Pinang", "Sabah", "Sarawak", "Selangor",
    "Terengganu", "W.P. Kuala Lumpur", "W.P. Labuan", "W.P. Putrajaya",
]


def _form_fields(form):
    return (
        form.get("name", "").strip(),
        form.get("registration_no") or None,
        form.get("sst_reg_no") or None,
        form.get("tin") or None,
        form.get("industry") or None,
        form.get("address") or None,
        form.get("city") or None,
        form.get("state") or None,
        form.get("postcode") or None,
        form.get("phone") or None,
        form.get("email") or None,
        form.get("notes") or None,
    )


def _filtered_companies():
    q = request.args.get("q", "").strip()
    sql = """SELECT co.*, (SELECT COUNT(*) FROM leads l WHERE l.company_id = co.id) AS lead_count
              FROM companies co WHERE 1=1"""
    args = []
    if q:
        sql += " AND co.name LIKE ?"
        args.append(f"%{q}%")
    sql += " ORDER BY co.name"
    return db.query(sql, args), q


@bp.route("/")
@login_required
def index():
    companies, q = _filtered_companies()
    return render_template("companies/list.html", companies=companies, q=q)


@bp.route("/export")
@admin_required
def export():
    companies, _q = _filtered_companies()
    rows = (
        (c["name"], c["industry"] or "", c["registration_no"] or "", c["sst_reg_no"] or "",
         c["tin"] or "", c["address"] or "", c["city"] or "", c["state"] or "", c["postcode"] or "",
         c["phone"] or "", c["email"] or "", c["lead_count"])
        for c in companies
    )
    return csv_response(
        "clients.csv",
        ["Name", "Industry", "Registration No", "SST Reg No", "TIN", "Address", "City", "State",
         "Postcode", "Phone", "Email", "Leads"],
        rows,
    )


@bp.route("/new", methods=("GET", "POST"))
@login_required
def new():
    if request.method == "POST":
        fields = _form_fields(request.form)
        if not fields[0]:
            flash("Company name is required.", "danger")
        else:
            company_id = db.execute(
                """INSERT INTO companies (name, registration_no, sst_reg_no, tin, industry,
                       address, city, state, postcode, phone, email, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                fields,
            )
            activity.log("create", "company", company_id, f"Created client {fields[0]}")
            flash("Client added.", "success")
            return redirect(url_for("companies.index"))
    return render_template("companies/form.html", company=None, states=MY_STATES)


@bp.route("/quick-add", methods=("POST",))
@login_required
def quick_add():
    """Create a company from a small inline modal (e.g. from the New Lead form),
    without navigating away and losing whatever else was being filled in."""
    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Company name is required."}), 400
    company_id = db.execute(
        """INSERT INTO companies (name, industry, phone, email)
           VALUES (?,?,?,?)""",
        (name, request.form.get("industry") or None, request.form.get("phone") or None,
         request.form.get("email") or None),
    )
    return jsonify({"id": company_id, "name": name})


@bp.route("/<int:company_id>")
@login_required
def view(company_id):
    company = db.query("SELECT * FROM companies WHERE id = ?", (company_id,), one=True)
    if company is None:
        flash("Company not found.", "danger")
        return redirect(url_for("companies.index"))
    leads = db.query(
        "SELECT * FROM leads WHERE company_id = ? ORDER BY created_at DESC", (company_id,)
    )
    invoices = db.query(
        "SELECT * FROM invoices WHERE company_id = ? ORDER BY invoice_date DESC", (company_id,)
    )
    enrollments = db.query(
        """SELECT e.*, cs.start_date, c.title AS course_title FROM enrollments e
           JOIN course_sessions cs ON cs.id = e.session_id
           JOIN courses c ON c.id = cs.course_id
           WHERE e.company_id = ? ORDER BY cs.start_date DESC""",
        (company_id,),
    )
    return render_template("companies/view.html", company=company, leads=leads,
                            invoices=invoices, enrollments=enrollments)


@bp.route("/<int:company_id>/edit", methods=("GET", "POST"))
@login_required
def edit(company_id):
    company = db.query("SELECT * FROM companies WHERE id = ?", (company_id,), one=True)
    if company is None:
        flash("Company not found.", "danger")
        return redirect(url_for("companies.index"))
    if request.method == "POST":
        fields = _form_fields(request.form)
        if not fields[0]:
            flash("Company name is required.", "danger")
        else:
            db.execute(
                """UPDATE companies SET name=?, registration_no=?, sst_reg_no=?, tin=?, industry=?,
                       address=?, city=?, state=?, postcode=?, phone=?, email=?, notes=?
                   WHERE id=?""",
                fields + (company_id,),
            )
            activity.log("update", "company", company_id, f"Updated client {fields[0]}")
            flash("Company updated.", "success")
            return redirect(url_for("companies.view", company_id=company_id))
    return render_template("companies/form.html", company=company, states=MY_STATES)


@bp.route("/<int:company_id>/delete", methods=("POST",))
@login_required
def delete(company_id):
    company = db.query("SELECT name FROM companies WHERE id = ?", (company_id,), one=True)
    db.execute("DELETE FROM companies WHERE id = ?", (company_id,))
    activity.log("delete", "company", company_id,
                  f"Deleted client {company['name'] if company else company_id}")
    flash("Company deleted.", "success")
    return redirect(url_for("companies.index"))
