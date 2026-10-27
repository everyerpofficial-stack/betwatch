"""Turn RawItems into stored, deduplicated, classified and clustered Articles."""
import logging
import statistics
from datetime import datetime, timedelta

from rapidfuzz import fuzz
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Article, Cluster, Outlet, Source, Subject, utcnow
from . import outlets as registry
from .adapters import RawItem
from .classify import categorize, is_relevant, looks_live, tone_of
from .text import canonical_url, contains_term, fingerprint, title_tokens

log = logging.getLogger("betwatch.pipeline")


def resolve_outlet(db: Session, item: RawItem, cache: dict) -> Outlet:
    known = registry.lookup(item.outlet_name, item.outlet_domain)
    if item.platform in ("YouTube", "Reddit", "Bluesky", "Instagram"):
        known = None  # social/video channels are distinct outlets
    name = known[0] if known else (item.outlet_name or item.outlet_domain or "Unknown outlet").strip()
    if item.platform == "YouTube" and not name.endswith("· YouTube"):
        name = f"{name} · YouTube"
    elif item.platform == "Instagram" and not name.endswith("· Instagram"):
        name = f"{name} · Instagram"
    if name in cache:
        return cache[name]
    outlet = db.scalar(select(Outlet).where(Outlet.name == name))
    if outlet is None:
        domain = (known[1][0] if known else item.outlet_domain) or None
        outlet = Outlet(
            name=name, domain=domain,
            kind=(known[2] if known else item.outlet_kind) or "News site",
            # Known registry > domain TLD > the source's country (e.g. the Google News edition, gl=IN).
            country=(known[3] if known else None) or registry.guess_country(domain) or item.outlet_country,
            language=item.language,
        )
        db.add(outlet)
        db.flush()
    elif outlet.country is None and (item.outlet_country or registry.guess_country(outlet.domain)):
        outlet.country = item.outlet_country or registry.guess_country(outlet.domain)
    cache[name] = outlet
    return outlet


def match_subjects(text: str, subjects: list[Subject]) -> list[Subject]:
    out = []
    for s in subjects:
        terms = [s.name] + list(s.aliases or [])
        if any(contains_term(text, t) for t in terms):
            out.append(s)
    return out


def assign_cluster(db: Session, art: Article) -> Cluster:
    """Attach the article to the most similar recent story, or start a new one."""
    s = get_settings()
    when = art.published_at or art.collected_at
    window = timedelta(hours=s.cluster_window_hours)
    toks = title_tokens(art.title)
    norm_title = " ".join(sorted(toks))
    best, best_score = None, 0
    if toks:
        candidates = db.scalars(
            select(Cluster).where(Cluster.last_seen >= when - window, Cluster.first_seen <= when + window)
            .order_by(Cluster.last_seen.desc()).limit(800)
        ).all()
        for c in candidates:
            ctoks = title_tokens(c.title)
            shared = len(toks & ctoks)
            if shared < 2:
                continue
            score = fuzz.token_set_ratio(norm_title, " ".join(sorted(ctoks)))
            # Token-set ratio rewards subset titles; require real overlap relative to the shorter title too.
            overlap = shared / max(1, min(len(toks), len(ctoks)))
            if overlap < 0.4:
                continue
            if score > best_score:
                best, best_score = c, score
    if best is not None and best_score >= s.cluster_threshold:
        best.first_seen = min(best.first_seen, when)
        best.last_seen = max(best.last_seen, when)
        best.article_count += 1
        art.cluster = best
        return best
    c = Cluster(title=art.title, first_seen=when, last_seen=when, article_count=1)
    db.add(c)
    db.flush()
    art.cluster = c
    return c


def refresh_cluster(db: Session, c: Cluster) -> None:
    """Recompute a story's bounds and count from its posts; delete it if none are left."""
    arts = db.scalars(select(Article).where(Article.cluster_id == c.id)).all()
    if not arts:
        db.delete(c)
        return
    whens = [a.published_at or a.collected_at for a in arts]
    c.first_seen, c.last_seen, c.article_count = min(whens), max(whens), len(arts)
    if not any(a.title == c.title for a in arts):
        c.title = min(arts, key=lambda a: a.published_at or a.collected_at).title


def refile_post(db: Session, art: Article) -> set[int]:
    """After a post's date is corrected, move it to the story it belongs to at its real date.

    Returns the ids of stories that changed (for re-summarising)."""
    old = art.cluster
    art.cluster = None
    db.flush()
    new = assign_cluster(db, art)
    db.flush()
    refresh_cluster(db, new)
    touched = {new.id}
    if old is not None and old.id != new.id:
        refresh_cluster(db, old)
        if old in db:
            touched.add(old.id)
    db.flush()
    return touched


def store_items(db: Session, source: Source | None, subject: Subject | None, items: list[RawItem]) -> dict:
    """Insert new items, link subjects on duplicates, return counters and touched cluster ids."""
    home = get_settings().home_country
    subjects = db.scalars(select(Subject).where(Subject.enabled.is_(True))).all()
    outlet_cache: dict = {}
    stats = {"fetched": len(items), "new": 0, "duplicates": 0, "skipped": 0, "clusters": set(), "delays": []}
    # General outlet feeds (RSS / YouTube channels) are filtered for relevance; subject searches and
    # trade-press feeds are relevant by construction.
    filter_general = (get_settings().relevance_filter and source is not None and source.kind in ("rss", "youtube_channel")
                      and (source.outlet_kind or "") != "Trade press")
    seen_in_batch: set[str] = set()
    now = utcnow()

    for it in items:
        if not it.title or not it.url:
            continue
        outlet = resolve_outlet(db, it, outlet_cache)
        canon = canonical_url(it.url)
        fp = fingerprint(it.title, outlet.name)
        if canon in seen_in_batch or fp in seen_in_batch:
            stats["duplicates"] += 1
            continue
        seen_in_batch.update((canon, fp))
        existing = db.scalar(select(Article).where(or_(Article.canonical_url == canon, Article.fingerprint == fp)))
        text = f"{it.title}. {it.description or ''}"
        linked = match_subjects(text, subjects)
        if subject is not None and subject not in linked:
            linked.append(subject)  # the search engine matched it on this subject (may be in body text)
        cats = categorize(text)
        if filter_general and not is_relevant(cats, linked):
            stats["skipped"] += 1
            continue
        if existing is not None:
            stats["duplicates"] += 1
            for sj in linked:
                if sj not in existing.subjects:
                    existing.subjects.append(sj)
            # Prefer a direct publisher URL over a Google News redirect, and fill gaps.
            if "news.google.com" in existing.url and "news.google.com" not in it.url:
                existing.url = it.url
            if not existing.author and it.author:
                existing.author = it.author
            if existing.published_is_estimate and it.published_at and not it.published_is_estimate:
                existing.published_at, existing.published_is_estimate = it.published_at, False
            continue

        if it.published_at and it.published_at > now + timedelta(hours=1):
            it.published_at = now  # guard against bad feed clocks
        # Estimated YouTube dates are queued for enrich.enrich_video_dates, which reads the exact upload date.
        needs_date = it.platform == "YouTube" and (it.published_is_estimate or it.published_at is None)
        tone, score = tone_of(text)
        art = Article(
            kind=it.kind, url=it.url, canonical_url=canon, fingerprint=fp, title=it.title[:1000],
            description=(it.description or None), author=it.author, outlet=outlet,
            source_id=source.id if source else None,
            published_at=it.published_at, published_is_estimate=it.published_is_estimate or it.published_at is None,
            collected_at=now, language=it.language or outlet.language,
            scope="National" if (outlet.country or "") == home else "International",
            categories=cats, tone=tone, tone_score=score,
            is_live=it.is_live or looks_live(it.title), platform=it.platform, thumbnail=it.thumbnail,
            enriched=(bool(it.author) or it.kind != "article") and not needs_date,
        )
        art.subjects = linked
        db.add(art)
        db.flush()
        cluster = assign_cluster(db, art)
        stats["clusters"].add(cluster.id)
        stats["new"] += 1
        # Collection delay is only meaningful for items published since this source last succeeded;
        # first runs and search backfills pick up old items and would inflate it.
        last_ok = source.last_success_at if source else None
        if last_ok and it.published_at and not it.published_is_estimate and it.published_at >= last_ok:
            stats["delays"].append((now - it.published_at).total_seconds() / 60)
    stats["median_delay"] = statistics.median(stats["delays"]) if stats["delays"] else None
    return stats
