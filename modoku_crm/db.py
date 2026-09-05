"""SQLite data layer for Modoku Hub.

Deliberately dependency-free (uses Python's built-in sqlite3) so the app
runs anywhere with just Flask installed. Swappable for Postgres/MySQL later
by replacing this module if Modoku Hub outgrows SQLite.
"""
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import current_app, g

# Unmistakable characters only — no 0/O or 1/I/L — since this code is
# hand-typed by trainers on their phone after reading it off a printed form.
_SESSION_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def generate_session_code(existing_codes=()):
    """A short, phone-typeable code identifying one class — stamped on the
    printed T3 Attendance Form and keyed in by trainers on the public Return
    Attendance Form page. existing_codes lets callers avoid a DB round trip
    per attempt when backfilling many rows at once."""
    existing_codes = set(existing_codes)
    for _ in range(50):
        code = "".join(secrets.choice(_SESSION_CODE_ALPHABET) for _ in range(6))
        if code in existing_codes:
            continue
        if not existing_codes and query("SELECT id FROM course_sessions WHERE session_code = ?", (code,), one=True):
            continue
        return code
    raise RuntimeError("Could not generate a unique session code")


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'staff',   -- admin | staff
    active INTEGER NOT NULL DEFAULT 1,
    avatar_file TEXT,
    position TEXT,                 -- job title, shown under the signature on documents
    signature_file TEXT,           -- uploaded signature image, used on Purchase Orders/Quotations
    contact_phone TEXT,            -- shown on Quotations ("contact me at ...")
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    registration_no TEXT,
    sst_reg_no TEXT,
    tin TEXT,                              -- LHDN Tax Identification No. (e-invoice)
    industry TEXT,
    address TEXT,
    city TEXT,
    state TEXT,
    postcode TEXT,
    phone TEXT,
    email TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT,                             -- PIC's job title/role at their company
    email TEXT,
    phone TEXT,
    company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    source TEXT,                           -- Website, Referral, Social Media, Phone, Walk-in, Other
    status TEXT NOT NULL DEFAULT 'New',    -- New, Contacted, Had Meeting, Proposal Sent, Deal Closed, Lost
    assigned_to INTEGER REFERENCES users(id) ON DELETE SET NULL,
    interested_course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    next_follow_up TEXT,                    -- date of next call/action due
    namecard_file TEXT,                     -- uploaded business card image
    proposal_file TEXT,                     -- uploaded proposal deck (pdf/ppt/doc)
    linkedin_url TEXT,
    lost_reason TEXT,                       -- why this lead was marked Lost, for later analysis
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lead_activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    activity_type TEXT NOT NULL DEFAULT 'Note',  -- Call, Email, Meeting, Note
    note TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trainers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    specialization TEXT,
    notes TEXT,
    profile_file TEXT,             -- trainer profile / CV document
    ttt_cert_file TEXT,            -- Train-the-Trainer certificate
    accredited_cert_file TEXT,     -- HRD Corp / other accreditation certificate
    avatar_file TEXT,              -- small display photo shown on the trainers list/detail
    rate_per_day REAL DEFAULT 0,   -- used to auto-prefill Purchase Order fee (rate x course duration)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trainer_rate_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trainer_id INTEGER NOT NULL REFERENCES trainers(id) ON DELETE CASCADE,
    old_rate REAL,                 -- NULL for the very first recorded rate (nothing to compare against)
    new_rate REAL NOT NULL,
    changed_at TEXT NOT NULL DEFAULT (datetime('now')),
    changed_by INTEGER REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE,
    title TEXT NOT NULL,
    category TEXT,
    description TEXT,
    duration_days REAL DEFAULT 1,
    price REAL NOT NULL DEFAULT 0,   -- legacy single price field, superseded by price_inhouse/price_public below
    price_inhouse REAL NOT NULL DEFAULT 0,   -- In-house Training price
    price_public REAL NOT NULL DEFAULT 0,    -- Public Training price, per pax per day
    hrdf_claimable INTEGER NOT NULL DEFAULT 0,
    hrdcorp_programme_no TEXT,       -- HRDCorp-assigned programme number, for the client's grant application
    active INTEGER NOT NULL DEFAULT 1,
    outline_file TEXT,              -- uploaded course outline / syllabus document
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS course_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    trainer_id INTEGER REFERENCES trainers(id) ON DELETE SET NULL,
    client_company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,  -- sponsoring client for in-house training
    venue TEXT,
    start_date TEXT NOT NULL,
    end_date TEXT,
    training_time TEXT,             -- e.g. '9:00 AM - 5:00 PM'
    training_type TEXT,             -- In-house Training, Public Training, Workshop, Conference
    training_mode TEXT NOT NULL DEFAULT 'Physical',  -- Physical, Virtual, Hybrid
    meeting_link TEXT,               -- Zoom/Teams/etc. link, used when Virtual or Hybrid
    capacity INTEGER DEFAULT 20,
    status TEXT NOT NULL DEFAULT 'Scheduled',  -- Scheduled, Ongoing, Completed, Cancelled
    attendance_file TEXT,            -- uploaded (signed) attendance sheet, added after the session runs
    evaluation_report_file TEXT,     -- uploaded post-training evaluation report
    evaluation_sent_at TEXT,         -- when the evaluation report was last emailed to the client
    evaluation_sent_to TEXT,
    evaluation_form_link TEXT,       -- URL to the external evaluation form (e.g. Google Form)
    evaluation_qr_poster_file TEXT,  -- generated "Training Evaluation" QR poster (JPEG)
    training_banner_file TEXT,       -- training banner image, attached to the trainer's PO email if set
    jd14_file TEXT,                  -- uploaded signed HRDCorp JD14 claim form
    jd14_sent_at TEXT,                -- when the signed JD14 form was last emailed
    jd14_sent_to TEXT,
    jd14_return_token TEXT,          -- public link token for the client's JD14 return-upload page
    jd14_received_at TEXT,           -- when the signed copy first arrived via that public page
    jd14_received_via TEXT,          -- 'client_upload' when submitted through the public page
    session_code TEXT,               -- short code stamped on the printed T3 form; trainers key it
                                      -- into the public Return Attendance Form page to find this class
    notes TEXT,
    grant_quotation_file TEXT,       -- quotation copy manually uploaded for the HRDCorp Grant Documents pack
                                      -- (the other 3 grant docs are auto-derived: course outline, trainer
                                      -- profile, accredited cert — see courses.outline_file / trainers.*)
    grant_docs_token TEXT,           -- public link token for the client's HRDCorp Grant ID entry page
    grant_docs_sent_at TEXT,         -- when the Grant Documents email was last sent
    grant_docs_sent_to TEXT,
    hrdcorp_grant_id TEXT,           -- submitted by the client via the public grant-ID page once approved
    hrdcorp_grant_id_updated_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Photos of the signed attendance form the trainer snaps/scans and submits
-- through the public "Return Attendance Form" page (no login) after
-- training ends — matched to the class by session_code. Kept separate from
-- the single attendance_file the office can also upload directly, since a
-- multi-day training may come back as several photos over several days.
CREATE TABLE IF NOT EXISTS attendance_returns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES course_sessions(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    original_name TEXT,
    submitted_by_note TEXT,          -- optional free-text the trainer can leave (e.g. "Day 2 of 2")
    ai_names_json TEXT,               -- names Claude read off this photo, cached (see ai_match.py)
    ai_analyzed_at TEXT,              -- when that AI read last ran, so it isn't repeated every page load
    ai_detected_title TEXT,           -- course title Claude read off the sheet itself (cross-check)
    ai_detected_date TEXT,            -- training date Claude read off the sheet, normalized YYYY-MM-DD
    training_date TEXT,               -- the training day this photo was resolved to (once matched to
                                       -- one of the class's actual training dates); NULL until resolved
    ai_mismatch INTEGER NOT NULL DEFAULT 0,   -- 1 if the detected title/date didn't check out — see ai_match.py
    ai_mismatch_reason TEXT,
    ai_action TEXT,                   -- 'auto_marked' / 'mismatch' once auto_mark_attendance has
                                       -- processed this photo, so it's never re-processed (and never
                                       -- double-notified) on a later run
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Per-day T3 attendance for multi-day trainings. t3_participants.attended
-- stays the single "certificate eligible" flag every other part of the app
-- already checks, but for a training spanning more than one calendar day it
-- is no longer set by hand for each day — attendance_days.py is the only
-- writer, and only flips it to 1 once a participant has a row here for
-- *every* one of the session's scheduled training days (HRDCorp's own rule:
-- missing a day of a multi-day programme means not fully attended).
CREATE TABLE IF NOT EXISTS t3_day_attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id INTEGER NOT NULL REFERENCES t3_participants(id) ON DELETE CASCADE,
    training_date TEXT NOT NULL,      -- YYYY-MM-DD, one of the session's training days
    marked_at TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT NOT NULL DEFAULT 'manual',   -- 'manual' (staff, via Attendance List) or 'ai'
    UNIQUE(participant_id, training_date)
);

-- A training session can have more than one trainer (co-facilitators).
-- course_sessions.trainer_id is kept as the "primary" trainer for backward
-- compatibility with existing single-trainer displays; this table holds the
-- full assigned roster.
CREATE TABLE IF NOT EXISTS session_trainers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES course_sessions(id) ON DELETE CASCADE,
    trainer_id INTEGER NOT NULL REFERENCES trainers(id) ON DELETE CASCADE,
    UNIQUE(session_id, trainer_id)
);

-- Participants on the printed T3 attendance list — deliberately separate from
-- `enrollments` (which drives invoicing/HRDF-claim tracking). Attendance-list
-- names are specific to that one training day and shouldn't affect enrollment
-- counts, capacity, or claims.
CREATE TABLE IF NOT EXISTS t3_participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES course_sessions(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    ic_no TEXT,
    employer_name TEXT,
    gender TEXT,                     -- Male, Female
    citizenship TEXT DEFAULT 'Malaysian',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per generated e-Certificate — created (and kept in sync) the
-- moment staff mark a t3_participants row attended, so certificates are
-- ready and browsable ahead of time under the Certificates tab, grouped by
-- Class, rather than only ever being built on demand when a participant
-- claims theirs on the public front-end (that flow still works too, and
-- self-heals into this table if a file is somehow missing).
CREATE TABLE IF NOT EXISTS certificates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES course_sessions(id) ON DELETE CASCADE,
    participant_id INTEGER NOT NULL UNIQUE REFERENCES t3_participants(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    generated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS enrollments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES course_sessions(id) ON DELETE CASCADE,
    lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    participant_name TEXT NOT NULL,
    participant_email TEXT,
    participant_phone TEXT,
    ic_no TEXT,                             -- NRIC, for the HRDCorp T3 Attendance Form
    gender TEXT,                            -- Male, Female
    citizenship TEXT DEFAULT 'Malaysian',
    status TEXT NOT NULL DEFAULT 'Registered',   -- Registered, Attended, Completed, Cancelled, No-show
    hrdf_claim_status TEXT NOT NULL DEFAULT 'Not Applicable', -- Not Applicable, Pending, Approved, Claimed, Rejected
    hrdf_claim_no TEXT,
    amount REAL NOT NULL DEFAULT 0,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_no TEXT NOT NULL UNIQUE,
    company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    bill_to_name TEXT NOT NULL,
    bill_to_address TEXT,
    sst_reg_no TEXT,
    buyer_tin TEXT,
    invoice_date TEXT NOT NULL,
    due_date TEXT,
    currency TEXT NOT NULL DEFAULT 'RM',
    subtotal REAL NOT NULL DEFAULT 0,
    sst_rate REAL NOT NULL DEFAULT 0,
    sst_amount REAL NOT NULL DEFAULT 0,
    total REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'Draft',  -- Draft, Sent, Paid, Overdue, Cancelled
    notes TEXT,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS invoice_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    enrollment_id INTEGER REFERENCES enrollments(id) ON DELETE SET NULL,
    description TEXT NOT NULL,
    quantity REAL NOT NULL DEFAULT 1,
    unit_price REAL NOT NULL DEFAULT 0,
    amount REAL NOT NULL DEFAULT 0,
    duration TEXT,      -- optional, e.g. "2 Days" — shown in the item's sub-detail block
    venue TEXT,          -- optional
    item_date TEXT,       -- optional
    item_date_end TEXT     -- optional: set only for items spanning more than one day
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    po_no TEXT NOT NULL UNIQUE,
    session_id INTEGER NOT NULL REFERENCES course_sessions(id) ON DELETE CASCADE,
    trainer_id INTEGER NOT NULL REFERENCES trainers(id) ON DELETE RESTRICT,
    fee_amount REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'RM',
    status TEXT NOT NULL DEFAULT 'Draft',   -- Draft, Sent, Confirmed, Cancelled
    terms TEXT,                             -- Terms & Conditions
    trainer_responsibilities TEXT,
    issue_date TEXT NOT NULL,
    sent_at TEXT,                           -- when the PO email was actually sent
    sent_to_email TEXT,
    notes TEXT,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- authorising staff (signature/name/position)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS po_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    original_name TEXT NOT NULL,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS training_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL UNIQUE REFERENCES course_sessions(id) ON DELETE CASCADE,
    pax_count INTEGER NOT NULL DEFAULT 0,
    lunch_rate REAL NOT NULL DEFAULT 0,
    vegetarian_count INTEGER NOT NULL DEFAULT 0,
    tea_break_rate REAL NOT NULL DEFAULT 0,
    laptop_rental_qty INTEGER NOT NULL DEFAULT 0,
    laptop_rental_rate REAL NOT NULL DEFAULT 0,
    courseware_qty INTEGER NOT NULL DEFAULT 0,
    courseware_rate REAL NOT NULL DEFAULT 0,
    manual_qty INTEGER NOT NULL DEFAULT 0,
    manual_rate REAL NOT NULL DEFAULT 0,
    book_qty INTEGER NOT NULL DEFAULT 0,
    book_rate REAL NOT NULL DEFAULT 0,
    certificate_qty INTEGER NOT NULL DEFAULT 0,
    certificate_type TEXT,
    certificate_rate REAL NOT NULL DEFAULT 0,
    exam_qty INTEGER NOT NULL DEFAULT 0,
    exam_type TEXT,
    exam_rate REAL NOT NULL DEFAULT 0,
    others_fee REAL NOT NULL DEFAULT 0,
    others_remarks TEXT,
    trainer_fee_per_day REAL NOT NULL DEFAULT 0,
    trainer_allowance_per_day REAL NOT NULL DEFAULT 0,
    bus_air_fee REAL NOT NULL DEFAULT 0,
    venue_fee REAL NOT NULL DEFAULT 0,
    hotel_fee REAL NOT NULL DEFAULT 0,
    training_revenue REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL
);

-- Per-user notification inbox — system-generated reminders (quotation
-- follow-up due, invoice overdue, evaluation report overdue, etc.) land here
-- for the relevant staff member rather than only as an email. dedupe_key
-- lets a background check avoid re-notifying the same person about the same
-- thing on every request (e.g. "quotation:42:followup").
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    link TEXT,
    dedupe_key TEXT,
    read_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_dedupe ON notifications(user_id, dedupe_key);

-- Custom, dynamically-added fee line items on a class's Training Costs
-- worksheet (replaced the old single "Others Fee" field so staff can add as
-- many one-off cost lines as a class actually needs).
CREATE TABLE IF NOT EXISTS training_cost_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES course_sessions(id) ON DELETE CASCADE,
    description TEXT,
    amount REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trainer_invoice_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES course_sessions(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    original_name TEXT NOT NULL,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS po_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    quantity REAL NOT NULL DEFAULT 1,
    unit_price REAL NOT NULL DEFAULT 0,
    amount REAL NOT NULL DEFAULT 0
);

-- Quotation number format: #DDMMYYYY + 2-digit revision (e.g. #0806202601).
-- base_date fixes the DDMMYYYY portion; revision increments (01, 02, 03...)
-- each time the quotation is revised, keeping the original date.
CREATE TABLE IF NOT EXISTS quotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_no TEXT NOT NULL UNIQUE,
    base_date TEXT NOT NULL,                -- date the number is derived from (YYYY-MM-DD)
    revision INTEGER NOT NULL DEFAULT 1,
    client_company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    attention_to TEXT,
    company_name_override TEXT,             -- used when not linked to a saved client
    address TEXT,
    tel TEXT,
    quote_date TEXT NOT NULL,
    ref_no TEXT,
    course_title TEXT,                      -- used to build the default document title
    is_hrdcorp INTEGER NOT NULL DEFAULT 0,   -- SBL-Khas naming/title variant
    title_override TEXT,                    -- optional custom document title
    training_mode TEXT,                     -- Physical, Virtual, Hybrid — used in Terms & Conditions
    venue TEXT,
    valid_until TEXT,
    terms TEXT,
    status TEXT NOT NULL DEFAULT 'Draft',    -- Draft, Sent, Follow-up, Accepted, Rejected
    notes TEXT,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    sent_at TEXT,
    sent_to_email TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS quotation_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quotation_id INTEGER NOT NULL REFERENCES quotations(id) ON DELETE CASCADE,
    programme TEXT NOT NULL,
    no_of_pax INTEGER DEFAULT 1,
    training_type TEXT,
    duration TEXT,
    item_date TEXT,
    item_date_end TEXT,  -- optional: set only for items spanning more than one day
    item_time TEXT,  -- e.g. '9:00 AM - 5:00 PM', same format as course_sessions.training_time
    investment_fee REAL NOT NULL DEFAULT 0
);

-- Staff activity trail (admin-only view) — who did what, when.
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,            -- login, logout, create, update, delete, send_email, etc.
    entity_type TEXT,                -- e.g. 'lead', 'purchase_order', 'quotation'
    entity_id INTEGER,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Every outgoing email attempt, success or failure — admin-only view.
CREATE TABLE IF NOT EXISTS mail_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    to_email TEXT NOT NULL,
    subject TEXT,
    status TEXT NOT NULL,            -- sent, failed
    error TEXT,
    related_type TEXT,               -- e.g. 'purchase_order', 'quotation'
    related_id INTEGER,
    sent_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- One row per staff member per connected calendar provider — each user
-- connects their OWN Google or Outlook account (OAuth), independent of
-- what anyone else on the team uses. access_token is short-lived and
-- refreshed on demand via refresh_token; token_expiry is a UTC ISO
-- datetime string. calendar_email is just for display ("Connected as
-- name@company.com") so a user can tell which account is linked.
CREATE TABLE IF NOT EXISTS calendar_connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,          -- 'google' or 'microsoft'
    access_token TEXT,
    refresh_token TEXT,
    token_expiry TEXT,
    calendar_email TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, provider)
);

-- Hotel/venue database — a simple directory of hotels Modoku can book for
-- in-house/public training, with their meeting-package rates and room
-- capacities, kept separate from `companies` (clients) since a hotel is a
-- venue supplier, not a training client.
CREATE TABLE IF NOT EXISTS hotels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    location TEXT,
    contact_name TEXT,
    contact_position TEXT,
    contact_phone TEXT,
    contact_email TEXT,
    rate_full_day REAL,             -- Full-day meeting package rate
    rate_half_day REAL,             -- Half-day meeting package rate
    rate_others_label TEXT,         -- free-text label for a third rate option, e.g. "Overnight package"
    rate_others_amount REAL,
    minimum_pax INTEGER,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A hotel can have several bookable rooms/halls, each with its own pax
-- capacity (e.g. "Ballroom 1 — 200 pax", "Ballroom 2 — 50 pax") — kept as
-- separate rows, entered line by line, rather than one free-text field.
CREATE TABLE IF NOT EXISTS hotel_capacities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hotel_id INTEGER NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    room_name TEXT NOT NULL,
    pax_capacity INTEGER
);

-- Vendor directory — non-training suppliers (photographers, caterers,
-- printers, transport, etc.), separate from Trainers (who deliver training)
-- and Hotels (venues). Freelancer vs Company just for staff's own record.
CREATE TABLE IF NOT EXISTS vendors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    vendor_type TEXT NOT NULL DEFAULT 'Company',   -- 'Company' or 'Freelancer'
    service TEXT,                                  -- what they provide, e.g. "Photography"
    contact_name TEXT,
    contact_phone TEXT,
    contact_email TEXT,
    rating INTEGER,                                -- 0-5 stars, staff's own rating of this vendor
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A vendor can offer several priced services, each optionally billed
-- per-day rather than a flat one-off amount — entered line by line like a
-- hotel's room capacities.
CREATE TABLE IF NOT EXISTS vendor_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id INTEGER NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    service TEXT NOT NULL,
    price REAL,
    per_day INTEGER NOT NULL DEFAULT 0
);

-- Vendor Purchase Orders — same shape/lifecycle as the existing (trainer)
-- purchase_orders table, but for a Vendor instead of a Trainer, and
-- session_id is optional since a vendor PO can stand alone (not tied to
-- any one class), e.g. a bulk printing order. Kept as its own table
-- (rather than reusing purchase_orders with a nullable trainer_id) so the
-- two document types can have their own status history/columns without
-- constantly branching on "is this a trainer or vendor PO" everywhere —
-- but see _next_po_no() in purchase_orders.py: vendor POs draw from the
-- SAME running po_no sequence as trainer POs (by design, per Erik), so the
-- numbering module treats them as one shared counter across both tables.
CREATE TABLE IF NOT EXISTS vendor_purchase_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    po_no TEXT UNIQUE NOT NULL,
    vendor_id INTEGER NOT NULL REFERENCES vendors(id) ON DELETE RESTRICT,
    session_id INTEGER REFERENCES course_sessions(id) ON DELETE SET NULL,
    description TEXT,                    -- what this PO is for (esp. useful when not class-linked)
    fee_amount REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'RM',
    status TEXT NOT NULL DEFAULT 'Draft', -- Draft, Sent, Confirmed, Cancelled
    terms TEXT,
    issue_date TEXT NOT NULL,
    job_end_date TEXT,               -- when the vendor's job finishes; once passed, an invoice-request email fires automatically
    notes TEXT,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at TEXT,
    sent_to_email TEXT,
    confirm_token TEXT,
    confirm_responded_at TEXT,
    vendor_invoice_token TEXT,
    vendor_invoice_email_sent_at TEXT
);

CREATE TABLE IF NOT EXISTS vendor_invoice_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    po_id INTEGER NOT NULL REFERENCES vendor_purchase_orders(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    original_name TEXT NOT NULL,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS vendor_po_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    po_id INTEGER NOT NULL REFERENCES vendor_purchase_orders(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    quantity REAL NOT NULL DEFAULT 1,
    unit_price REAL NOT NULL DEFAULT 0,
    amount REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vendor_po_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    po_id INTEGER NOT NULL REFERENCES vendor_purchase_orders(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,        -- on-disk stored name (uuid-prefixed)
    original_name TEXT,            -- name shown to/downloaded by the user
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Payment Receipt(s) finance uploads once a trainer/vendor's invoice has
-- been processed and paid — separate from the invoice/claim documents the
-- trainer/vendor themselves submitted (po_documents / trainer_invoice_documents
-- and vendor_po_documents / vendor_invoice_documents above).
CREATE TABLE IF NOT EXISTS po_payment_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    original_name TEXT,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS vendor_po_payment_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    po_id INTEGER NOT NULL REFERENCES vendor_purchase_orders(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    original_name TEXT,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Staff expense claims (/claims, /claim) — a staff member submits a claim
-- tied to a class they worked on; finance later approves/pays it and emails
-- the payment receipt back, which flips the claim to Paid.
CREATE TABLE IF NOT EXISTS staff_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,   -- staff member who submitted it
    session_id INTEGER NOT NULL REFERENCES course_sessions(id) ON DELETE CASCADE,
    claimant_name TEXT NOT NULL,
    claimant_email TEXT,
    bank_name TEXT NOT NULL,
    bank_account_no TEXT NOT NULL,
    total_amount REAL NOT NULL DEFAULT 0,
    claim_note TEXT,                         -- claimant's own note on what the claim is for
    status TEXT NOT NULL DEFAULT 'Unpaid',   -- Unpaid, Paid
    approved_amount REAL,                    -- finance-confirmed amount, set when processed
    remark TEXT,                             -- finance's note, e.g. why some items weren't approved
    paid_at TEXT,
    paid_to_email TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS staff_claim_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES staff_claims(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    original_name TEXT,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS staff_claim_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES staff_claims(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    original_name TEXT,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS company_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,             -- stored filename on disk (uuid-prefixed)
    original_name TEXT NOT NULL,        -- original filename, shown to users on download
    description TEXT,
    tag TEXT NOT NULL DEFAULT 'Others', -- SSM | HRDC | LHDN | Kastam | Bank | Others
    pinned INTEGER NOT NULL DEFAULT 0,
    uploaded_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per class holding the last-generated Training Report — a cached
-- rollup of that class's Google Forms evaluation responses (see
-- training_reports.py). Deliberately a cache the "Refresh Report" button
-- rebuilds on demand, not something recomputed on every page view, since
-- building it re-reads every response from Google and re-runs the AI
-- summary. Only ever populated for classes with an auto-generated Form
-- (course_sessions.evaluation_form_id) — that's the only case where
-- Modoku Hub controls a form ID it can call the Forms API against.
CREATE TABLE IF NOT EXISTS training_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL UNIQUE REFERENCES course_sessions(id) ON DELETE CASCADE,
    response_count INTEGER NOT NULL DEFAULT 0,
    numeric_summary_json TEXT,   -- JSON list of {question, kind, ...aggregates} for rating/choice questions
    text_summary_json TEXT,      -- JSON list of {question, answers: [...]} for open-text questions (AI fallback/evidence)
    ai_summary_json TEXT,        -- JSON {overall, by_question: [{question, summary}]} — null if AI unavailable/failed
    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
    generated_by INTEGER REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_hotel_capacities_hotel ON hotel_capacities(hotel_id);
CREATE INDEX IF NOT EXISTS idx_po_payment_receipts_po ON po_payment_receipts(po_id);
CREATE INDEX IF NOT EXISTS idx_vendor_po_payment_receipts_po ON vendor_po_payment_receipts(po_id);
CREATE INDEX IF NOT EXISTS idx_staff_claims_session ON staff_claims(session_id);
CREATE INDEX IF NOT EXISTS idx_staff_claim_files_claim ON staff_claim_files(claim_id);
CREATE INDEX IF NOT EXISTS idx_staff_claim_receipts_claim ON staff_claim_receipts(claim_id);
CREATE INDEX IF NOT EXISTS idx_po_session ON purchase_orders(session_id);
CREATE INDEX IF NOT EXISTS idx_po_trainer ON purchase_orders(trainer_id);
CREATE INDEX IF NOT EXISTS idx_po_items_po ON po_items(po_id);
CREATE INDEX IF NOT EXISTS idx_po_documents_po ON po_documents(po_id);
CREATE INDEX IF NOT EXISTS idx_vendor_rates_vendor ON vendor_rates(vendor_id);
CREATE INDEX IF NOT EXISTS idx_vendor_po_vendor ON vendor_purchase_orders(vendor_id);
CREATE INDEX IF NOT EXISTS idx_vendor_po_session ON vendor_purchase_orders(session_id);
CREATE INDEX IF NOT EXISTS idx_vendor_po_items_po ON vendor_po_items(po_id);
CREATE INDEX IF NOT EXISTS idx_vendor_po_documents_po ON vendor_po_documents(po_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vendor_po_confirm_token ON vendor_purchase_orders(confirm_token);
CREATE INDEX IF NOT EXISTS idx_session_trainers_session ON session_trainers(session_id);
CREATE INDEX IF NOT EXISTS idx_session_trainers_trainer ON session_trainers(trainer_id);
CREATE INDEX IF NOT EXISTS idx_t3_participants_session ON t3_participants(session_id);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_id);
CREATE INDEX IF NOT EXISTS idx_sessions_course ON course_sessions(course_id);
CREATE INDEX IF NOT EXISTS idx_enrollments_session ON enrollments(session_id);
CREATE INDEX IF NOT EXISTS idx_invoices_company ON invoices(company_id);
CREATE INDEX IF NOT EXISTS idx_invoice_items_invoice ON invoice_items(invoice_id);
CREATE INDEX IF NOT EXISTS idx_quotations_company ON quotations(client_company_id);
CREATE INDEX IF NOT EXISTS idx_quotation_items_quotation ON quotation_items(quotation_id);
CREATE INDEX IF NOT EXISTS idx_activity_log_user ON activity_log(user_id);
CREATE INDEX IF NOT EXISTS idx_activity_log_entity ON activity_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_mail_log_related ON mail_log(related_type, related_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_code ON course_sessions(session_code);
CREATE INDEX IF NOT EXISTS idx_attendance_returns_session ON attendance_returns(session_id);
CREATE INDEX IF NOT EXISTS idx_certificates_session ON certificates(session_id);
CREATE INDEX IF NOT EXISTS idx_t3_day_attendance_participant ON t3_day_attendance(participant_id);
CREATE INDEX IF NOT EXISTS idx_company_files_pinned ON company_files(pinned);
CREATE INDEX IF NOT EXISTS idx_training_reports_session ON training_reports(session_id);
"""


def get_db():
    if "db" not in g:
        db_path = current_app.config["DATABASE"]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Columns added to tables after their initial release. CREATE TABLE IF NOT
# EXISTS above won't add these to a database that already exists, so
# _apply_light_migrations adds any that are missing — existing data (and
# whatever the user has already entered) is left untouched.
_COLUMN_MIGRATIONS = [
    ("leads", "role", "TEXT"),
    ("enrollments", "ic_no", "TEXT"),
    ("enrollments", "gender", "TEXT"),
    ("enrollments", "citizenship", "TEXT DEFAULT 'Malaysian'"),
    ("course_sessions", "client_company_id", "INTEGER REFERENCES companies(id) ON DELETE SET NULL"),
    ("course_sessions", "evaluation_report_file", "TEXT"),
    ("course_sessions", "evaluation_sent_at", "TEXT"),
    ("course_sessions", "evaluation_sent_to", "TEXT"),
    ("trainers", "avatar_file", "TEXT"),
    ("users", "avatar_file", "TEXT"),
    ("trainers", "rate_per_day", "REAL DEFAULT 0"),
    ("courses", "outline_file", "TEXT"),
    ("course_sessions", "evaluation_form_link", "TEXT"),
    ("course_sessions", "evaluation_qr_poster_file", "TEXT"),
    ("course_sessions", "training_banner_file", "TEXT"),
    ("course_sessions", "jd14_file", "TEXT"),
    ("course_sessions", "jd14_sent_at", "TEXT"),
    ("course_sessions", "jd14_sent_to", "TEXT"),
    ("course_sessions", "jd14_return_token", "TEXT"),
    ("course_sessions", "jd14_received_at", "TEXT"),
    ("course_sessions", "jd14_received_via", "TEXT"),
    ("purchase_orders", "trainer_responsibilities", "TEXT"),
    ("users", "position", "TEXT"),
    ("users", "signature_file", "TEXT"),
    ("users", "contact_phone", "TEXT"),
    ("leads", "namecard_file", "TEXT"),
    ("leads", "linkedin_url", "TEXT"),
    ("leads", "lost_reason", "TEXT"),
    ("purchase_orders", "created_by", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
    ("invoices", "created_by", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
    ("course_sessions", "session_code", "TEXT"),
    ("quotation_items", "item_date_end", "TEXT"),
    ("users", "failed_login_count", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "locked_until", "TEXT"),
    ("invoices", "employer", "TEXT"),
    ("invoices", "grant_id", "TEXT"),
    ("invoices", "project_title", "TEXT"),
    ("invoice_items", "duration", "TEXT"),
    ("invoice_items", "venue", "TEXT"),
    ("invoice_items", "item_date", "TEXT"),
    ("invoice_items", "item_date_end", "TEXT"),
    ("course_sessions", "client_logo_file", "TEXT"),
    ("quotations", "sst_rate", "REAL NOT NULL DEFAULT 0"),
    ("leads", "proposal_file", "TEXT"),
    ("quotation_items", "item_time", "TEXT"),
    ("quotations", "session_id", "INTEGER REFERENCES course_sessions(id) ON DELETE SET NULL"),
    ("quotations", "return_token", "TEXT"),
    ("quotations", "signed_file", "TEXT"),
    ("quotations", "signed_received_at", "TEXT"),
    ("quotations", "signed_received_via", "TEXT"),
    ("quotations", "t3_link_sent_at", "TEXT"),
    ("course_sessions", "t3_public_token", "TEXT"),
    ("t3_participants", "attended", "INTEGER NOT NULL DEFAULT 0"),
    ("course_sessions", "pic_lead_id", "INTEGER REFERENCES leads(id) ON DELETE SET NULL"),
    ("purchase_orders", "confirm_token", "TEXT"),
    ("purchase_orders", "confirm_responded_at", "TEXT"),
    ("course_sessions", "owner_user_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
    ("course_sessions", "calendar_blocked_at", "TEXT"),
    ("course_sessions", "requires_laptop_rental", "INTEGER NOT NULL DEFAULT 0"),
    ("course_sessions", "laptop_rental_qty", "INTEGER"),
    ("course_sessions", "has_exam", "INTEGER NOT NULL DEFAULT 0"),
    ("course_sessions", "exam_participants", "INTEGER"),
    ("course_sessions", "trainer_invoice_token", "TEXT"),
    ("course_sessions", "trainer_invoice_email_sent_at", "TEXT"),
    ("course_sessions", "room_setup", "TEXT"),
    ("training_costs", "meeting_package_rate", "REAL NOT NULL DEFAULT 0"),
    ("invoices", "sent_at", "TEXT"),
    ("invoices", "sent_to_email", "TEXT"),
    ("courses", "price_inhouse", "REAL NOT NULL DEFAULT 0"),
    ("courses", "price_public", "REAL NOT NULL DEFAULT 0"),
    ("courses", "hrdcorp_programme_no", "TEXT"),
    ("vendor_purchase_orders", "job_end_date", "TEXT"),
    ("vendor_purchase_orders", "vendor_invoice_token", "TEXT"),
    ("vendor_purchase_orders", "vendor_invoice_email_sent_at", "TEXT"),
    ("course_sessions", "grant_quotation_file", "TEXT"),
    ("course_sessions", "grant_docs_token", "TEXT"),
    ("course_sessions", "grant_docs_sent_at", "TEXT"),
    ("course_sessions", "grant_docs_sent_to", "TEXT"),
    ("course_sessions", "hrdcorp_grant_id", "TEXT"),
    ("course_sessions", "hrdcorp_grant_id_updated_at", "TEXT"),
    ("mail_log", "cc_email", "TEXT"),
    ("purchase_orders", "payment_status", "TEXT NOT NULL DEFAULT 'Unpaid'"),
    ("purchase_orders", "payment_receipt_sent_at", "TEXT"),
    ("purchase_orders", "payment_receipt_sent_to", "TEXT"),
    ("vendor_purchase_orders", "payment_status", "TEXT NOT NULL DEFAULT 'Unpaid'"),
    ("vendor_purchase_orders", "payment_receipt_sent_at", "TEXT"),
    ("vendor_purchase_orders", "payment_receipt_sent_to", "TEXT"),
    ("staff_claims", "claimant_email", "TEXT"),
    ("staff_claims", "claim_note", "TEXT"),
    ("attendance_returns", "ai_names_json", "TEXT"),
    ("attendance_returns", "ai_analyzed_at", "TEXT"),
    ("attendance_returns", "ai_detected_title", "TEXT"),
    ("attendance_returns", "ai_detected_date", "TEXT"),
    ("attendance_returns", "training_date", "TEXT"),
    ("attendance_returns", "ai_mismatch", "INTEGER NOT NULL DEFAULT 0"),
    ("attendance_returns", "ai_mismatch_reason", "TEXT"),
    ("attendance_returns", "ai_action", "TEXT"),
    ("trainers", "half_day_rate", "REAL DEFAULT 0"),
    ("trainers", "outstation_rate", "REAL DEFAULT 0"),
    ("courses", "focus", "TEXT"),
    # "This amount is SST included" — when set, the amounts typed on the
    # form (item fees / unit prices) are treated as already SST-inclusive,
    # and the stored subtotal/sst_amount are back-calculated so the grand
    # total matches exactly what was typed, instead of SST being added on top.
    ("quotations", "sst_inclusive", "INTEGER NOT NULL DEFAULT 0"),
    ("invoices", "sst_inclusive", "INTEGER NOT NULL DEFAULT 0"),
    # e-Signature attendance: an opt-in per class (see t3.py/t3_public.py) —
    # on a scheduled training day, participants can sign their own row from
    # their phone instead of (or alongside) the printed sheet. sign_fail_count
    # / sign_locked_until are a lockout against guessing someone else's IC
    # number, mirroring the existing users.locked_until pattern. signature_file
    # / signed_ip on t3_day_attendance keep an auditable record of who signed
    # and from where.
    ("course_sessions", "e_signature_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("t3_participants", "sign_fail_count", "INTEGER NOT NULL DEFAULT 0"),
    ("t3_participants", "sign_locked_until", "TEXT"),
    ("t3_day_attendance", "signature_file", "TEXT"),
    ("t3_day_attendance", "signed_ip", "TEXT"),
    # Auto-generated evaluation Forms (see evaluation_forms.py) — the Drive
    # file ID of the Form Modoku Hub created for this class (a copy of the
    # shared master template), kept separately from evaluation_form_link
    # (the public responderUri already on this table) since the file ID is
    # what later API calls — reading responses back for the rollup — need,
    # while the responderUri is only useful to a human clicking the link.
    ("course_sessions", "evaluation_form_id", "TEXT"),
    ("course_sessions", "evaluation_form_generated_at", "TEXT"),
]


def _apply_light_migrations(db):
    for table, column, coltype in _COLUMN_MIGRATIONS:
        existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    db.commit()

    # Indexes on columns added above via ALTER TABLE — created here (after
    # the column is guaranteed to exist) rather than in SCHEMA, since SCHEMA
    # runs before this migration step and would fail referencing a column
    # that doesn't exist yet on a pre-existing database.
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_quotations_return_token ON quotations(return_token)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_t3_token ON course_sessions(t3_public_token)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_jd14_return_token ON course_sessions(jd14_return_token)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_po_confirm_token ON purchase_orders(confirm_token)")
    db.commit()

    # Lead status labels were renamed ('Qualified' -> 'Had Meeting',
    # 'Closed' -> 'Deal Closed') — update any existing rows still holding the
    # old text so filters/reports keep matching them. Safe to re-run: once
    # converted, no rows match the old values any more.
    db.execute("UPDATE leads SET status = 'Had Meeting' WHERE status = 'Qualified'")
    db.execute("UPDATE leads SET status = 'Deal Closed' WHERE status = 'Closed'")
    db.commit()

    # Training type label capitalization fix ('In-House Training' ->
    # 'In-house Training') — update any existing rows still holding the old
    # text so filters/reports keep matching them. Safe to re-run.
    db.execute("UPDATE course_sessions SET training_type = 'In-house Training' WHERE training_type = 'In-House Training'")
    db.execute("UPDATE quotation_items SET training_type = 'In-house Training' WHERE training_type = 'In-House Training'")
    db.commit()

    # Training Costs' old single "Others Fee" field was replaced by a
    # dynamic list of custom fee line items (training_cost_items) — migrate
    # any existing nonzero others_fee into one item (using its remarks text
    # as the description) and zero it out, so past figures aren't silently
    # dropped from the total costing. Self-guarding/idempotent: once
    # others_fee is zeroed, a re-run finds nothing left to migrate.
    rows_with_others_fee = db.execute(
        "SELECT session_id, others_fee, others_remarks FROM training_costs WHERE others_fee != 0"
    ).fetchall()
    for row in rows_with_others_fee:
        db.execute(
            "INSERT INTO training_cost_items (session_id, description, amount) VALUES (?,?,?)",
            (row["session_id"], row["others_remarks"] or "Other costs", row["others_fee"]),
        )
        db.execute("UPDATE training_costs SET others_fee = 0 WHERE session_id = ?", (row["session_id"],))
    db.commit()

    # Every class needs a session_code (stamped on the printed T3 form, keyed
    # in by trainers on the public Return Attendance Form page) — backfill
    # any class created before this feature existed, or seeded without one.
    missing = db.execute(
        "SELECT id FROM course_sessions WHERE session_code IS NULL OR session_code = ''"
    ).fetchall()
    if missing:
        existing_codes = {row["session_code"] for row in db.execute(
            "SELECT session_code FROM course_sessions WHERE session_code IS NOT NULL"
        ).fetchall()}
        for row in missing:
            code = generate_session_code(existing_codes)
            existing_codes.add(code)
            db.execute("UPDATE course_sessions SET session_code = ? WHERE id = ?", (code, row["id"]))
        db.commit()

    # Course pricing was split into two mandatory fields — In-house and
    # Public (per pax per day) — replacing the old single "price" field.
    # Backfill: an existing course's old price becomes its starting
    # In-house price (the closer real-world match for how "price" was
    # actually used before), leaving Public at 0 for someone to fill in.
    # Guarded on price_inhouse still being 0 so this only ever runs once
    # per course, even though the migration step itself re-runs every boot.
    db.execute(
        "UPDATE courses SET price_inhouse = price WHERE price_inhouse = 0 AND price != 0"
    )
    db.commit()

    # Trainer rate-change history — backfill a baseline entry (old_rate NULL)
    # for any trainer with a nonzero rate_per_day but no history row yet, so
    # existing trainers show their current rate as a starting point instead
    # of an empty history. Guarded on "no history row exists for this
    # trainer" so it only ever runs once per trainer.
    trainers_missing_history = db.execute(
        """SELECT id, rate_per_day, created_at FROM trainers
           WHERE rate_per_day != 0 AND id NOT IN (SELECT DISTINCT trainer_id FROM trainer_rate_history)"""
    ).fetchall()
    for row in trainers_missing_history:
        db.execute(
            "INSERT INTO trainer_rate_history (trainer_id, old_rate, new_rate, changed_at) VALUES (?, NULL, ?, ?)",
            (row["id"], row["rate_per_day"], row["created_at"]),
        )
    db.commit()

    # Erik's own admin account, for his testing — added here (not just in
    # seed.py) so it also appears on an already-seeded database that will
    # never run seed.py again. Gated on "the users table is non-empty" so
    # this never fires on a brand-new database ahead of seed.py's own
    # insert — otherwise seed.py's "already has data, skipping" guard would
    # trip on this one row and it would never seed anything else. Safe to
    # re-run either way: skipped once the email already exists.
    has_any_user = db.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    has_erik = db.execute("SELECT 1 FROM users WHERE email = ?", ("eriktajudin@modoku.tech",)).fetchone()
    if has_any_user and not has_erik:
        from werkzeug.security import generate_password_hash
        db.execute(
            "INSERT INTO users (name, email, password_hash, role) VALUES (?,?,?,?)",
            ("Erik Tajudin", "eriktajudin@modoku.tech", generate_password_hash("admin123"), "admin"),
        )
        db.commit()

    # Currency label switched from the ISO code 'MYR' to the locally-used
    # 'RM' sign everywhere (system defaults, forms, PDFs) — fix up any
    # existing rows still holding the old code so old and new records
    # display consistently. Safe to re-run: nothing left to match once done.
    db.execute("UPDATE purchase_orders SET currency = 'RM' WHERE currency = 'MYR'")
    db.execute("UPDATE vendor_purchase_orders SET currency = 'RM' WHERE currency = 'MYR'")
    db.execute("UPDATE invoices SET currency = 'RM' WHERE currency = 'MYR'")
    db.commit()

    # Data repair: a template bug rendered an unset (NULL) quotation field's
    # value as the literal text "None" whenever that quotation was opened
    # for editing — if the field was left untouched, saving the form wrote
    # that literal text back as the real value (showing as "Company None" /
    # a "None.pdf" attachment). The template is fixed so this can no longer
    # happen going forward; this clears out any quotation that already got
    # corrupted that way before the fix. Safe to re-run: nothing left to
    # match once cleaned up.
    for _col in ("attention_to", "company_name_override", "address", "tel", "ref_no",
                 "course_title", "venue", "title_override", "notes"):
        db.execute(f"UPDATE quotations SET {_col} = NULL WHERE {_col} = 'None'")
    db.commit()


def init_db(app):
    with app.app_context():
        db = get_db()
        db.executescript(SCHEMA)
        db.commit()
        _apply_light_migrations(db)


def query(sql, args=(), one=False):
    db = get_db()
    cur = db.execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    last_id = cur.lastrowid
    cur.close()
    return last_id


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def get_setting(key, default=None):
    row = query("SELECT value FROM settings WHERE key = ?", (key,), one=True)
    return row["value"] if row is not None else default


def set_setting(key, value):
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def register(app):
    app.teardown_appcontext(close_db)
