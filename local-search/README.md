# Local browser rental search

Simple Python Playwright runner for Yad2 and Komo on macOS, using visible Chromium and a separate
persistent profile. No stealth plugins or CAPTCHA solving.

Yad2 has a built-in adapter based on rendered rental search results captured on
9 November 2026. The latest price-capped run on this Mac traversed all eight observed result pages
and saved 205 unique matching houses at ₪13,000–₪26,000. Earlier local app import
and reimport checks are described below. The earlier two-page unfiltered test saved 82 priced listings
and is described below for comparison. Madlan's direct results route showed an access challenge; its
adapter remains unfinished. No working Madlan extraction is claimed.

## Install

Python 3.10+ (the local test used Python 3.15):

```bash
cd local-search
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp config.example.json config.json
```

## Search Yad2

```bash
python search.py
```

The example opens Center/Sharon rental results filtered on Yad2 to houses/cottages
and duplex houses (`property=5,39`) costing ₪13,000–₪26,000 (`minPrice=13000`, `maxPrice=26000`).
Those parameter values were verified against Yad2's rendered search state. Choose the desired rental filters
in the browser, then press Enter in Terminal. Save a filtered search URL in
`config.json` to reuse it. `--no-prompt` uses the configured URL without pausing;
access challenges stop the run. Chromium remains visible.

The adapter reads address, city, price, property type, rooms and the card image.
Title combines address and city. Apartments, garden apartments and penthouses
keep their actual property types. Only explicit house labels are normalized to
house types. The card's area does not reliably distinguish building from land,
so `built_sqm` and `lot_sqm` remain null. Description is empty; detail pages are
not visited.

New-project advertisements are excluded. Explicitly unpublished prices are
skipped and counted; missing fields or unexpected price formats stop collection.
Automatic pagination waits for new listing IDs. Reordered/repeated pages stop the
run. Each complete page is saved atomically to `results.json`, deduplicated by
`(source, source_id)`. A failed page does not overwrite earlier completed pages.
The runner and app share `sharon-homes/search_filters.json`: a ₪13,000 minimum,
a ₪26,000 maximum, the configured Sharon towns/moshavim and private-house types.
The town list is explicit and editable; it is not every town in the broad Yad2
Center/Sharon region. The runner filters cards before writing `results.json`;
the app independently applies the same criteria. Environment overrides
`MIN_PRICE`, `MAX_PRICE` and `ALLOWED_CITIES` can still change server criteria.

`filter_report.json` records each page, unpriced cards, excluded listings and all
rejection reasons. Its status is `complete`, `running`, `failed` or `page_limit`.
Repeated promoted ads are deduplicated in results and in the printed rejection
summary. The default safety limit is 100 pages; reaching it with more results
remaining stops the run and prevents automatic upload. A disabled next control
marks the end of the observed results. Missing Yad2 pagination controls stop collection rather than
claiming that no results remain.

## Capture or configure another layout

```bash
python search.py --capture yad2
python search.py --capture madlan
```

Paste an actual search-results URL, handle access challenges manually, and press
Enter once the cards are visible. Capture saves rendered HTML and the final URL
under `captures/`. A landing page or challenge screen is not a usable listing
capture. HTML may contain private data; inspect it before sharing.

The original configurable selector reader remains available. A search can supply
`cards`, `next_page`, and `fields` containing relative selectors for `url`,
`title`, `city`, `price`, `property_type`, plus optional `address`, `description`
and `images`. Madlan requires these verified selectors; none are supplied yet.
No missing property type is defaulted to a house.

## Test the app locally

From the repository root, install test dependencies:

```bash
local-search/.venv/bin/python -m pip install -r local-search/requirements-dev.txt
```

In one Terminal, run the app against an isolated local database:

```bash
cd sharon-homes
DB_PATH=/tmp/sharon-local-test.sqlite APP_PASSWORD= ALLOW_LOCAL_NO_PASSWORD=1 ENABLE_SCHEDULER=0 ENABLE_EMAIL_DELIVERY=0 ../local-search/.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

In another Terminal, preview an existing result file without opening a browser:

```bash
cd local-search
.venv/bin/python search.py --upload-file results.json --site-url http://127.0.0.1:8000 --dry-run
```

Omit `--dry-run` to preview and then confirm an actual local import. Open
http://127.0.0.1:8000 to inspect the imported houses. The default filters are
₪13,000–₪26,000 inclusive, allowed Sharon towns and private-house types.

## Optional Railway upload

```bash
python search.py --upload-file results.json --site-url https://YOUR-APP-HOST
```

Or use `python search.py --upload` to collect first. Credentials are prompted for
and are not saved. The runner first checks the app's read-only import capability
endpoint, then previews the import and shows rejected records and ID conflicts.
An older app without preview support must be updated before using this flow.
An actual upload still requires typing `yes`; collection failures do not upload.

Stable Yad2 IDs are the final listing URL token, excluding region and tracking
parameters. Preview flags an existing URL with a different source ID, including
legacy Yad2 URLs without the region segment. Resolve conflicts before importing;
no automatic ID migration is performed. This does not verify IDs against a
production database that has not been inspected. Matching IDs preserve notes,
favorites and hidden status. Import never sends email.

Nothing in this change deploys or changes the running Railway app. Browser
profiles, captures, local config and results remain ignored by Git.

## Checks and known limits

```bash
cd local-search
.venv/bin/python -B -m unittest discover -s tests -v
.venv/bin/python -B search.py --help
cd ../sharon-homes
../local-search/.venv/bin/python -B -m unittest discover -s tests -v
```

Browser tests use real Chromium against small local fixtures modeled on the
observed Yad2 markup. The HTTP integration test extracts those cards, starts the
actual FastAPI app on loopback with temporary SQLite, previews and imports,
rejects apartments, and checks reimported notes/favorites/hidden state and ID
conflicts. It runs without external requests or email.

The real 82-record Yad2 output was also sent to a temporary local app: one house
passed current filters, 81 records were rejected, and reimport updated the same
house without duplication. Production upload, production ID matching, complete
site coverage and live Madlan extraction remain unverified. Website layouts and
access controls can change.

## Updated filtered live run

The initial broad two-page test was unsuitable as a measure of available houses:
79 of 82 records failed the old price range, two failed the town list and only
one passed. Most records were apartments or other property types. Native Yad2
house/price-range filters plus the shared local town filter find substantially
more relevant listings. The full filtered run reached the disabled next-page
control after 14 pages.

Yad2 can contain miscategorized records. One ₪12 million entry in its rental
feed explicitly described a sale on its detail page. That listing was manually
excluded in ignored `config.json`. A search's optional `excluded_ids` list
supports this review without silently adding an upper price cap. This is a
specific reviewed exclusion, not automated detection of every miscategorized ad.

For a single collection-and-upload command after the app is ready:

```bash
python search.py --no-prompt --upload --site-url https://YOUR-APP-HOST
```

This opens visible Chromium, paginates through the configured results, saves
matching records and a rejection report, previews the app import and asks for
confirmation before a live upload. `--no-prompt` skips only the browser-readiness
pause; it never confirms uploads or handles challenges.

Earlier local verification before the price cap (9 November 2026): 392 matches across 14 pages, 163
unique records rejected by town and one manually reviewed sale listing excluded.
All 392 were imported into the local app and rendered in its UI; reimport updated
the existing records with zero duplicates. The local browser tests, HTTP/UI
integration tests and backend tests total 28 passing tests. Email remains off.
This verification did not deploy or import anything into Railway.

## Current price cap and location filters

The current budget is **₪13,000–₪26,000 inclusive**. Yad2 search URLs include
`minPrice=13000&maxPrice=26000`, the local runner applies the shared bounds, and
the upload command re-filters even older result files before sending any data.
The app independently rejects out-of-budget imports and excludes older
out-of-budget records from its API/display and counts. Existing records, notes,
favorites and hidden state remain in SQLite.

`search_filters.json` defines editable location groups: All, Moshavim, Herzliya,
Hod HaSharon, Ramat HaSharon, Kfar Saba, Rosh HaAyin and Other cities. Rosh HaAyin
is now an allowed import town. Kibbutzim and unlisted towns fall under Other
cities; the Moshavim list is explicit rather than inferred from house type.
The site no longer displays a Search now button.

Earlier verification counts above describe earlier runs and criteria, before the
₪26,000 cap was requested. Generated `results.json` and `filter_report.json`
contain the current run.

Current verification after the price cap: 205 unique matches across eight pages;
113 unique records excluded by town. All 32 automated checks pass, including
price boundaries, filtering an older file before upload, existing database
compatibility and location tabs combined with favorites/text search on mobile.

## Komo adapter

Komo's public rental page was observed on 9 November 2026. The built-in adapter
reads `div.modaaRowAd` cards, title/city/address, numeric price, room count and
the card image. Its explicit `וילות/ בתים` category maps to `בית פרטי`; apartments
keep their own type and are rejected by the shared house filter. Card area does
not distinguish building from land area, so both area fields remain null.

The example config includes Komo's house filter `nehes=5` and observed price
parameters `fromPrice=13000&toPrice=26000`. Local/server filters still enforce the
shared town list and budget. Run just Komo with:

```bash
python search.py --source komo
```

Komo redirected page 2 to `account.komo.co.il` during the initial unsigned test.
Sign in manually through capture mode before collection; the same ignored
persistent profile retains the session:

```bash
python search.py --capture komo
```

Open the filtered rental results, use the next-page link, complete the sign-in,
then press Enter once actual listings return. The collector stops on a sign-in
redirect, page limit or missing pagination/count markup. It confirms the end
using `#paging`, the current page and the displayed result count; it does not
assume a missing next link proves completion.

Komo listing IDs are the numeric `modaaNum` query values. Canonical listing URLs
preserve that parameter and drop tracking values. The app must include this
query value in its import identity checks; the older backend treated all of
Komo's detail URLs as the same listing path. Deploy that compatibility change
before importing Komo records.

Local Chromium tests cover extraction, pagination, duplicate IDs, preserving
apartment types, missing prices, invalid query IDs and sign-in redirects.
Live authenticated pagination and production import need separate verification;
fixture tests alone do not prove them.

Live Komo verification (9 November 2026): after manual sign-in, the runner
reopened the saved browser session and completed all four pages of the native
₪13,000–₪26,000 house search. It read 62 unique listings, accepted six in the
configured town list and rejected 56 by town. This verifies authenticated live
pagination and card extraction for that run, not future availability of every ad.
