"""The SQLite file shared between the scheduled collector and serverless deployments.

Vercel keeps no files and cannot run a collector, so .github/workflows/collect.yml collects on a schedule and publishes
the database as a release asset. `python -m app.snapshot out.db` writes that file; refresh() is how a running deployment
picks up the newest one. Without SNAPSHOT_URL (local, Docker, PostgreSQL) refresh() does nothing.

GitHub fires that schedule only every few hours, so request_collection() starts the workflow when someone visits and the
data is older than COLLECT_AFTER_MINUTES. Without GITHUB_DISPATCH_TOKEN it does nothing.
"""
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from .config import get_settings
from .db import engine
from .models import Source, utcnow

log = logging.getLogger("betwatch.snapshot")
CHECK_EVERY_SECONDS = 300
RETRY_SECONDS = 60
_lock = threading.Lock()
_next_check = 0.0
_etag: str | None = None

WORKFLOW_API = "https://api.github.com/repos/everyerpofficial-stack/betwatch/actions/workflows/collect.yml"
COLLECT_AFTER_MINUTES = 30  # well under STALE_AFTER_MINUTES in services/dashboard.py; a run takes about 4 minutes
_collect_lock = threading.Lock()
_next_collect_check = 0.0


def _post_count(path: Path) -> int:
    """Posts in a SQLite file. Raises if the file is damaged or is not a BetWatch database."""
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("failed the integrity check")
        return con.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    finally:
        con.close()


def due() -> bool:
    return bool(get_settings().snapshot_url) and time.monotonic() >= _next_check


def refresh() -> bool:
    """Swap in a newer published snapshot, if there is one. Returns whether it did. Never raises: on any problem the
    deployment keeps serving the copy it has and tries again shortly."""
    global _next_check, _etag
    if not due() or not _lock.acquire(blocking=False):
        return False
    current = Path(engine.url.database)
    new = current.with_name(current.name + ".new")
    try:
        _next_check = time.monotonic() + CHECK_EVERY_SECONDS
        r = httpx.get(get_settings().snapshot_url, headers={"If-None-Match": _etag} if _etag else {},
                      follow_redirects=True, timeout=httpx.Timeout(6, connect=2))
        if r.status_code == 304:
            return False
        r.raise_for_status()
        new.write_bytes(r.content)
        posts, have = _post_count(new), _post_count(current)
        if posts < have:  # the collector only ever adds posts, so this copy is damaged or from before ours
            log.warning("ignored snapshot with %d posts: this deployment already has %d", posts, have)
            return False
        engine.dispose()  # Windows will not replace an open file
        os.replace(new, current)
        engine.dispose()  # also any connection opened meanwhile: from here on they open the new file
        _etag = r.headers.get("etag")
        log.info("loaded snapshot with %d posts", posts)
        return True
    except (httpx.HTTPError, OSError, ValueError, sqlite3.Error) as e:
        _next_check = time.monotonic() + RETRY_SECONDS
        log.warning("could not load snapshot: %s", e)
        return False
    finally:
        new.unlink(missing_ok=True)
        _lock.release()


def collect_due() -> bool:
    return bool(get_settings().github_dispatch_token) and time.monotonic() >= _next_collect_check


def _old(at: datetime | None) -> bool:
    return at is None or utcnow() - at >= timedelta(minutes=COLLECT_AFTER_MINUTES)


def request_collection() -> bool:
    """Start the Collect news workflow if this deployment's data is old and no run is under way or just finished (its
    database is then on the way). Returns whether it started one. Never raises."""
    global _next_collect_check
    if not collect_due() or not _collect_lock.acquire(blocking=False):
        return False
    try:
        _next_collect_check = time.monotonic() + CHECK_EVERY_SECONDS
        with engine.connect() as con:
            if not _old(con.scalar(select(func.max(Source.last_success_at)))):
                return False
        headers = {"Authorization": f"Bearer {get_settings().github_dispatch_token}",
                   "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        with httpx.Client(headers=headers, timeout=httpx.Timeout(5, connect=2)) as gh:
            runs = gh.get(f"{WORKFLOW_API}/runs", params={"per_page": 1}).raise_for_status().json()["workflow_runs"]
            if runs and (runs[0]["status"] != "completed"
                         or not _old(datetime.fromisoformat(runs[0]["updated_at"].replace("Z", "+00:00")).replace(tzinfo=None))):
                return False
            gh.post(f"{WORKFLOW_API}/dispatches", json={"ref": "main"}).raise_for_status()
        _next_collect_check = time.monotonic() + COLLECT_AFTER_MINUTES * 60
        log.info("data older than %d minutes: started the Collect news workflow", COLLECT_AFTER_MINUTES)
        return True
    except (httpx.HTTPError, KeyError, TypeError, ValueError, SQLAlchemyError) as e:
        log.warning("could not start a collection: %s", e)
        return False
    finally:
        _collect_lock.release()


def export(out: Path) -> int:
    """Write a compact, self-contained copy of the database, including anything still in its write-ahead log."""
    out.unlink(missing_ok=True)
    con = sqlite3.connect(engine.url.database)
    try:
        con.execute("VACUUM INTO ?", (str(out),))
    finally:
        con.close()
    return _post_count(out)


if __name__ == "__main__":
    posts = export(Path(sys.argv[1]))
    if not posts:
        sys.exit("Refusing to publish a database with no posts.")
    print(f"Wrote {sys.argv[1]} with {posts} posts.")
