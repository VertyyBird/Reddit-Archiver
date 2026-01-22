#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import calendar
import html
import json
import random
import re
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingTCPServer
from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse

import feedparser
import requests

USER_AGENT = "reddit-rss-archiver/4.0 (personal use; LAN dashboard)"
WAYBACK_SAVE_PREFIX = "https://web.archive.org/save/"
WAYBACK_AVAIL_ENDPOINT = "https://archive.org/wayback/available"

ARCHIVE_TODAY_BASE = "https://archive.vn"
ARCHIVE_TODAY_SUBMIT = f"{ARCHIVE_TODAY_BASE}/submit/"

REDDIT_ID_RE = re.compile(r"/comments/([^/]+)/", re.IGNORECASE)

# -------------------------
# DB schema + migrations
# -------------------------

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS posts (
  reddit_id TEXT PRIMARY KEY,
  subreddit TEXT NOT NULL,
  created_utc INTEGER,

  title TEXT,
  reddit_url TEXT,

  url_www TEXT NOT NULL,
  url_old TEXT NOT NULL,

  -- Wayback snapshot info (may be filled by submit headers OR verification)
  wayback_www TEXT,
  wayback_old TEXT,
  wayback_www_ts TEXT,
  wayback_old_ts TEXT,
  wayback_www_status TEXT,
  wayback_old_status TEXT,

  -- Wayback verification status (Level B)
  wayback_www_submit_ts TEXT,
  wayback_old_submit_ts TEXT,
  wayback_www_ok INTEGER,
  wayback_old_ok INTEGER,
  wayback_www_checked_at TEXT,
  wayback_old_checked_at TEXT,

  -- Archive.today-ish best-effort (we treat having a stored link as success)
  atoday_www TEXT,
  atoday_old TEXT,
  atoday_www_ok INTEGER,
  atoday_old_ok INTEGER,
  atoday_www_checked_at TEXT,
  atoday_old_checked_at TEXT,

  -- Per-leg errors
  err_wayback_www TEXT,
  err_wayback_old TEXT,
  err_atoday_www TEXT,
  err_atoday_old TEXT,
  err_wayback_avail_www TEXT,
  err_wayback_avail_old TEXT,

  inserted_at TEXT NOT NULL
);
"""

REQUIRED_COLUMNS: dict[str, str] = {
    "title": "TEXT",
    "reddit_url": "TEXT",
    "wayback_www": "TEXT",
    "wayback_old": "TEXT",
    "wayback_www_ts": "TEXT",
    "wayback_old_ts": "TEXT",
    "wayback_www_status": "TEXT",
    "wayback_old_status": "TEXT",
    "wayback_www_submit_ts": "TEXT",
    "wayback_old_submit_ts": "TEXT",
    "wayback_www_ok": "INTEGER",
    "wayback_old_ok": "INTEGER",
    "wayback_www_checked_at": "TEXT",
    "wayback_old_checked_at": "TEXT",
    "atoday_www": "TEXT",
    "atoday_old": "TEXT",
    "atoday_www_ok": "INTEGER",
    "atoday_old_ok": "INTEGER",
    "atoday_www_checked_at": "TEXT",
    "atoday_old_checked_at": "TEXT",
    "err_wayback_www": "TEXT",
    "err_wayback_old": "TEXT",
    "err_atoday_www": "TEXT",
    "err_atoday_old": "TEXT",
    "err_wayback_avail_www": "TEXT",
    "err_wayback_avail_old": "TEXT",
}

# -------------------------
# Config
# -------------------------

@dataclass
class Settings:
    interval: int = 180
    scan_limit: int = 25
    out_json: str = "latest_archives.json"
    json_limit: int = 25

    do_wayback: bool = True
    do_archive_today: bool = True

    delay_wayback: float = 5.0
    delay_atoday: float = 8.0

    verify_batch: int = 40
    verify_min_age: int = 60
    verify_recheck_interval: int = 900

    dashboard_enabled: bool = False
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080


def parse_bool(s: str) -> bool:
    v = s.strip().lower()
    return v in {"1", "true", "yes", "y", "on"}


def parse_config(path: str) -> tuple[Settings, list[str]]:
    settings = Settings()
    subs: list[str] = []

    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                if line.startswith("#") or line.startswith(";"):
                    continue

                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip().lower()
                    val = val.strip()

                    if key == "interval":
                        settings.interval = int(val)
                    elif key == "scan_limit":
                        settings.scan_limit = int(val)
                    elif key == "out_json":
                        settings.out_json = val
                    elif key == "json_limit":
                        settings.json_limit = int(val)

                    elif key == "do_wayback":
                        settings.do_wayback = parse_bool(val)
                    elif key in {"do_archive_today", "do_atoday"}:
                        settings.do_archive_today = parse_bool(val)

                    elif key == "delay_wayback":
                        settings.delay_wayback = float(val)
                    elif key == "delay_atoday":
                        settings.delay_atoday = float(val)

                    elif key == "verify_batch":
                        settings.verify_batch = int(val)
                    elif key == "verify_min_age":
                        settings.verify_min_age = int(val)
                    elif key == "verify_recheck_interval":
                        settings.verify_recheck_interval = int(val)

                    elif key == "dashboard_enabled":
                        settings.dashboard_enabled = parse_bool(val)
                    elif key == "dashboard_host":
                        settings.dashboard_host = val
                    elif key == "dashboard_port":
                        settings.dashboard_port = int(val)

                    else:
                        # Unknown key: ignore
                        pass
                    continue

                # Otherwise, subreddit name
                name = line.replace("r/", "").replace("/r/", "").strip()
                if name:
                    subs.append(name)

    except FileNotFoundError:
        pass

    # Dedupe while keeping order
    seen = set()
    deduped: list[str] = []
    for s in subs:
        k = s.lower()
        if k not in seen:
            seen.add(k)
            deduped.append(s)

    return settings, deduped


# -------------------------
# Helpers
# -------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ts14() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def ts14_to_epoch(ts14: str) -> int | None:
    try:
        dt = datetime.strptime(ts14, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def iso_to_epoch(iso: str) -> int | None:
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def canonicalise_reddit_post_url(url: str) -> str:
    p = urlparse(url)
    scheme = "https"
    netloc = p.netloc or "www.reddit.com"
    path = p.path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", "", ""))


def to_reddit_view(url: str, view: str) -> str:
    p = urlparse(url)
    netloc = "www.reddit.com" if view == "www" else "old.reddit.com"
    return urlunparse(("https", netloc, p.path, "", "", ""))


def extract_reddit_id(url: str) -> str | None:
    m = REDDIT_ID_RE.search(url)
    return m.group(1) if m else None


def polite_sleep(base_seconds: float) -> None:
    time.sleep(base_seconds + random.uniform(0.2, 1.2))


def rss_entry_created_utc(entry) -> int | None:
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return int(calendar.timegm(entry.published_parsed))
    if hasattr(entry, "updated_parsed") and entry.updated_parsed:
        return int(calendar.timegm(entry.updated_parsed))
    return None


# -------------------------
# DB functions
# -------------------------

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(CREATE_TABLE_SQL)
    conn.commit()
    migrate_db(conn)
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cols = set()
    for row in conn.execute(f"PRAGMA table_info({table})"):
        cols.add(row["name"])
    return cols


def migrate_db(conn: sqlite3.Connection) -> None:
    existing = table_columns(conn, "posts")
    for col, coltype in REQUIRED_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE posts ADD COLUMN {col} {coltype}")
    conn.commit()


def seen(conn: sqlite3.Connection, reddit_id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM posts WHERE reddit_id=?", (reddit_id,))
    return cur.fetchone() is not None


def insert_post(
    conn: sqlite3.Connection,
    subreddit: str,
    reddit_id: str,
    title: str,
    reddit_url: str,
    url_www: str,
    url_old: str,
    created_utc: int | None,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO posts
           (reddit_id, subreddit, created_utc, title, reddit_url, url_www, url_old, inserted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (reddit_id, subreddit, created_utc, title, reddit_url, url_www, url_old, now_iso()),
    )
    conn.commit()


def update_fields(conn: sqlite3.Connection, reddit_id: str, **fields: Any) -> None:
    if not fields:
        return
    keys = []
    vals = []
    for k, v in fields.items():
        keys.append(f"{k}=?")
        vals.append(v)
    vals.append(reddit_id)
    sql = f"UPDATE posts SET {', '.join(keys)} WHERE reddit_id=?"
    conn.execute(sql, tuple(vals))
    conn.commit()


# -------------------------
# Archiving
# -------------------------

def submit_wayback(session: requests.Session, url: str, timeout: int = 45) -> tuple[bool, str | None, str | None]:
    try:
        r = session.get(WAYBACK_SAVE_PREFIX + url, timeout=timeout, allow_redirects=False)

        if "Content-Location" in r.headers:
            loc = r.headers["Content-Location"].strip()
            if loc.startswith("/"):
                return True, "https://web.archive.org" + loc, None
            return True, loc, None

        if "Location" in r.headers:
            loc = r.headers["Location"].strip()
            if loc.startswith("/"):
                return True, "https://web.archive.org" + loc, None
            if "web.archive.org" in loc:
                return True, loc, None

        if 200 <= r.status_code < 300:
            return True, None, None

        return False, None, f"Wayback HTTP {r.status_code}"
    except Exception as e:
        return False, None, f"Wayback exception: {e}"


def wayback_availability(
    session: requests.Session,
    url: str,
    timestamp: str | None,
    timeout: int = 30,
) -> tuple[bool, str | None, str | None, str | None, str | None]:
    try:
        params = {"url": url}
        if timestamp:
            params["timestamp"] = timestamp

        r = session.get(WAYBACK_AVAIL_ENDPOINT, params=params, timeout=timeout)
        if not (200 <= r.status_code < 300):
            return False, None, None, None, f"Wayback available HTTP {r.status_code}"

        data = r.json()
        closest = (data.get("archived_snapshots") or {}).get("closest") or {}
        available = bool(closest.get("available"))
        snap_url = closest.get("url")
        snap_ts = closest.get("timestamp")
        snap_status = closest.get("status")
        return available, snap_url, snap_ts, snap_status, None

    except Exception as e:
        return False, None, None, None, f"Wayback available exception: {e}"


def submit_archive_today(session: requests.Session, url: str, timeout: int = 45) -> tuple[bool, str | None, str | None]:
    try:
        r = session.post(
            ARCHIVE_TODAY_SUBMIT,
            data={"url": url},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=timeout,
            allow_redirects=False,
        )

        if r.status_code in (301, 302, 303, 307, 308) and "Location" in r.headers:
            loc = r.headers["Location"].strip()
            if loc.startswith("/"):
                return True, ARCHIVE_TODAY_BASE + loc, None
            return True, loc, None

        body_lower = (r.text or "").lower()
        if "captcha" in body_lower or "cloudflare" in body_lower:
            return False, None, "Archive.today blocked (captcha/Cloudflare)"

        m = re.search(r"(https?://archive\.[a-z]+/wip/[A-Za-z0-9]+)", r.text or "", re.IGNORECASE)
        if m:
            return True, m.group(1), None

        m2 = re.search(r"(https?://archive\.[a-z]+/[A-Za-z0-9]+)", r.text or "", re.IGNORECASE)
        if m2:
            return True, m2.group(1), None

        if 200 <= r.status_code < 300:
            return True, None, None

        return False, None, f"Archive.today HTTP {r.status_code}"
    except Exception as e:
        return False, None, f"Archive.today exception: {e}"


# -------------------------
# JSON snapshot
# -------------------------

def write_latest_json(conn: sqlite3.Connection, out_path: str, limit: int = 25) -> None:
    cur = conn.execute(
        """SELECT
             reddit_id, subreddit, created_utc, inserted_at,
             title, reddit_url, url_www, url_old,

             wayback_www, wayback_old,
             wayback_www_ts, wayback_old_ts,
             wayback_www_ok, wayback_old_ok,
             wayback_www_checked_at, wayback_old_checked_at,

             atoday_www, atoday_old,
             atoday_www_ok, atoday_old_ok,
             atoday_www_checked_at, atoday_old_checked_at,

             err_wayback_www, err_wayback_old,
             err_wayback_avail_www, err_wayback_avail_old,
             err_atoday_www, err_atoday_old
           FROM posts
           ORDER BY inserted_at DESC
           LIMIT ?""",
        (limit,),
    )

    rows = []
    for r in cur.fetchall():
        rows.append(
            {
                "reddit_id": r["reddit_id"],
                "subreddit": r["subreddit"],
                "created_utc": r["created_utc"],
                "inserted_at": r["inserted_at"],
                "title": r["title"],
                "reddit_url": r["reddit_url"],
                "url_www": r["url_www"],
                "url_old": r["url_old"],
                "wayback_www": r["wayback_www"],
                "wayback_old": r["wayback_old"],
                "wayback_www_ts": r["wayback_www_ts"],
                "wayback_old_ts": r["wayback_old_ts"],
                "wayback_www_ok": r["wayback_www_ok"],
                "wayback_old_ok": r["wayback_old_ok"],
                "wayback_www_checked_at": r["wayback_www_checked_at"],
                "wayback_old_checked_at": r["wayback_old_checked_at"],
                "archive_today_www": r["atoday_www"],
                "archive_today_old": r["atoday_old"],
                "archive_today_www_ok": r["atoday_www_ok"],
                "archive_today_old_ok": r["atoday_old_ok"],
                "errors": {
                    "err_wayback_www": r["err_wayback_www"],
                    "err_wayback_old": r["err_wayback_old"],
                    "err_wayback_avail_www": r["err_wayback_avail_www"],
                    "err_wayback_avail_old": r["err_wayback_avail_old"],
                    "err_atoday_www": r["err_atoday_www"],
                    "err_atoday_old": r["err_atoday_old"],
                },
            }
        )

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)


# -------------------------
# Polling + verification
# -------------------------

def poll_subreddit(
    conn: sqlite3.Connection,
    session: requests.Session,
    subreddit: str,
    scan_limit: int,
    do_wayback: bool,
    do_atoday: bool,
    delay_wayback: float,
    delay_atoday: float,
) -> int:
    feed_url = f"https://www.reddit.com/r/{subreddit}/new.rss"
    parsed = feedparser.parse(feed_url, agent=USER_AGENT)

    if parsed.bozo and getattr(parsed, "bozo_exception", None):
        print(f"[{subreddit}] RSS parse warning: {parsed.bozo_exception}", file=sys.stderr)

    entries = parsed.entries[:scan_limit]
    new_count = 0

    for e in entries:
        raw_link = getattr(e, "link", None)
        if not raw_link:
            continue

        reddit_url = canonicalise_reddit_post_url(raw_link)
        rid = extract_reddit_id(reddit_url)
        if not rid:
            continue

        url_www = to_reddit_view(reddit_url, "www")
        url_old = to_reddit_view(reddit_url, "old")

        if seen(conn, rid):
            continue

        title_raw = getattr(e, "title", "") or "(no title)"
        title = html.unescape(title_raw).strip()
        created_utc = rss_entry_created_utc(e)

        insert_post(conn, subreddit, rid, title, reddit_url, url_www, url_old, created_utc)
        new_count += 1
        print(f"[{subreddit}] New post: {rid} | {title}")

        if do_wayback:
            submit_ts_www = now_ts14()
            ok, snap_url, err = submit_wayback(session, url_www)
            update_fields(
                conn,
                rid,
                wayback_www=snap_url,
                wayback_www_submit_ts=submit_ts_www,
                err_wayback_www=err,
            )
            polite_sleep(delay_wayback)

            submit_ts_old = now_ts14()
            ok2, snap_url2, err2 = submit_wayback(session, url_old)
            update_fields(
                conn,
                rid,
                wayback_old=snap_url2,
                wayback_old_submit_ts=submit_ts_old,
                err_wayback_old=err2,
            )
            polite_sleep(delay_wayback)

        if do_atoday:
            ok, aurl, err = submit_archive_today(session, url_www)
            update_fields(
                conn,
                rid,
                atoday_www=aurl,
                atoday_www_ok=1 if aurl else 0,
                atoday_www_checked_at=now_iso(),
                err_atoday_www=err,
            )
            polite_sleep(delay_atoday)

            ok2, aurl2, err2 = submit_archive_today(session, url_old)
            update_fields(
                conn,
                rid,
                atoday_old=aurl2,
                atoday_old_ok=1 if aurl2 else 0,
                atoday_old_checked_at=now_iso(),
                err_atoday_old=err2,
            )
            polite_sleep(delay_atoday)

    return new_count


def verify_wayback_pending(
    conn: sqlite3.Connection,
    session: requests.Session,
    verify_batch: int,
    verify_min_age: int,
    verify_recheck_interval: int,
) -> int:
    checked = 0
    now_epoch = int(time.time())

    cur = conn.execute(
        """SELECT
             reddit_id, url_www, url_old,
             wayback_www_submit_ts, wayback_old_submit_ts,
             wayback_www_ok, wayback_old_ok,
             wayback_www_checked_at, wayback_old_checked_at
           FROM posts
           WHERE (wayback_www_submit_ts IS NOT NULL AND (wayback_www_ok IS NULL OR wayback_www_ok=0))
              OR (wayback_old_submit_ts IS NOT NULL AND (wayback_old_ok IS NULL OR wayback_old_ok=0))
           ORDER BY inserted_at DESC
           LIMIT ?""",
        (verify_batch,),
    )
    rows = cur.fetchall()

    for r in rows:
        rid = r["reddit_id"]

        # WWW
        if r["wayback_www_submit_ts"] and (r["wayback_www_ok"] is None or r["wayback_www_ok"] == 0):
            submit_epoch = ts14_to_epoch(r["wayback_www_submit_ts"]) or 0
            last_check_epoch = iso_to_epoch(r["wayback_www_checked_at"] or "") or 0

            if (now_epoch - submit_epoch) >= verify_min_age and (now_epoch - last_check_epoch) >= verify_recheck_interval:
                available, snap_url, snap_ts, snap_status, err = wayback_availability(
                    session, r["url_www"], r["wayback_www_submit_ts"]
                )
                ok = 1 if (available and snap_ts and snap_ts >= r["wayback_www_submit_ts"]) else 0

                update_fields(
                    conn,
                    rid,
                    wayback_www=snap_url,
                    wayback_www_ts=snap_ts,
                    wayback_www_status=snap_status,
                    wayback_www_ok=ok,
                    wayback_www_checked_at=now_iso(),
                    err_wayback_avail_www=err,
                )
                checked += 1
                polite_sleep(1.0)

        # OLD
        if r["wayback_old_submit_ts"] and (r["wayback_old_ok"] is None or r["wayback_old_ok"] == 0):
            submit_epoch = ts14_to_epoch(r["wayback_old_submit_ts"]) or 0
            last_check_epoch = iso_to_epoch(r["wayback_old_checked_at"] or "") or 0

            if (now_epoch - submit_epoch) >= verify_min_age and (now_epoch - last_check_epoch) >= verify_recheck_interval:
                available, snap_url, snap_ts, snap_status, err = wayback_availability(
                    session, r["url_old"], r["wayback_old_submit_ts"]
                )
                ok = 1 if (available and snap_ts and snap_ts >= r["wayback_old_submit_ts"]) else 0

                update_fields(
                    conn,
                    rid,
                    wayback_old=snap_url,
                    wayback_old_ts=snap_ts,
                    wayback_old_status=snap_status,
                    wayback_old_ok=ok,
                    wayback_old_checked_at=now_iso(),
                    err_wayback_avail_old=err,
                )
                checked += 1
                polite_sleep(1.0)

    return checked


# -------------------------
# Embedded dashboard server
# -------------------------

def _db_read_connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _count(db: sqlite3.Connection, where: str = "", params: tuple[Any, ...] = ()) -> int:
    q = "SELECT COUNT(*) AS n FROM posts"
    if where:
        q += " WHERE " + where
    return int(db.execute(q, params).fetchone()["n"])


def _latest_rows(db: sqlite3.Connection, limit: int, offset: int) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT
             reddit_id, subreddit, created_utc, inserted_at,
             title, reddit_url, url_www, url_old,

             wayback_www, wayback_old,
             wayback_www_ok, wayback_old_ok,
             wayback_www_submit_ts, wayback_old_submit_ts,
             wayback_www_ts, wayback_old_ts,
             wayback_www_checked_at, wayback_old_checked_at,

             atoday_www, atoday_old,
             atoday_www_ok, atoday_old_ok,
             atoday_www_checked_at, atoday_old_checked_at,

             err_wayback_www, err_wayback_old,
             err_wayback_avail_www, err_wayback_avail_old,
             err_atoday_www, err_atoday_old
           FROM posts
           ORDER BY inserted_at DESC
           LIMIT ? OFFSET ?""",
        (limit, offset),
    ).fetchall()


def _pill(text: str, klass: str) -> str:
    return f'<span class="pill {klass}">{html.escape(text)}</span>'


def _status_wayback(r: sqlite3.Row, view: str) -> tuple[str, str]:
    ok = r["wayback_www_ok"] if view == "www" else r["wayback_old_ok"]
    submit_ts = r["wayback_www_submit_ts"] if view == "www" else r["wayback_old_submit_ts"]
    checked_at = r["wayback_www_checked_at"] if view == "www" else r["wayback_old_checked_at"]

    if ok == 1:
        return "ok", "✓ ok"
    if submit_ts and ok == 0:
        # Pending or failed. If checked_at exists, it's been checked and still not OK.
        if checked_at:
            return "pending", "… pending"
        return "pending", "… queued"
    if submit_ts and ok is None:
        return "pending", "… queued"
    return "unknown", "—"


def _status_atoday(r: sqlite3.Row, view: str) -> tuple[str, str]:
    ok = r["atoday_www_ok"] if view == "www" else r["atoday_old_ok"]
    checked_at = r["atoday_www_checked_at"] if view == "www" else r["atoday_old_checked_at"]
    if ok == 1:
        return "ok", "✓ ok"
    if checked_at:
        return "bad", "✕ no link"
    return "unknown", "—"


def _fmt_time(r: sqlite3.Row) -> str:
    if r["created_utc"]:
        dt = datetime.fromtimestamp(int(r["created_utc"]), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    # fallback to inserted_at
    s = (r["inserted_at"] or "")[:16].replace("T", " ")
    return s + " UTC" if s else "—"


def start_dashboard(db_path: str, host: str, port: int) -> tuple[ThreadingTCPServer, threading.Thread]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            # Keep console quieter. Comment out if you want request logs.
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path in ("/api/latest", "/api/latest.json"):
                self._handle_api_latest(qs)
                return

            if path in ("/", "/index.html"):
                self._handle_index(qs)
                return

            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Not found")

        def _handle_api_latest(self, qs: dict[str, list[str]]) -> None:
            try:
                limit = int(qs.get("limit", ["100"])[0])
                limit = max(1, min(limit, 500))
            except Exception:
                limit = 100

            try:
                db = _db_read_connect(db_path)
                rows = _latest_rows(db, limit=limit, offset=0)
                payload = []
                for r in rows:
                    payload.append(
                        {
                            "reddit_id": r["reddit_id"],
                            "subreddit": r["subreddit"],
                            "title": r["title"],
                            "reddit_url": r["reddit_url"],
                            "url_www": r["url_www"],
                            "url_old": r["url_old"],
                            "inserted_at": r["inserted_at"],
                            "wayback_www": r["wayback_www"],
                            "wayback_old": r["wayback_old"],
                            "wayback_www_ok": r["wayback_www_ok"],
                            "wayback_old_ok": r["wayback_old_ok"],
                            "atoday_www": r["atoday_www"],
                            "atoday_old": r["atoday_old"],
                            "atoday_www_ok": r["atoday_www_ok"],
                            "atoday_old_ok": r["atoday_old_ok"],
                            "errors": {
                                "err_wayback_www": r["err_wayback_www"],
                                "err_wayback_old": r["err_wayback_old"],
                                "err_wayback_avail_www": r["err_wayback_avail_www"],
                                "err_wayback_avail_old": r["err_wayback_avail_old"],
                                "err_atoday_www": r["err_atoday_www"],
                                "err_atoday_old": r["err_atoday_old"],
                            },
                        }
                    )
                db.close()

                body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"Error: {e}".encode("utf-8"))

        def _handle_index(self, qs: dict[str, list[str]]) -> None:
            try:
                page = int(qs.get("page", ["1"])[0])
                per_page = int(qs.get("per_page", ["50"])[0])
            except Exception:
                page = 1
                per_page = 50

            page = max(1, page)
            per_page = max(10, min(200, per_page))
            offset = (page - 1) * per_page

            try:
                db = _db_read_connect(db_path)

                total = _count(db)
                wayback_ok_any = _count(db, "(wayback_www_ok=1 OR wayback_old_ok=1)")
                atoday_ok_any = _count(db, "(atoday_www_ok=1 OR atoday_old_ok=1)")
                both_ok_any = _count(
                    db,
                    "(wayback_www_ok=1 OR wayback_old_ok=1) AND (atoday_www_ok=1 OR atoday_old_ok=1)",
                )

                wayback_pending_any = _count(
                    db,
                    "((wayback_www_submit_ts IS NOT NULL AND (wayback_www_ok IS NULL OR wayback_www_ok=0)) OR "
                    " (wayback_old_submit_ts IS NOT NULL AND (wayback_old_ok IS NULL OR wayback_old_ok=0)))"
                )

                rows = _latest_rows(db, limit=per_page, offset=offset)
                db.close()

                def link_or_dash(u: str | None) -> str:
                    if not u:
                        return "—"
                    safe = html.escape(u)
                    return f'<a href="{safe}" target="_blank" rel="noreferrer">open</a>'

                # Build HTML
                out = []
                out.append("<!doctype html><html><head>")
                out.append('<meta charset="utf-8" />')
                out.append('<meta name="viewport" content="width=device-width, initial-scale=1" />')
                out.append("<title>Reddit Archive Dashboard</title>")
                out.append(
                    """
<style>
body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 18px; }
h2 { margin: 0 0 10px 0; }
.muted { color: #666; font-size: 12px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin: 14px 0; }
.card { border: 1px solid #ddd; border-radius: 12px; padding: 10px 12px; }
.k { color: #666; font-size: 12px; }
.v { font-size: 20px; font-weight: 800; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 8px; border-bottom: 1px solid #eee; vertical-align: top; }
th { position: sticky; top: 0; background: #fff; z-index: 1; }
.title a { text-decoration: none; }
.pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; border: 1px solid #ddd; }
.ok { border-color: #2e7d32; color: #2e7d32; }
.pending { border-color: #999; color: #666; }
.bad { border-color: #b71c1c; color: #b71c1c; }
.unknown { border-color: #bbb; color: #777; }
.err { color: #b71c1c; font-size: 12px; max-width: 640px; white-space: pre-wrap; }
.nav { margin: 12px 0; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.nav a { text-decoration: none; border: 1px solid #ddd; padding: 6px 10px; border-radius: 10px; }
.smalllinks a { font-size: 12px; }
</style>
"""
                )
                out.append("</head><body>")
                out.append("<h2>Reddit Archive Dashboard</h2>")
                out.append(f'<div class="muted">DB: <code>{html.escape(db_path)}</code></div>')
                out.append(f'<div class="muted">Updated: {html.escape(now_iso())}</div>')

                out.append('<div class="stats">')
                out.append(f'<div class="card"><div class="k">Posts tracked</div><div class="v">{total}</div></div>')
                out.append(f'<div class="card"><div class="k">Wayback ok (any view)</div><div class="v">{wayback_ok_any}</div></div>')
                out.append(f'<div class="card"><div class="k">Wayback pending (any view)</div><div class="v">{wayback_pending_any}</div></div>')
                out.append(f'<div class="card"><div class="k">Archive.today ok (any view)</div><div class="v">{atoday_ok_any}</div></div>')
                out.append(f'<div class="card"><div class="k">Both services ok</div><div class="v">{both_ok_any}</div></div>')
                out.append("</div>")

                prev_page = max(1, page - 1)
                next_page = page + 1
                out.append('<div class="nav">')
                out.append(f'<a href="/?page={prev_page}&per_page={per_page}">◀ Prev</a>')
                out.append(f'<span class="muted">Page {page} (showing {per_page}/page)</span>')
                out.append(f'<a href="/?page={next_page}&per_page={per_page}">Next ▶</a>')
                out.append(f'<span class="muted">API: <a href="/api/latest.json?limit=200">/api/latest.json</a></span>')
                out.append("</div>")

                out.append("<table><thead><tr>")
                out.append("<th>Time</th>")
                out.append("<th>Post</th>")
                out.append("<th>Wayback</th>")
                out.append("<th>Archive.today</th>")
                out.append("<th>Errors</th>")
                out.append("</tr></thead><tbody>")

                for r in rows:
                    t = _fmt_time(r)
                    title = html.escape(r["title"] or "(no title)")
                    reddit_url = html.escape(r["reddit_url"] or r["url_www"] or "")
                    sub = html.escape(r["subreddit"] or "")

                    wb_www_status, wb_www_label = _status_wayback(r, "www")
                    wb_old_status, wb_old_label = _status_wayback(r, "old")

                    at_www_status, at_www_label = _status_atoday(r, "www")
                    at_old_status, at_old_label = _status_atoday(r, "old")

                    err_parts = []
                    for k in ("err_wayback_www", "err_wayback_old", "err_wayback_avail_www", "err_wayback_avail_old", "err_atoday_www", "err_atoday_old"):
                        v = r[k]
                        if v:
                            err_parts.append(f"{k}: {v}")
                    err_text = "\n".join(err_parts) if err_parts else "—"

                    out.append("<tr>")
                    out.append(f'<td class="muted">{html.escape(t)}<br><span class="muted">r/{sub}</span></td>')

                    out.append('<td class="title">')
                    out.append(f'<a href="{reddit_url}" target="_blank" rel="noreferrer"><strong>{title}</strong></a><br>')
                    out.append('<span class="muted">views:</span> ')
                    out.append(f'<span class="smalllinks"><a href="{html.escape(r["url_www"])}" target="_blank" rel="noreferrer">www</a> · ')
                    out.append(f'<a href="{html.escape(r["url_old"])}" target="_blank" rel="noreferrer">old</a></span>')
                    out.append("</td>")

                    out.append("<td>")
                    out.append(_pill(wb_www_label, wb_www_status) + " " + _pill(wb_old_label, wb_old_status) + "<br>")
                    out.append(f'<span class="muted">www:</span> {link_or_dash(r["wayback_www"])} · ')
                    out.append(f'<span class="muted">old:</span> {link_or_dash(r["wayback_old"])}')
                    out.append("</td>")

                    out.append("<td>")
                    out.append(_pill(at_www_label, at_www_status) + " " + _pill(at_old_label, at_old_status) + "<br>")
                    out.append(f'<span class="muted">www:</span> {link_or_dash(r["atoday_www"])} · ')
                    out.append(f'<span class="muted">old:</span> {link_or_dash(r["atoday_old"])}')
                    out.append("</td>")

                    out.append(f'<td class="err">{html.escape(err_text)}</td>')
                    out.append("</tr>")

                out.append("</tbody></table>")
                out.append("</body></html>")

                body = "\n".join(out).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"Dashboard error: {e}".encode("utf-8"))

    server = ThreadingTCPServer((host, port), Handler)
    server.daemon_threads = True

    th = threading.Thread(target=server.serve_forever, name="dashboard", daemon=True)
    th.start()
    return server, th


# -------------------------
# Main
# -------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="subreddits.conf", help="Config file path (settings + subreddit list)")
    ap.add_argument("--db", default="reddit_archiver.sqlite")
    ap.add_argument("--once", action="store_true", help="Run one cycle and exit")
    ap.add_argument("--subreddit", default=None, help="Fallback single subreddit if config has none")
    args = ap.parse_args()

    settings, subs = parse_config(args.config)
    if not subs:
        subs = [args.subreddit or "ChatGPT"]

    conn = init_db(args.db)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    dash_server = None
    if settings.dashboard_enabled:
        try:
            dash_server, _ = start_dashboard(args.db, settings.dashboard_host, settings.dashboard_port)
            print(f"[dashboard] Serving on http://{settings.dashboard_host}:{settings.dashboard_port}/")
        except Exception as e:
            print(f"[dashboard] Failed to start: {e}", file=sys.stderr)

    print(f"Config: {args.config}")
    print(f"DB: {args.db}")
    print(f"Subreddits: {', '.join(subs)}")
    print(
        f"Wayback={settings.do_wayback} | Archive.today={settings.do_archive_today} | "
        f"scan_limit={settings.scan_limit} | interval={settings.interval}s"
    )

    try:
        while True:
            total_new = 0
            for sub in subs:
                try:
                    total_new += poll_subreddit(
                        conn=conn,
                        session=session,
                        subreddit=sub,
                        scan_limit=settings.scan_limit,
                        do_wayback=settings.do_wayback,
                        do_atoday=settings.do_archive_today,
                        delay_wayback=settings.delay_wayback,
                        delay_atoday=settings.delay_atoday,
                    )
                except Exception as e:
                    print(f"[{sub}] Poll error: {e}", file=sys.stderr)

            verified = 0
            if settings.do_wayback:
                try:
                    verified = verify_wayback_pending(
                        conn=conn,
                        session=session,
                        verify_batch=settings.verify_batch,
                        verify_min_age=settings.verify_min_age,
                        verify_recheck_interval=settings.verify_recheck_interval,
                    )
                except Exception as e:
                    print(f"[verify] Wayback verify error: {e}", file=sys.stderr)

            try:
                if settings.out_json:
                    write_latest_json(conn, settings.out_json, limit=settings.json_limit)
            except Exception as e:
                print(f"[json] Write error: {e}", file=sys.stderr)

            print(f"Cycle done. New posts: {total_new} | Wayback legs verified this cycle: {verified}")

            if args.once:
                break

            polite_sleep(settings.interval)

    except KeyboardInterrupt:
        print("\nStopping…")

    finally:
        try:
            conn.close()
        except Exception:
            pass
        if dash_server:
            try:
                dash_server.shutdown()
                dash_server.server_close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
