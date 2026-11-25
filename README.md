# PhilGEPS bid-notice scraper

Scrapes **open bid opportunities** from the Philippine Government Electronic
Procurement System ([notices.philgeps.gov.ph](https://notices.philgeps.gov.ph/GEPSNONPILOT/Tender/SplashOpportunitiesSearchUI.aspx?menuIndex=3&ClickFrom=OpenOpp&Result=3))
with Playwright. It then sorts every notice into a sector with plain rules
(no AI).

> PhilGEPS publishes procurement notices (Invitations to Bid, Requests for
> Quotation, Requests for Expression of Interest), not laws.

## Setup

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Usage

```bash
# First run: walk all ~100 listing pages and fetch every notice's details
python -m philgeps scrape --full --export

# Later runs: pick up only new notices (stops after 2 pages with nothing new)
python -m philgeps scrape --export

python -m philgeps stats          # counts by sector, region, budget, mode
python -m philgeps export         # data/exports/notices.csv, .json, notices_by_sector.json
python -m philgeps recategorize   # re-apply rules after editing categories.py
python -m philgeps dashboard --open   # build a static data/dashboard.html and open it
python -m philgeps serve --open       # dashboard with a live "Scrape now" button
```

`export` (and `scrape --export`) also rebuilds `data/exports/dashboard.html`.

## Dashboard

Two ways to open it:

- **`python -m philgeps serve`**: runs at http://127.0.0.1:8765, reachable
  from this computer only. The sidebar's **Scrape now** button runs an
  incremental scrape and streams progress live: pages checked, new listings
  appearing in the list as they are found (tagged *New*), and details being
  fetched. Scrapes are limited to one every 15 minutes
  (`--min-interval MINUTES`). The limit is counted from the last run in the
  database, so restarting the server doesn't reset it.
- **`python -m philgeps dashboard`**: a single self-contained HTML file with
  the notices embedded and no external requests, so it can be emailed or
  dropped into Databricks/DBFS. The Scrape button there only explains how to
  start the server.

Layout: a left sidebar holds the **Listings** and **Data** tabs and the
scrape controls. The toolbar has a search box and one **Filter & sort**
dropdown with categories on the left and options with live counts on the
right: Sort by, Closing (any / within 24 hours / within 7 days / still open
/ already closed), Published, Sector, Region, Type and Budget size. Active
filters show as removable chips. Filters apply to both tabs.

- **Listings** (the default tab): what was posted. Sort by *Newest first*
  (grouped by publish date), *Closing soonest* (all listings closing within
  24 hours come first in one group, then by deadline day), *Largest budget*
  or *Smallest budget*. Click a listing for a detail sheet with budget,
  dates, mode, area, contact and a link to PhilGEPS. Drag the sheet's header
  or flick it right to dismiss.
- **Data** (`#data`): the money, narrowed to what matters.
  - *At a glance:* total approved budget, number of listings, median budget,
    pesos up for bid this week, and the largest listing with its share of
    the total.
  - *Where the money goes:* budget by sector and by region. Click a bar to
    filter. Listings with no stated location are pinned last.
  - *Who and how big:* largest listings, number of listings per budget
    range, and top spending agencies (click to search).
  - Each chart has a **Table** view.
- Light/dark (follows the OS, with a toggle), reduced-motion,
  reduced-transparency and higher-contrast preferences are respected.

Template: [`philgeps/dashboard_template.html`](philgeps/dashboard_template.html).
Data shaping: [`philgeps/dashboard.py`](philgeps/dashboard.py).

Useful flags for `scrape`: `--max-pages N`, `--max-details N`, `--no-details`,
`--concurrency 3`, `--min-delay/--max-delay`, `--headed` (shows the browser).

Data lives in `data/philgeps.db` (SQLite, one row per notice, keyed by
PhilGEPS reference number). Re-running never creates duplicates. A notice
that leaves the open list (because it closed) stays in the database.

## What's collected

| From | Fields |
|---|---|
| Listing | reference no., title, procuring entity, site category, publish date, closing date/time |
| Notice page | notice type, procurement mode, classification (Goods / Civil Works / Consulting), approved budget (ABC), delivery period, area of delivery, solicitation no., trade agreement, contact person, status, description, bid supplements, document requests, dates created/updated |
| Derived | **sector**, sector source, region, budget bracket |

## How categorisation works

PhilGEPS uses 168 fine-grained categories. [`philgeps/categories.py`](philgeps/categories.py)
groups them into 18 sectors:

Construction & Infrastructure · Health & Medical · IT & Telecommunications ·
Food & Catering · Agriculture & Fisheries · Vehicles, Fuel & Transport Equipment ·
Logistics & Transport Services · Office Supplies & Equipment ·
Furniture, Fixtures & Appliances · Power, Water & Utilities ·
Industrial & Scientific Equipment · Security, Defense & Safety ·
Facilities & Maintenance Services · Professional & Consulting Services ·
Media, Printing & Events · Education, Sports & Culture ·
Clothing, Textiles & Personal Care · General Merchandise & Other

The rules run in this order:

1. **Specific site category:** the notice takes that category's sector.
2. **Vague or often-misused site category** (*General Merchandise*, *Services*,
   *Chemicals and Chemical Products*, *General Repair and Maintenance Services*,
   *General Contractor*, *Plastic Products*) **or a missing one:** keyword rules
   check the title, then the first 300 characters of the description. For
   example, "HEPA B VACCINE" filed under *Chemicals* becomes *Health & Medical*.
   If no keyword matches, the mapped sector is used.
3. Anything left over is `Uncategorized`.

`sector_source` records which rule decided (`site_category`, `title_keywords`
or `uncategorized`), so you can audit the results. Region comes from the Area
of Delivery (province → one of 17 regions, plus *Multiple Regions* and
*Nationwide / Other*). When that field is blank, the region is read from the
procuring entity's name ("CITY OF MAKATI", "… - REGION VII", "…, MISAMIS
ORIENTAL"). National agencies with no location in their name stay `Unknown`. Budget brackets: below ₱1M, ₱1–5M, ₱5–15M, ₱15–50M,
₱50M and above.

`tests/` checks that every one of the site's 168 categories and 87 locations
is mapped. Run `python -m unittest discover -s tests -t .`. If PhilGEPS adds
a category, the test fails and the category falls back to keyword rules.

## Reliability notes

- PhilGEPS often returns an error page ("Transaction cannot be completed at
  this time") or a redirect loop to `ErrorPage.aspx`. Much of this is
  **session affinity**: the `PHILGEPS_AFC` cookie pins a visitor to one
  backend server. When that server is broken, every request with the cookie
  fails (6 of 6 in testing, versus about half on fresh sessions). On each
  retry the scraper clears cookies, pauses all tabs and backs off (up to 3
  minutes per wait). Notices that still fail are retried on the next run.
- The same thing happens in a normal browser. If "Open on PhilGEPS" lands on
  an error or "redirected you too many times", use a private window or clear
  cookies for `notices.philgeps.gov.ph`.
- Requests are paced at 0.8–2 s per tab, 3 tabs by default. Images and fonts
  are not downloaded. Please keep it polite.
- A plain Chromium passes the site's cookie check. Don't add
  fingerprint-spoofing patches.

## How often to scrape

Nothing runs on a schedule. A scrape only happens when you run `scrape` or
press the button. Approximate load per run:

| Run | Page loads | Time |
|---|---|---|
| Full (`--full`) | about 100 listing pages + 1 per open notice (about 2,100) | about 25 min |
| Incremental (default, and the button) | 2–3 listing pages + 1 per new notice | seconds to a few minutes |

Suggested pace: incremental every 1–3 hours during office hours, and a full
re-scan at most weekly. Incremental runs don't refresh notices already
stored, so status or deadline changes only come from a full run.
