# Modoku Hub (CRM & TMS) — Changelog

One entry per numbered fix (FixNN), newest first. The commit for each fix uses the same number in
its message (`FixNN: <short change>`). Earlier history (before Fix58) isn't recorded here.

Notes for anyone (or any Claude session) working on this repo:
- Deploy = `git pull` on the VPS, then restart the app service. If `requirements.txt` changed,
  run `pip install -r requirements.txt` first.
- The VPS renders PDFs with **wkhtmltopdf 0.12.6.1 / Qt 4.8.7**, which scales CSS px differently
  from a typical dev machine (see Fix66d). Check PDF layout changes against that.
- Database changes go in `db.py`'s `_COLUMN_MIGRATIONS` so they apply automatically on boot.

## Fix97 — JD14: signature another 10% larger, company stamp another 5% larger

**Date:** 2026-09-26

- `pdfgen.py` (`_jd14_decl`): the signature area went from 52.9 x 14.4mm to 58.2 x 15.8mm (+10%),
  same centre on the SIGNATURE line. The company stamp area went from 50.6 x 18.7mm to 53.1 x 19.6mm
  (+5%), grown left and down so its right edge stays where it was, clear of the
  "(Managing Director/…)" caption.
- The JD14 page's live preview uses the same HTML (Fix93), so it changes too.

**Testing:** rendered a signed JD14 through the preview route (Flask test client) and directly,
plus a 1.18x zoom render for the server's larger px scale: one page each, identical sizes
(signature 57.7 x 15.6mm, stamp 52.0 x 19.5mm measured on the render), no collision with the
labels, the caption or the DATE row.

## Fix96 — JD14: signature 15% larger, company stamp 10% larger

**Date:** 2026-09-25

- `pdfgen.py` (`_jd14_decl`): the signature area went from 46 x 12.5mm to 52.9 x 14.4mm (+15%),
  still centred on the SIGNATURE line. The company stamp area went from 46 x 17mm to 50.6 x 18.7mm
  (+10%), and moved 1.5mm left so a wide stamp still clears the "(Managing Director/…)" caption.
  Images scale to fit these areas, keeping their proportions.
- The JD14 page's live preview uses the same HTML (Fix93), so it changes too.

**Testing:** rendered normal and long-text samples: both one page, and the signature and stamp
don't collide with neighbouring labels or the caption.

## Fix95 — Full Report page: one back button, back to the Training Report

**Date:** 2026-09-25

- `templates/full_reports/edit.html`: removed the **Back to Class** button from the page header
  and renamed **Training Report Data** to **Back to Training Report Data** (same link, now with a
  back arrow). The Full Report is reached from the Training Report, so that's where "back" goes.
  The Training Report page itself still has Back to Report List and Back to Class (Fix94).

**Testing:** Flask test-client check: the Full Report page shows the renamed button and no longer
shows Back to Class.

## Fix94 — "Back to Report List" button on the Training Report page

**Date:** 2026-09-25

- `templates/training_reports/view.html`: new **Back to Report List** button (linking to
  `training_reports.index`), placed first in the header's button row, before the existing
  **Back to Class**. Before this, the only way back to the list of all Training Reports was the
  sidebar. No other changes.

**Testing:** Flask test-client check: the report page renders 200 with the new button linking to
`/training-report/`, and that list page loads 200.

## Fix93 — JD14 page: live preview now uses the real form layout; clear "already sent" status

**Date:** 2026-09-25

Erik, after deploying Fix91/92: the PDF had the new layout, but the JD14 page's live preview was
still the old one, the email preview box wasn't showing (the old "Send to Client" button was), and
he asked for a clear sign that the JD14 had already been emailed to the PIC, to avoid sending twice.

**1. Missing email box: a deploy issue, not code.** The Fix92 template (`templates/jd14/edit.html`)
contains the email box and "Send to PIC"; Erik's screenshot shows the pre-Fix92 template, so that
file hadn't reached the server (or the app wasn't restarted after it did). It's in this fix's zip
too. Check on the server: `grep -c "Send to PIC" modoku_crm/templates/jd14/edit.html` should print
at least 1.

**2. Live preview = the PDF layout, one source of truth.** Fix91 rebuilt only the PDF and left the
hand-maintained table preview as it was. Rather than rebuild a second copy of the layout, the preview
is now the same HTML the PDF is made from:
- New route `POST /jd14/sessions/<id>/live-preview` (login + CSRF) returns
  `pdfgen._build_jd14_html(...)` using the values currently typed on the page, including unsaved
  ones.
- The page shows it in a frame scaled to the column width, and re-renders about 0.4s after you stop
  typing. If a render fails, it says so and points to PDF Preview.
- `_build_jd14_html(..., for_browser=True)` loads the two fonts from `/static/fonts/` so the
  browser caches them, instead of re-sending about 300KB of base64 on every refresh. The PDF still
  embeds them.
- Before anyone signs, the preview shows the logged-in user's signature, name and stamp, as before.
- The old preview markup, its CSS and its mirroring JS are removed, so there's nothing left to keep
  in sync.

**3. "Already sent" is obvious, and double sending is blocked:**
- A blue banner at the top: "Already emailed to <address> on <date, time>".
- In the email box: a green "Sent" badge. The send form is collapsed behind "Need to send it
  again?"; opening it shows an orange **Resend to PIC** button.
- The confirm dialog on a resend repeats when it was already sent and to whom.
- The button locks ("Sending…") after the first click, so a double-click can't send twice.
- Server-side guard: once `sent_at` is set, `send()` refuses unless the request carries
  `confirm_resend=1`, which only the "send it again" box includes. A re-submitted form or stray
  click can't resend.

**Testing:**
- Test client (mailer mocked): the edit page uses the frame preview and the old markup is gone;
  live-preview returns the real layout with unsaved values and static font URLs, and is still
  CSRF-protected. After signing (not sent) the email box is open with no banner. After sending
  there's the banner, the Sent badge and the collapsed resend box. A second send without the
  confirm flag is refused, and a deliberate resend works.
- Playwright on a live server: the frame renders the form in the embedded narrow font, typing a
  course title shows up in the preview, and there are no JS errors.

## Fix92 — JD14: send to the class's PIC, with an email preview box

**Date:** 2026-09-25

Erik: the JD14 "Send to Client" button shouldn't email the client company's email but the class's
PIC, and the page should show a preview of the email (who it goes to, subject, content) before
sending.

- **Recipient is now the PIC.** The default is the email of the class's PIC (`course_sessions.pic_lead_id`
  → `leads.email`), no longer `companies.email`. The company email is never used as a fallback: if
  the class has no PIC (or the PIC has no email) and nothing is typed in, sending is refused with a
  message pointing to the class's Edit page.
- **Email preview box**, shown once the form is signed (admins only, as before), replacing the
  bare button:
  - **To:** prefilled with the PIC's email and labelled with the PIC's name.
  - **CC:** optional.
  - **Subject:** "JD14 Form - <course>".
  - **Message:** now greets the PIC by name and includes the upload link for returning the signed copy.
  - **Attachment:** "JD14_Form.pdf", with a link to open it.
  - All fields are editable, and what's in the boxes is exactly what gets sent. If there's no PIC,
    a red note explains how to fix it.
- The button is now **"Send to PIC"**, with a confirm step that shows the address. After sending,
  the box shows "Sent to … on …" and the button becomes **"Resend to PIC"**.
- Unchanged: the status tracker (`sent_at`/`sent_to` still stamped on send) and the return-upload
  flow. Added an activity-log entry for each send.

**Testing:** Flask test-client with the mailer mocked, a seeded class with a PIC and another with
none. Checked:
- No box before signing, and after signing it's prefilled with the PIC's email, greeting and subject.
- Edited To/CC/subject/body are sent exactly, with the PDF attached, and empty fields fall back to
  the PIC and the defaults.
- The sent status and Resend button appear after sending.
- With no PIC, the warning shows and sending is refused, with nothing going to the company email.

## Fix91 — JD14 PDF redrawn to match the official HRDCorp form

**Date:** 2026-09-25

Erik sent the generated JD14 next to the official PSMB/SBL-KHAS/JD/14 form and asked for the PDF to
match the official one as closely as possible. PDF layout only; no data, flow or wording changes.

**Why it looked different:** the official form is set in **Arial Narrow**. Our HTML asked for
Arial, which the server doesn't have, so wkhtmltopdf fell back to a much wider face. That made
everything look bigger and re-flowed every paragraph, and the flow layout (tables and margins)
only roughly followed the form's proportions.

**What changed (`pdfgen.py`, JD14 builder only):**
- **Typeface:** Liberation Sans Narrow (metrically identical to Arial Narrow), embedded as base64
  in the HTML so the output doesn't depend on server fonts. New files:
  `static/fonts/LiberationSansNarrow-{Regular,Bold}.ttf`, from Liberation Fonts 1.07 (GPLv2 +
  font exception, fine to embed).
- **Measured, absolute layout:** every box, label, underline and paragraph is placed at the
  position measured off the official form (rendered 1200px wide = 5.714px/mm). Type sizes were
  also derived by measurement: 11pt labels and headings, 10pt intro and Part 3 labels/captions,
  11pt declarations, about 9pt reminder. The layout is built at 1 CSS px = 0.2032mm
  (as measured on Claude's test build, wkhtmltopdf 0.12.6 / Qt 5). **Correction (2026-09-26):**
  the VPS runs a different build (0.12.6.1 / Qt 4.8.7, see Fix66d) that maps px about 18% larger.
  The JD14 still comes out right there because its page is built exactly as wide as the printable
  area, so wkhtmltopdf scales it to fit the page; Erik confirmed the production PDF looks correct.
  Keep the JD14 page width equal to the printable width, or this breaks on the VPS.
  `_JD14_PX_PER_MM` / `_jd14_x` / `_jd14_y` / `_jd14_pt` do the maths, and PDF margins are now
  4mm/4.5mm to match.
- The official form's quirks are reproduced: the intro's first line starts further left than the
  rest; the right-hand Part 1 underlines run to the box edge; (b) is indented further than (a); (b)'s
  DATE has no underline (it sits on the box edge); the captions are regular type, not italic.
- **Our filled-in values:** each sits on its underline and shrinks to fit when long (course
  title, venue, fees). The employer name and address shrink to fit the space under their label.
  Checked with a deliberately extreme long-text case, still one page.
- **Signature:** centred on the SIGNATURE line and allowed to cross it, like a hand signature,
  in the blank band above NAME.
- **Company stamp:** in the empty area under the COMPANY STAMP label, left of the "(Managing
  Director/…)" caption, so the stamp and caption don't overlap.
- The on-screen live preview on the JD14 page is unchanged. It's still an approximate table
  layout; the PDF Preview button shows the exact output. The code comment says so.

**Testing:** overlaid our render on Erik's official form at the same scale. Boxes, lines and
labels line up to within about 1mm, and both declarations wrap on the same words as the official
form. One-page check for normal, unsigned and extreme long-text data. Flask smoke test of the
JD14 routes (edit, save, preview.pdf, download, sign, download after signing): all 200 with valid
1-page PDFs.

## Fix90 — Full Report PDF: new "Key Findings" section

**Date:** 2026-09-25

Erik wanted the Training Report page's AI summaries (the Ratings narrative, the Open-Text Feedback
overall and the per-question summaries) in the client PDF too, without anything repeated and short
enough that a client manager will actually read it.

**Where it goes:** a new **Key Findings** section right under the "Training Performance Details"
table, before the charts. It's not in the Conclusion: that's a warm closing paragraph at the very
end, and burying findings there means nobody sees them.

**What it is:** 4 to 6 one-sentence bullets (25 words max each), each with a bold label: Overall,
Strengths, Areas to improve, Recommendation and so on. Written by AI from the exact numbers
(combined rating, per-criterion averages) plus the Training Report page's existing summaries, but
condensed:
- The source summaries repeat each other heavily (e.g. "93% Good or Excellent" appears in both the
  Ratings and Open-Text summaries). The prompt makes every fact appear once and bans repeating a
  number or criterion.
- It doesn't restate participant/response counts, which are already in the table right above it.
- It keeps concrete, actionable points for the client (e.g. a Copilot licence gap, requests for a
  longer programme, departments that would benefit), phrased constructively, and never names
  individual participants.
- Numbers only come from the computed data. The AI may not invent or recompute them.

**Not added:** the full per-question paragraphs and the long Ratings/Open-Text narratives. They'd
double the length and mostly repeat the charts, the verbatim feedback tables and Key Findings.

**Conclusion made non-overlapping:** its prompt now says the Key Findings section already carries
the exact scores and concerns, so it must not repeat any number, percentage or criterion name. It
refers to results only in general terms. This affects newly generated/rewritten Conclusions.

**Editor (Full Report page):** a fourth box, Key Findings, between Objective and Conclusion, with
its own "Rewrite with AI". One point per line, and "Label: text" shows the label in bold. Emptying
the box leaves the section out of the PDF. The same edit-protection rules as the other sections
apply (Generate never overwrites saved text; Save/Preview/Approve save what's on screen).
- Existing drafts: the box starts empty. Click Generate Full Report again (fills only empty
  sections) or Rewrite with AI on the box.
- Class with no evaluation data yet: Key Findings stays empty without an error. Rewrite explains
  there's nothing to summarize.

**Data:** new `full_training_reports.key_findings_text` column, added automatically on next boot
(light migration).

**Testing:** 9-step Flask test-client suite with Claude mocked, seeded with the numbers and
summaries from Erik's screenshots. Checked: migration; Generate fills Key Findings and the prompt
carries the right data and no-repeat rules; the Conclusion prompt forbids repeating numbers; Save,
Generate-doesn't-overwrite, Rewrite; the PDF preview renders with the section in the right place;
an empty box omits it; a no-feedback class raises no error; the locked (sent) view shows it.
Checked the rendered PDF page visually.

## Fix89 — Trainer Profile PDF: custom sections without bullet points

**Date:** 2026-09-24

- Custom sections (e.g. "Signature Programmes") no longer show bullet markers. Each line typed
  in the section's box (each Enter) is its own paragraph, set like Background/Experience:
  #595959 Morion Regular 11pt, justified with the last line left-aligned, and hyphenation.
  There's a small 1.6mm gap between paragraphs rather than a full blank line, so a list of
  short programme names still reads as a tight list. A long line wraps as a proper paragraph.
- Form copy updated: the section hint, textarea placeholders and the word-count guide no longer
  talk about bullets. Stored data is unchanged, so existing sections render the new way the next
  time they're saved. The save step still strips a leading •/· if someone pastes one in.

**Testing:** 13-step module suite passed. Rendered a sample with a custom section mixing two
short lines and one long paragraph and checked it visually.

## Fix88 — Trainer Profile PDF: meta lines #a6a4a4, colour-accurate PDF output

**Date:** 2026-09-24

- The "Institution | Year | Location" and "Company | Period" lines changed from #a9a9a9 to
  **#a6a4a4**. Those were the only #a9a9a9 text in the app.
- **Colour accuracy fix found while checking this:** Pillow stores each PDF page as a JPEG, and
  its defaults (quality 75, 4:2:0 chroma subsampling) lost the slight warm tint on thin text.
  #a6a4a4 came out as neutral #a3a3a3, so specified hex colours weren't reproduced exactly.
  Pages are now saved at quality 95 with no chroma subsampling. Measured in the output PDF, the
  text now comes out as (166, 164, 165) and is a little sharper. Trade-off: files are roughly
  2–3× larger (a 2-page sample went from about 290KB to about 630KB with a plain photo, and about
  1.1MB with a gradient photo).

**Testing:** 13-step module suite passed. Pixel-checked the raw page (exact #a6a4a4) and the
decoded PDF image at three compression settings; only quality 95 without subsampling kept the
tint. Checked the rendered sample visually.

## Fix87 — Trainer Profile PDF: grey meta lines, logo and triangle lowered

**Date:** 2026-09-24

- The "Institution | Year | Location" and "Company | Period" lines are now **#a9a9a9**. They were
  R 34% G 50% B 78% (RGB 87, 128, 199), the last place that blue was used.
- **Sidebar:** Fix86 left the logo too high. The logo and yellow triangle both moved down 8%
  together (their positions × 1.08). Net result against the original layout: triangle top at
  about 186mm (was 202mm), logo top at about 273mm (was 281mm), leaving about 11mm below the logo.

**Testing:** 13-step module suite passed. Checked the rendered sample page as an image.

## Fix86 — Trainer Profile PDF: sidebar spacing, grey body text, role-first experience, proper justify

**Date:** 2026-09-24

Erik's review of Fix85 (with a screenshot of a real profile).

- **Sidebar:** yellow triangle raised 15% and the modoku logo 10% (vertical positions × 0.85 /
  × 0.90). The logo sat 3mm from the bottom edge. Both are one-line constants
  (`_TRIANGLE_RAISE`, `_LOGO_TOP_MM`) if the amount needs changing. The Areas of Expertise /
  Companies Trained list limit moved up with the triangle.
- **Body text colour is now #595959.** The blue from Fix85 (RGB 87, 128, 199) stays only on the
  "Institution | Year | Location" and "Company | Period" lines. The credentials line is #595959 too.
- **Professional Experience:** Role is the main line, with "Company | Period" underneath and no
  brackets around the period. The form's input format (`Company | Period | Role`) is unchanged.
- **Justify fixed.** Fix85 fell back to left-aligned when a line's word gaps got wide, which
  left ragged short lines mid-paragraph (Erik's screenshot). Now every line except a paragraph's
  last one is fully justified. To keep the gaps tight, the word that doesn't fit is hyphenated,
  as Adobe does with justify on: new dependency **`pyphen`** (added to `requirements.txt`).
  Only lowercase words of 7+ letters get hyphenated (never names, acronyms or numbers), 3+
  letters on each side, and at most 2 hyphenated line-ends in a row. Without pyphen installed it
  still justifies, just with looser lines. (A no-hyphenation "balanced breaks" approach was
  tried first and rejected: it spread wide gaps across every line.)
- **Enter = new paragraph** in Background and Experience. Each line break in the form becomes
  its own paragraph in the PDF. Blank lines are ignored, so one Enter or two looks the same.
  The form's hint text is updated to match.

**Testing:** 13-step module suite passed. Rendered a sample with a long real-world Background
(typed with a line break) and checked it as an image: tight justified lines with hyphenation,
the second paragraph split correctly, grey body text with blue meta lines, role-first
Professional Experience, and the raised triangle and logo. Checked the no-pyphen fallback.

## Fix85 — Trainer Profile PDF: typography, colour and photo-ring amendments

**Date:** 2026-09-24

Erik's review of the Fix84 sample. All changes in `trainer_profile_pdf.py`, plus one new font file.

- **Photo ring:** back to the reference design — one gold ring, same size as the photo, offset
  down-right and drawn *behind* the photo so only its lower-right arc shows. Replaces Fix84's
  ring around the photo's edge. (Note: the earlier test photos were cropped from Erik's
  reference PDF and already had that ring baked in, which is where the "two rings" in the
  Fix83 sample came from. Visual checks now use a clean synthetic photo.)
- **Line heights:** cover-page "Trainer / Profile" title −30% (1.05 → 0.735); trainer name
  −20% (0.9 → 0.72), with the credentials line's spacing below the name kept as it was so it
  doesn't butt against the name.
- **No Morion Bold in body content:** entry titles (qualification names, company/period lines)
  are now Morion Regular. Section headings stay Morion Bold.
- **Body line height +10%** across every right-column body block (paragraphs, entries,
  certifications, custom-section bullets).
- **Background and Experience are justified** (last line of each paragraph left-aligned; a
  line that would need gaps wider than 4 spaces falls back to left-aligned).
- **Body font size stays 11pt:** measured against Erik's reference PDF, where the same Morion
  line sets at exactly the width 11pt produces. So it already matches his Adobe 11pt.
- **One body colour, R 34% G 50% B 78% = RGB (87, 128, 199):** all body text, including the
  content under Academic Qualifications and Professional Experience (titles and details) and the
  credentials line. Before this it was a mix of navy, dark grey and black. Section headings stay
  navy so they stand apart.
- **Sidebar pill labels** ("AREAS OF EXPERTISE" / "COMPANIES TRAINED") are one weight lighter:
  Poppins Bold 700 → SemiBold 600. New `static/fonts/Poppins-SemiBold.ttf` (Latin subset,
  from the OFL Fontsource build; the labels are fixed English text).
- Also fixed: the Companies Trained page-splitter measured lines with Poppins while the list
  draws in Morion (since Fix84), so it now measures with Morion too.

**Testing:** 13-step module suite re-run, all passed. Checked the rendered cover and content
pages as images: ring position, name/credentials spacing, justified paragraphs, body colour,
SemiBold pills.

## Fix84 — Trainer Profile generator moved into its own standalone module; visual polish

**Date:** 2026-09-24

Two rounds of feedback on Fix83 (same day), both addressed here.

**1. "Generate Trainer Profile" is now its own module, not a card on a trainer's page**

Erik: "the trainer profile no need to sit inside Trainer's view. make a separate stand-alone
module name 'Generate Trainer Profile'. because this one is just to create/generate trainer
profiles in case our designer is on leave. also after done generating the trainer profile
then can choose to link to which trainer (existing list)."

- New **Generate Trainer Profile** sidebar module (own list page, own "+ New Trainer Profile"
  page) — the "Trainer Profile" card added to a trainer's own page in Fix83 is gone; the
  trainer's Documents list now just links over to this module instead ("Build/Edit one via
  Generate Trainer Profile").
- A profile is now its own record with its own name and its own photo upload — **not** tied to
  a Trainer row at all until someone deliberately links it. That's the actual point of the
  request: it's meant as an in-house fallback for producing a finished profile document even
  when whoever normally designs these by hand is unavailable, so it has to work standalone,
  for anyone, whether or not they're already a Trainer record.
- **Link to Trainer**, on the profile's own edit page once it's been generated at least once: a
  plain dropdown of existing trainers. Linking copies the generated PDF straight into that
  trainer's own `profile_file` slot (the same slot a manual upload always used) and keeps it in
  sync there on every later regeneration for as long as the link stays in place; unlinking just
  stops that sync; the trainer's already-copied PDF is left in place either way. Deleting a
  profile record does the same — the trainer keeps whatever copy was last pushed to them.
- New-profile page accepts an optional `?trainer_id=` (used by the trainer page's "Build one"
  link) that just prefills the Display Name field from that trainer's name — a convenience,
  not a link; nothing is connected until Link to Trainer is used explicitly.
- **Data model:** `trainer_profiles` went from one row per trainer (`trainer_id` as its own
  primary key) to its own `id`-keyed table with an optional `trainer_id`; every child table
  (expertise/companies/academic/certifications/experience/sections) now keys off the new
  `profile_id` instead. Fix83 never actually went live before this correction landed, so
  there's no real profile data to carry across — a new migration step detects the old shape
  (no `display_name` column) and rebuilds those tables fresh and empty; a database that never
  had Fix83's tables at all just gets the new shape directly, no manual step either way.

**2. PDF layout polish, from a look at the first generated sample**

- **Photo no longer cropped** — was a center-square crop that could cut off the top of a head
  or the sides of a wider shot; now the whole photo is scaled down to fit inside the circle
  (preserving its aspect ratio) and centered, with any leftover gap inside the circle filled
  with the sidebar's own navy so it reads as the photo floating on the sidebar rather than
  sitting in a visible box.
- **One ring around the photo, not two** — the orange ring is now drawn right at the photo
  circle's own edge instead of offset outward with a gap, so it reads as a single clean
  boundary.
- **Trainer name: 25% bigger, tighter line spacing** — 27pt → 33.75pt, and the space between
  the two name lines brought in (1.05 → 0.9 leading) — it was sitting noticeably far apart at
  the larger size.
- **Credentials line (e.g. "PhD, MAITD") switched from Poppins to Morion**, matching the rest
  of the body content.
- **Areas of Expertise / Companies Trained list content switched from Poppins to Morion** —
  their pill titles ("AREAS OF EXPERTISE" / "COMPANIES TRAINED") stay Poppins Bold as before,
  only the list items underneath changed font. Caught the same bullet-glyph issue Fix83 already
  found and fixed once (Morion has no middle-dot glyph, U+00B7) in this second spot too — the
  sidebar list's bullet character was switched to U+2022 alongside the font change, so it
  doesn't silently go blank the way the first bug did before it was caught.

**Testing:** rebuilt the Flask test-client suite end-to-end against the new standalone routes —
create a profile with a photo (no trainer link), download its PDF, serve its photo, link it to
a trainer (confirms `trainers.profile_file` gets the copy), regenerate while linked (confirms
the trainer's copy stays in sync), unlink, prefill-from-`?trainer_id=`, the missing-Background
warning, delete (confirms cascade to every child table, and confirms the trainer's own copy is
untouched by the profile's deletion) — 13/13 passed. Confirmed the trainer's own page no longer
renders the removed card and instead shows the module hint link. Ran the app against this
session's own dev database, which still had Fix83's old-shape tables with real test rows in it
— confirmed the new migration step detects and rebuilds them cleanly before the schema script
runs (moving the detection earlier than a first attempt, which failed: the schema script's own
index-creation statements referencing the new `profile_id` column would otherwise fail against
the still-old-shaped child tables before the migration ever got a chance to run). Visually
verified the layout changes by generating sample PDFs with a non-square (portrait) test photo
and rendering pages to images at 150dpi: the photo now shows uncropped and centered with a
single ring, the name is visibly larger with tighter spacing, and both the credentials line and
the sidebar list content (checked on both an Areas of Expertise page and a Companies Trained
page) render in Morion with working bullet points. `py_compile` clean across every touched file.
## Fix83 — New module: Trainer Profile generator

**Date:** 2026-09-24

Erik asked for a way to generate the branded, multi-page "Trainer Profile" brochure (the
document that goes in `trainers.profile_file`) from structured data inside Modoku Hub, instead
of building it by hand in an external tool and uploading the finished PDF — following the exact
layout of two reference PDFs he supplied, with two attached fonts (Morion, for section headings
and body text; Poppins for the trainer's name and the sidebar).

**How it works**
- New **Trainer Profile** card on a trainer's own page, next to Documents — "Build" if nothing's
  been generated yet, "Edit" once something has. Opens a structured editor: Background and
  Experience (free text), Academic Qualifications, Professional Certifications, Professional
  Experience, Areas of Expertise, Companies Trained, and any number of custom sections (a title
  plus its own bullet list — e.g. "Development Training Program", "Corporate Training Program").
  Most repeatable lists are one-entry-per-line textareas (paste straight from an existing CV) —
  multi-part entries use a simple `Field | Field | Field` format, explained inline on the page.
- **Save both persists the data and immediately rebuilds the PDF**, writing it straight into
  `trainers.profile_file` — the exact same slot a manual upload used before, so nothing else in
  the app needs to change: a class's HRDCorp Grant Documents pack and its "Trainer Profile" link
  already read from that slot and immediately pick up the generated version.
- The trainer's **photo is their existing display picture** (Trainer → Edit → Display picture) —
  no second upload for the profile.
- A short **word-count guide** sits at the top of the editor (not enforced, just guidance) for
  how much to write per section so a profile fills the layout cleanly without looking empty or
  overflowing — happy to adjust the numbers if real profiles come out too short/long in practice.
- **Naming calls, per Erik's question ("which suits better?")**: the certificates section is
  labelled **Professional Certifications** (reads more complete on a formal document), and the
  job-history table is **Professional Experience**, kept distinct from the plain **Experience**
  narrative paragraph above it (both were mandatory per Erik's brief; a trainer profile with no
  narrative or no job-history entries just omits that section rather than showing an empty
  heading).
- Left sidebar: **Areas of Expertise** on the first page, **Companies Trained** from the second
  page on — matching Erik's spec. A short companies list simply repeats on every later page; a
  long one continues (splits) across as many pages as it needs rather than repeating a huge list
  wholesale, and a trainer with no companies list at all just keeps showing Areas of Expertise on
  every page instead of an empty box.

**Why Pillow, not the HTML/wkhtmltopdf path most of this app's documents use**
- This layout's left sidebar has to repaint full page height on every page while the right
  column's text flows and paginates on its own — a real difference from every other document
  generator in this app, all of which are one page or a simple repeating table.
- Spike-tested directly in this environment (wkhtmltopdf 0.12.6): a `position: fixed; height:
  100%` sidebar paints correctly on a page whose content fills it, but gets clipped short on a
  page that doesn't — exactly what a short trailing certificate list produces. Rather than fight
  that, this module (like `certificates.py`/`poster.py` before it) draws each page itself with
  Pillow and runs its own simple text-flow paginator over the right column, with the sidebar
  redrawn at a size it controls exactly on every page.
- Colours, the sidebar's width (72.5mm of the 210mm page), and its decorative triangles/icon were
  measured directly off Erik's two reference PDFs (rendered at 300dpi, pixel-sampled) rather than
  eyeballed — including confirming the navy/gold are an almost exact match for this app's
  existing certificate brand colours, which this module reuses. The one thing not reverse-
  engineered pixel-for-pixel is a very faint full-bleed diagonal watermark visible behind some of
  the reference's body text — a low-emphasis flourish left out rather than chased exactly;
  everything structural (sidebar ratio, triangle vertices, photo/pill/logo position, section
  order, both fonts) is matched from real measurements.

**Testing:** Flask test-client end-to-end against the real routes — profile data saved and PDF
regenerated on every save; a trainer with no Background yet is refused with a clear flash
instead of generating a broken PDF; a trainer with only a Background and nothing else still
generates a clean, minimal document rather than showing empty section headings. Verified visually
by rendering the generated PDFs to images at high resolution and inspecting them page by page: a
profile built from content modeled on Erik's own reference PDFs (name, Background, Experience,
4 Academic Qualifications, 18 Professional Certifications, 3 Professional Experience entries, a
custom section, 10 Areas of Expertise, 21 Companies Trained) came out correctly across 4 pages —
including the circular photo composited and cropped correctly once a display picture is on file,
the Companies Trained list correctly splitting 16/5 across two sidebar pages, and every section
appearing in the right place with the right font. One real bug caught this way and fixed before
delivery: the Morion font has no middle-dot glyph, so a first pass at custom-section bullets
silently rendered with no bullet marker at all — switched to the bullet glyph Morion does have.
`py_compile` clean across the whole package; the app boots cleanly with the new blueprint
registered and the new `trainer_profile_*` tables created automatically on next boot (`CREATE
TABLE IF NOT EXISTS`, same as every other table in this app — no manual migration step needed).
## Fix82 — Trainer invoice notification links to the PO; Training Report auto-shows on first visit

**Date:** 2026-09-24

Two amendments Erik asked for:

**1. "Invoice documents submitted" notification/email pointed at the wrong place**
(`trainer_invoice.py`)
- Erik: "the notification is fine but the link is wrong, pls link to the respective PO's view... right now it goes to the current class." Correct — the office email and the in-app notification both linked to the class page, when staff actually go to review a submitted invoice on the trainer's Purchase Order page (that's where these documents are displayed — see `purchase_orders.view`'s `trainer_invoice_documents` query).
- The upload link itself is shared by every trainer on a class (there's no `trainer_id` on `trainer_invoice_documents`, only `session_id`), so there isn't always one single "the" PO — the fix looks up any PO tied to that class and links there. Even with more than one trainer/PO on the same class, every one of their PO pages shows the exact same shared document list (it's a session-scoped query, not trainer-scoped), so picking whichever PO comes first is safe and correct either way. Falls back to the class page only for a class that has no PO on file yet at all.
- Nothing else about the upload flow, the email content, or the sanity-check warnings changed.

**2. Training Report didn't show up automatically after the evaluation form was filled in**
(`training_reports.py`)
- Erik: "why my evaluation form... didnt do automatically when the form is filled up and it wont appear. i have to paste into Existing Form's file ID and link it then only it appeared."
- The Training Report has always been a deliberate cache, not something that rebuilds itself live — by design, it shows whatever was last generated, with a "Generate/Refresh Report" button to pull in the Google Form's responses on demand (this is documented on the page itself: "No report generated yet. Click Generate Report above..."). For a class whose report had never been generated even once, that meant landing on an empty page until someone clicked the button — which reads exactly like "it wont appear" from the outside.
- Fixed the gap without changing the deliberate design: the very first visit to a class's Training Report now auto-builds it once, if a Form is linked and no report exists yet. Every visit after that still behaves exactly as before — manual Refresh only, nothing rebuilt behind your back. If the auto-build itself fails (e.g. the Google connection needs reconnecting), it fails gracefully with a flash message instead of a crash, same empty state as before.
- Likely also relevant to what actually happened on the class you had to manually re-link: a Form that gets accidentally regenerated (a duplicate click, or a genuine "Regenerate" before Fix80 added its confirmation guard) silently creates a brand-new, empty Form and orphans the real one that already had responses on it — exactly what Fix80's confirmation step now prevents going forward, and exactly what "Link Existing Form by file ID" is designed to recover from when it's already happened. No other bug was found in the generate → fill-in → report pipeline beyond the auto-build gap above.

**Testing:** DB/app-context tests with the Google Forms API mocked (`get_valid_access_token`, `get_form_structure`, `list_form_responses`) and a separate logged-out test client for the public trainer-invoice link — (1) first visit to a class's Training Report with a linked Form and no report yet auto-builds and renders it, with exactly one Forms API call; (2) a second visit serves the cached report with no extra API call; (3) a failed auto-build (no valid Google token) flashes cleanly and still renders the normal empty-state page; (4) a trainer invoice submission on a class with no PO yet still notifies with a link to the class page; (5) the same submission on a class that has a PO links straight to that PO's view page. All 5 tests passed. `py_compile` clean on both files.
## Fix81 — Training Report: attended-only participant list; drop the redundant "Full Name" field

**Date:** 2026-09-21

Two amendments Erik asked for on the Training Report / Full Report module:

**1. Participant list now only includes people who actually attended**
(`full_reports.py`)
- `_participant_names()` — which feeds both the "Total Participants" count and the roster
  printed in the Full Report PDF — read every `t3_participants` row for the class regardless
  of its `attended` flag, so a no-show, a duplicate entry, or a registration that never
  panned out still showed up in a report meant to describe who was actually in the room.
- Added `AND attended = 1` to that one query — the same flag `certificates.py` already gates
  certificate eligibility on, and the flag staff set from the signed T3 attendance form. Both
  call sites (the live preview and the final "Approve & Send" PDF build) read from this same
  function, so the fix covers both automatically; nothing else changed.

**2. The Google Form's own "Full Name" question no longer shows up as a report question**
(`training_reports.py`)
- Erik: "There's a full name field on google form and system captures it and produce in the
  report as well. Can this field be hidden/no need to pull because it's redundant with the
  participants list already." Correct — that question only identifies who's answering, and
  the report already lists who attended from the T3 roster (see #1 above), so surfacing it
  again as its own "question" — a bare list of names with no real evaluation content — was
  pure noise, and would have been an odd thing for the AI open-text summarizer to try to find
  "themes" in.
- New `_is_identity_question()` matches a question's title (case-insensitive, exact) against
  a small set of known identity-field titles ("Full Name", "Name", "Your Name", "Participant
  Name", "Participant's Name", "Trainee Name", "Trainee's Name", "Attendee Name"). `build_report()`
  now skips any question that matches, before it's aggregated into either the ratings table or
  the open-text summary — so it never reaches the numeric/text summaries, the AI narratives,
  or the Full Report PDF, all of which read from what `build_report()` already filtered.
- Every other question on the Form — every rating, every multiple-choice, every genuine
  open-text question — is completely untouched; this only drops questions that match the
  identity-field title list above.

**Testing:** DB/app-context tests (a seeded class with 2 attended + 1 non-attended
participant, and `build_report()` run against a mocked Forms API response — Google's own
structure/response calls stubbed, real DB) — (1) `_participant_names()` returns only the 2
attended participants, confirming the no-show is excluded; (2) a mocked form structure with a
"Full Name" text question alongside a real rating question and a real open-text question:
after `build_report()`, "Full Name" appears in neither `numeric_summary` nor `text_summary`,
while the rating question's exact average (4.5/5 from two responses) and the open-text
question both come through untouched — confirming the filter removes only the identity
question and nothing else. `py_compile` clean.

## Fix80 — Safeguards against overwriting a class's Evaluation Form once it has real responses

**Date:** 2026-09-21

Erik, right after connecting Google Forms automation and generating his first auto-built
Evaluation Form for a class: "but i have generated the training evaluation QR and
participants have filled up, if i regenerate again then it will create a new one." Correct,
and a real bug: `evaluation_forms.generate_form_for_session()` always makes a brand-new copy
of the master template — it had no idea whether the class's *current* linked Form already has
real responses sitting in it. Clicking "Regenerate" would have overwritten
`course_sessions.evaluation_form_id`/`evaluation_form_link` with a fresh, empty Form; the old
Form and its responses stay untouched in Google Drive, but Modoku Hub loses all record of it,
and the Training Report would start reading from the new, empty one instead. Confirmed the
exact mechanism by reading the code before changing anything, and told Erik not to click
Regenerate on that class in the meantime.

**1. Regenerating now requires an explicit, typed-out confirmation**
(`evaluation_forms.py`, `templates/sessions/view.html`)
- Once a class already has an Evaluation Form linked (`s.evaluation_form_id` set), the plain
  "Regenerate" button is gone. In its place: a collapsed **"Need to regenerate?"** disclosure
  that, when opened, requires ticking a checkbox spelling out exactly what happens — "I
  understand this creates a brand-new, empty Form and this class stops tracking the current
  one. Responses already collected stay in Google Drive but will no longer show in this
  class's Training Report" — before the (now danger-styled) Regenerate button will do
  anything.
- Enforced server-side, not just in the template: `generate()` now refuses to run
  (`EvaluationFormError`-free early return with a flash message) unless the request explicitly
  carries `confirm_regenerate=on`, whenever the class already has a Form linked. A class with
  nothing linked yet has nothing to lose and generates immediately, exactly as before.
- A class with a linked Form now also shows a plain confirmation line above the fold —
  "Linked to a generated Evaluation Form since ⟨date⟩" with an Open Form link — so it's
  obvious at a glance that regenerating is a real, deliberate replacement, not routine upkeep.

**2. New "Link Existing Form" — adopt a Form that already has responses, no copy made**
(`evaluation_forms.py`, new `link_existing()` route; `templates/sessions/view.html`)
- A class can now be pointed at a Google Form that already exists — one created by hand before
  this automation was connected, or any other Form Erik wants to keep using — by pasting its
  file ID (the same "long string in its edit URL" convention already used for the Settings
  master-template field, see `set_template()`). This reads the Form (to confirm it exists, is
  reachable, and is published) and writes its id/link onto the class — **no Drive copy is
  made, and nothing on Google's side is touched at all** — so an already-collecting Form
  starts showing up in that class's Training Report without ever being duplicated.
- Available any time Google Forms automation is connected, via an **"Already have a Form for
  this class?"** disclosure — whether or not a Form is already linked, since it's also the fix
  for a class that already has a *manually*-pasted `evaluation_form_link` (from before Google
  was connected) that would otherwise be silently orphaned the moment someone clicks the
  now-only-option "Generate" button. A first-time "Generate" screen with an existing manual
  link on file shows an explicit warning pointing at this option instead.
- Fails cleanly (flash message, nothing written) if the pasted file ID isn't reachable, or the
  Form has no public link yet (not published) — same defensive pattern
  `generate_form_for_session()` already uses for its own failure paths.

**No changes to what a genuinely new Form generation does** — `generate_form_for_session()`
itself, the Drive copy/retitle/publish/QR-poster sequence, and the ordinary first-time
"Generate Evaluation Form" flow for a class with nothing linked yet are all untouched. This is
purely about not letting a second click silently destroy the link to a class's real,
already-answered Form.

**Testing:** Flask test-client, Google Forms API calls mocked throughout (a real class seeded
in a fresh DB, `is_connected()`/`get_template_id()` faked so the eval-forms UI is live) — (1)
initial class page shows the plain "Generate" button plus the "Already have a Form?" toggle,
no regenerate/confirm UI yet; (2) `link_existing()` sets `evaluation_form_id`/
`evaluation_form_link` from a mocked Forms API response and **makes zero calls to the Drive
copy or Forms batchUpdate/publish endpoints** — proving no new Form is ever created by this
path; (3) the class page now shows "Linked to a generated Evaluation Form" plus the gated
"Need to regenerate?" disclosure; (4) posting to `generate()` **without** `confirm_regenerate`
is rejected with a flash message and makes zero calls to the Drive/Forms APIs — the existing
linked form is left completely untouched; (5) posting **with** `confirm_regenerate=on` goes
through exactly as the original always-overwrite behavior did (Drive copy + batchUpdate +
setPublishSettings + Forms get, 3 POSTs, new form id saved) — confirming the safeguard blocks
only the unconfirmed case, not the deliberate one. `py_compile` clean.

## Fix79 — e-Certificate download 405 on mobile; mobile padding on public pages; "/how" page couldn't scroll

**Date:** 2026-09-21

Three separate issues Erik reported from testing on his phone.

**1. "Method Not Allowed" clicking View/Download on a claimed e-Certificate (mobile only)**
(`certificates.py`, `templates/certificates/success.html`, `db.py`)
- The claim flow's "Download Certificate" step was a `<form method="post">` resubmitting the
  original start-date/IC lookup to `/cert/download`, which returned the PDF `inline`. On
  desktop that's the whole interaction. On mobile — mainly iOS Safari — after receiving an
  `inline` PDF response to a POST, the browser's own PDF viewer silently re-requests the exact
  same URL with a plain GET to actually render/print/share it. Since `/cert/download` was
  registered POST-only, that follow-up GET hit Flask's 405 Method Not Allowed — which is what
  Erik was seeing. This wasn't reproducible by just looking at the desktop flow, since desktop
  browsers never issue that second GET.
- Fixed by switching the download step to a plain GET, so there's no second, different-method
  request for a mobile PDF viewer to make in the first place: each certificate now gets a
  random, unguessable `download_token` (`certificates.download_token`, new column, unique
  index), and `claim()` hands back a `Download Certificate` **link** to `GET
  /cert/download/<token>` instead of a resubmittable form.
- **Still re-validated on every request, same as before** — the token lookup re-checks
  `t3_participants.attended`, so un-marking someone as attended after a link was
  generated/shared immediately stops it working. This preserves the original design intent
  (nothing enumerable, never trust a stale claim) while fixing the mobile bug — it's a random
  token in place of a participant id, not a shortcut around the re-check.
- **The token is stable across regeneration** — re-marking a participant attended (which
  regenerates their certificate file) does *not* rotate their token, so a link a participant
  already saved or shared keeps working.
- A light migration backfills `download_token` for every certificate already on file (from
  before this column existed), so existing participants' `certificates` rows aren't left
  without a working link — this runs automatically the next time the app starts, no manual
  step needed.
- The old POST `/cert/download` route is gone; nothing else in the app referenced it.

**2. Public pages ran edge-to-edge on a phone screen, hiding the background**
(`static/css/style.css`)
- Erik: "for all mobile view public link like e-cert claim, /how page and any layout identical
  like these pages pls give enough padding left and right so we can see the background."
- Root cause: `.auth-card` is `width: 100%` inside `.auth-wrap`, and `.auth-wrap` had no
  horizontal padding of its own — so on a phone-width screen the card's width came out equal
  to the full viewport width, leaving no gutter for the background image to show through.
- Fixed with one shared rule (`padding-left`/`padding-right: 1.25rem` on `.auth-wrap`) rather
  than touching each page — every public/guest page reuses this same `.auth-wrap`/`.auth-card`
  markup (certificate claim + its result pages, the T3/JD14/quotation/PO/trainer-invoice/
  vendor-invoice return links, login, the "/how" tutorial page, and others — 28 templates in
  total), so this one CSS change gives all of them a visible gutter on mobile without changing
  anything on desktop (the card was already narrower than the viewport there via its own
  `max-width`, so the extra padding on the wrap has no visible effect at that width).

**3. "/how" page (the projector "Get Your e-Certificate" page) couldn't scroll on mobile**
(`templates/tutorial/index.html`)
- That page is deliberately built to fit on one screen with no scrollbar when it's projected
  or shown on a laptop (`html, body { height: 100%; overflow: hidden; }`) — intentional, so it
  reads cleanly on a training-room screen. But at phone width the QR code and the three
  numbered steps (a 2-column row on a wider screen) stack into one column and run taller than
  the phone's viewport, and with scrolling disabled outright, the bottom of the card was
  simply unreachable — not a rendering bug, the content was there, there was just no way to
  get to it.
- Fixed by gating that "fits on one screen" lock to `@media (min-width: 768px)` — the same
  breakpoint where the two columns actually sit side by side and the page genuinely does fit
  in one screen. Below that width the page scrolls normally, like every other page in the app.
  The projector/laptop behavior this page was built for is completely unchanged.

**Testing:** Flask test-client end-to-end on the certificate flow (claim renders a working GET
download link; that link serves a real PDF; fetching the exact same link a second time — the
scenario that used to 405 — still works; the old POST route no longer exists; a bogus token
falls back to the existing "not found" page instead of crashing; un-marking a participant
attended immediately breaks their link; a certificate's `download_token` stays the same across
regeneration) + a direct backfill test simulating a pre-migration `certificates` row with a
`NULL` download_token, confirming the light migration fills it in on the next app boot without
touching anything else. `py_compile` clean. Mobile-width (375px) real-browser check on the
padding fix confirmed a visible ~19px gutter on both sides of the card on the certificate claim
page; the same check on the "/how" page's computed styles confirmed the identical padding rule
is applied there too, and confirmed the page's `overflow-y` is no longer locked at that width
(this sandbox couldn't get a fully reliable pixel-for-pixel render of the "/how" page's two
external CDN stylesheets — Bootstrap/Google Fonts are blocked here, a known sandbox-only
limitation noted in earlier fixes — so the direct computed-style check is the authoritative
one; Erik to confirm the visual result on an actual phone).

## Fix78 — Row numbers on the Manage Participants list

**Date:** 2026-09-21

Erik, looking at the T3 Manage Participants page: "can you add a number besides Name, so i
know the numbers and easy for me to look."

- `templates/t3/manage.html`: new **No.** column between the select-all checkbox and Name,
  showing each participant's position in the list (1, 2, 3, …) via `loop.index`. Purely a
  display convenience — it's the row's position on this page, not a stored ID, so it doesn't
  affect selection, sorting, or any of the bulk actions (Mark Attended/Unmark/Delete
  Selected), which all still key off the participant's real database ID as before.
- The same pattern already used for the "No." column on the printable T3 Attendance Form page
  (Fix77 and earlier) — just applied here too.

**Testing:** Flask test-client render check — the new header cell is present, and three
participants added to a test class come back numbered 1, 2, 3 in order.

## Fix77 — Edit a participant directly from the T3 Attendance Form page

**Date:** 2026-09-21

Erik asked for a way to edit participant details "on backend" from the T3 Attendance Form
page. Editing already existed (name/IC/employer/gender/citizenship, via the Manage
Participants page — click a participant's name), but only from that separate page. Erik
confirmed he specifically wanted it reachable directly from the T3 Attendance Form page
itself, without navigating away to Manage Participants first.

- `templates/sessions/t3_attendance_form.html`: each participant row now has a small pencil
  icon next to their name (screen-only, `d-print-none` — never shows on the printed sheet or
  the downloaded/emailed PDF). Clicking it opens a modal, pre-filled with that participant's
  current Name/IC No./Employer/Gender/Citizenship, right there on the page — no navigation
  away and back.
- The modal reuses the existing `t3.edit` route and its validation (required name, duplicate-IC
  check) rather than a new endpoint — same save logic as the Manage Participants edit page,
  just presented inline.
- `t3.py`: `edit()`'s POST handler now accepts a `return_to=t3_form` hidden field (plus
  `extra_blank_rows`, to preserve that page's setting) and redirects back to the T3 Attendance
  Form page on success instead of always going to Manage Participants. Editing from the Manage
  Participants page itself is completely unchanged — that path doesn't send `return_to`, so it
  keeps redirecting there as before.
- If validation fails (blank name, duplicate IC) while editing from the modal, it falls back
  to the existing full-page Edit Participant screen (same as it always has) rather than
  silently failing — and that fallback page now also carries `return_to`/`extra_blank_rows`
  forward, so retrying from there still lands back on the T3 Attendance Form page instead of
  defaulting away to Manage Participants.
- `sessions.py`: `t3_attendance_form()` now passes `genders`/`citizenships` (from `t3.py`'s
  existing `GENDERS`/`CITIZENSHIPS` constants) into the template for the modal's dropdowns —
  no new import cycle (`t3.py` doesn't import `sessions.py`, so a top-level import is safe;
  checked `t3.py`'s own imports transitively, including `certificates.py`, for the same).

**Testing:** Flask test-client end-to-end: the T3 Attendance Form page renders the edit icon
and modal correctly data-wired to each participant's real edit URL and current values;
submitting the modal's form updates the participant and redirects back to the T3 Attendance
Form page with `extra_blank_rows` preserved, and the page reflects the change on reload; the
ordinary Manage Participants edit flow (no `return_to`) is confirmed unchanged; a validation
failure (blank name) falls back to the full edit page while still carrying `return_to`/
`extra_blank_rows` forward for a retry. `py_compile` clean; confirmed the app boots with the
new `sessions.py → t3.py` import with no circularity.

## Fix76 — IC/NRIC and Sex auto-capture in AI attendance matching

**Date:** 2026-09-21

Erik asked: "i thought when participants wrote their NRIC/IC No in the blank field, the
system will auto capture and update my T3 attendance, no?" — it didn't; the AI read only
names off the signed sheet, and matching was pure fuzzy-name comparison. Erik asked to build
IC-based matching, and to do the same for the Sex column.

- `ai_match.py`: the Claude vision extraction prompt now asks for each signed row's IC/NRIC
  number and Sex ("Male"/"Female") alongside the name (`{"name", "ic_no", "sex"}` per row,
  was a bare name string). `analyze_attendance_photo()` returns `{"course_title",
  "training_date", "rows": [...]}` in place of the old `"names"` list.
- New `_normalize_ic()` — strips everything but letters/digits and uppercases, so
  "901231-14-5566", "901231145566" and stray OCR spacing all compare equal; a result shorter
  than `MIN_USABLE_IC_LENGTH` (6) is treated as unusable rather than risking a false match
  from a badly-misread scrap of digits. New `_normalize_sex()` mirrors `t3.py`'s own
  `_normalize_gender()` (duplicated rather than imported — `t3.py` imports `ai_match.py`, so
  importing back would be circular).
- New `match_rows_to_participants()` replaces the old name-only matcher. **An exact IC match
  (after normalizing both sides) always wins outright** (confidence 100%, "matched by IC")
  over a name-only match — a sheet's IC column, when legible, identifies a specific person far
  more reliably than a name that handwriting or OCR can blur into a similar-looking one. Falls
  back to the existing fuzzy name match when no usable IC was read, or it didn't match anyone
  on the class's list.
- New `_backfill_participant_identity()`: once a signed-sheet row is confidently matched to a
  participant, any IC number or Sex read off that row is saved onto the participant's own
  record — but **only fields currently blank**; a value a human already entered (by hand, or
  from a CSV import) is never overwritten. Runs for every confidently-matched row, even one
  whose attendance was already marked by an earlier photo of the same day — so a repeat photo
  with clearer handwriting can still be the one that fills in someone's IC/Sex.
- Old cached reads (`attendance_returns.ai_names_json` rows saved before this feature existed)
  are a flat list of plain name strings rather than the new `{"name","ic_no","sex"}` shape —
  new `_normalize_stored_rows()` parses both formats transparently, so historical data keeps
  working with no migration needed.
- AI Match Attendance review page (`templates/t3/ai_match.html`): each row now shows the
  IC/Sex read off the photo, and a **"Matched by IC"** badge next to the matched participant's
  name whenever that's how the match was made (as opposed to by name).

**Testing:** pure unit tests for `_normalize_ic`/`_normalize_sex` (dash/spacing-insensitive
comparison, too-short-to-trust guard, unknown values → null) and `_normalize_stored_rows`
(both old flat-string and new object formats, and malformed/empty input all handled). Isolated
DB integration tests for `match_rows_to_participants` (an exact IC match wins even against a
badly garbled name; falls back to fuzzy name matching when no usable IC was read; a too-short
IC is correctly ignored rather than risking a wrong match) and `_backfill_participant_identity`
(fills a blank IC and blank Sex; never overwrites an IC or Sex already on file). Full
Flask-test-client-free, app-context end-to-end test of the two real call sites
(`get_review_data()` for the review page, `auto_mark_attendance()` for the fully-automated
path) against both an old-format and a new-format cached photo read in the same session —
confirms both paths resolve correctly, attendance gets marked, and identity backfill fires
without overwriting a participant's existing IC. `ast.parse` clean.

## Fix75 — Automatic photo compression on every upload (Return Attendance, Claims, Name Cards)

**Date:** 2026-09-21

Erik: "can you do a image compression feature (but maintain the quality as well)? this is for
every upload photos. because phone nowadays has massive megapixels thus the filesize also
big." Scoped with Erik to three fields: the Return Attendance Form, Submit Claims, and Name
Card upload — trainer/vendor invoice uploads are documents-only already and were confirmed to
stay that way. Erik also confirmed the compressed copy can BE the stored "original" for these
three fields (his earlier requirement that the original attendance *photo* stay untouched
refers to Fix72's separate scan-mode feature, which still never touches the stored file).

- New `image_compress.py` — resizes a photo to a 2000px long edge and re-encodes it as a JPEG
  at quality 85, honoring the photo's EXIF orientation tag first (so a phone photo doesn't end
  up sideways). Best-effort and non-blocking throughout, same pattern as `scan_enhance.py`:
  any failure, or a result that doesn't actually come out smaller than the original, falls
  back to passing the original bytes through untouched — never a blocked upload. A file that
  isn't a recognizable image (a PDF, a Word doc, ...) is left completely alone.
- Wired in via `maybe_compress(file_storage)`, called right before the existing size-cap check
  on each field's upload:
  - `attendance_return.py` — the Return Attendance Form's photo field.
  - `claims.py` — the public claim submission's "files" field only. The separate finance/
    staff-side "receipts" field (`process()`) is untouched, per scope.
  - `leads.py` — the Name Card upload field only. The separate OCR-scan temp upload used to
    autofill a lead form is untouched, per scope.
- `db.py`: schema comment updated to note that a stored `attendance_returns` photo may now be
  the compressed copy rather than the exact original bytes.

**Testing:** unit tests on `compress_photo()` against realistic noise-based synthetic photos
(flat/sparse test images compress unrealistically small and don't exercise the pipeline
properly — regenerated with genuine per-pixel random noise to get a real-world-sized raw
file), plus an EXIF-orientation test. Full Flask test-client smoke tests against all three
real routes: an oversized synthetic photo (well past each field's size cap — 8.5 MB against
the Return Attendance Form's 4 MB cap, ~4.2 MB and ~2.7 MB against Claims/Name Card's 2 MB
default cap) is compressed and accepted where the raw upload would have been rejected; a PDF
submitted to the Return Attendance Form passes through byte-for-byte untouched (verified by
SHA-256); the claim and name-card flows both confirmed against their real routes with a valid
CSRF token — file saved, DB row correct, stored file provably smaller than the raw upload.
`ast.parse` clean.

## Fix74 — Return Attendance Form upload cap raised to 4 MB (this field only)

**Date:** 2026-09-21

- `uploadutil.py`: new `RETURN_ATTENDANCE_MAX_BYTES = 4 * 1024 * 1024`, following the same
  per-field-override pattern already used for `TRAINER_DOCUMENT_MAX_BYTES` (1 MB) and
  `PROPOSAL_DECK_MAX_BYTES` (5 MB) — the app-wide default (`MAX_UPLOAD_BYTES`) stays at 2 MB
  everywhere else.
- `attendance_return.py`: `submit()`'s `validate_upload()` call now passes
  `max_bytes=uploadutil.RETURN_ATTENDANCE_MAX_BYTES`, so only this one public upload field is
  affected — every other upload in the app (staff-side attendance uploads, signed POs/
  quotations, trainer documents, name cards, etc.) keeps its existing cap untouched.
- `templates/attendance_return/details.html`: the "Max 2 MB per photo" helper text updated to
  "Max 4 MB per photo" to match. The over-limit error message itself is generated from
  `max_bytes` automatically, so no other copy needed changing.

**Testing:** Flask test-client smoke test against the real public submit route — a 3 MB image
(would have been rejected under the old 2 MB cap) now uploads successfully; a 5 MB image is
still correctly rejected, with the error message auto-reflecting the new limit ("File is too
large (5.0 MB). The maximum is 4 MB."). `ast.parse` clean on both touched Python files.

## Fix73 — iPhone couldn't open the file picker on the public Return Attendance Form

**Date:** 2026-09-21

Erik tested the Fix70-72 upload flow live: worked fine on his laptop, but on his iPhone
tapping "Choose Files" did nothing at all.

- `templates/attendance_return/details.html`: the file input had `capture="environment"` set
  alongside `multiple` and a mixed `accept="image/*,.pdf"`. `capture` is a mobile hint that
  tells the browser to jump straight into the camera instead of showing a normal file picker
  — desktop browsers ignore it entirely (why it worked on the laptop), but it's a known iOS
  Safari issue that combining it with `multiple` and a non-photo file type in `accept` (a PDF
  doesn't fit "go straight to camera") can make WebKit just not respond to the tap at all on
  some iOS versions.
- Fix: removed `capture="environment"`. iPhone users still get the option to take a photo —
  tapping the field on iOS shows the normal action sheet ("Take Photo," "Choose Photo,"
  "Browse" for a PDF) — it just isn't forced into camera-only mode anymore. `multiple` and the
  photo/PDF `accept` are untouched.
- Grepped the rest of the templates for the same pattern — this was the only file input using
  `capture` anywhere in the app, so it's an isolated fix.

**Testing:** Flask test-client smoke test confirming the rendered details page no longer
contains `capture=` while `accept="image/*,.pdf"` and `multiple required` are both still
present. `py_compile` clean. (No iOS device available in this sandbox to click-test directly —
Erik to confirm the picker now opens on his phone.)

## Fix72 — "Scan mode" for photos returned via the public Attendance Form (optional, free, local)

**Date:** 2026-09-20

Erik asked whether anything could sharpen up a blurry trainer photo of a signed T3 sheet into
something closer to a proper scan. Built as fully local, deterministic image processing
(OpenCV) — no AI model, no API key, no per-use cost, and nothing generative: it straightens
and sharpens what's actually in the photo, it never invents detail that wasn't there.

- New `modoku_crm/scan_enhance.py` — for one submitted photo, detects the sheet of paper's
  four corners (Canny edge detection + contour finding), perspective-corrects it flat,
  denoises, boosts contrast (CLAHE) and sharpens (unsharp mask). Optional/best-effort
  throughout, same pattern as `ai_match.is_configured()`: missing OpenCV, a corrupt image, or
  no confident document-shaped region found all just mean no enhanced copy for that one photo
  — never a blocked submission or a crash.
- **The original upload is never touched or replaced** — this was Erik's explicit requirement
  ("keep the original photo untouched ya so i can always refer back if anything happens").
  `scan_enhance.py` only ever writes a brand-new file; `attendance_return.py`'s upload loop
  saves it to a new `attendance_returns.enhanced_filename` column (new light-migration in
  `db.py`), alongside the existing `filename` column, which is never modified. Verified with a
  byte-for-byte SHA-256 check of the original file before and after enhancement.
- Images only — a PDF submission is usually already a compiled scan itself and isn't what the
  document-edge detection is built for, so `enhanced_filename` stays `NULL` for those (no
  attempt, no error).
- Class page (`templates/sessions/view.html`): each photo return with an enhanced copy now
  shows a second **"Scanned version"** link next to the original, opening it separately
  (`sessions.py`, new `GET /sessions/<id>/returns/<id>/download-enhanced` route). The original
  "download" link/behavior is completely unchanged.
- `requirements.txt`: added `opencv-python-headless`. `README.md`: new "Scan mode for returned
  attendance photos (optional)" section — what it does, no-cost framing, the
  original-file-preservation guarantee, graceful degradation, PDF exclusion, and a
  troubleshooting note (`apt-get install -y libglib2.0-0`) for minimal Linux server images
  where OpenCV's system dependencies might be missing.

**Testing:** unit-verified the corner-detection/perspective-correction step in isolation with
a synthetic rotated "photo of a signed sheet" test image (first synthetic image was a bad
fixture — the rotated paper ran off the top/bottom of the frame, so no closed 4-point contour
could ever be found; rebuilt so the whole document sits inside the frame, confirmed the output
is genuinely flattened/de-rotated and visibly crisper, not just contrast-adjusted). Full
Flask test-client smoke test against the real public submit route: an image submission gets a
populated `enhanced_filename` pointing at a valid, non-trivial JPEG; the original file's
SHA-256 is identical before and after; a PDF submission leaves `enhanced_filename` NULL with
no error; `download_return_enhanced` serves the enhanced file correctly and falls back
cleanly (flash + redirect) when none exists; the plain "download" route still serves the
untouched original byte-for-byte; the class page renders the "Scanned version" link only for
the photo return, not the PDF one. `py_compile`/`ast.parse` clean across all touched files.

## Fix71 — "Photos submitted by trainer" label updated after Fix70

**Date:** 2026-09-20

- `templates/sessions/view.html`: the class page's list of files a trainer sent back through
  the public Return Attendance Form was still headed **"Photos submitted by trainer"** and
  its intro line still said "...to send back photos of the signed sheet" — both written before
  Fix70 (same day) let that page accept a PDF too. Relabeled to **"Files submitted by
  trainer"** and "...photos (or a PDF) of the signed sheet", so the copy matches what the page
  actually accepts. No behavior change — text only.

**Testing:** Flask test-client smoke test — class view renders 200 with the new label present
and the old "Photos submitted by trainer" wording gone.

## Fix70 — Public "Return Attendance Form" page now accepts a PDF, not just photos

**Date:** 2026-09-20

Found while answering Erik's question about how a multi-page signed T3 sheet gets returned:
the public return page (`attendance_return.py`) is built for a trainer submitting multiple
photos at once (one per page, no need to compile anything) — that part always worked. But the
upload field's picker also advertised `.pdf` as an option (`accept="image/*,.pdf"` in
`templates/attendance_return/details.html`) and even had a leftover "...or a PDF" error
message, while the actual server-side validation only allowed image files — so a trainer who
picked a PDF got a confusing generic rejection instead.

- `uploadutil.py`: new `RETURN_ATTENDANCE_EXTENSIONS` (images + `pdf`), used by
  `attendance_return.submit()` in place of the plain `IMAGE_EXTENSIONS` allowlist it had before.
- `ai_match.py`: the AI auto-read step (`analyze_attendance_photo`) previously always built an
  `"image"` content block for Claude's vision API, so simply widening the allowlist without
  this would have let a PDF upload through but had it silently fail to read (wrong bytes sent
  under an `image/jpeg` label, caught by the existing best-effort error handling and treated
  as "nothing read"). Replaced `_encode_image()` with `_content_block()`, which builds a
  `"document"` block for a `.pdf` and an `"image"` block otherwise — same split
  `doc_sanity.py` already uses for its own file-type checks. A multi-page PDF is read in one
  API call across all its pages, same as a set of individual photos would be read one call
  each.
- `doc_sanity.check_document()`'s own sanity-check step already handled PDF correctly before
  this — no change needed there.
- Docstrings/flash copy updated to reflect that either photos or a PDF work.

**Testing:** Flask test-client smoke test against the real submit route with a valid CSRF
token — a `.pdf` submission now saves and returns 200 (previously would have been rejected by
`validate_upload`); a genuinely disallowed extension (`.txt`) is still rejected; a mixed
photo+PDF multi-file submission in one request still saves both. `ai_match._content_block()`
unit-checked directly — returns a `"document"`/`application/pdf` block for a `.pdf` path and
an `"image"`/`image/png` block for a `.png` path. `py_compile` clean.

## Fix69 — Back button on the class (Class view) page

**Date:** 2026-09-20

- `templates/sessions/view.html`: added a **Back** button to the class page's header, linking
  to the classes list (`sessions.index`) — it was the only button missing from that page;
  every other detail page (Quotation, Invoice, Purchase Order, Voucher, …) already has one.
- Placed last in the button row, `btn-outline-secondary btn-sm`, matching the existing
  convention elsewhere in the app (e.g. `templates/petty_cash/view.html`). No other buttons
  on the page changed.

**Testing:** Flask test-client smoke test — class view renders 200 with the Back button
present in the response.

## Fix68 — Split "Print / PDF" into separate Print and Download PDF buttons (T3 Attendance Form)

**Date:** 2026-09-20

- Class → T3 Attendance view (`templates/sessions/t3_attendance_form.html`): the single
  **Print / PDF** button is now two buttons — **Print** (still `window.print()`, for anyone
  who wants a paper printout or to use their browser's own "save as PDF") and **Download
  PDF** (new), which fetches an actual generated PDF file rather than relying on the browser's
  print dialog.
- **Download PDF** (`sessions.py`, new `GET /sessions/<id>/t3-attendance/download` route)
  reuses the same `pdfgen.generate_t3_form_pdf()` builder that already powers the "Email T3
  Form to Trainer" attachment, so the downloaded file is identical to what a trainer receives
  by email — just delivered as a direct browser download (`Content-Disposition: attachment`)
  instead. Respects the page's "Extra blank rows" setting, same as the on-screen form.
- The other buttons on that page (Manage Participants, Email T3 Form to Trainer, Back) are
  unchanged.

**Testing:** Flask test-client smoke test — the page renders with both buttons present and
the old combined "Print / PDF" label gone; the download route returns a real `application/pdf`
response (`%PDF` header bytes, correct filename in `Content-Disposition`) both with and
without extra blank rows. `py_compile` clean.

## Fix67 — T3 name auto-capitalization; new JD14 Form module

**Date:** 2026-09-19

**1. T3 attendance form — names auto-capitalize to Title Case**
- Front-end: any name field types-as-you-go into Title Case ("ALI BIN ABU" / "aLi bIn AbU" →
  "Ali Bin Abu") on both the staff-side T3 pages (`t3/edit.html`, `t3/manage.html`) and the
  public self-registration form (`t3_public/form.html`) — new shared
  `static/js/title-case-name.js`.
- Back-end: the same Title Case rule is applied on save in `t3.py` (`add`, `edit`, and bulk
  CSV/paste adds) and in `t3_public.py`, so a submission with JS disabled still lands
  capitalized correctly. IC number and employer name are untouched.

**2. New JD14 Form module** — generates OUR side of the HRDCorp SBL-KHAS Joint Declaration
(form PSMB/SBL-KHAS/JD/14), signed and stamped by an admin, ready to send to the client for
them to complete, sign, stamp and return. This is the missing "outgoing" half — the existing
"receive the client's signed copy" flow (`jd14_return.py`, `sessions.py`'s upload/email
routes, `course_sessions.jd14_file`/`jd14_return_token`) is untouched and still handles the
signed copy coming back, exactly as before.
- New **JD14 Forms** sidebar tab, plus a "Prepare JD14 Form" link on each class page
  (alongside — not replacing — the existing "Signed JD14 Copy" upload/return-link box, which
  was relabeled for clarity).
- **Layout matches the official reference PDF exactly** — corrected after Erik flagged that
  the first pass, built from a paraphrased description, wasn't close enough for an
  HRDCorp-mandated document. Rebuilt against an exact `pdftotext -layout` extraction and a
  150dpi visual comparison of the actual reference PDF he supplied: same section order (MyCoID
  digit-cell box, form-code box, title/subtitle/intro paragraph, Part 1 employer box, Part 2
  claim table, Part 3 joint declaration box, REMINDER footer), same box/border structure, same
  wording verbatim (including the reference's own quirks — the "SBL –KHAS" spacing, the
  double space in "...charged above.  I am responsible..." the doubled "REMINDER: :"). Part
  3(b) — the employer's half — is always left completely blank for the client to fill by hand;
  none of our data goes in it.
- Exact HRDCorp layout, built entirely with `<table>` markup rather than flexbox — this
  project already learned (Fix66) that wkhtmltopdf's engine silently breaks flexbox and
  doesn't repeat table headers, so the JD14 builder was written table-based from the start,
  and stayed that way through the layout correction.
- Everything is prefilled from the class's own data and directly editable with a live preview
  before saving:
  - Registered Name/Address of Employer ← the class's linked client company.
  - Approval No ← the class's HRDCorp Grant ID as submitted by the client. Employer Code ←
    the first segment of that Grant ID before the underscore (e.g. `46829P_26_0838` →
    `46829P`).
  - Course Title / Training Dates / Venue ← the class record.
  - Number of Trainees ← count of attended T3 participants.
  - Total Fee Approved and Total Fee Claimed ← both default from the linked Quotation's
    amount **excluding SST**.
  - **Group Approved and Group Claimed are always left blank**, as specified — never derived.
- Only admin-level users can sign and stamp (Part 3, our side); any staff member can fill in
  and edit the rest. Signing requires the admin to have a signature on file (Profile page); a
  missing company stamp doesn't block signing, the box just prints empty.
- **Company stamp: one global upload (Settings → Company Stamp)**, reused on every JD14 from
  then on — no more uploading it per form.
- Added a MyKad No. field to the staff Profile page, shown on the signed JD14 alongside name,
  designation and signature.
- "Send to Client" (admin-only, after signing) emails the generated PDF and reminds/links the
  client to the existing public JD14 return page, so the returned signed copy lands in the
  back-end exactly the way it already does today.
- **Sent → Awaiting Return → Received status tracker**, per Erik's request: a 3-step badge on
  both the JD14 Forms list and a class's own JD14 edit page. "Sent" lights up once the signed
  form is emailed to the client (`jd14_forms.sent_at`/`sent_to`, stamped by the Send action);
  "Awaiting Return" is the active step while it's out with the client; "Received" lights up
  the moment the client's signed copy comes back through the existing return flow
  (`course_sessions.jd14_file`) — this reads that column, it doesn't duplicate it. Nothing
  shows until the form has actually been signed on our side.
- **Full-page utilization** (follow-up): the PDF content was resized to fill the page rather
  than leaving generous white space, per Erik's "utilize the whole page" request.
- **Search & sort on the JD14 Forms list** (`jd14.py`, `templates/jd14/index.html`): a `?q=`
  search box matching course title, client and venue (same pattern as Courses/Sessions), and
  click-to-sort Date/Status columns via a shared `sort_link` macro, ascending/descending,
  defaulting to newest-first. Status sorts by its derived pipeline order (not started →
  drafted → signed → sent → received), not alphabetically.
- **Single-page fit fix** (follow-up, `pdfgen.py`, `templates/jd14/edit.html`): the generated
  PDF was overflowing onto a second page in production — just the REMINDER footer spilling
  over — even though the earlier full-page-fill pass had reported a comfortable single-page
  fit. Root cause: that earlier verification was done through wkhtmltopdf's `--zoom` flag, but
  this sandbox's wkhtmltopdf build has "smart shrinking" permanently on (an unpatched-Qt
  limitation — `--disable-smart-shrinking` is silently ignored), which auto-scales content to
  always report one page regardless of true overflow, so the earlier fill measurement never
  actually proved the content fit at real size. Re-verified with a Chromium-based render
  (which does not auto-shrink) against the exact reference PDF Erik uploaded, reproduced the
  same 2-page overflow, then trimmed spacing/font-sizes across the form in three rounds —
  cross-checked with the Chromium render after each round — until it genuinely fits one page.
- **Bigger signature & company stamp images** (follow-up): enlarged from 24px to 64px
  max-height per Erik's request, with the declaration table rows heightened (46px → 70px) to
  match so the larger images don't get clipped or overlap neighboring fields.

**Resolved 2026-09-25:** after the server was brought back in line with the repo (Fix93 deploy: a `git stash` of hand-copied files, then `git pull`), the search/sort JD14 Forms list loads fine in production. The 500 was most likely those out-of-sync files, not the search/sort code. The note below is kept for history.

**Known issue (historical):** the search/sort deploy above 500'd in Erik's production
environment; he worked around it by reverting to the pre-search/sort `jd14.py`/`index.html`
(confirmed that cleared the 500). Not reproduced locally against clean or messy synthetic
data. A standalone diagnostic script (`diagnose_jd14.py`, run from the repo root against the
real production DB) was written to capture the actual traceback but has not been run yet —
search/sort should stay reverted in production until that's done and the real bug is found.

**Testing:** implemented and verified end-to-end, including after the layout correction —
`py_compile` across the whole package, full app boot with a fresh DB (`jd14_forms` table incl.
the added `sent_at`/`sent_to` columns, plus `users.mykad_no`, all migrate cleanly on both a
fresh DB and an existing one), `generate_jd14_pdf()` rendered (signed and unsigned) at 150dpi
and compared side-by-side against the actual reference PDF — section order, box structure and
verbatim text all line up. All JD14 routes (index, edit, save, reset-prefill, preview,
download, sign — both the missing-signature and success paths, send) exercised through
Flask's test client with real CSRF tokens, including confirming `send()` now stamps
`sent_at`/`sent_to` and the status stepper advances through all three stages as a class
progresses from signed → sent → received (simulated by setting `jd14_file` the same way the
existing receive-flow does it). Company-stamp upload/serve routes and the updated
`sessions/view.html`/`settings/index.html`/`profile/edit.html` templates all render clean.

**Follow-up testing (2026-09-19, single-page fit + bigger images):** re-verified with a
Chromium print-to-PDF render (a genuine pagination oracle, unlike this sandbox's
auto-shrinking wkhtmltopdf build) on both the exact real-world case Erik's production PDF
showed (Sarawak Shell Berhad / Building Future Ready Teams) and a deliberately extreme
stress case (very long employer name/address/course title/venue) — both confirmed 1 page
(91.8% and 94.0% content-fill respectively), no clipping or overlap. Also re-rendered through
the actual wkhtmltopdf production code path in both unsigned and signed states — 1 page in
both, signature and company stamp legible and correctly boxed at the larger size. Flask
test-client smoke test (index, edit, preview.pdf, save, sign) re-run with real CSRF tokens —
all 200s, confirming the two 400s seen in an earlier ad-hoc check were purely that script
omitting the token, not a regression from this round's changes. `py_compile` clean.

## Fix66 — 5-digit invoice numbers, Quoted Price excl. SST, compact T3 form, manual certificates, em-dash sweep

**Date:** 2026-09-18

**1. Invoice numbering is now `INV-<yy>-<00000>`** (`invoices.py`)
- `INV-26-00297`: two-digit year, five-digit sequence (was `INV-2026-0297`).
- **Numbers already issued in the old format keep counting.** The sequence is read from the
  last dash-separated segment either way, so an existing `INV-2026-0296` produces
  `INV-26-00297` next, not `INV-26-00001`. This is the part that would have hurt on the live
  system, so it is tested explicitly.
- Rollover past 9,999 widens naturally (`09999` → `10000`).
- The Fix65 "Reset Next Number" peek/consume behaviour is unchanged and still passes.

**2. Quoted Price on the class page now shows the figure EXCLUDING SST**
(`quotations.py`, `templates/sessions/view.html`)
- `quoted_price_for_session()` returns `subtotal` alongside `grand_total`; the class page
  renders the subtotal with an "(excl. SST)" note. Fix64 showed the SST-inclusive grand total.
- `grand_total` is still returned and still computed by the same `_totals()` the quotation page
  uses, so nothing else that wants the inclusive figure had to change.
- Which quotation wins (Accepted outright, else most recent) is unchanged.

**3. T3 Attendance Form print/PDF row density** (`pdfgen.py`, `static/css/style.css`,
`templates/sessions/t3_attendance_form.html`)
- Rows were cut 33px -> 23px here to force a 25-pax sheet onto one page. That proved too tight
  to sign and was revised (see Fix66a, Fix66c, Fix66d below for the final values).
- **Name of Employer(s), NRIC and Citizenship are centre-aligned.** The trainee **Name**
  column stays left-aligned on purpose: it is the one column that wraps, and centred wrapped
  text reads badly.

**4. Manual certificate generator** (`cert_admin.py`, `templates/cert_admin/index.html`)
- New card on the Certificates page: pick a class, paste participant names one per line
  (limit **50**), get certificates back. Course title and dates come from the class as usual.
- The existing automatic, attendance-driven issuing is untouched and still the main route.
- **Deliberately writes nothing to `t3_participants` or `certificates`.** The T3 list is the
  signed document behind an HRDCorp claim — adding names for people who never signed would
  corrupt a record the business has to stand behind — and a `certificates` row is keyed to a
  participant row that doesn't exist here. Manual certificates come back as a direct download
  instead: one PDF for a single name, a zip for several.
- Duplicate names in one batch are both issued, with the second file renamed rather than
  overwritten. An over-long list is refused outright rather than silently truncated.
- Unlike the automatic route, the class list here is **every** class, not only those with
  attendance recorded.

**5. Em dashes reduced across the app** (~120 files)
- **Templates: 347 → 17.** What remains is structural, not prose: `—` as the empty-option
  placeholder in dropdowns, `—` as the "no value" badge in tables, and four JS/Jinja comments.
- **Python user-facing strings: 188 rewritten, 0 left** (the remaining dashes in `.py` are all
  in code comments, docstrings, and SQL/CSS comments inside builder strings).
- Rewrites were **not** a blind character swap: a full stop, colon, comma, `·`, ` - ` or
  parentheses, whichever the sentence actually needed.
- A full backup of `modoku_crm/` was taken before the mass edit, and the resulting diff was
  read line by line before shipping.

**Testing:** new checks on the numbering format (incl. continuity from an old-format number and
9999 rollover), T3 compaction, manual certificates (incl. nothing written to
`t3_participants`/`certificates`, the 50 limit, auth), the Fix64 suite updated to excl-SST, plus
a full regression re-run after the em-dash edit and the 23-page render smoke test.

**Supersedes a note in Fix65:** that entry recorded the invoice format as `INV-2026-0297`.
Item 1 above changes it.

## Fix66a — T3 rows opened up for signing

**Date:** 2026-09-18

Fix66's 23px rows were "too cramped, it's hard for participants to sign." Revised to 30px, then
settled at **44px** with the original `4px 6px` padding, accepting a 2-page sheet for 25 pax.
The print stylesheet is written in **px, not rem** (this app sets `html { font-size: 15px }`).
Superseded by Fix66c/66d for the emailed PDF.

## Fix66c — The emailed T3 PDF now matches the on-screen form (and the real cause)

**Date:** 2026-09-18

The emailed PDF is built by `pdfgen._build_t3_form_html` and rendered by **wkhtmltopdf**, whose
old WebKit ignores three things the on-screen version relies on:

1. **No flexbox.** The header strip stacked vertically. Rebuilt as a table.
2. **No repeating table headers** (`thead { display: table-header-group }` ignored).
3. **No `page-break-inside: avoid`**, so rows were sliced across pages.

So the list is now **paginated in Python** (`_t3_row_chunks`): each page gets its own complete
table, headings repeat, no row is split. Column headings are black (were grey). The on-screen/
print view was reverted to its pre-Fix66 values; it is the reference for how the form looks.

## Fix66d — Sizing the T3 sheet against the server's own rendering scale

**Date:** 2026-09-19

Fix66c came back from the server as three pages (13 rows, then 2 rows, then the rest).

**Cause: wkhtmltopdf's CSS-px-to-point mapping is not portable.** Measured off real output:

| | build | pt per CSS px |
|---|---|---|
| Server (VPS) | 0.12.6.1 / Qt 4.8.7 | **0.677** |
| Claude's test environment | 0.12.6 / Qt 5.15.13 | 0.575 |

The server renders **~18% larger** for content that doesn't fill the page width.
`--dpi 96 --disable-smart-shrinking` was tried and rejected. **Fix: size for the server** (the
tighter of the two): row height **64px**, **13** rows on page 1, **15** on continuation pages;
25 pax = 13 + 12 = two pages. `test_t3_form.py` re-renders at `--zoom` 1.177x / 1.35x / 1.55x to
prove the sheet still fits at the server's scale.

## Fix65 — Invoice in Poppins with a bigger logo, and the Reset Next Number bug

**Date:** 2026-09-15

**1. BUG: "Reset Next Number" was silently discarded** (`settings.py`, `invoices.py`)
- Reproduced before fixing: set the override to 297, create an invoice, get `INV-2026-0001`.
- Cause: `consume_invoice_number_override()` **clears the setting as a side effect of reading
  it**, and `_next_invoice_no()` was being called on the **GET** of the New Invoice form to
  render the "next number will be…" preview. So merely *opening the form* burned the override;
  the subsequent save found nothing pending and fell back to the running sequence.
- Fix: split the setting read into `_peek_override()` (read only) and `_consume_override()`
  (read + clear), with `peek_invoice_number_override()` / `peek_po_number_override()` exposed.
  `_next_invoice_no(consume=True)` now takes a flag; the form preview passes `consume=False`,
  the save path keeps consuming.
- **Purchase Orders were never affected** — `_next_po_no()` is only called at save time, never
  for a preview. Left alone, but given a matching `peek_` helper for symmetry.
- Behaviour confirmed end to end: preview shows `INV-2026-0297`, opening the form repeatedly
  doesn't consume it, the saved invoice IS `INV-2026-0297`, the next is `0298`, and the
  override is one-time rather than sticky. A non-numeric override is ignored, not crashed on.
- **Note on format:** the number is 4-digit within the year — `INV-2026-0297`, not `#00297`.
  That's the app's existing scheme (`INV-<year>-<0000>`), unchanged here. (Superseded by Fix66.)

**2. Invoice logo +20%** — 90px → **108px**, in both the web page (`.invoice-doc-logo`) and the
invoice PDF (`img.logo` inside `_build_invoice_html` only).

**3. Invoice set in Poppins** (invoice only — nothing else in the app moved off Inter)
- **Web:** `@font-face` rules in `style.css` self-host Poppins from `static/fonts`, and the
  font is scoped to the `.invoice-doc` card that already wrapped the document. Verified in a
  browser: the invoice resolves to Poppins, `document.fonts.check` confirms it actually loaded,
  and the surrounding app still computes to Inter.
- **PDF:** new `pdfgen.poppins_font_face_css()` embeds the font as base64 `@font-face`, matching
  how the module already embeds the logo — `pdfgen` is explicitly designed to render with no
  network access, and the VPS can't be assumed to have Poppins installed. Follows the existing
  pattern in `full_report_pdf._font_face_css()`.
- **Subsetted font files**, `Poppins-{Regular,Bold}.subset.ttf` (~14KB each vs ~155KB full),
  because an invoice PDF gets emailed and bundled into the audit-export zip. Rendered invoice
  PDF is ~170KB. Same files serve the web page.
- **Degrades gracefully:** if the font files are missing, `poppins_font_face_css()` returns ""
  and the Arial/Helvetica stack still applies — verified.
- **Near-miss worth recording:** the first subsetting pass **overwrote** the existing
  `Poppins-Regular.ttf` / `Poppins-Bold.ttf` in `static/fonts`, which `poster.py` and the
  certificate generator (`pdfgen._CERT_FONT_*`) draw with via PIL and need complete. Restored
  from source and checksum-verified; the subsets now carry distinct `.subset.ttf` names. Tests
  assert all four full files are still >100KB.
- Confirmed the font is genuinely applied, not silently falling back: rendering the same
  invoice with and without the embedded faces produces **different** output.

**New files:** `modoku_crm/static/fonts/Poppins-Regular.subset.ttf`,
`modoku_crm/static/fonts/Poppins-Bold.subset.ttf` — these must be committed, or the invoice
falls back to Arial.

**Testing:** 39 checks (override peek/consume semantics incl. non-numeric and empty, preview
not consuming, save consuming, one-time not sticky, sequence continuing; web logo size, scoped
Poppins with Inter fallback, quotation/PO logos provably untouched, global font still Inter,
font files served and under 25KB, full font files intact; PDF logo, embedded font, Arial
fallback, only the invoice builder embedding, other builders' Arial stack unchanged, rendered
PDF size) + 5 real-browser checks on the live invoice page (computed font, font actually
loaded, 108px logo, rest of app still Inter, no JS errors) + a rendered PDF inspected visually.
Fix58/59/61/62/63/64 suites all still pass.

## Fix64 — "+ Quotation for Client" button on a class, and a Quoted Price row

**Date:** 2026-09-15

**Wording:** "Quotation **for** Client" is the correct one, and is what shipped. A quotation is
prepared *for* someone; *to* only works alongside a verb of sending ("send the quotation **to**
the client"). As a bare noun-phrase button label, "for" is right.

**1. Button** (`templates/sessions/view.html`, `quotations.py`, `templates/quotations/form.html`)
- **+ Quotation for Client** sits next to + Enroll Participant on the class page, gated on the
  Quotations module being enabled (same as the existing Invoice/PO buttons).
- Links to `/quotations/new?session_id=<id>`. `quotations.new()` now reads that param, marks
  the class selected in the Class dropdown, and the form's existing `applyClass()` runs on load
  so course title, client, venue, dates, pax and time are all prefilled — the same result as
  picking the class by hand.
- `_linkable_sessions()` is called with the class as `include_id`, so **a class whose status
  has moved past Proposed/Scheduled is still in the dropdown when you arrive from its page**.
  Without this the button would have silently landed on an unlinked form for any Confirmed or
  Completed class. A plain `/quotations/new` still offers only Proposed/Scheduled, unchanged.
- The preselect is scoped to new quotations only (`{% if preselect_session and not quotation %}`),
  so editing an existing quotation never has its saved values overwritten.

**2. Quoted Price row** (`quotations.py`, `sessions.py`, `templates/sessions/view.html`)
- New `quotations.quoted_price_for_session(session_id)` returns the quote no., status, grand
  total and SST rate for the quotation tied to a class — or `None`.
- Sits directly beneath the existing course-derived **Price** row, showing the amount, a link
  to the quotation, its quote number and status, and "incl. SST" when a rate applies.
- **Hidden entirely when no quotation is linked**, as asked — the course's list price row is
  always there, but a quoted figure only exists once something has gone to the client.
- **Which quotation wins when a class has several** (revisions increment the quote number):
  an **Accepted** one wins outright; otherwise the most recent by date then id. So a newer
  Draft doesn't displace the accepted figure.
- Reuses `_totals()`, the same function the quotation page itself displays, so the two can
  never disagree — including the SST-inclusive case, where the grand total is the figure typed
  rather than fees + tax.
- Imported inside `sessions.view()` rather than at module scope: `quotations.py` already
  imports `sessions`, so a top-level import either way would be circular.

**Testing:** 33 checks (button presence/position/URL and module gating; the form arriving
preselected and prefilled; a Confirmed class still offered from its own page but not on a plain
new quotation; quoted price hidden with no quotation and shown with one; grand total incl. SST;
Accepted-beats-newer and newest-wins-otherwise precedence; SST-inclusive totals; row and button
both disappearing when the Quotations module is off) + 7 real-browser checks clicking the
button through to a prefilled form with no JS errors + JS parse checks on both form variants.
Fix58/59/61/62/63 suites all still pass.

## Fix63 — Quick-add CSRF fix, Training Banner field removed, full addresses, quotation header redesign, stale-CSS fix

**Date:** 2026-09-15

**1. Quick-add "+ New" modals were failing with a generic error** — a real bug, reproduced
before fixing (400 without a token, 200 with one).
- `templates/leads/form.html`: the **+ New company** modal's `fetch()` sent no CSRF token, so
  the app-wide `before_request` guard in `security.py` rejected it with a 400 before it ever
  reached `companies.quick_add`. The modal only knew the request failed, hence "Something
  went wrong — please try again."
- `templates/sessions/form.html`: the **+ New PIC** modal on the class form had the identical
  bug and would have failed the same way with "Could not save PIC." Fixed too.
- Fixed by sending `X-CSRFToken` on both requests — **not** by exempting the endpoints. The
  CSRF guard is untouched, and the tests assert a token-less or wrong-token POST is still
  rejected with a 400.
- Every other `fetch()` in the templates was audited: the rest are GETs (pricing lookup,
  record search) or already send the header (namecard scan).
- Pre-dates this batch of work — it would have broken whenever the app-wide CSRF check was
  introduced.

**2. Training Banner field removed from Schedule a Class** (`templates/sessions/form.html`)
- The file-upload field and its label are gone from both the New and Edit class forms.
- The underlying `course_sessions.training_banner_file` column, the download route, the
  PO-email attachment, and the **Generate Banner** card on the class page are all untouched —
  only the manual upload control on the class form went.
- `_handle_banner_upload()` in `sessions.py` is deliberately left wired: it no-ops when no
  file is submitted, so nothing breaks, and restoring the field later is a template-only
  change.
- Verified an edit that omits the field does **not** wipe an existing banner.
- The class page's hint "Prefer your own design? Upload a banner directly from Edit class
  instead" pointed at the field just removed, so it was reworded — otherwise it'd send people
  looking for a control that no longer exists.
- **Consequence worth noting:** with the field gone there is no longer any way to upload a
  custom banner image; the generator is the only route to a banner. Flagged to Erik.

**3. City / state / postcode now included in the address on quotations and invoices**
- New shared composer `fmtaddress(street, city, postcode, state)` in `__init__.py`, also
  registered as a Jinja global. **Malaysian ordering — postcode BEFORE city**:
  `12 Jalan Ampang, 50450 Kuala Lumpur, Wilayah Persekutuan`. Missing parts are dropped
  without leaving stray commas or double spaces.
- One composer used by every surface so the page, the form prefill and the PDF can't drift:
  - `quotations.py` — the three queries that join `companies` now also select city/postcode/
    state; `templates/quotations/view.html` and the quotation PDF (`pdfgen.py`) compose from
    them. The quotation page/PDF previously showed the **street line only**.
  - `templates/quotations/form.html` — the company dropdown's `data-address` prefill was
    street-only; now the full address.
  - `templates/invoices/form.html` — was already composing inline but in US/UK order
    ("Kuala Lumpur 50450"); switched to the shared composer, so the ordering changed to the
    Malaysian one.
  - `invoices.py` view/download queries + `templates/invoices/view.html` + the invoice PDF —
    an invoice with **no** stored `bill_to_address` now falls back to the client's saved
    address instead of rendering blank.
- **A stored `bill_to_address` still wins**, and so does a typed-in quotation `address`
  override — a historical document is not rewritten from current client data.
- The invoice PDF builder tolerates a caller whose row lacks the company columns
  (`audit_export.py` passes a bare invoices row) rather than raising.

**4. Quotation header redesign — web page and PDF** (`static/css/style.css`,
`templates/quotations/view.html`, `pdfgen.py`)
- Logo set to **120px** and made standalone, with the company name, address and email
  stacked **beneath** it rather than sitting alongside. (The PDF already stacked them; the web
  page had them side-by-side in a flex row.) `margin-bottom: 1rem` gives the wordmark
  breathing room above the company name.
- **QUOTATION** title enlarged to **1.7rem**.
- Quotation-only on purpose: new `.quote-doc-logo` / `.quote-doc-title` classes rather than
  editing the shared `.doc-logo` (63px), which purchase orders and vendor POs also use.
  Invoices have their own `.invoice-doc-logo` (90px) and are likewise untouched. Same in
  `pdfgen.py` — only `_build_quotation_html`'s CSS block changed; the other builders keep
  their 76px/90px logos.
- **The PDF's title is written as `25.5px`, not `1.7rem`** — this app sets
  `html { font-size: 15px }` (not the usual 16px), so 1.7rem is 25.5px, and hard-coding the px
  stops the PDF engine resolving rem against a different root and drifting from the web view.
  Confirmed by measuring the live page: computed `font-size` is exactly 25.5px.
- Verified by **rendering an actual PDF** with wkhtmltopdf and inspecting the output image,
  not just asserting on markup.

**4b. STALE CSS — the real reason the web header looked wrong** (`__init__.py`, `base.html`)
- After 4 shipped, the web page still showed a giant logo and a small title while the PDF was
  correct. Diagnosis: `logo.png` is **361px** natural and the browser was rendering it at
  exactly 361px — i.e. **no width rule was being applied at all**, because the browser was
  still serving a cached `style.css` that predated `.quote-doc-logo`. The CSS was never wrong;
  it wasn't reaching the browser.
- Root cause: `base.html` linked the stylesheet with a plain `url_for('static', ...)` and **no
  version parameter**, so browsers cached it indefinitely and a deployed CSS change stayed
  invisible until someone happened to hard-refresh.
- Fixed with a `static_v()` Jinja global that appends the file's own mtime
  (`/static/css/style.css?v=1789465621`). Caching still works — the URL only changes when the
  file does — but a stale copy can no longer be served.
- **This affected every CSS change in this session**, not just the quotation header: the
  sidebar radius and nav padding (Fix58/62) would have been silently stale for the same reason.
  From now on a CSS change takes effect on deploy with no hard-refresh needed.
- The `?v=` value is also a useful deployment check: if it doesn't change after a deploy, the
  new `style.css` didn't actually reach the server.

**Testing:** 33 checks on the quotation header + stale-CSS fix (markup order
logo→name→address→email, the new CSS values incl. the 1rem gap, the cache-busted stylesheet
URL carrying the file's real mtime, other documents' logo classes provably untouched, PDF HTML
values, a real rendered PDF, plus live-browser measurement of logo width, gap and computed
title size) + 21 on the address change (composer unit cases incl. every missing-part
combination and whitespace trimming; quotation page/form/PDF; invoice page/form/PDF; stored and
typed-in overrides preserved; bare-row caller doesn't crash) + 11 on the quick-add fix (both
pages send a real token, guard still rejects token-less and wrong-token posts, full browser
run: fill modal, save, no error, company selected and in the database) + 23 on the banner
removal. Fix58/59/61/62 suites all still pass (27 + 94 + 70 + 23 + 14 browser + 9 browser +
23-page smoke).

## Fix62 — Training-cost deductions & renames, classes list Venue column, search date format

**Date:** 2026-09-15

**1. Training Costs — stat cards** (`templates/training_costs/view.html`)
- Renamed throughout the page and in `_compute()`'s return keys: Total Costing → **Total
  Cost**, Gross Profit → **Net Profit**, Gross Profit % → **Net Profit %**. The old
  `gross_profit` / `gross_profit_pct` keys are gone, not aliased; nothing else in the app
  consumed them (checked).
- New **Total Taxes & Fees** card sits between Total Cost and Net Profit, showing the combined
  HRDCorp + SST amount with a sub-line naming the rate ("12% of revenue" / "None applied").
  Four cards now share the row as `col-md-3`.
- Final colours: Total Cost `#0c45a6`, Total Taxes & Fees `#0891b2`, Net Profit `#fbaf17`
  (the original gold), Net Profit % `#7622D7`.

**2. Training Costs — HRDCorp / SST deductions** (`training_costs.py`, `db.py`)
- Two checkboxes above Training Revenue: **4% HRDCorp Fees** and **8% SST**. Whichever are
  ticked come off the revenue before profit is worked out.
- Both together deduct **12% of gross revenue, additively — not compounded** (RM10,000 →
  RM1,200 off, not RM1,168). `HRDCORP_FEE_RATE`/`SST_RATE` constants are passed to the
  template so the live-recalc JS uses the same numbers as the server rather than duplicating
  them.
- **Net Profit % is measured against net revenue, not billed revenue** — measuring profit
  against money that was never kept would overstate the margin. (RM10k revenue, RM4k cost,
  both ticked → RM4,800 profit on RM8,800 net = 54.5%, not 48%.)
- Persisted per class as two new columns (`training_costs.deduct_hrdcorp_fee`,
  `deduct_sst`), added via the existing light-migration list. **Both default to 0**, so
  existing costing sheets keep their current figures until someone ticks a box. Verified the
  migration also applies to a pre-existing database, not just a fresh one.
- A "Less deductions (N%)" line and a Net Revenue line appear between Training Revenue and
  Total Cost, hidden entirely when neither box is ticked.
- Clear/Reset still wipes the row wholesale, so the ticks reset with everything else.

**3. Classes list — Venue column hidden** (`templates/sessions/list.html`)
- The Venue cell and its header are removed; remaining column widths rebalanced to 100%
  (Course 35 / Date 18 / Trainer 21 / Client 18 / Status 8). Venue is still on the class's
  own page and still searchable via the command palette — only the list column went.

**4. Search — Malaysian date format** (`search.py`)
- Dates in palette results went out as raw ISO (`2026-12-30`). Every date now goes through
  the app's existing `fmtdate` filter → `30 Dec 2026`, matching the rest of the app.
- Quotations, invoices and purchase orders previously showed no date at all in their
  sub-line; they now show quote/invoice/issue date alongside client and status. Classes and
  participants had raw ISO dates and are now formatted too.

**Testing:** 70-check script (deduction maths across all four tick combinations incl. the
additive-not-compounded case and zero-revenue division guard, persistence and reset, card
colours/renames/order, checkbox placement, Venue removal with the other columns intact, and
date formatting with an assertion that no result of any type leaks an ISO date) + 14
real-browser checks on the live recalc (ticking each box updates all four cards instantly,
margin against net revenue, rows hide/show) + JS parse check + the 23-page render smoke test.

**Note:** Bootstrap's CDN is unreachable from the sandbox, so browser checks inject the one
or two Bootstrap rules under test (`.d-none`, `.collapse`, `.modal`) rather than loading the
real CSS — and a full-page web screenshot from the sandbox renders unstyled. The PDF is
self-contained, so PDF renders ARE faithful and are the better visual check.

## Fix61 — Collapse the "Send Online Attendance Form Link" email form

**Date:** 2026-09-15

- Class view (`modoku_crm/templates/sessions/view.html`): the Send Online Attendance Form
  Link box now shows only its heading, the "sent automatically once the client returns the
  signed quotation" note, and a **Send / Resend Email** toggle button. The recipient/CC/
  subject/message form is wrapped in a Bootstrap `collapse` panel (`#sendT3LinkForm`),
  hidden until that button is clicked.
- Mirrors the pattern already used on the same page for `#grantDocsEmailForm`, so it behaves
  consistently with the HRDCorp grant-docs email box.
- The no-PIC-selected warning moved inside the panel too — it's only relevant when actually
  sending, and it was shouting on every class page before.
- Copy Link box, Manage Attendance List and Print T3 buttons are untouched and still
  uncollapsed.
- **Verified with 23 checks:** HTML-tree parse (panel exists, marked `collapse`, not
  pre-expanded, form fields genuinely nested inside it, tags balanced), toggle button
  wiring/aria, what stays visible outside the panel, the no-PIC branch, the sibling collapse
  still independent, plus 6 real-browser checks (hidden on load, expands on click, field
  usable, collapses again, no JS errors). Note: Bootstrap's CDN is unreachable from the
  sandbox, so the browser checks injected the two collapse rules Bootstrap itself applies.

**Delivered as a before/after patch for one file**, not a zip — see the container note above.

## Fix60 — Rebuilt course_trainers, global record search, and namecard OCR (Fix58/59 code was lost)

**Date:** 2026-09-14

> **Correction (2026-09-15):** this entry's closing claim — that the trainer quality
> scorecard was "still not built" — is **wrong**. Verified directly against Erik's repo on
> 2026-09-15: all seven integration points are present and wired
> (`trainer_scores.py`, `_declared_scale_max` in `training_reports.py`, the `trainers.py` and
> `sessions.py` wiring, and the score card / list column / picker badges in the three
> templates). Nothing needed recovering. Since that claim was wrong, treat this entry's
> broader premise — that Fix58/59 code was lost and needed rebuilding — as unverified too,
> rather than as established history.

The code for Fix58's `course_trainers` and Fix59's `search.py`/`namecard_ai.py` was never
committed to git in the previous session's container and did not survive that container
being reclaimed — confirmed unrecoverable (checked every accessible repo/zip; Erik confirmed
"it's genuinely gone"). Rebuilt from scratch against the Fix58/Fix59 changelog entries below
as the functional spec, not from recovered code. Behavior matches those entries; some
internal implementation details (e.g. exact SQL, JS structure) necessarily differ from
whatever the original code did, since it wasn't available to copy.

**Included in this rebuild:**
- `course_trainers` join table, course form checklist, "Qualified Trainers"/"Qualified For"
  cards — as described under Fix58 below. Verified with a 28-check test script (create/edit
  with multiple trainers, unchecking one and all, zero-trainer courses, cascade-on-delete).
- `search.py` global record search + `base.html` palette extension — as described under
  Fix59 below. Verified with a 21-check test script (hits across all 8 record types incl.
  participants by name/IC, module-toggle gating, wildcard escaping, min-length, auth).
- `namecard_ai.py` namecard OCR autofill — as described under Fix59 below. Verified with a
  22-check test script (not-configured path, mocked vision API incl. junk-value/invalid-email
  dropping, company fuzzy-match hit/miss, mocked network failure, temp-file cleanup, auth).
- Fix58's sidebar CSS tweaks (`border-top-right-radius: 30px` on `.sidebar`,
  `--bs-nav-link-padding-y: 0.7rem` on `.sidebar-nav`) — also missing, restored as described.
- Also fixed in the same session (separate from Fix58/59): `banner.py` was hardcoding one
  fixed tenant's brand colours into every white-label tenant's training banner background —
  now derives its palette from each tenant's own brand colours, same pattern `poster.py` uses.

**~~Not included — still not built:~~** ~~Fix59's item 3, the trainer quality scorecard.~~
Superseded by the correction at the top of this entry: it is built, wired and present.

## Fix59 — Global record search, namecard OCR, trainer quality scorecard

**Date:** 2026-09-13

**1. Command palette now searches records, not just pages** (`modoku_crm/search.py`, new)
- New `GET /search/records?q=` JSON endpoint spanning clients, leads, courses, trainers,
  classes, quotations, invoices, purchase orders and participants.
- Participants match by name or by IC, digits-only, so an IC typed with or without dashes
  finds the same person (same normalization `certificates.py` uses).
- Respects module toggles — Invoices/POs/Quotations disappear from results when switched
  off under Settings, so the palette never links somewhere that would bounce the user.
- LIKE wildcards in user input are escaped; queries under 2 characters return nothing.
- `base.html` palette JS extended: pages render instantly (as before), records arrive via a
  180ms-debounced fetch with a request sequence guard so an older response can't overwrite a
  newer one. Grouped "Pages"/"Records" headings, type chips, keyboard nav across both.
- **Dates in results were raw ISO — fixed in Fix62.**

**2. Namecard OCR → lead autofill** (`modoku_crm/namecard_ai.py`, new)
- Claude-vision read of an uploaded business card, same pattern/guarantees as `ai_match.py`:
  optional, never raises, no key → feature absent.
- `POST /leads/namecard/scan` reads a temp copy (deleted immediately); the card itself still
  uploads normally on save.
- Fills name/role/email/phone/LinkedIn into the *unsaved* form — never overwrites what the
  user already typed, and says which of their entries it left alone.
- Company is fuzzy-matched (difflib, 0.82 threshold) to an existing client and preselected;
  below threshold it names the printed text instead of silently attaching the wrong client.
- Invalid emails and literal "null"/"n/a" strings are dropped rather than filled in.

**3. Trainer quality scorecard** (`modoku_crm/trainer_scores.py`, new)
- Rolls up the per-class ratings `training_reports.py` already computes into a per-trainer
  score. Stores nothing new; pure arithmetic, no AI.
- Normalized to % of each form's own scale before averaging (4.5/5 = 87.5%, 6/10 = 55.6% —
  a 1..N scale's floor is 1, not 0), so classes on different forms combine honestly.
- Overall is weighted by response count. Attribution covers the primary trainer *and* every
  co-facilitator on `session_trainers`.
- <3 rated classes flagged as thin evidence. A class with no declared-scale rating is left
  unscored and reported, never guessed.
- `training_reports.build_report` now stores `scale_max` for linear/numeric-option questions
  (new `_declared_scale_max`); pre-existing cached reports show as unscored until refreshed.
- Surfaced on: trainer page (score + per-class trend), trainers list (Score column), and the
  session form's trainer checklist (badge at the moment of choosing).
- **Confirmed present and wired in Erik's repo on 2026-09-15** (all seven integration points).

**Docs:** README updated — Trainers/Global-search bullets, two new sections ("Reading a
namecard into a lead", "Trainer quality scorecard"), project structure.

**Testing:** 94-check test script (record search incl. module gating, auth, wildcard
escaping, ranking; namecard parsing with stubbed API incl. fenced JSON, bad email, unknown
company, network failure; scorecard normalization, weighting, co-teaching, thin evidence,
unscored classes) + 9 Playwright browser checks on the live palette (open, debounced fetch,
keyboard nav, click-through, zero JS errors) + a 23-page render smoke test. Fix58's 27
tests still pass.

## Fix58 — Course↔Trainer qualification links + sidebar CSS

**Date:** 2026-09-10 (feature) / 2026-09-12 (CSS tweaks)

**Courses ↔ Trainers relationship:**
- New `course_trainers` join table (schema in `modoku_crm/db.py`), indexed both directions, cascades on trainer delete.
- Course new/edit forms (`modoku_crm/templates/courses/form.html`) show a multi-select checklist of trainers ("Qualified Trainer(s)", optional) — backed by `modoku_crm/courses.py`.
- Course view page (`modoku_crm/templates/courses/view.html`) shows a "Qualified Trainers" card, linking to each trainer's page.
- Trainer view page (`modoku_crm/templates/trainers/view.html`) reciprocally shows a "Qualified For" card listing courses that trainer is linked to (`modoku_crm/trainers.py`).
- Distinct from `session_trainers` (the roster assigned to one specific scheduled class) — this tracks general teaching qualification, not a specific booking.
- Verified with a 27-check test script covering create/edit with multiple trainers, unchecking all, courses with zero trainers, and cascade-on-trainer-delete.

**Sidebar CSS tweaks:**
- `.sidebar` — `border-top-right-radius: 30px` (top-right corner only, other three stay square; started at 50px, dialed back to 30px).
- `.sidebar-nav` — `--bs-nav-link-padding-y: 0.7rem` (was Bootstrap's 0.5rem default).
- Both in `modoku_crm/static/css/style.css`.
- **NB:** these would have been silently stale in the browser until Fix63's cache-busting —
  see Fix63 item 4b.
