"""Seed the Modoku Hub database with sample data for demo/testing purposes.

Usage: python seed.py [--reset]
  --reset  drop and recreate all tables before seeding
"""
import sys
from datetime import date, timedelta

from werkzeug.security import generate_password_hash

from modoku_crm import create_app, db

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
            DROP TABLE IF EXISTS t3_participants;
            DROP TABLE IF EXISTS session_trainers;
            DROP TABLE IF EXISTS enrollments;
            DROP TABLE IF EXISTS course_sessions;
            DROP TABLE IF EXISTS courses;
            DROP TABLE IF EXISTS trainers;
            DROP TABLE IF EXISTS lead_activities;
            DROP TABLE IF EXISTS leads;
            DROP TABLE IF EXISTS companies;
            DROP TABLE IF EXISTS settings;
            DROP TABLE IF EXISTS calendar_connections;
            DROP TABLE IF EXISTS users;
            """
        )
        conn.commit()
        conn.executescript(db.SCHEMA)
        conn.commit()

    existing = db.query("SELECT COUNT(*) c FROM users", one=True)["c"]
    if existing:
        print("Database already has data — skipping seed. Use `python seed.py --reset` to start fresh.")
        sys.exit(0)

    # --- Staff users ---
    admin_id = db.execute(
        "INSERT INTO users (name, email, password_hash, role) VALUES (?,?,?,?)",
        ("Admin", "admin@modoku.tech", generate_password_hash("admin123"), "admin"),
    )
    # Second admin account for Erik's own testing.
    db.execute(
        "INSERT INTO users (name, email, password_hash, role) VALUES (?,?,?,?)",
        ("Erik Tajudin", "eriktajudin@modoku.tech", generate_password_hash("admin123"), "admin"),
    )
    sales1_id = db.execute(
        "INSERT INTO users (name, email, password_hash, role) VALUES (?,?,?,?)",
        ("Aisyah Rahman", "aisyah@modoku.tech", generate_password_hash("staff123"), "staff"),
    )
    sales2_id = db.execute(
        "INSERT INTO users (name, email, password_hash, role) VALUES (?,?,?,?)",
        ("Wei Jian Lim", "weijian@modoku.tech", generate_password_hash("staff123"), "staff"),
    )

    # --- Clients ---
    c1 = db.execute(
        """INSERT INTO companies (name, registration_no, sst_reg_no, tin, industry, address, city,
               state, postcode, phone, email)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("Petrofin Sdn Bhd", "199801012345", "SST-B16-1234567", "C1234567890",
         "Oil & Gas", "Level 20, Menara Petrofin, Jalan Ampang", "Kuala Lumpur",
         "W.P. Kuala Lumpur", "50450", "03-21234567", "hr@petrofin.example"),
    )
    c2 = db.execute(
        """INSERT INTO companies (name, registration_no, sst_reg_no, tin, industry, address, city,
               state, postcode, phone, email)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("Selatan Bank Berhad", "197201002233", "SST-B16-9988776", "C9988776655",
         "Banking & Finance", "Menara Selatan, Jalan Sultan Ismail", "Kuala Lumpur",
         "W.P. Kuala Lumpur", "50250", "03-27654321", "training@selatanbank.example"),
    )
    c3 = db.execute(
        """INSERT INTO companies (name, registration_no, sst_reg_no, tin, industry, address, city,
               state, postcode, phone, email)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("Nusantara Manufacturing Sdn Bhd", "201001011111", None, "C5566778899",
         "Manufacturing", "Lot 15, Kawasan Perindustrian Shah Alam", "Shah Alam",
         "Selangor", "40200", "03-55123456", "admin@nusantaramfg.example"),
    )

    # --- Trainers ---
    t1 = db.execute("INSERT INTO trainers (name, email, phone, specialization) VALUES (?,?,?,?)",
                     ("Dr. Farah Iskandar", "farah@modoku.tech", "012-3456789", "Leadership & Management"))
    t2 = db.execute("INSERT INTO trainers (name, email, phone, specialization) VALUES (?,?,?,?)",
                     ("Kamal Hisham", "kamal@modoku.tech", "013-9876543", "Occupational Safety & Health"))
    t3 = db.execute("INSERT INTO trainers (name, email, phone, specialization) VALUES (?,?,?,?)",
                     ("Priya Menon", "priya@modoku.tech", "016-2223344", "Digital Marketing & IT"))

    # --- Courses ---
    course1 = db.execute(
        """INSERT INTO courses (code, title, category, description, duration_days, price, hrdf_claimable, active)
           VALUES (?,?,?,?,?,?,1,1)""",
        ("LDR-101", "Effective Leadership for New Managers", "Leadership",
         "A 2-day programme equipping new managers with core leadership and people skills.", 2, 1800),
    )
    course2 = db.execute(
        """INSERT INTO courses (code, title, category, description, duration_days, price, hrdf_claimable, active)
           VALUES (?,?,?,?,?,?,1,1)""",
        ("OSH-201", "Occupational Safety & Health Awareness", "Compliance",
         "Statutory OSH awareness training for supervisors and safety committee members.", 1, 950),
    )
    course3 = db.execute(
        """INSERT INTO courses (code, title, category, description, duration_days, price, hrdf_claimable, active)
           VALUES (?,?,?,?,?,?,0,1)""",
        ("DM-301", "Digital Marketing Fundamentals", "Marketing",
         "Hands-on introduction to SEO, social media and paid ads for SMEs.", 2, 1500),
    )

    today = date.today()

    # --- Course sessions ---
    s1 = db.execute(
        """INSERT INTO course_sessions (course_id, trainer_id, venue, start_date, end_date, capacity, status)
           VALUES (?,?,?,?,?,?,?)""",
        (course1, t1, "Modoku Training Centre, KL", (today + timedelta(days=10)).isoformat(),
         (today + timedelta(days=11)).isoformat(), 20, "Scheduled"),
    )
    s2 = db.execute(
        """INSERT INTO course_sessions (course_id, trainer_id, venue, start_date, end_date, capacity, status)
           VALUES (?,?,?,?,?,?,?)""",
        (course2, t2, "Nusantara Manufacturing, Shah Alam (in-house)", (today + timedelta(days=20)).isoformat(),
         (today + timedelta(days=20)).isoformat(), 25, "Scheduled"),
    )
    s3 = db.execute(
        """INSERT INTO course_sessions (course_id, trainer_id, venue, start_date, end_date, capacity, status)
           VALUES (?,?,?,?,?,?,?)""",
        (course3, t3, "Modoku Training Centre, KL", (today - timedelta(days=15)).isoformat(),
         (today - timedelta(days=14)).isoformat(), 15, "Completed"),
    )

    # --- Leads ---
    l1 = db.execute(
        """INSERT INTO leads (name, role, email, phone, company_id, source, status, assigned_to,
               interested_course_id, next_follow_up, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("Nurul Huda", "HR Manager", "nurul.huda@petrofin.example", "012-3334455", c1, "Referral",
         "Proposal Sent", sales1_id, course1, (today + timedelta(days=1)).isoformat(),
         "Wants leadership training for 15 first-line managers. Proposal sent, awaiting budget approval."),
    )
    l2 = db.execute(
        """INSERT INTO leads (name, role, email, phone, company_id, source, status, assigned_to,
               interested_course_id, next_follow_up, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("Azman Yusof", "Safety Officer", "azman@nusantaramfg.example", "019-8887766", c3, "Website",
         "Qualified", sales1_id, course2, today.isoformat(),
         "Confirmed interest in OSH training for 25 factory supervisors, in-house preferred."),
    )
    l3 = db.execute(
        """INSERT INTO leads (name, role, email, phone, company_id, source, status, assigned_to,
               interested_course_id, next_follow_up, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("Chong Mei Ling", "L&D Executive", "meiling@selatanbank.example", "017-2223344", c2, "Phone",
         "Contacted", sales2_id, course3, (today + timedelta(days=3)).isoformat(),
         "Exploring digital marketing training for branch marketing staff."),
    )
    l4 = db.execute(
        """INSERT INTO leads (name, role, email, phone, company_id, source, status, assigned_to,
               interested_course_id, next_follow_up, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("Suresh Kumar", "Individual", "suresh.k@example.com", "011-55667788", None, "Social Media",
         "New", sales2_id, course1, (today - timedelta(days=1)).isoformat(),
         "Self-sponsored, asked about instalment payment for leadership course."),
    )
    l5 = db.execute(
        """INSERT INTO leads (name, email, phone, company_id, source, status, assigned_to, notes)
           VALUES (?,?,?,?,?,?,?,?)""",
        ("Fatimah Zainal", "fatimah.z@example.com", "013-1112233", None, "Walk-in", "Closed", sales1_id,
         "Enrolled in Digital Marketing Fundamentals — closed deal."),
    )
    l6 = db.execute(
        """INSERT INTO leads (name, email, phone, company_id, source, status, assigned_to, notes)
           VALUES (?,?,?,?,?,?,?,?)""",
        ("Ravi Chandran", "ravi.c@example.com", "012-9998877", None, "Referral", "Lost", sales2_id,
         "Went with a competitor offering a lower price."),
    )

    # --- Lead activities ---
    db.execute("INSERT INTO lead_activities (lead_id, activity_type, note, created_by) VALUES (?,?,?,?)",
               (l1, "Call", "Initial discovery call — discussed training needs for 15 managers.", sales1_id))
    db.execute("INSERT INTO lead_activities (lead_id, activity_type, note, created_by) VALUES (?,?,?,?)",
               (l1, "Email", "Sent proposal PDF with pricing for LDR-101.", sales1_id))
    db.execute("INSERT INTO lead_activities (lead_id, activity_type, note, created_by) VALUES (?,?,?,?)",
               (l2, "Call", "Confirmed in-house training preferred, discussing dates.", sales1_id))
    db.execute("INSERT INTO lead_activities (lead_id, activity_type, note, created_by) VALUES (?,?,?,?)",
               (l3, "Call", "Left voicemail, will call again.", sales2_id))
    db.execute("INSERT INTO lead_activities (lead_id, activity_type, note, created_by) VALUES (?,?,?,?)",
               (l4, "Note", "Enquired via Instagram DM about payment plans.", sales2_id))

    # --- Enrollments ---
    e1 = db.execute(
        """INSERT INTO enrollments (session_id, lead_id, company_id, participant_name, participant_email,
               status, hrdf_claim_status, hrdf_claim_no, amount)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (s3, l5, None, "Fatimah Zainal", "fatimah.z@example.com", "Completed", "Not Applicable", None, 1500),
    )
    e2 = db.execute(
        """INSERT INTO enrollments (session_id, company_id, participant_name, participant_email,
               status, hrdf_claim_status, hrdf_claim_no, amount)
           VALUES (?,?,?,?,?,?,?,?)""",
        (s1, c1, "Ahmad Firdaus", "ahmad.firdaus@petrofin.example", "Registered", "Pending",
         "HRDF-2026-00123", 1800),
    )
    e3 = db.execute(
        """INSERT INTO enrollments (session_id, company_id, participant_name, participant_email,
               status, hrdf_claim_status, hrdf_claim_no, amount)
           VALUES (?,?,?,?,?,?,?,?)""",
        (s1, c1, "Siti Aminah", "siti.aminah@petrofin.example", "Registered", "Pending",
         "HRDF-2026-00124", 1800),
    )
    e4 = db.execute(
        """INSERT INTO enrollments (session_id, company_id, participant_name, participant_email,
               status, hrdf_claim_status, amount)
           VALUES (?,?,?,?,?,?,?)""",
        (s2, c3, "Rosli Ibrahim", "rosli@nusantaramfg.example", "Registered", "Approved", 950),
    )

    # --- Invoices ---
    inv1 = db.execute(
        """INSERT INTO invoices (invoice_no, company_id, bill_to_name, bill_to_address, sst_reg_no,
               buyer_tin, invoice_date, due_date, currency, subtotal, sst_rate, sst_amount, total, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"INV-{today.year}-0001", c1, "Petrofin Sdn Bhd",
         "Level 20, Menara Petrofin, Jalan Ampang, 50450 Kuala Lumpur", "SST-B16-1234567",
         "C1234567890", today.isoformat(), (today + timedelta(days=30)).isoformat(), "RM",
         3600, 0, 0, 3600, "Sent"),
    )
    db.execute(
        """INSERT INTO invoice_items (invoice_id, enrollment_id, description, quantity, unit_price, amount)
           VALUES (?,?,?,?,?,?)""",
        (inv1, e2, "Effective Leadership for New Managers — Ahmad Firdaus", 1, 1800, 1800),
    )
    db.execute(
        """INSERT INTO invoice_items (invoice_id, enrollment_id, description, quantity, unit_price, amount)
           VALUES (?,?,?,?,?,?)""",
        (inv1, e3, "Effective Leadership for New Managers — Siti Aminah", 1, 1800, 1800),
    )

    inv2 = db.execute(
        """INSERT INTO invoices (invoice_no, company_id, bill_to_name, invoice_date, due_date, currency,
               subtotal, sst_rate, sst_amount, total, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (f"INV-{today.year}-0002", None, "Fatimah Zainal", (today - timedelta(days=14)).isoformat(),
         (today - timedelta(days=0)).isoformat(), "RM", 1500, 0, 0, 1500, "Paid"),
    )
    db.execute(
        """INSERT INTO invoice_items (invoice_id, enrollment_id, description, quantity, unit_price, amount)
           VALUES (?,?,?,?,?,?)""",
        (inv2, e1, "Digital Marketing Fundamentals — Fatimah Zainal", 1, 1500, 1500),
    )

    print("Seed complete.")
    print("Log in with: admin@modoku.tech / admin123 (admin)")
    print("             eriktajudin@modoku.tech / admin123 (admin)")
    print("             aisyah@modoku.tech / staff123 (staff)")
    print("             weijian@modoku.tech / staff123 (staff)")
