"""Local server for the dashboard, with a "Scrape now" button.

    python -m philgeps serve            ->  http://127.0.0.1:8765

Routes
  GET  /                    dashboard (built fresh from the database)
  GET  /api/data            dashboard payload as JSON
  GET  /api/status          {running, lastFinishedAt, nextAllowedIn, minInterval}
  POST /api/scrape          start an incremental scrape (202), or 409 running / 429 too soon
  GET  /api/scrape/events   Server-Sent Events: live progress of the current/last scrape

Only binds to localhost. Scrapes are rate-limited (default: one every 15
minutes, counted from the last run in the database, so restarting the server
does not reset it) to keep the load on PhilGEPS light.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
from urllib.parse import urlparse

from . import dashboard, scraper

log = logging.getLogger("philgeps.server")


class ScrapeJob:
    """One scrape at a time; keeps the event log of the latest run for SSE clients."""

    def __init__(self, db_path: str, min_interval: float):
        self.db_path = db_path
        self.min_interval = min_interval
        self.cond = threading.Condition()
        self.running = False
        self.run_id = 0
        self.events: List[Dict] = []
        self.last_finished: Optional[float] = self._last_finished_from_db()

    def _last_finished_from_db(self) -> Optional[float]:
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute("SELECT finished_at FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            conn.close()
            if row and row[0]:
                return datetime.fromisoformat(row[0]).timestamp()
        except sqlite3.Error:
            pass
        return None

    def wait_seconds(self) -> int:
        if self.last_finished is None:
            return 0
        return max(0, int(self.last_finished + self.min_interval - time.time()))

    def status(self) -> Dict:
        with self.cond:
            return {
                "running": self.running,
                "runId": self.run_id,
                "lastFinishedAt": (datetime.fromtimestamp(self.last_finished, timezone.utc)
                                   .isoformat() if self.last_finished else None),
                "nextAllowedIn": 0 if self.running else self.wait_seconds(),
                "minInterval": int(self.min_interval),
            }

    def _push(self, event: Dict) -> None:
        if event.get("type") == "new":
            event = {"type": "new", "notice": dashboard.compact(event["row"])}
        event["at"] = time.time()
        with self.cond:
            self.events.append(event)
            self.cond.notify_all()

    def start(self) -> Dict:
        with self.cond:
            if self.running:
                return {"ok": False, "reason": "running"}
            wait = self.wait_seconds()
            if wait > 0:
                return {"ok": False, "reason": "cooldown", "nextAllowedIn": wait}
            self.running = True
            self.run_id += 1
            self.events = []
        threading.Thread(target=self._run, name="scrape", daemon=True).start()
        return {"ok": True, "runId": self.run_id}

    def _run(self) -> None:
        settings = scraper.Settings(db_path=self.db_path, concurrency=2, progress=self._push)
        try:
            asyncio.run(scraper.run(settings))
        except Exception as e:  # already reported as an "error" event by the scraper
            log.warning("scrape failed: %s", e)
            if not any(ev.get("type") == "error" for ev in self.events):
                self._push({"type": "error", "message": str(e)[:200]})
        finally:
            with self.cond:
                self.running = False
                self.last_finished = time.time()
                self.events.append({"type": "end", "at": time.time()})
                self.cond.notify_all()


def make_handler(db_path: str, job: ScrapeJob):
    class Handler(BaseHTTPRequestHandler):
        server_version = "philgeps"

        def log_message(self, fmt, *args):  # quieter console
            log.debug("%s " + fmt, self.address_string(), *args)

        # ---- helpers -------------------------------------------------------
        def _send(self, status: int, body: bytes, ctype: str, extra: Optional[Dict] = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            return urlparse(origin).netloc == self.headers.get("Host")

        # ---- routes --------------------------------------------------------
        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                html = dashboard.render_html(dashboard.build_payload(db_path), server=True)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/api/data":
                return self._send(200, dashboard.to_json(dashboard.build_payload(db_path)).encode("utf-8"),
                                  "application/json; charset=utf-8")
            if path == "/api/status":
                return self._json(200, job.status())
            if path == "/api/scrape/events":
                return self._events()
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if urlparse(self.path).path != "/api/scrape":
                return self._json(404, {"error": "not found"})
            if not self._same_origin():
                return self._json(403, {"error": "cross-origin request refused"})
            result = job.start()
            if result["ok"]:
                return self._json(202, result)
            self._json(409 if result["reason"] == "running" else 429, result)

        def _events(self):
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            sent, run_id = 0, job.run_id
            try:
                while True:
                    with job.cond:
                        if job.run_id != run_id:      # a new run started: replay it from the top
                            sent, run_id = 0, job.run_id
                        if sent >= len(job.events):
                            job.cond.wait(timeout=15)
                        batch = job.events[sent:]
                        sent += len(batch)
                    if not batch:
                        self.wfile.write(b": keep-alive\n\n")
                    for ev in batch:
                        self.wfile.write(("data: %s\n\n" % json.dumps(ev, ensure_ascii=False)).encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8765,
          min_interval: float = 15 * 60, open_browser: bool = False) -> None:
    job = ScrapeJob(db_path, min_interval)
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path, job))
    httpd.daemon_threads = True
    url = "http://%s:%d/" % (host, port)
    print("Dashboard: %s   (Ctrl+C to stop)" % url)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
