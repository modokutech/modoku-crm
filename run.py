import os

try:
    # Optional: if python-dotenv is installed and a .env file exists next to
    # this script, load it into the environment before config.py reads it —
    # lets you keep MAIL_* (and other) settings in a local .env file instead
    # of re-exporting them in every terminal session. Falls back silently to
    # plain shell-exported environment variables if python-dotenv isn't
    # installed or there's no .env file (both are totally fine).
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from modoku_crm import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
