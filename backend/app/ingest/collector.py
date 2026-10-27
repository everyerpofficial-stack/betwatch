"""Runs sources: fetch → store → enrich → summarise, with retries, backoff and per-run logs."""
import logging
import time
from datetime import timedelta

import httpx
from sqlalchemy import select

from ..ai import summarize_clusters
from ..db import session_scope
from ..models import CollectionRun, Source, Subject, utcnow
from .adapters import ADAPTERS, PER_SUBJECT_KINDS, AdapterError, RateLimited, http_client
from .enrich import enrich_bylines, enrich_video_dates, repair_undated_instagram, requeue_estimated_video_dates
from .pipeline import store_items

log = logging.getLogger("betwatch.collector")
MAX_BACKOFF_EXP = 5
PACE_SECONDS = {"gdelt": 6.0, "google_news": 1.5, "youtube_search": 1.0, "reddit": 1.0, "bluesky": 1.0, "instagram_search": 2.0}


def is_due(src: Source, now=None) -> bool:
    now = now or utcnow()
    if not src.enabled:
        return False
    if src.last_run_at is None:
        return True
    wait = src.interval_minutes * (2 ** min(src.consecutive_failures, MAX_BACKOFF_EXP))
    return now >= src.last_run_at + timedelta(minutes=wait)


def _fetch_with_retry(fn, src, subject, client, attempts=3):
    delay = 2.0
    for i in range(attempts):
        try:
            return fn(src, subject, client)
        except RateLimited:
            raise  # don't hammer a rate-limited host; backoff happens via consecutive_failures
        except (httpx.TransportError, httpx.TimeoutException):
            if i == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2


def run_source(source_id: int, subject_ids: list[int] | None = None) -> list[dict]:
    """Run one source (once, or once per subject for search sources). Returns per-run summaries."""
    results = []
    with session_scope() as db:
        src = db.get(Source, source_id)
        if src is None:
            return results
        fn = ADAPTERS.get(src.kind)
        if fn is None:
            src.last_error = f"Unknown source kind {src.kind}"
            return results
        if src.kind in PER_SUBJECT_KINDS:
            q = select(Subject).where(Subject.enabled.is_(True))
            if subject_ids:
                q = q.where(Subject.id.in_(subject_ids))
            subjects = list(db.scalars(q).all())
        else:
            subjects = [None]
        src.last_run_at = utcnow()
        any_error = None
        touched: set[int] = set()
        with http_client() as client:
            for i, sj in enumerate(subjects):
                if i and PACE_SECONDS.get(src.kind):
                    time.sleep(PACE_SECONDS[src.kind])  # respect per-provider request rates
                run = CollectionRun(source_id=src.id, subject_id=sj.id if sj else None)
                db.add(run)
                db.flush()
                t0 = time.perf_counter()
                rate_limited = False
                try:
                    items = _fetch_with_retry(fn, src, sj, client) or []
                    stats = store_items(db, src, sj, items)
                    touched |= stats["clusters"]
                    run.status, run.fetched, run.new, run.duplicates = "ok", stats["fetched"], stats["new"], stats["duplicates"]
                    run.median_delay_minutes = stats["median_delay"]
                except (AdapterError, httpx.HTTPError, ValueError) as e:
                    # Adapter/network failures happen before any rows are written, so the session stays clean.
                    run.status, run.error = "error", f"{type(e).__name__}: {e}"[:1000]
                    any_error = run.error
                    rate_limited = isinstance(e, RateLimited)
                    log.warning("source %s subject %s failed: %s", src.name, sj.name if sj else "-", e)
                run.latency_ms = int((time.perf_counter() - t0) * 1000)
                run.finished_at = utcnow()
                results.append({"source": src.name, "subject": sj.name if sj else None, "status": run.status,
                                 "fetched": run.fetched, "new": run.new, "error": run.error})
                if rate_limited:
                    break  # the provider asked us to slow down: skip its remaining subjects until the backoff ends
            if any_error:
                src.consecutive_failures += 1
                src.last_error = any_error
            else:
                src.consecutive_failures = 0
                src.last_error = None
                src.last_success_at = utcnow()
            db.flush()
        summarize_clusters(db, touched)
    return results


def run_enrichment(limit: int | None = None) -> int:
    """Enrichment batch: bylines (resolves Google News links, reads author metadata) and exact YouTube upload
    dates. Never raises."""
    found = 0
    for step in (enrich_bylines, enrich_video_dates):
        try:
            with session_scope() as db, http_client() as client:
                found += step(db, client, limit)
        except Exception as e:
            log.warning("enrichment step %s failed: %s", step.__name__, e)
    return found


def repair_dates_on_start() -> tuple[int, int]:
    """Worker start: fix Instagram dates from stored text, and queue estimated YouTube dates for exact lookup."""
    try:
        with session_scope() as db:
            return repair_undated_instagram(db), requeue_estimated_video_dates(db)
    except Exception as e:
        log.warning("date repair on start failed: %s", e)
        return 0, 0


def run_due_sources() -> list[dict]:
    with session_scope() as db:
        due = [s.id for s in db.scalars(select(Source)).all() if is_due(s)]
    out = []
    for sid in due:
        try:
            out.extend(run_source(sid))
        except Exception as e:  # one broken source must not stop the others
            log.exception("source %s crashed: %s", sid, e)
    return out
