import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-this-secret-key-in-production")
    DATABASE = os.environ.get("DATABASE", str(BASE_DIR / "instance" / "modoku_crm.db"))
    COMPANY_NAME = os.environ.get("COMPANY_NAME", "Modoku Tech")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Set SESSION_COOKIE_SECURE=1 once the site is served over HTTPS (any real
    # deployment) so the session cookie is never sent over plain HTTP. Left
    # off by default so local http://localhost testing still works.
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"

    # Where uploaded files (trainer documents, etc.) are stored on disk.
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", str(BASE_DIR / "instance" / "uploads"))
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max upload size

    # Outgoing email (used for sending Purchase Orders to trainers). Works with
    # any SMTP provider — see README "Setting up email" for free options
    # (Brevo's free tier is the easiest fit here) and exact setup steps.
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "587"))
    MAIL_USE_TLS = os.environ.get("MAIL_USE_TLS", "1") == "1"
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")       # SMTP login
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")       # SMTP password / API key
    MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", COMPANY_NAME)
    # The "From" address shown to recipients. Defaults to MAIL_USERNAME, but
    # some providers (Brevo, SendGrid, etc.) use a login that's different from
    # the verified sending address — set this separately if so.
    MAIL_FROM_ADDRESS = os.environ.get("MAIL_FROM_ADDRESS", "")

    # Per-staff Google/Outlook calendar integration (each user connects their
    # own account from their Profile page) — see README "Setting up calendar
    # integration" for how to create these in Google Cloud Console / the
    # Azure Portal, and which redirect URIs to register there.
    GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    MS_OAUTH_CLIENT_ID = os.environ.get("MS_OAUTH_CLIENT_ID", "")
    MS_OAUTH_CLIENT_SECRET = os.environ.get("MS_OAUTH_CLIENT_SECRET", "")
    # Azure AD tenant to authenticate against — 'common' (default) accepts
    # both personal Microsoft accounts and any work/school account.
    MS_OAUTH_TENANT = os.environ.get("MS_OAUTH_TENANT", "common")

    # Optional — powers the AI attendance-matching feature (reads names off
    # a photographed/scanned T3 attendance form, see ai_match.py). Get a key
    # at https://platform.claude.com/ (pay-as-you-go, no separate
    # subscription). Leave unset and that feature simply stays hidden —
    # nothing else in Modoku Hub needs this.
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
