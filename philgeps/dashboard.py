"""Build a self-contained HTML dashboard from the SQLite database.

The page is a single file (no server, no external requests): the notices are
embedded as JSON and all filtering/aggregation happens in the browser.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from . import categories

TEMPLATE = Path(__file__).with_name("dashboard_template.html")

# Order classifications so colours stay fixed per entity, not per rank.
CLASSIFICATION_ORDER = ["Goods", "Civil Works", "Goods - General Support Services",
                        "Consulting Services"]
DESC_CHARS = 600


def _iso_date(s: Optional[str]) -> Optional[str]:
    """'19/11/2026' -> '2026-11-19'."""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s or "")
    return "%s-%02d-%02d" % (m.group(3), int(m.group(2)), int(m.group(1))) if m else None


def _iso_datetime(s: Optional[str]) -> Optional[str]:
    """'26/11/2026 1:00 AM' -> '2026-11-26T01:00' (Philippine time, no tz)."""
    s = (s or "").strip()
    for fmt in ("%d/%m/%Y %I:%M %p", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            continue
    return None


def _days(period: Optional[str]) -> Optional[int]:
    m = re.match(r"\s*(\d+)", period or "")
    return int(m.group(1)) if m else None


def compact(r: Dict) -> Dict:
    """One notice row (DB or freshly scraped) -> the short-keyed record the page uses."""
    desc = (r.get("description") or "").strip()
    # The description usually repeats the title first; keep it only if it adds text.
    if desc.lower() == (r.get("title") or "").strip().lower():
        desc = ""
    return {
        "id": r["ref_id"],
        "t": r.get("title") or "",
        "e": r.get("procuring_entity") or "",
        "s": r.get("sector") or categories.UNCATEGORIZED,
        "ss": r.get("sector_source") or "",
        "c": r.get("site_category") or "",
        "r": r.get("region") or "Unknown",
        "a": r.get("area_of_delivery") or "",
        "k": r.get("classification") or "Not stated",
        "m": r.get("procurement_mode") or "Not stated",
        "nt": r.get("notice_type") or "",
        "b": r.get("approved_budget"),
        "bb": r.get("budget_bracket") or "Not stated",
        "p": _iso_date(r.get("publish_date")),
        "cl": _iso_datetime(r.get("closing_datetime")),
        "dp": _days(r.get("delivery_period")),
        "sol": r.get("solicitation_number") or "",
        "st": r.get("status") or "",
        "cp": r.get("contact_person") or "",
        "d": desc[:DESC_CHARS] + ("…" if len(desc) > DESC_CHARS else ""),
        "u": r.get("url") or "",
    }


def build_payload(db_path: str) -> Dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM notices")]
    last_run = conn.execute(
        "SELECT finished_at FROM runs WHERE note IS NULL OR note = '' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    notices: List[Dict] = [compact(r) for r in rows]

    def ordered(values, preferred):
        present = set(values)
        head = [v for v in preferred if v in present]
        tail = sorted(present - set(head))
        return head + tail

    return {
        "generatedAt": datetime.now().strftime("%Y-%m-%dT%H:%M"),
        "lastScrape": last_run[0] if last_run else None,
        "sectors": ordered([n["s"] for n in notices], list(categories.SECTORS)
                           + [categories.UNCATEGORIZED]),
        "regions": ordered([n["r"] for n in notices], list(categories.REGIONS)
                           + ["Multiple Regions", "Unknown"]),
        "classifications": ordered([n["k"] for n in notices], CLASSIFICATION_ORDER),
        "budgetBrackets": [label for _, label in categories.BUDGET_BRACKETS] + ["Not stated"],
        "notices": notices,
    }


def to_json(obj) -> str:
    # "</" is escaped so the JSON is safe inside a <script> element.
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def render_html(payload: Dict, server: bool = False) -> str:
    """server=True enables the Scrape button (the page talks to `philgeps serve`)."""
    return (TEMPLATE.read_text(encoding="utf-8")
            .replace("__SERVER__", "true" if server else "false")
            .replace("__DATA__", to_json(payload)))


def build(db_path: str, out_path: str) -> int:
    payload = build_payload(db_path)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(payload), encoding="utf-8")
    return len(payload["notices"])
