# Modoku Hub

A simple, self-hosted CRM + Training Management System built specifically for Modoku Tech — a Malaysian training
provider. It's designed to feel like a lightweight CMS: forms and tables in a browser,
no command line needed day-to-day, so non-technical staff can use it comfortably.

## What's included

- **Leads & Enquiries** — pipeline (New → Contacted → Qualified → Proposal Sent → Closed/Lost),
  assign to a salesperson, log calls/emails/meetings against each lead.
- **Sales Call List** — a worklist showing who to call next, sorted by follow-up date
  (overdue items float to the top), with a quick "log a call/email" box per lead.
- **Reports** — pipeline by stage, per-salesperson performance (leads, won/lost, calls
  made, emails/proposals sent — filterable by date range), invoice/revenue summary,
  course popularity.
- **Clients** — company records with SST registration no. and LHDN TIN for
  e-Invoice, address, and a rollup of that company's leads, enrollments and invoices.
- **Courses** — searchable catalog (by title, code or category), with HRDF-claimable flag.
- **Training Schedule** — scheduled sessions with venue, trainer, capacity, training time,
  training type (In-House/Public/Workshop/Conference), and training mode
  (Physical/Virtual/Hybrid — a meeting link field appears for Virtual/Hybrid). Sortable
  list view grouped by date, plus a Google-Calendar-style month view. Upload an
  attendance sheet once a session has run.
- **Enrollments** — participants per session, linked to a lead and/or sponsoring
  company, with **HRDCorp claim status** tracking (Not Applicable / Pending / Approved /
  Claimed / Rejected) and claim number.
- **Invoices** — auto-numbered (INV-YYYY-NNNN), line items (can be pulled straight from
  an enrollment), SST rate/amount, buyer SST reg. no. and TIN for e-Invoice, status
  tracking (Draft/Sent/Paid/Overdue/Cancelled), and a print-friendly view for PDF export
  via the browser's Print dialog.
- **Trainers** — contact details plus document uploads (Trainer Profile/CV, TTT
  certificate, Accredited/HRDCorp certificate), each viewable/downloadable from the
  trainer's page.
- **Purchase Orders** — for engaging external/freelance trainers once a client has
  confirmed a class date. Auto-numbered (PO-YYYY-NNNN), linked to a trainer and a class
  session, warns you if that trainer already has a Sent/Confirmed PO overlapping dates
  (so you don't accidentally double-book them), and can **email the PO straight to the
  trainer** from inside Modoku Hub — no need to also send it manually via Gmail.
- **Staff Users** (admin-managed logins, role-based: admin/staff).
- **Dashboard news** — the dashboard's "Corporate Training & HRDCorp News" card pulls the
  5 most recent headlines relevant to corporate training in Malaysia / HRDCorp from
  Google News' public search feed. No API key needed, but it does need outbound
  internet access from wherever the app is running — if that's not available (or the
  fetch fails), the card just shows a quiet "unavailable" note instead of breaking the
  page. Results are cached for an hour.
- **Settings** (admin-only) — toggle the Invoices and Purchase Orders modules on/off. A
  disabled module's sidebar link disappears and its pages redirect back to the dashboard
  for everyone, including admins — turn it back on from Settings to get back in. Handy if
  your team isn't using a module yet.

## Tech stack

Flask + Jinja2 + SQLite, no build step, no JavaScript framework — just server-rendered
HTML with Bootstrap 5 (loaded from CDN) for styling. This keeps the app easy to run
anywhere and easy for a developer to extend later.

## Running it locally

Requires Python 3.9+.

```bash
cd modoku-crm
pip install -r requirements.txt
python seed.py --reset      # creates the database and loads sample data
python run.py                # starts the dev server on http://localhost:5000
```

Sample logins (from the seed data):

| Role  | Email                  | Password  |
|-------|-------------------------|-----------|
| Admin | admin@modoku.tech        | admin123  |
| Staff | aisyah@modoku.tech       | staff123  |
| Staff | weijian@modoku.tech      | staff123  |

**Change these passwords (or create fresh accounts and deactivate these) before using
real company data.** You can manage staff accounts under Staff Users once logged in
as an admin.

To start with an empty database instead of sample data, just run `python run.py`
without running `seed.py` first — the schema is created automatically on first run.

## Going live with real data

For a real deployment, don't use `seed.py` — it loads the sample companies, leads,
courses, and documents above, which you don't want in production. Use
`seed_production.py` instead: it creates the database with exactly one admin account
and no sample data of any kind.

```bash
python seed_production.py --reset
```

This creates a single login: `eriktajudin@modoku.tech` / `admin123`. **Change this
password immediately after your first login** (Staff Users > your account), and add
any other staff accounts you need from there. Like `seed.py --reset`, the `--reset`
flag drops and recreates all tables first, so only run this once against a fresh
database — never against one that already holds real data.

## Setting up PDF generation (Purchase Orders, Quotations, Invoices, T3 forms, Certificates)

Every PDF this app produces — Purchase Order PDFs, Quotation PDFs, Invoice PDFs, the
printable T3 Attendance Form, and e-Certificates — is rendered by a command-line tool
called **wkhtmltopdf**, which is a separate program your operating system needs to have
installed. It isn't a Python package, so `pip install -r requirements.txt` does **not**
install it, and it won't show up in `requirements.txt` either.

If `wkhtmltopdf` isn't installed, you'll typically see one of: a "Couldn't generate the
PDF — is wkhtmltopdf installed on the server?" message, a blank/near-empty PDF, or (in a
couple of older admin-only download routes) a generic server error page — all of which
mean the same thing: install `wkhtmltopdf` and try again.

**To check if it's already installed**, run this in a terminal on the machine running
Modoku Hub:
```bash
wkhtmltopdf --version
```
If that prints a version number, you're set. If it says "command not found," install it:

- **Windows**: download and run the installer from
  https://wkhtmltopdf.org/downloads.html (pick the `.exe` under "Windows"). After
  installing, make sure its `bin` folder (e.g.
  `C:\Program Files\wkhtmltopdf\bin`) is on your **PATH** — the installer usually offers
  to do this for you; if not, add it manually via
  *System Properties → Environment Variables*. Restart your terminal (and the app)
  afterwards so it picks up the updated PATH.
- **macOS**: `brew install --cask wkhtmltopdf` (requires [Homebrew](https://brew.sh)),
  or download the `.pkg` installer from https://wkhtmltopdf.org/downloads.html.
- **Linux (Debian/Ubuntu)**: `sudo apt-get install wkhtmltopdf`
- **Linux (Fedora/RHEL)**: `sudo dnf install wkhtmltopdf`

After installing, restart Modoku Hub (close and re-run `python run.py`, or restart your
gunicorn process) so the new PATH is picked up, then try generating/downloading the PDF
again.

## Setting up email (for sending Purchase Orders)

Until this is configured, the "Send Purchase Order Email" button (and the test-email
button under Settings) will show a message saying email isn't set up yet — everything
else in Modoku Hub works fine without it.

The mailer (`modoku_crm/mailer.py`) speaks plain SMTP, so **any** SMTP provider works —
just set a few environment variables, no code changes needed. A few free options, in
the order I'd try them:

### Option 1 — Brevo (recommended)

Brevo (formerly Sendinblue) gives you 300 emails/day free, forever, with no credit
card, and its SMTP relay works with an SMTP-based mailer exactly like this one. It'll
also send to any recipient (your trainers) from day one, without needing a verified
custom domain first — good enough to get started immediately, though verifying your
own domain later improves deliverability.

1. Sign up free at https://www.brevo.com
2. Go to **Settings → SMTP & API → SMTP** and note your SMTP login and generate an
   "SMTP key" (this is your password — not your Brevo account password).
3. Set these environment variables:
   ```bash
   export MAIL_SERVER="smtp-relay.brevo.com"
   export MAIL_PORT="587"
   export MAIL_USERNAME="your-brevo-login-email@example.com"
   export MAIL_PASSWORD="the-smtp-key-brevo-gave-you"
   export MAIL_FROM_ADDRESS="yourcompany@modoku.tech"   # must be a "Sender" you verify in Brevo
   ```
4. In Brevo, go to **Senders & IP → Senders** and add/verify the address you put in
   `MAIL_FROM_ADDRESS` (a quick email-link confirmation — no domain DNS setup required
   for this step, though Brevo will nudge you to add domain authentication later for
   better inbox placement).
5. Restart the app, then use **Settings → Send Test Email** to confirm it works.

### Option 2 — Gmail

Simplest if you already use Gmail/Google Workspace day-to-day, but Google caps regular
Gmail accounts at ~500 emails/day and personal Gmail can land in spam more easily for
business email than a dedicated provider.

1. Turn on 2-Step Verification on the Gmail account you want to send from:
   myaccount.google.com/security
2. Create an "App Password" at myaccount.google.com/apppasswords — pick any name (e.g.
   "Modoku Hub"), and copy the 16-character password it gives you. This is **not** your
   normal Gmail password.
3. Set these environment variables:
   ```bash
   export MAIL_USERNAME="yourcompany@gmail.com"
   export MAIL_PASSWORD="the16characterapppassword"
   ```
4. Restart the app. Purchase orders will now send from that Gmail address.

### Option 3 — SendGrid or Resend

Both have free tiers (SendGrid: 100/day forever; Resend: 3,000/month) and both offer
an SMTP relay you can point at the same way as Brevo above (SendGrid:
`smtp.sendgrid.net`, username `apikey`, password = your API key). Resend's SMTP relay
needs a verified sending domain before it'll deliver to arbitrary recipients, which is
an extra setup step worth knowing about before you pick it.

### Why not Web3Forms?

Web3Forms (and similar "form backend" services) are built for static contact forms —
a form on your website submits to their endpoint, which forwards it to *your own*
inbox. They're not designed to send to a different recipient per request (i.e. a
different trainer's email for each Purchase Order), which is what this feature needs,
so they don't fit here. The SMTP providers above are the right category of tool for
"send this specific email to this specific external address."

Any other standard SMTP provider (a business email host, Mailgun, Postmark, etc.) works
too — just set `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, and
optionally `MAIL_FROM_ADDRESS` / `MAIL_FROM_NAME` accordingly (see `config.py` for all
the options).

## Setting up calendar integration (Google / Outlook)

Modoku Hub can automatically block time on your team's calendars the moment a class is
confirmed (status becomes Scheduled — whether set directly, or automatically once a
signed quotation comes back). This is **per staff member**: each person connects their
own Google or Outlook account from their **Profile** page (top-right menu). Once
connected, every confirmed class blocks time on that person's calendar too — not just
whoever happens to "own" the class — so anyone on the team who's connected their
calendar stays in the loop, automatically, with no extra setup per class.

Until the environment variables below are set, the Connect buttons on Profile explain
that an admin needs to configure this first — everything else in Modoku Hub works fine
without it. Once they're set, any staff member who clicks Connect on their Profile page
starts getting classes blocked on their calendar from that point on — someone who never
connects simply never gets anything blocked, and nothing else about scheduling a class
changes either way.

### Google Calendar

1. Go to https://console.cloud.google.com/ and create a project (or use an existing one).
2. **APIs & Services → Library** — enable the **Google Calendar API**.
3. **APIs & Services → OAuth consent screen** — set it up (External is fine for a small
   team; add your staff's Google accounts as test users if it stays in "Testing" mode).
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID** — Application
   type **Web application**. Under **Authorized redirect URIs**, add:
   ```
   https://your-domain.example.com/calendar/oauth/google/callback
   ```
   (use your real deployed domain — for local testing, `http://localhost:5000/calendar/oauth/google/callback`).
5. Copy the generated **Client ID** and **Client secret**, then set:
   ```bash
   export GOOGLE_OAUTH_CLIENT_ID="your-client-id.apps.googleusercontent.com"
   export GOOGLE_OAUTH_CLIENT_SECRET="your-client-secret"
   ```
6. Restart the app. Each staff member can now click **Connect** under Google Calendar on
   their own Profile page.

### Outlook Calendar (Microsoft 365 / personal Microsoft accounts)

1. Go to https://portal.azure.com/ → **Azure Active Directory → App registrations → New
   registration**.
2. Name it (e.g. "Modoku Hub"), and under **Supported account types** pick "Accounts in
   any organizational directory and personal Microsoft accounts" (the widest option —
   works whether staff use a work/school account or a personal Outlook.com account).
3. Under **Redirect URI**, choose platform **Web** and add:
   ```
   https://your-domain.example.com/calendar/oauth/microsoft/callback
   ```
4. **Certificates & secrets → New client secret** — copy the secret **value** immediately
   (it's hidden after you leave the page).
5. **API permissions → Add a permission → Microsoft Graph → Delegated permissions** —
   add `Calendars.ReadWrite`, `offline_access`, and `User.Read` (these match the scopes
   the app requests; an admin may need to grant consent depending on your tenant policy).
6. Copy the **Application (client) ID** from the app registration's Overview page, then set:
   ```bash
   export MS_OAUTH_CLIENT_ID="your-application-client-id"
   export MS_OAUTH_CLIENT_SECRET="the-client-secret-value"
   ```
7. Restart the app. Each staff member can now click **Connect** under Outlook Calendar on
   their own Profile page.

## Setting up Evaluation Forms automation (optional)

Modoku Hub can generate a class's post-training evaluation Google Form automatically —
duplicating a shared master template, retitling it with the course/trainer/date, and
saving the live link straight onto the class — instead of someone duplicating, editing,
and publishing a Form by hand in Google Drive every time. Unlike calendar integration,
this is **one shared connection**, not per staff member: whoever clicks "Generate
Evaluation Form" on a class page, the Form is always created under the one Google account
that owns the master template.

This reuses the exact same `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` as
calendar integration above — if that's already set up, you don't need a new Google Cloud
project, just a few additions to it:

1. **APIs & Services → Library** — also enable the **Google Forms API** and **Google
   Drive API** on the same project.
2. **APIs & Services → OAuth consent screen → Data Access** — add these scopes on top of
   whatever calendar integration already requests:
   ```
   https://www.googleapis.com/auth/forms.body
   https://www.googleapis.com/auth/forms.responses.readonly
   https://www.googleapis.com/auth/drive
   ```
   If your Google Workspace lets you set the consent screen's **User Type** to
   **Internal**, do that — it skips Google's verification process entirely, which
   otherwise applies to the sensitive `drive` scope above. If you're stuck with
   **External**, add the connecting account as a **Test user** instead; you'll see an
   "unverified app" warning the first time you connect (click "Advanced → Go to [app
   name] (unsafe)") — harmless for an internal tool like this one.
3. **APIs & Services → Credentials** — open your existing OAuth client (the same one
   `GOOGLE_OAUTH_CLIENT_ID` points at) and add this to **Authorized redirect URIs**:
   ```
   https://your-domain.example.com/evaluation-forms/oauth/google/callback
   ```
4. Restart the app, then as an admin go to **Settings → Evaluation Forms** and:
   - Click **Connect Google Account** and sign in as the account that owns (or will own)
     your master evaluation Form template.
   - Paste that template's Drive file ID under **Master Template** (the long string in
     its edit URL: `docs.google.com/forms/d/THIS_PART/edit`) and save.
5. A **Generate Evaluation Form** button now appears on every class page, next to the
   existing Evaluation Form link field — it fills that field in automatically once
   clicked.

## Setting up AI attendance matching (optional)

When a trainer returns photo(s) of the signed T3 attendance sheet (via their "Return
Attendance Form" link), Modoku Hub first reads the course title and date printed on the
sheet itself and checks them against the class the photo was actually submitted against.
If that doesn't check out — wrong class, or a date that isn't one of this class's
scheduled training days — the photo is **not** auto-marked from at all; it's flagged on
the **AI Match Attendance** page with the reason, and shown as a warning right on the
trainer's own submission page too, so it gets caught immediately rather than silently
mis-marking attendance. A photo that's simply too blurry to read isn't treated as
suspicious by itself — only a positively wrong reading blocks it.

Once a photo checks out, Modoku Hub reads the names off it and, for anyone it can match
confidently against that class's T3 participant list, marks them attended **for that
specific training day** and generates their e-Certificate automatically — no staff review
step. For a class that runs more than one day, each day gets its own sheet and a
participant needs every scheduled day covered before they're certificate-eligible; the
Attendance List shows a running "2/3 Days" count so it's obvious who's still short. A name
it can't confidently match (unclear handwriting, no matching participant on file) is left
alone rather than guessed — it shows up on the **AI Match Attendance** page (also linked
from the class's own page) for a quick manual look, the one thing that still needs a
human. Staff also get an in-app notification and an email summarizing what happened on
every submission, so nothing goes unnoticed even though nothing requires action.

This is entirely optional. Without it configured, the **AI Match Attendance** button
explains it isn't set up yet, and the normal manual workflow (open the photo, tick names
by hand on the T3 Attendance List) keeps working exactly as it always has.

1. Go to https://platform.claude.com/ and create an account (pay-as-you-go billing, no
   separate subscription — new accounts get a small amount of free credit to try it).
2. Create an API key.
3. Set it as an environment variable:
   ```bash
   export ANTHROPIC_API_KEY="sk-ant-your-key-here"
   ```
4. Restart the app. The **AI Match Attendance** button (on a class's page, and on its T3
   Attendance List page) now works for any class with a returned photo.

This uses Claude Haiku by default (fast and inexpensive — reading one photo costs a small
fraction of a cent). Set `ANTHROPIC_MODEL` to override the model if you ever want to.

## Deploying for real use

The app is a standard Flask app, so it runs on any host that supports Python:

1. **Set a real secret key** — set the `SECRET_KEY` environment variable to a long
   random string (used to sign login sessions).
2. **Use a production server** — don't use `python run.py` in production. Use gunicorn
   (included in requirements.txt):
   ```bash
   gunicorn -w 2 -b 0.0.0.0:8000 run:app
   ```
3. **Database location** — by default the SQLite file lives at `instance/modoku_crm.db`.
   Set the `DATABASE` environment variable to point elsewhere (e.g. a persistent disk
   on your host). SQLite comfortably handles a small team's usage; if the company
   grows a lot, this is the piece to swap for Postgres/MySQL later — all database
   access goes through `modoku_crm/db.py`, so that's the only file that would need
   rewriting.
4. **HTTPS** — put the app behind a reverse proxy (e.g. Caddy, Nginx, or your hosting
   provider's built-in HTTPS) since login credentials are sent on every request.
5. **Backups** — since everything lives in one SQLite file, backing up is as simple as
   copying `instance/modoku_crm.db` on a schedule.

Reasonable low-cost hosting options: a small VPS (DigitalOcean, Linode, AWS Lightsail),
or a PaaS that runs Python apps (Render, Railway, PythonAnywhere). Any of these can run
this app for a few dollars a month.

## Project structure

```
modoku-crm/
  run.py                  # entry point
  config.py                # configuration (secret key, DB path, company name)
  seed.py                   # sample data loader
  requirements.txt
  modoku_crm/
    __init__.py             # app factory, blueprint registration
    db.py                    # SQLite schema + query helpers
    auth.py                  # login/logout, @login_required, @admin_required
    dashboard.py, leads.py, companies.py, courses.py, sessions.py,
    enrollments.py, invoices.py, trainers.py, users.py, reports.py
    templates/               # one folder per module, plus base.html/login.html
    static/css/style.css
```

## Notes on the Malaysia-specific fields

- **SST**: Company and invoice records both carry an SST registration number field.
  Invoices have a configurable SST rate (%) applied to the subtotal.
- **e-Invoice (LHDN)**: Company and invoice records carry a TIN (Tax Identification
  Number) field. Modoku Hub stores the data needed for e-Invoice line items but does not
  submit to LHDN's MyInvois system directly — that would need a separate integration
  with LHDN's API using your company's credentials, which is a good next step once
  you're ready to automate submission.
- **HRDF (HRD Corp)**: Courses can be flagged as HRDF-claimable, and each enrollment
  tracks claim status and claim/grant number, so you can see at a glance which claims
  are pending, approved, claimed, or rejected.

## Extending it

Some natural next steps, roughly in order of likely usefulness:
- Email notifications (e.g. reminders for upcoming follow-ups, invoice due dates).
- CSV export for leads/invoices (pandas is already available if you add this).
- A proper "certificate" generator per participant on course completion.
- LHDN MyInvois API integration for real e-Invoice submission.
- Migrating from SQLite to Postgres if the team/data grows significantly.
