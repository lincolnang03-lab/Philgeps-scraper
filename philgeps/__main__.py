"""Command line:  python -m philgeps <command> [options]

  scrape        Fetch new notices (incremental) and their details, then categorise.
  export        Write CSV / JSON / per-sector JSON from the database.
  serve         Dashboard at http://127.0.0.1:8765 with a live "Scrape now" button.
  dashboard     Build a static dashboard HTML file.
  stats         Print counts per sector, source, region and budget bracket.
  recategorize  Re-apply the rules in categories.py to every stored notice.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from pathlib import Path

from . import scraper
from .db import Store


def _cmd_scrape(a):
    s = scraper.Settings(
        db_path=a.db, full=a.full, max_pages=a.max_pages, details=not a.no_details,
        max_details=a.max_details, concurrency=a.concurrency,
        min_delay=a.min_delay, max_delay=a.max_delay, headless=not a.headed,
    )
    stats = asyncio.run(scraper.run(s))
    print("\nDone: %d listing pages, %d new notices, %d detail pages, %d errors"
          % (stats.pages_scanned, stats.new_notices, stats.details_scraped, stats.errors))
    if stats.failed_ids:
        print("Failed (will be retried next run): %s" % ", ".join(stats.failed_ids[:20]))
    if a.export:
        _cmd_export(a)


def _cmd_export(a):
    store = Store(a.db)
    out = Path(a.out)
    n = store.export_csv(out / "notices.csv")
    store.export_json(out / "notices.json")
    per = store.export_by_sector(out / "notices_by_sector.json")
    print("Exported %d notices to %s/ (notices.csv, notices.json, notices_by_sector.json)"
          % (n, out))
    for k, v in per.items():
        print("  %5d  %s" % (v, k))
    from . import dashboard
    dashboard.build(a.db, str(out.parent / "dashboard.html"))
    print("Dashboard: %s" % (out.parent / "dashboard.html"))


def _cmd_stats(a):
    conn = sqlite3.connect(a.db)

    def table(title, sql):
        print("\n" + title)
        print("-" * len(title))
        for row in conn.execute(sql):
            print("  %6s  %s" % (row[1], row[0] if row[0] is not None else "(none)"))

    total, detailed = conn.execute(
        "SELECT COUNT(*), COUNT(detail_scraped_at) FROM notices").fetchone()
    print("Notices: %d  (with details: %d)" % (total, detailed))
    table("By sector", "SELECT sector, COUNT(*) c FROM notices GROUP BY 1 ORDER BY c DESC")
    table("Sector source", "SELECT sector_source, COUNT(*) c FROM notices GROUP BY 1 ORDER BY c DESC")
    table("By classification", "SELECT classification, COUNT(*) c FROM notices GROUP BY 1 ORDER BY c DESC")
    table("By procurement mode", "SELECT procurement_mode, COUNT(*) c FROM notices GROUP BY 1 ORDER BY c DESC")
    table("By region", "SELECT region, COUNT(*) c FROM notices GROUP BY 1 ORDER BY c DESC")
    table("By budget bracket", "SELECT budget_bracket, COUNT(*) c FROM notices GROUP BY 1 ORDER BY c DESC")
    last = conn.execute("SELECT started_at, pages_scanned, new_notices, details_scraped, errors, note "
                        "FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        print("\nLast run %s: pages=%s new=%s details=%s errors=%s %s" % last)


def _cmd_dashboard(a):
    from . import dashboard
    n = dashboard.build(a.db, a.out)
    print("Dashboard with %d notices written to %s" % (n, a.out))
    if getattr(a, "open", False):
        import webbrowser
        webbrowser.open(Path(a.out).resolve().as_uri())


def _cmd_serve(a):
    from . import server
    server.serve(a.db, host="127.0.0.1", port=a.port, min_interval=a.min_interval * 60,
                 open_browser=a.open)


def _cmd_recategorize(a):
    n = Store(a.db).recategorize(scraper.enrich)
    print("Updated %d notices" % n)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m philgeps", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="data/philgeps.db", help="SQLite path (default: %(default)s)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sc = sub.add_parser("scrape", help="fetch new notices")
    sc.add_argument("--full", action="store_true", help="walk every listing page")
    sc.add_argument("--max-pages", type=int, help="cap listing pages this run")
    sc.add_argument("--no-details", action="store_true", help="listing only, skip detail pages")
    sc.add_argument("--max-details", type=int, help="cap detail pages this run")
    sc.add_argument("--concurrency", type=int, default=3, help="parallel detail tabs (default 3)")
    sc.add_argument("--min-delay", type=float, default=0.8)
    sc.add_argument("--max-delay", type=float, default=2.0)
    sc.add_argument("--headed", action="store_true", help="show the browser window")
    sc.add_argument("--export", action="store_true", help="export files after scraping")
    sc.add_argument("--out", default="data/exports")
    sc.set_defaults(func=_cmd_scrape)

    ex = sub.add_parser("export", help="write CSV/JSON exports")
    ex.add_argument("--out", default="data/exports")
    ex.set_defaults(func=_cmd_export)

    db_ = sub.add_parser("dashboard", help="build the HTML dashboard")
    db_.add_argument("--out", default="data/dashboard.html")
    db_.add_argument("--open", action="store_true", help="open it in the browser")
    db_.set_defaults(func=_cmd_dashboard)

    sv = sub.add_parser("serve", help="dashboard with a Scrape button at http://127.0.0.1:8765")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--min-interval", type=float, default=15,
                    help="minimum minutes between scrapes (default: %(default)s)")
    sv.add_argument("--open", action="store_true", help="open it in the browser")
    sv.set_defaults(func=_cmd_serve)

    sub.add_parser("stats", help="summary counts").set_defaults(func=_cmd_stats)
    sub.add_parser("recategorize", help="re-apply category rules").set_defaults(
        func=_cmd_recategorize)

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    a.func(a)


if __name__ == "__main__":
    sys.exit(main())
