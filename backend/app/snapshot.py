"""The SQLite file shared between the scheduled collector and serverless deployments.

Vercel keeps no files and cannot run a collector, so .github/workflows/collect.yml collects on a schedule and publishes
the database as a release asset. `python -m app.snapshot out.db` writes that file; refresh() is how a running deployment
picks up the newest one. Without SNAPSHOT_URL (local, Docker, PostgreSQL) refresh() does nothing.
"""
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

import httpx

from .config import get_settings
from .db import engine

log = logging.getLogger("betwatch.snapshot")
CHECK_EVERY_SECONDS = 300
RETRY_SECONDS = 60
_lock = threading.Lock()
_next_check = 0.0
_etag: str | None = None


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
