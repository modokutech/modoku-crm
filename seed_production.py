"""Set up a fresh, empty Modoku Hub database for real production use.

Unlike seed.py (which loads a full set of sample companies, leads, courses,
sessions, and documents for demo/testing), this script creates the database
schema and exactly ONE user account — the admin login you'll actually use —
with no sample/demo data of any kind.

Usage: python seed_production.py [--reset]
  --reset  drop and recreate all tables first (⚠ destroys any existing data)
"""
import sys

from werkzeug.security import generate_password_hash

from modoku_crm import create_app, db

ADMIN_NAME = "Erik Tajudin"
ADMIN_EMAIL = "eriktajudin@modoku.tech"
ADMIN_PASSWORD = "admin123"

app = create_app()

with app.app_context():
    conn = db.get_db()

    if "--reset" in sys.argv:
        conn.executescript(
            """
            DROP TABLE IF EXISTS invoice_items;
            DROP TABLE IF EXISTS invoices;
            DROP TABLE IF EXISTS po_documents;
            DROP TABLE IF EXISTS po_items;
            DROP TABLE IF EXISTS purchase_orders;
            DROP TABLE IF EXISTS certificates;
            DROP TABLE IF EXISTS attendance_returns;
            DROP TABLE IF EXISTS t3_day_attendance;
            DROP TABLE IF EXISTS t3_participants;
            DROP TABLE IF EXISTS session_trainers;
            DROP TABLE IF EXISTS enrollments;
            DROP TABLE IF EXISTS course_sessions;
            DROP TABLE IF EXISTS courses;
            DROP TABLE IF EXISTS trainers;
            DROP TABLE IF EXISTS lead_activities;
            DROP TABLE IF EXISTS leads;
            DROP TABLE IF EXISTS companies;
            DROP TABLE IF EXISTS training_costs;
            DROP TABLE IF EXISTS settings;
            DROP TABLE IF EXISTS calendar_connections;
            DROP TABLE IF EXISTS users;
            """
        )
        conn.commit()

    # Creates any tables that don't exist yet — safe to run on a brand new
    # database file or one that was just dropped by --reset above.
    conn.executescript(db.SCHEMA)
    conn.commit()

    existing = db.query("SELECT COUNT(*) c FROM users", one=True)["c"]
    if existing:
        print("Database already has user accounts — skipping. "
              "Use `python seed_production.py --reset` to wipe everything and start clean.")
        sys.exit(0)

    db.execute(
        "INSERT INTO users (name, email, password_hash, role) VALUES (?,?,?,?)",
        (ADMIN_NAME, ADMIN_EMAIL, generate_password_hash(ADMIN_PASSWORD), "admin"),
    )
    conn.commit()

    print("Production database ready — no sample data loaded.")
    print(f"Admin login: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
    print("Change this password immediately after your first login (Staff Users > your account).")
