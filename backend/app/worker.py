"""Collector worker. `python -m app.worker` runs forever; `python -m app.worker --once` runs due sources once."""
import argparse
from datetime import datetime
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler

from .config import get_settings
from .ingest.collector import repair_dates_on_start, run_due_sources, run_enrichment

logging.basicConfig(level=logging.INFO, format='{"t":"%(asctime)s","lvl":"%(levelname)s","log":"%(name)s","msg":"%(message)s"}')
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("betwatch.worker")


def tick():
    results = run_due_sources()
    found = run_enrichment()
    if results or found:
        new = sum(r["new"] for r in results)
        errs = sum(1 for r in results if r["status"] == "error")
        log.info("tick: %d runs, %d new posts, %d errors, %d bylines/dates found", len(results), new, errs, found)


def _on_start():
    ig_fixed, queued = repair_dates_on_start()
    if ig_fixed:
        log.info("recovered dates of %d Instagram posts from their stored text", ig_fixed)
    if queued:
        log.info("queued %d YouTube videos for exact upload-date lookup", queued)


def start_background() -> BackgroundScheduler:
    _on_start()
    sched = BackgroundScheduler(job_defaults={"max_instances": 1, "coalesce": True})
    sched.add_job(tick, "interval", seconds=get_settings().scheduler_tick_seconds, next_run_time=datetime.now())
    sched.start()
    return sched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    _on_start()
    if args.once:
        tick()
        return
    sched = BlockingScheduler(job_defaults={"max_instances": 1, "coalesce": True})
    sched.add_job(tick, "interval", seconds=get_settings().scheduler_tick_seconds)
    tick()
    sched.start()


if __name__ == "__main__":
    main()
