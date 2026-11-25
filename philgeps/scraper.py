"""Playwright scraper for PhilGEPS open bid opportunities.

Flow
----
1. Open the "Open Opportunities" listing (ASP.NET WebForms, 20 rows/page,
   paginated via postbacks / a page dropdown).
2. Walk pages newest-first, saving each row (ref id, title, entity, site
   category, publish & closing dates).
   * Incremental mode (default): stop after N consecutive pages that contain
     no unseen notices.
   * Full mode: walk every page.
3. Visit the "Bid Notice Abstract" page of each notice that has no details
   yet, using a few parallel tabs, and store the full record.
4. Categorise every notice with the rule-based classifier.

PhilGEPS is flaky. It intermittently returns an "Information Page"
("Transaction cannot be completed at this time"), and during outages or
throttling answers every request with a 302 to ErrorPage.aspx, which
redirects to itself (ERR_TOO_MANY_REDIRECTS). Both are treated as transient:
a shared cooldown pauses every tab, then requests retry with exponential
backoff.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright.async_api import Error as PWError  # base class; includes timeouts

from . import categories
from .db import Store, now_iso

log = logging.getLogger("philgeps")

ROOT = "https://notices.philgeps.gov.ph/GEPSNONPILOT/Tender/"
LISTING_URL = ROOT + "SplashOpportunitiesSearchUI.aspx?menuIndex=3&ClickFrom=OpenOpp&Result=3"
DETAIL_URL = ROOT + "SplashBidNoticeAbstractUI.aspx?menuIndex=3&refID={ref_id}&Result=3"

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Detail-page span id -> our column name
DETAIL_FIELDS = {
    "lblHeader": "notice_type",
    "lblDisplayReferenceNo": "ref_id",
    "lblDisplayProcuringEntity": "procuring_entity",
    "lblDisplayTitle": "title",
    "lblDisplayAOD": "area_of_delivery",
    "lblDisplaySolNumber": "solicitation_number",
    "lblDisplayTradeAgree": "trade_agreement",
    "lblDisplayProcureMode": "procurement_mode",
    "lblDisplayClass": "classification",
    "lblDisplayCategory": "site_category",
    "lblDisplayBudget": "approved_budget",
    "lblDisplayPeriod": "delivery_period",
    "lblDisplayClientAgency": "client_agency",
    "lblDisplayContactPerson": "contact_person",
    "lblDisplayStatus": "status",
    "lblDisplayBidSupplements": "bid_supplements",
    "lblDisplayDocReqList": "document_requests",
    "lblDisplayDatePublish": "publish_date",
    "lblDisplayLastUpdateTime": "last_updated",
    "lblDisplayCloseDateTime": "closing_datetime",
    "lblAbstractText": "description",
    "lblDisplayDateCreated": "date_created",
}


@dataclass
class Settings:
    db_path: str = "data/philgeps.db"
    full: bool = False                 # walk every listing page
    max_pages: Optional[int] = None    # hard cap on listing pages
    stop_after_known_pages: int = 2    # incremental stop condition
    details: bool = True               # visit detail pages
    max_details: Optional[int] = None  # cap detail pages per run
    concurrency: int = 3               # parallel detail tabs
    min_delay: float = 0.8             # politeness delay between requests (s)
    max_delay: float = 2.0
    retries: int = 6
    max_backoff: float = 180.0         # cap on a single backoff wait (s)
    nav_timeout_ms: int = 90_000
    headless: bool = True
    # Optional callback receiving progress events (dicts with a "type" key);
    # used by the local server to stream live updates to the dashboard.
    progress: Optional[Callable[[Dict], None]] = None


@dataclass
class RunStats:
    pages_scanned: int = 0
    new_notices: int = 0
    details_scraped: int = 0
    errors: int = 0
    failed_ids: List[str] = field(default_factory=list)
    new_ids: List[str] = field(default_factory=list)


def _emit(s: Settings, **event) -> None:
    if s.progress:
        try:
            s.progress(event)
        except Exception:  # a broken listener must never break the scrape
            log.debug("progress callback failed", exc_info=True)


class TransientSiteError(Exception):
    """PhilGEPS returned its generic error/'Information Page'."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _clean(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _to_float(s: Optional[str]) -> Optional[float]:
    s = (s or "").replace(",", "").strip()
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _to_int(s: Optional[str]) -> Optional[int]:
    s = (s or "").strip()
    return int(s) if s.isdigit() else None


_KNOWN_CATS = sorted({c for cs in categories.SECTORS.values() for c in cs}, key=len, reverse=True)


def split_category_and_entity(text: str):
    """Listing rows show ', <Category> ,<Procuring Entity>'. Both parts can
    contain commas, so match against the known category list first."""
    t = _clean(text).lstrip(", ").strip()
    for cat in _KNOWN_CATS:
        if t.lower().startswith(cat.lower()):
            rest = t[len(cat):].strip().lstrip(",").strip()
            return cat, rest
    if " ," in t:
        cat, rest = t.split(" ,", 1)
        return cat.strip(), rest.strip()
    return "", t


def enrich(row: Dict) -> Dict:
    """Add rule-based derived fields."""
    sector, source = categories.categorize(row.get("site_category"), row.get("title", ""),
                                           row.get("description", ""))
    row["sector"] = sector
    row["sector_source"] = source
    if row.get("area_of_delivery") or row.get("procuring_entity"):
        region = categories.region_for(row.get("area_of_delivery"))
        if region == "Unknown":
            region = categories.region_from_entity(row.get("procuring_entity"))
        row["region"] = region
    if "approved_budget" in row:
        row["budget_bracket"] = categories.budget_bracket(row.get("approved_budget"))
    return row


async def _polite_pause(s: Settings):
    await asyncio.sleep(random.uniform(s.min_delay, s.max_delay))


class Cooldown:
    """Shared back-off: when any tab hits a site error, every tab waits."""

    def __init__(self):
        self._until = 0.0

    def trip(self, seconds: float):
        loop = asyncio.get_event_loop()
        self._until = max(self._until, loop.time() + seconds)

    async def wait(self):
        loop = asyncio.get_event_loop()
        delay = self._until - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)


COOLDOWN = Cooldown()


def _backoff(attempt: int, s: Settings) -> float:
    return min(s.max_backoff, 5 * 2 ** (attempt - 1)) + random.uniform(0, 3)


async def _reset_affinity(page: Page) -> None:
    """PhilGEPS pins each session to one backend via its PHILGEPS_AFC cookie.
    When that backend is broken, every request with the cookie fails (tested:
    6/6 failures on a pinned bad session vs. ~50% on fresh ones), so drop the
    cookies before retrying to get assigned a different backend."""
    try:
        await page.context.clear_cookies()
    except PWError:
        pass


async def _is_error_page(page: Page) -> bool:
    return await page.locator("#ErrorDetailLabel").count() > 0


# ---------------------------------------------------------------------------
# browser
# ---------------------------------------------------------------------------
async def _new_context(browser: Browser) -> BrowserContext:
    ctx = await browser.new_context(
        user_agent=USER_AGENT,
        locale="en-PH",
        timezone_id="Asia/Manila",
        viewport={"width": 1366, "height": 850},
        extra_http_headers={"Accept-Language": "en-PH,en;q=0.9"},
    )
    # A stock Chromium passes PhilGEPS's cookie check (PHILGEPS_AFC); no
    # fingerprint patching is needed, and patching only risks tripping it.

    # Skip heavy assets we don't need (faster, lighter on the server).
    async def _route(route):
        if route.request.resource_type in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()
    await ctx.route("**/*", _route)
    return ctx


# ---------------------------------------------------------------------------
# listing pages
# ---------------------------------------------------------------------------
LISTING_JS = r"""
() => {
  const rows = [...document.querySelectorAll('#dgSearchResult tr.GridItem, #dgSearchResult tr.GridAltItem')];
  const out = rows.map(tr => {
    const tds = tr.querySelectorAll('td');
    const a = tr.querySelector('a[id$="hyLinkTitle"]');
    const span = tr.querySelector('span[id$="lblOrgAndBusCat"]');
    const m = a ? a.getAttribute('href').match(/refID=(\d+)/) : null;
    return {
      idx: tds[0] ? tds[0].innerText.trim() : '',
      publish: tds[1] ? tds[1].innerText.trim() : '',
      closing: tds[2] ? tds[2].innerText.trim() : '',
      ref_id: m ? m[1] : null,
      title: a ? a.innerText : '',
      meta: span ? span.innerText : ''
    };
  });
  const sel = document.querySelector('#pgCtrlDetailedSearch_pageDropDownList');
  const totalTxt = (document.body.innerText.match(/([\d,]+)\s+opportunities found/) || [])[1] || '0';
  return {
    rows: out,
    page: sel ? parseInt(sel.options[sel.selectedIndex].text, 10) : 1,
    pages: sel ? sel.options.length : 1,
    total: parseInt(totalTxt.replace(/,/g, ''), 10)
  };
}
"""


async def _load_listing_page(page: Page, n: int, s: Settings) -> Dict:
    """Navigate to listing page n (1-based) with retries."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, s.retries + 1):
        await COOLDOWN.wait()
        try:
            await page.goto(LISTING_URL, wait_until="domcontentloaded")
            if await _is_error_page(page):
                raise TransientSiteError("error page on listing")
            data = await page.evaluate(LISTING_JS)
            if data["page"] != n:
                async with page.expect_navigation(wait_until="domcontentloaded"):
                    await page.select_option("#pgCtrlDetailedSearch_pageDropDownList", label=str(n))
                if await _is_error_page(page):
                    raise TransientSiteError("error page on listing page %d" % n)
                data = await page.evaluate(LISTING_JS)
            if data["page"] != n or not data["rows"]:
                raise TransientSiteError("listing page %d did not load (got %s)" % (n, data["page"]))
            return data
        except (TransientSiteError, PWError) as e:
            last_exc = e
            wait = _backoff(attempt, s)
            log.warning("listing page %d attempt %d failed: %s; retrying in %.0fs",
                        n, attempt, str(e).splitlines()[0][:120], wait)
            COOLDOWN.trip(wait)
            await _reset_affinity(page)
            _emit(s, type="retry", wait=round(wait))
    raise RuntimeError("giving up on listing page %d: %s" % (n, last_exc))


async def _next_listing_page(page: Page, current: int, s: Settings) -> Dict:
    """Go to current+1 via the <Next> postback; fall back to the dropdown."""
    target = current + 1
    try:
        async with page.expect_navigation(wait_until="domcontentloaded"):
            await page.click("#pgCtrlDetailedSearch_nextLB")
        if not await _is_error_page(page):
            data = await page.evaluate(LISTING_JS)
            if data["page"] == target and data["rows"]:
                return data
    except PWError:
        pass
    return await _load_listing_page(page, target, s)


async def crawl_listing(ctx: BrowserContext, store: Store, s: Settings, stats: RunStats):
    page = await ctx.new_page()
    page.set_default_timeout(s.nav_timeout_ms)
    known = store.known_ids()
    data = await _load_listing_page(page, 1, s)
    log.info("listing: %d open opportunities across %d pages", data["total"], data["pages"])
    pages_without_new = 0
    while True:
        n = data["page"]
        new_here = 0
        for r in data["rows"]:
            if not r["ref_id"]:
                continue
            cat, entity = split_category_and_entity(r["meta"])
            row = enrich({
                "ref_id": r["ref_id"],
                "url": DETAIL_URL.format(ref_id=r["ref_id"]),
                "title": _clean(r["title"]),
                "procuring_entity": entity,
                "site_category": cat,
                "publish_date": r["publish"],
                "closing_datetime": r["closing"],
            })
            if store.upsert(row):
                new_here += 1
                stats.new_ids.append(r["ref_id"])
                _emit(s, type="new", row=row)
            known.add(r["ref_id"])
        store.commit()
        stats.pages_scanned += 1
        stats.new_notices += new_here
        log.info("listing page %d/%d: %d rows, %d new", n, data["pages"], len(data["rows"]), new_here)
        _emit(s, type="listing", page=n, pages=data["pages"], new=stats.new_notices)

        pages_without_new = 0 if new_here else pages_without_new + 1
        if n >= data["pages"]:
            break
        if s.max_pages and stats.pages_scanned >= s.max_pages:
            log.info("reached --max-pages=%d", s.max_pages)
            break
        if not s.full and pages_without_new >= s.stop_after_known_pages:
            log.info("incremental: %d pages in a row with nothing new, stopping",
                     pages_without_new)
            break
        await _polite_pause(s)
        data = await _next_listing_page(page, n, s)
    await page.close()


# ---------------------------------------------------------------------------
# detail pages
# ---------------------------------------------------------------------------
DETAIL_JS = """
(ids) => {
  const out = {};
  for (const id of ids) {
    const el = document.getElementById(id);
    if (el) out[id] = el.innerText;
  }
  return out;
}
"""


async def scrape_detail(page: Page, ref_id: str, s: Settings) -> Dict:
    url = DETAIL_URL.format(ref_id=ref_id)
    last_exc: Optional[Exception] = None
    for attempt in range(1, s.retries + 1):
        await COOLDOWN.wait()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            if await _is_error_page(page):
                raise TransientSiteError("error page")
            raw = await page.evaluate(DETAIL_JS, list(DETAIL_FIELDS))
            if not raw.get("lblDisplayReferenceNo"):
                raise TransientSiteError("detail fields missing")
            row = {col: _clean(raw.get(span)) for span, col in DETAIL_FIELDS.items()}
            row["ref_id"] = ref_id
            row["url"] = url
            row["approved_budget"] = _to_float(row["approved_budget"])
            row["bid_supplements"] = _to_int(row["bid_supplements"])
            row["document_requests"] = _to_int(row["document_requests"])
            row["detail_scraped_at"] = now_iso()
            return enrich(row)
        except (TransientSiteError, PWError) as e:
            last_exc = e
            wait = _backoff(attempt, s)
            log.info("detail %s attempt %d failed: %s; pausing all tabs %.0fs",
                     ref_id, attempt, str(e).splitlines()[0][:120], wait)
            COOLDOWN.trip(wait)
            await _reset_affinity(page)
            _emit(s, type="retry", wait=round(wait))
    raise RuntimeError("detail %s failed: %s" % (ref_id, last_exc))


async def scrape_details(ctx: BrowserContext, store: Store, s: Settings, stats: RunStats):
    todo = store.ids_missing_details()
    if s.max_details is not None:
        todo = todo[: s.max_details]
    if not todo:
        log.info("details: nothing to fetch")
        return
    log.info("details: fetching %d notices with %d tabs", len(todo), s.concurrency)
    queue: asyncio.Queue = asyncio.Queue()
    for i in todo:
        queue.put_nowait(i)
    total = len(todo)

    async def worker(wid: int):
        page = await ctx.new_page()
        page.set_default_timeout(s.nav_timeout_ms)
        while True:
            try:
                ref_id = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                row = await scrape_detail(page, ref_id, s)
                store.upsert(row)
                stats.details_scraped += 1
                _emit(s, type="details", done=stats.details_scraped, total=total)
                if stats.details_scraped % 25 == 0 or stats.details_scraped == total:
                    store.commit()
                    log.info("details: %d/%d done", stats.details_scraped, total)
            except Exception as e:  # keep going; it will be retried next run
                stats.errors += 1
                stats.failed_ids.append(ref_id)
                log.warning("details: %s", e)
            await _polite_pause(s)
        await page.close()

    await asyncio.gather(*(worker(i) for i in range(max(1, s.concurrency))))
    store.commit()


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
async def run(s: Settings) -> RunStats:
    store = Store(s.db_path)
    stats = RunStats()
    started = now_iso()
    note = ""
    _emit(s, type="start")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=s.headless)
            ctx = await _new_context(browser)
            try:
                await crawl_listing(ctx, store, s, stats)
                if s.details:
                    await scrape_details(ctx, store, s, stats)
                store.recategorize(enrich)
            finally:
                await ctx.close()
                await browser.close()
    except Exception as e:
        note = "aborted: %s" % e
        stats.errors += 1
        _emit(s, type="error", message=str(e).splitlines()[0][:200] if str(e) else type(e).__name__)
        raise
    finally:
        store.commit()
        store.log_run(started_at=started, finished_at=now_iso(),
                      pages_scanned=stats.pages_scanned, new_notices=stats.new_notices,
                      details_scraped=stats.details_scraped, errors=stats.errors, note=note)
    _emit(s, type="done", new=stats.new_notices, details=stats.details_scraped,
          errors=stats.errors, pages=stats.pages_scanned, newIds=stats.new_ids)
    return stats
