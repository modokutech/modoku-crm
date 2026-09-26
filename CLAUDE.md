# Modoku Hub: project rules

## App
- Modoku Hub is a Flask CRM/TMS (package `modoku_crm`) on SQLite, deployed on the owner's VPS.
- Deploy = `git pull` on the VPS + restart the app service. Never tell the owner to copy files onto the VPS by hand.

## Commit messages
- Format: `FixNN: <short change>`, where NN is the next number after the latest FixNN in `git log`.
- No Claude, Co-Authored-By or session lines, ever.

## Database changes
- Add columns via the `_COLUMN_MIGRATIONS` list in `modoku_crm/db.py` so they apply automatically on boot. No manual migration steps.

## PDFs
- wkhtmltopdf 0.12.6 (unpatched Qt) renders 1 CSS px = 0.2032mm. The JD14 layout in `modoku_crm/pdfgen.py` depends on this.
- The trainer profile PDF is drawn with Pillow (`modoku_crm/trainer_profile_pdf.py`).

## Testing
- Test every change before pushing: Flask test client at minimum. For PDFs, render them to images and check them visually.

## Reporting after each fix
- What changed, the commit hash, and "git pull + restart" to deploy.
- If `requirements.txt` changed, say so: run `pip install -r requirements.txt` first.
