"""Byline enrichment: read the author from an article page's metadata (not its body text).

Only runs for direct publisher URLs, only if robots.txt allows, only for public hosts (SSRF guard), and reads
<meta> / JSON-LD only. Paywalled or blocked pages are skipped, never bypassed.
"""
import json
import logging
import re
from datetime import UTC

import httpx
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..ai import summarize_clusters
from ..config import get_settings
from ..models import Article
from ..security import is_public_http_url, robots_allows
from .adapters import split_instagram_description
from .pipeline import refile_post
from .text import canonical_url, clean_text

log = logging.getLogger("betwatch.enrich")
BAD_AUTHORS = {"admin", "staff", "web desk", "desk", "news desk", "online desk", "editor", "team", "unknown"}


def _ok_name(name: str | None, outlet: str | None = None) -> str | None:
    if not name:
        return None
    name = clean_text(name, 200).removeprefix("By ").removeprefix("by ").strip()
    if not name or name.startswith(("@", "http")) or name.lower() in BAD_AUTHORS or len(name) < 3:
        return None
    if outlet and name.lower().replace("the ", "") == outlet.lower().replace("the ", ""):
        return None  # outlet credited as its own author
    return name


def author_from_html(html: str, outlet: str | None = None) -> str | None:
    soup = BeautifulSoup(html[:600_000], "html.parser")
    # 1) JSON-LD NewsArticle/Article author — the most reliable byline source
    for name in _jsonld_authors(soup):
        ok = _ok_name(name, outlet)
        if ok:
            return ok
    # 2) meta tags
    for attrs in ({"name": "author"}, {"property": "article:author"}, {"name": "parsely-author"},
                  {"name": "byl"}, {"property": "og:article:author"}):
        tag = soup.find("meta", attrs=attrs)
        ok = _ok_name(tag.get("content") if tag else None, outlet)
        if ok:
            return ok
    return None


def _jsonld_authors(soup) -> list[str]:
    out = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        nodes = data if isinstance(data, list) else (data.get("@graph", [data]) if isinstance(data, dict) else [])
        for node in nodes:
            if not isinstance(node, dict):
                continue
            a = node.get("author")
            if isinstance(a, list):
                names = [x.get("name") for x in a if isinstance(x, dict) and isinstance(x.get("name"), str)]
                names = [n for n in names if _ok_name(n)]
                if names:
                    out.append(", ".join(names))
            elif isinstance(a, dict) and isinstance(a.get("name"), str):
                out.append(a["name"])
            elif isinstance(a, str):
                out.append(a)
    return out


def resolve_google_news(db: Session, art: Article) -> bool:
    """Swap a news.google.com redirect for the publisher URL (open-source googlenewsdecoder, MIT).
    If the publisher URL is already stored from another feed, keep this post as-is (it stays deduplicated by
    fingerprint) and skip enrichment."""
    try:
        from googlenewsdecoder import gnewsdecoder
    except ImportError:
        return False
    try:
        res = gnewsdecoder(art.url, interval=1)
    except Exception as e:  # network / format change in Google's redirect
        log.info("google decode failed: %s", e)
        return False
    direct = res.get("decoded_url")  # "status" is not set reliably by the library; the URL is what matters
    if not direct or not direct.startswith("http"):
        return False
    canon = canonical_url(direct)
    clash = db.scalar(select(Article.id).where(Article.canonical_url == canon, Article.id != art.id))
    if clash:
        return False
    art.url, art.canonical_url = direct, canon
    return True


def enrich_bylines(db: Session, client: httpx.Client, limit: int | None = None) -> int:
    s = get_settings()
    if not s.enrich_bylines:
        return 0
    todo = db.scalars(
        select(Article).where(Article.enriched.is_(False), Article.kind == "article")
        .order_by(Article.collected_at.desc()).limit(limit or s.enrich_max_per_run)
    ).all()
    found = 0
    for art in todo:
        art.enriched = True  # one attempt per article
        if "news.google.com" in art.url and not resolve_google_news(db, art):
            continue
        url = art.url
        if not is_public_http_url(url) or not robots_allows(url, client):
            continue
        try:
            r = client.get(url, timeout=12)
        except httpx.HTTPError:
            continue
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            continue
        name = author_from_html(r.text, art.outlet.name)
        if name:
            art.author = name
            found += 1
    return found


_UPLOAD_DATE_RE = re.compile(r'<meta itemprop="(?:datePublished|uploadDate)" content="([^"]+)"')


def requeue_estimated_video_dates(db: Session) -> int:
    """Queue every YouTube video that still has an estimated date for one exact-date lookup. Run at worker start,
    so videos collected before this fix (stamped with the collection time) get their real dates too."""
    res = db.execute(update(Article).where(Article.platform == "YouTube", Article.published_is_estimate.is_(True),
                                           Article.enriched.is_(True)).values(enriched=False))
    return res.rowcount or 0


def repair_undated_instagram(db: Session) -> int:
    """Instagram posts stored before the og:description parser understood every layout have no date (so they
    showed as collected today) and the raw "2,497 likes, 58 comments - handle - date:" text as headline. Re-read
    the date and caption from the stored text; no network needed."""
    fixed, touched = 0, set()
    for art in db.scalars(select(Article).where(Article.platform == "Instagram", Article.published_at.is_(None))).all():
        when, caption = split_instagram_description(art.description or art.title)
        if when is None:
            continue
        art.published_at, art.published_is_estimate = when, False
        art.title = clean_text(caption.split("\n")[0], 300) or art.title
        art.description = clean_text(caption, 500) or art.description
        touched |= refile_post(db, art)
        fixed += 1
    summarize_clusters(db, touched)
    return fixed


def enrich_video_dates(db: Session, client: httpx.Client, limit: int | None = None) -> int:
    """Replace estimated YouTube dates ("3 yr ago") with the exact upload date from the watch page's
    <meta itemprop="datePublished"> (YouTube's robots.txt allows /watch), then move the video to the story it
    belongs to at that date. Returns how many dates were corrected."""
    s = get_settings()
    todo = db.scalars(
        select(Article).where(Article.platform == "YouTube", Article.published_is_estimate.is_(True),
                              Article.enriched.is_(False))
        .order_by(Article.collected_at.desc()).limit(limit or s.enrich_max_per_run)
    ).all()
    fixed, touched = 0, set()
    for art in todo:
        art.enriched = True  # one attempt per video (requeued once per worker start)
        if not robots_allows(art.url, client):
            continue
        try:
            r = client.get(art.url, timeout=12)
        except httpx.HTTPError:
            continue
        m = _UPLOAD_DATE_RE.search(r.text) if r.status_code == 200 else None
        if not m:
            continue
        try:
            when = dateparser.isoparse(m.group(1))
        except ValueError:
            continue
        if when.tzinfo:
            when = when.astimezone(UTC).replace(tzinfo=None)
        art.published_at, art.published_is_estimate = when, False
        touched |= refile_post(db, art)
        fixed += 1
    summarize_clusters(db, touched)
    return fixed
