# Local browser search — unfinished draft

Simple Python Playwright runner for macOS. No lambdas, stealth plugins or CAPTCHA
solving. It uses a visible browser with a separate saved profile. No Apify required.

**Yad2/Madlan selectors are NOT verified or supplied.** Capture real pages first.
This is a configurable runner, not a working site integration yet. Only the helper
unit tests and CLI have been validated; no live extraction or macOS test is claimed.

## Install on your Mac (Python 3.10+)

In this folder, run:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

No sudo needed. Activate `.venv` each time you open a new Terminal.

## Capture real pages first

```bash
python search.py --capture yad2
python search.py --capture madlan
```

Paste the search URL. Open results normally, handle login/CAPTCHA manually, then
press Enter in Terminal. Files go under `captures/`. Review HTML before sharing:
it can contain personal information or tokens. Never share the `profile/` folder.
Captures, browser profile, configuration, and results are ignored by Git.

## Collection (after configuring verified selectors)

```bash
cp config.example.json config.json
python search.py
```

Fill `cards` and `next_page` with verified CSS selectors. Field selectors are
relative to a listing card. Price must select only the price; city/property type
must select separate Hebrew values accepted by the server. Optional fields may
stay empty. Add a search object with `source: "madlan"` for Madlan's own selectors.
Set rental search filters in the browser before pressing Enter.

Saves unique records to `results.json` after each page. Stops on unreadable cards
or repeated pages; already completed pages stay saved. Five pages per search is
the default. Missing next buttons might indicate a changed layout, not the end of
results. Virtualized/lazy-loaded lists or detail-only fields need site-specific
work. A completed run does not prove complete collection. Stable IDs come from
the URL's final path component; verify these match previous imports before upload.

## Optional upload

```bash
python search.py --upload
```

After successful collection, enter the app's HTTPS URL and login credentials.
Password is hidden and not saved. Explicit confirmation is required. Upload uses
`/api/import`; matching `(source, source_id)` records preserve notes, favorites
and hidden status. Server price/city/property filters still apply. No email is
sent. No upload happens by default or after a collection error.

## Basic checks

```bash
python -B -m unittest discover -s tests -v
python -B search.py --help
```
