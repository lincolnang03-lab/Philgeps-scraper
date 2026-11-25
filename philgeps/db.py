"""SQLite storage. One row per bid notice, keyed by PhilGEPS reference number."""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

COLUMNS = [
    # identity
    "ref_id", "url",
    # from the listing page
    "title", "procuring_entity", "site_category", "publish_date", "closing_datetime",
    # from the detail page
    "notice_type", "solicitation_number", "trade_agreement", "procurement_mode",
    "classification", "approved_budget", "delivery_period", "client_agency",
    "contact_person", "status", "area_of_delivery", "date_created", "last_updated",
    "description", "bid_supplements", "document_requests",
    # derived (rule-based)
    "sector", "sector_source", "region", "budget_bracket",
    # bookkeeping
    "first_seen_at", "detail_scraped_at", "updated_at",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
    ref_id TEXT PRIMARY KEY,
    url TEXT,
    title TEXT, procuring_entity TEXT, site_category TEXT,
    publish_date TEXT, closing_datetime TEXT,
    notice_type TEXT, solicitation_number TEXT, trade_agreement TEXT,
    procurement_mode TEXT, classification TEXT, approved_budget REAL,
    delivery_period TEXT, client_agency TEXT, contact_person TEXT, status TEXT,
    area_of_delivery TEXT, date_created TEXT, last_updated TEXT, description TEXT,
    bid_supplements INTEGER, document_requests INTEGER,
    sector TEXT, sector_source TEXT, region TEXT, budget_bracket TEXT,
    first_seen_at TEXT, detail_scraped_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notices_sector ON notices(sector);
CREATE INDEX IF NOT EXISTS idx_notices_publish ON notices(publish_date);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT,
    pages_scanned INTEGER, new_notices INTEGER, details_scraped INTEGER,
    errors INTEGER, note TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---- reads -----------------------------------------------------------
    def known_ids(self) -> Set[str]:
        return {r[0] for r in self.conn.execute("SELECT ref_id FROM notices")}

    def ids_missing_details(self) -> List[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT ref_id FROM notices WHERE detail_scraped_at IS NULL "
            "ORDER BY CAST(ref_id AS INTEGER) DESC")]

    def all_rows(self) -> List[Dict]:
        # publish_date is dd/mm/yyyy as shown on the site; sort as yyyymmdd.
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM notices ORDER BY "
            "substr(publish_date,7,4)||substr(publish_date,4,2)||substr(publish_date,1,2) DESC, "
            "CAST(ref_id AS INTEGER) DESC")]

    def get(self, ref_id: str) -> Optional[Dict]:
        r = self.conn.execute("SELECT * FROM notices WHERE ref_id=?", (ref_id,)).fetchone()
        return dict(r) if r else None

    # ---- writes ----------------------------------------------------------
    def upsert(self, row: Dict) -> bool:
        """Insert or merge a notice. Returns True if it was new."""
        existing = self.get(row["ref_id"])
        ts = now_iso()
        if existing is None:
            data = {c: row.get(c) for c in COLUMNS}
            data["first_seen_at"] = ts
            data["updated_at"] = ts
            cols = ",".join(data)
            self.conn.execute(
                "INSERT INTO notices (%s) VALUES (%s)" % (cols, ",".join("?" * len(data))),
                list(data.values()))
            return True
        # merge: only overwrite with non-empty new values
        changes = {c: row[c] for c in COLUMNS
                   if c in row and row[c] not in (None, "") and row[c] != existing.get(c)
                   and c not in ("first_seen_at",)}
        if changes:
            changes["updated_at"] = ts
            self.conn.execute(
                "UPDATE notices SET %s WHERE ref_id=?" % ",".join("%s=?" % c for c in changes),
                list(changes.values()) + [row["ref_id"]])
        return False

    def commit(self):
        self.conn.commit()

    def recategorize(self, enrich_fn) -> int:
        """Recompute derived fields (sector/region/budget) from stored data.
        Run after scraping, or after editing the rules in categories.py."""
        n = 0
        for r in self.all_rows():
            before = (r["sector"], r["sector_source"], r["region"], r["budget_bracket"])
            enriched = enrich_fn(dict(r))
            after = (enriched["sector"], enriched["sector_source"],
                     enriched.get("region"), enriched.get("budget_bracket"))
            if before != after:
                self.conn.execute(
                    "UPDATE notices SET sector=?, sector_source=?, region=?, budget_bracket=? "
                    "WHERE ref_id=?", after + (r["ref_id"],))
                n += 1
        self.conn.commit()
        return n

    def log_run(self, **kw) -> None:
        cols = ",".join(kw)
        self.conn.execute("INSERT INTO runs (%s) VALUES (%s)" % (cols, ",".join("?" * len(kw))),
                          list(kw.values()))
        self.conn.commit()

    # ---- exports ---------------------------------------------------------
    def export_csv(self, path: str | Path) -> int:
        rows = self.all_rows()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        return len(rows)

    def export_json(self, path: str | Path, rows: Optional[Iterable[Dict]] = None) -> int:
        rows = list(rows if rows is not None else self.all_rows())
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        return len(rows)

    def export_by_sector(self, path: str | Path) -> Dict[str, int]:
        grouped: Dict[str, List[Dict]] = {}
        for r in self.all_rows():
            grouped.setdefault(r["sector"] or "Uncategorized", []).append(r)
        grouped = dict(sorted(grouped.items(), key=lambda kv: -len(kv[1])))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(grouped, f, ensure_ascii=False, indent=1)
        return {k: len(v) for k, v in grouped.items()}
