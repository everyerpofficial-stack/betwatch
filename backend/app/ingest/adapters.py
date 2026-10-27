"""Source adapters. Each adapter turns one source (optionally for one subject) into a list of RawItem.

Adding a publisher = adding a Source row (admin panel or seed). Adding a new *type* of source = one function here
plus an entry in ADAPTERS.
"""
import calendar
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
import feedparser
import httpx
from dateutil import parser as dateparser

log = logging.getLogger("betwatch.adapters")

from ..config import get_settings
from .text import clean_text, domain_of, strip_outlet_suffix


@dataclass
class RawItem:
    title: str
    url: str
    outlet_name: str
    kind: str = "article"  # article | video | social
    description: str = ""
    author: str | None = None
    published_at: datetime | None = None
    published_is_estimate: bool = False
    outlet_domain: str | None = None
    outlet_kind: str | None = None
    outlet_country: str | None = None
    language: str | None = None
    platform: str | None = None
    thumbnail: str | None = None
    is_live: bool = False
    extra: dict = field(default_factory=dict)


class AdapterError(Exception):
    pass


class RateLimited(AdapterError):
    pass


def http_client() -> httpx.Client:
    s = get_settings()
    return httpx.Client(timeout=s.http_timeout_seconds, follow_redirects=True,
                        headers={"User-Agent": s.user_agent, "Accept-Language": "en-IN,en;q=0.9"})


def _get(client: httpx.Client, url: str, **kw) -> httpx.Response:
    r = client.get(url, **kw)
    if r.status_code == 429:
        raise RateLimited(f"429 rate limited by {domain_of(url)}")
    if r.status_code >= 400:
        raise AdapterError(f"HTTP {r.status_code} from {domain_of(url)}")
    return r


def _struct_to_dt(st) -> datetime | None:
    if not st:
        return None
    return datetime.fromtimestamp(calendar.timegm(st), UTC).replace(tzinfo=None)


def _entry_dt(e) -> datetime | None:
    return _struct_to_dt(e.get("published_parsed") or e.get("updated_parsed"))


def _entry_author(e) -> str | None:
    a = e.get("author") or e.get("dc_creator")
    if not a and e.get("authors"):
        a = ", ".join(x.get("name", "") for x in e["authors"] if x.get("name"))
    a = clean_text(a, 300) if a else None
    return a or None


# --- RSS / Atom feed of one outlet -------------------------------------------------------------------------------
STALE_FEED_DAYS = 45


def rss(source, subject, client) -> list[RawItem]:
    r = _get(client, source.url)
    feed = feedparser.parse(r.content)
    if feed.bozo and not feed.entries:
        raise AdapterError(f"Unparseable feed: {feed.bozo_exception}")
    # A feed whose newest entry is months old is frozen (moved / cached by a CDN); flag it instead of reporting "ok".
    dates = [d for d in (_entry_dt(e) for e in feed.entries) if d]
    if dates and max(dates) < datetime.now(UTC).replace(tzinfo=None) - timedelta(days=STALE_FEED_DAYS):
        raise AdapterError(f"Feed is stale: newest item is from {max(dates):%d %b %Y}. Find the outlet's current feed URL.")
    items = []
    for e in feed.entries:
        link = e.get("link")
        title = clean_text(e.get("title"))
        if not link or not title:
            continue
        thumb = None
        if e.get("media_thumbnail"):
            thumb = e["media_thumbnail"][0].get("url")
        elif e.get("media_content"):
            thumb = e["media_content"][0].get("url")
        items.append(RawItem(
            title=title, url=link, outlet_name=source.outlet_name or clean_text(feed.feed.get("title")),
            description=clean_text(e.get("summary"), 400), author=_entry_author(e), published_at=_entry_dt(e),
            outlet_domain=domain_of(feed.feed.get("link") or link), outlet_kind=source.outlet_kind or "News site",
            outlet_country=source.country, language=source.language, thumbnail=thumb,
        ))
    return items


# --- YouTube channel feed (no API key) ---------------------------------------------------------------------------
def youtube_channel(source, subject, client) -> list[RawItem]:
    r = _get(client, source.url)
    feed = feedparser.parse(r.content)
    items = []
    for e in feed.entries:
        vid = e.get("yt_videoid")
        if not vid:
            continue
        desc = ""
        if e.get("media_description"):
            desc = e["media_description"]
        elif e.get("summary"):
            desc = e["summary"]
        thumb = e["media_thumbnail"][0]["url"] if e.get("media_thumbnail") else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        items.append(RawItem(
            title=clean_text(e.get("title")), url=f"https://www.youtube.com/watch?v={vid}",
            outlet_name=source.outlet_name or clean_text(e.get("author")), kind="video",
            description=clean_text(desc, 400), published_at=_entry_dt(e), outlet_domain="youtube.com",
            outlet_kind="TV / video", outlet_country=source.country, language=source.language,
            platform="YouTube", thumbnail=thumb,
        ))
    return items


# --- Google News search (per subject) ----------------------------------------------------------------------------
# NOTE: Google states this feed is for personal, non-commercial use in a feed reader. Review before commercial use.
def google_news(source, subject, client) -> list[RawItem]:
    if subject is None:
        return []
    tmpl = source.url or "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
    m = re.search(r"[?&]gl=([A-Za-z]{2})", tmpl)
    edition = m.group(1).upper() if m else None
    r = _get(client, tmpl.format(q=quote_plus(subject.query)))
    feed = feedparser.parse(r.content)
    items = []
    for e in feed.entries:
        src = e.get("source") or {}
        outlet = clean_text(src.get("title")) or "Unknown outlet"
        items.append(RawItem(
            title=strip_outlet_suffix(clean_text(e.get("title")), outlet), url=e.get("link"), outlet_name=outlet,
            outlet_domain=domain_of(src.get("href") or ""), outlet_kind="News site", outlet_country=edition,
            published_at=_entry_dt(e), language=source.language, extra={"via": "google_news"},
        ))
    return items


# --- GDELT DOC 2.0 API (per subject, free, global) ----------------------------------------------------------------
GDELT_COUNTRIES = {"India": "IN", "United States": "US", "United Kingdom": "GB", "Bangladesh": "BD", "Pakistan": "PK",
                   "Nepal": "NP", "Sri Lanka": "LK", "Australia": "AU", "Canada": "CA", "New Zealand": "NZ",
                   "Singapore": "SG", "United Arab Emirates": "AE", "Philippines": "PH", "Malta": "MT"}


def gdelt(source, subject, client) -> list[RawItem]:
    if subject is None:
        return []
    q = subject.query if " " not in subject.query or subject.query.startswith('"') else f'"{subject.query}"'
    # First successful run backfills 3 months; later runs only look at the configured recent window.
    timespan = "3months" if source.last_success_at is None else ((source.notes or "").strip() or "1week")
    url = ("https://api.gdeltproject.org/api/v2/doc/doc?mode=artlist&format=json&maxrecords=250&sort=datedesc"
           f"&timespan={quote_plus(timespan)}&query={quote_plus(q)}")
    r = _get(client, url, timeout=45)
    if not r.text.strip().startswith("{"):
        # GDELT returns plain-text errors (e.g. query too short / too common)
        raise AdapterError("GDELT: " + r.text.strip()[:200])
    data = r.json()
    items = []
    for a in data.get("articles", []):
        seen = None
        if a.get("seendate"):
            seen = datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ")
        items.append(RawItem(
            title=clean_text(a.get("title")), url=a.get("url"), outlet_name=a.get("domain") or domain_of(a.get("url")),
            outlet_domain=a.get("domain"), outlet_kind="News site",
            outlet_country=GDELT_COUNTRIES.get(a.get("sourcecountry") or ""),
            published_at=seen, published_is_estimate=True,  # GDELT gives first-seen time, not publish time
            language=(a.get("language") or "")[:10] or None, thumbnail=a.get("socialimage") or None,
            extra={"via": "gdelt"},
        ))
    return items


# --- YouTube Data API search (per subject, needs YOUTUBE_API_KEY) -------------------------------------------------
# --- YouTube Search (with API key or web search fallback) ---------------------------------------------------
_AGE_RE = re.compile(r"(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|wks?|months?|mos?|years?|yrs?)\b")
_AGE_UNIT = {"s": timedelta(seconds=1), "mi": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1),
             "w": timedelta(weeks=1), "mo": timedelta(days=30), "y": timedelta(days=365)}


def parse_relative_age(text: str, now: datetime) -> datetime | None:
    """'3 yr ago', '2 months ago', '5 hr ago', 'Streamed 1 day ago' -> approximate datetime. None if unparseable.

    YouTube switched to abbreviated units (yr, mo, wk, hr, min); the old full-word-only parser stamped every
    unparsed video with the collection time, so years-old videos appeared as today's news.
    """
    m = _AGE_RE.search((text or "").lower())
    if not m:
        return None
    unit = m.group(2)
    key = "mo" if unit.startswith("mo") else "mi" if unit.startswith("mi") else unit[0]
    return now - int(m.group(1)) * _AGE_UNIT[key]


def _scrape_youtube_search(q: str, client: httpx.Client) -> list[RawItem]:
    try:
        r = client.get("https://www.youtube.com/results", params={"search_query": q},
                       headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        m = re.search(r"var ytInitialData = ({.*?});</script>", r.text) or re.search(r"ytInitialData\s*=\s*({.+?});", r.text)
        if not m:
            return []
        data = json.loads(m.group(1))
        contents = (data.get("contents", {})
                    .get("twoColumnSearchResultsRenderer", {})
                    .get("primaryContents", {})
                    .get("sectionListRenderer", {})
                    .get("contents", []))
        items = []
        now = datetime.now(UTC).replace(tzinfo=None)
        for section in contents:
            for item in section.get("itemSectionRenderer", {}).get("contents", []):
                v = item.get("videoRenderer")
                if not v:
                    continue
                vid = v.get("videoId")
                if not vid:
                    continue
                title = (v.get("title", {}).get("runs", [{}])[0].get("text") or "").strip()
                channel = (v.get("ownerText", {}).get("runs", [{}])[0].get("text") or "YouTube channel").strip()
                desc = "".join(run.get("text", "") for run in v.get("detailedMetadataSnippets", [{}])[0].get("snippetText", {}).get("runs", []))
                thumb = v.get("thumbnail", {}).get("thumbnails", [{}])[-1].get("url") if v.get("thumbnail") else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                pub_text = (v.get("publishedTimeText", {}).get("simpleText") or "").lower()
                live_now = any("LIVE" in (b.get("metadataBadgeRenderer", {}).get("style") or "")
                               for b in v.get("badges", []))
                # Unknown age stays None (not "now"); enrich.enrich_video_dates reads the exact date from the watch page.
                pub_dt = now if live_now else parse_relative_age(pub_text, now)
                is_est = True

                items.append(RawItem(
                    title=clean_text(title),
                    url=f"https://www.youtube.com/watch?v={vid}",
                    outlet_name=clean_text(channel) or "YouTube channel",
                    kind="video",
                    description=clean_text(desc, 400),
                    published_at=pub_dt,
                    published_is_estimate=is_est,
                    outlet_domain="youtube.com",
                    outlet_kind="YouTube channel",
                    platform="YouTube",
                    thumbnail=thumb,
                    is_live=live_now,  # "Streamed 2 days ago" is a past stream, not live
                ))
        return items
    except Exception as e:
        log.warning("Scrape youtube search failed for '%s': %s", q, e)
        return []


def youtube_search(source, subject, client) -> list[RawItem]:
    if subject is None:
        return []
    key = get_settings().youtube_api_key
    if not key:
        return _scrape_youtube_search(subject.query, client)
    items = []
    for live in (False, True):
        params = {"part": "snippet", "q": subject.query, "type": "video", "maxResults": 25, "key": key,
                  "order": "date", "regionCode": "IN", "relevanceLanguage": "en"}
        if live:
            params["eventType"] = "live"
        r = _get(client, "https://www.googleapis.com/youtube/v3/search", params=params)
        for it in r.json().get("items", []):
            sn = it.get("snippet", {})
            vid = it.get("id", {}).get("videoId")
            if not vid:
                continue
            items.append(RawItem(
                title=clean_text(sn.get("title")), url=f"https://www.youtube.com/watch?v={vid}",
                outlet_name=clean_text(sn.get("channelTitle")) or "YouTube channel", kind="video",
                description=clean_text(sn.get("description"), 400),
                published_at=dateparser.isoparse(sn["publishedAt"]).replace(tzinfo=None) if sn.get("publishedAt") else None,
                outlet_domain="youtube.com", outlet_kind="YouTube channel", platform="YouTube",
                thumbnail=(sn.get("thumbnails", {}).get("high") or {}).get("url"),
                is_live=live or sn.get("liveBroadcastContent") == "live",
            ))
    return items


# --- Instagram Post & Search (reels, posts) ----------------------------------------------------------------------
# og:description comes as '2,497 likes, 58 comments - mib_india on August 20, 2025: "caption"', with " - " instead
# of " on ", or without the likes prefix ('indianexpress on April 18, 2025: "caption"').
_IG_DESC_RE = re.compile(r"(?:^|-\s+)[a-zA-Z0-9._]+\s+(?:on|-)\s+([A-Za-z]+\s+\d{1,2},\s*\d{4}):\s*[\"']?(.*)$", re.DOTALL)


def split_instagram_description(og_desc: str) -> tuple[datetime | None, str]:
    """(post date, caption) from an Instagram og:description; (None, og_desc) if it has no date."""
    m = _IG_DESC_RE.search(og_desc or "")
    if not m:
        return None, og_desc
    try:
        when = dateparser.parse(m.group(1).strip())
    except (ValueError, OverflowError):
        when = None
    return when, m.group(2).strip().rstrip("\"' .")


def parse_instagram_post(url: str, client: httpx.Client | None = None) -> RawItem | None:
    try:
        headers = {"User-Agent": "facebookexternalhit/1.1", "Accept": "*/*"}
        if client is not None:
            r = client.get(url, headers=headers)
        else:
            with httpx.Client(timeout=15, follow_redirects=True) as cl:
                r = cl.get(url, headers=headers)
        if r.status_code >= 400:
            return None
        soup = BeautifulSoup(r.text, "html.parser")

        def get_meta(attr, val):
            tag = soup.find("meta", {attr: val})
            return tag["content"] if tag and tag.get("content") else None

        og_title = get_meta("property", "og:title") or ""
        tw_title = get_meta("name", "twitter:title") or ""
        og_desc = get_meta("property", "og:description") or get_meta("name", "description") or ""
        og_url = get_meta("property", "og:url") or str(r.url)
        og_img = get_meta("property", "og:image") or get_meta("name", "twitter:image")

        name = None
        handle = None

        m_tw = re.search(r"^(.*?)\s*\(@([a-zA-Z0-9._]+)\)", tw_title)
        if m_tw:
            name = m_tw.group(1).strip()
            handle = m_tw.group(2).strip()

        if not handle:
            m_u = re.search(r"instagram\.com/([a-zA-Z0-9._]+)/(?:reel|p)/", og_url)
            if m_u and m_u.group(1) not in ("reel", "p", "tv"):
                handle = m_u.group(1)

        if not name and " on Instagram:" in og_title:
            name = og_title.split(" on Instagram:", 1)[0].strip()
        elif not name and " on Instagram" in og_title:
            name = og_title.split(" on Instagram", 1)[0].strip()

        pub_date, caption = split_instagram_description(og_desc)

        if not caption and " on Instagram: " in og_title:
            caption = og_title.split(" on Instagram: ", 1)[1].strip().strip("\"'")

        first_line = caption.split("\n")[0] if caption else ""
        title = first_line[:200] if first_line else (og_title[:200] or "Instagram post")
        outlet = f"@{handle}" if handle else (name or "Instagram")

        return RawItem(
            title=clean_text(title, 300),
            url=og_url,
            outlet_name=outlet,
            kind="social",
            description=clean_text(caption or "", 500),
            author=name or (f"@{handle}" if handle else None),
            published_at=pub_date,
            published_is_estimate=pub_date is None,
            outlet_domain="instagram.com",
            outlet_kind="Social",
            platform="Instagram",
            thumbnail=og_img,
            extra={"via": "instagram"},
        )
    except Exception as e:
        log.warning("parse_instagram_post error on %s: %s", url, e)
        return None


def instagram_search(source, subject, client) -> list[RawItem]:
    items = []
    # If source points to a specific Instagram URL, ingest that post
    if source and source.url and "instagram.com" in source.url:
        item = parse_instagram_post(source.url, client)
        if item:
            items.append(item)
    if subject is None:
        return items
    gnews_url = f"https://news.google.com/rss/search?q=site:instagram.com+{quote_plus(subject.query)}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        r = _get(client, gnews_url)
        feed = feedparser.parse(r.content)
        from googlenewsdecoder import gnewsdecoder
        for e in feed.entries[:10]:
            try:
                decoded = gnewsdecoder(e.get("link", ""))
                dec_url = decoded.get("decoded_url") if isinstance(decoded, dict) else None
                if dec_url and ("instagram.com/reel/" in dec_url or "instagram.com/p/" in dec_url):
                    item = parse_instagram_post(dec_url, client)
                    if item:
                        items.append(item)
            except Exception:
                continue
    except Exception as e:
        log.warning("instagram_search error on '%s': %s", subject.query, e)
    return items


# --- Reddit search (per subject, needs REDDIT_CLIENT_ID/SECRET) ---------------------------------------------------
_reddit_token: dict = {}


def reddit(source, subject, client) -> list[RawItem]:
    s = get_settings()
    if not (s.reddit_client_id and s.reddit_client_secret) or subject is None:
        return []
    if not _reddit_token or _reddit_token["exp"] < datetime.now(UTC).replace(tzinfo=None):
        r = client.post("https://www.reddit.com/api/v1/access_token", data={"grant_type": "client_credentials"},
                        auth=(s.reddit_client_id, s.reddit_client_secret))
        if r.status_code >= 400:
            raise AdapterError(f"Reddit auth failed: HTTP {r.status_code}")
        tok = r.json()
        _reddit_token.update(token=tok["access_token"], exp=datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=tok.get("expires_in", 3600) - 60))
    r = _get(client, "https://oauth.reddit.com/search", params={"q": subject.query, "sort": "new", "limit": 50, "t": "month"},
             headers={"Authorization": f"bearer {_reddit_token['token']}"})
    items = []
    for ch in r.json().get("data", {}).get("children", []):
        d = ch.get("data", {})
        items.append(RawItem(
            title=clean_text(d.get("title")), url="https://www.reddit.com" + d.get("permalink", ""),
            outlet_name=f"r/{d.get('subreddit')} · Reddit", kind="social", description=clean_text(d.get("selftext"), 300),
            author=f"u/{d.get('author')}", published_at=datetime.fromtimestamp(d.get("created_utc", 0), UTC).replace(tzinfo=None),
            outlet_domain="reddit.com", outlet_kind="Social", platform="Reddit",
        ))
    return items


# --- Bluesky search (per subject, needs BLUESKY_HANDLE + app password) --------------------------------------------
def bluesky(source, subject, client) -> list[RawItem]:
    s = get_settings()
    if not (s.bluesky_handle and s.bluesky_app_password) or subject is None:
        return []
    r = client.post("https://bsky.social/xrpc/com.atproto.server.createSession",
                    json={"identifier": s.bluesky_handle, "password": s.bluesky_app_password})
    if r.status_code >= 400:
        raise AdapterError(f"Bluesky auth failed: HTTP {r.status_code}")
    jwt = r.json()["accessJwt"]
    r = _get(client, "https://bsky.social/xrpc/app.bsky.feed.searchPosts",
             params={"q": subject.query, "sort": "latest", "limit": 50}, headers={"Authorization": f"Bearer {jwt}"})
    items = []
    for p in r.json().get("posts", []):
        handle = p.get("author", {}).get("handle", "unknown")
        rkey = p.get("uri", "").rsplit("/", 1)[-1]
        text = clean_text(p.get("record", {}).get("text"))
        items.append(RawItem(
            title=text[:200] or "(no text)", url=f"https://bsky.app/profile/{handle}/post/{rkey}",
            outlet_name=f"@{handle} · Bluesky", kind="social", description=text[:300],
            author=p.get("author", {}).get("displayName") or handle,
            published_at=dateparser.isoparse(p["record"]["createdAt"]).replace(tzinfo=None) if p.get("record", {}).get("createdAt") else None,
            outlet_domain="bsky.app", outlet_kind="Social", platform="Bluesky",
        ))
    return items


ADAPTERS = {
    "rss": rss,
    "youtube_channel": youtube_channel,
    "google_news": google_news,
    "gdelt": gdelt,
    "youtube_search": youtube_search,
    "reddit": reddit,
    "bluesky": bluesky,
    "instagram_search": instagram_search,
}
PER_SUBJECT_KINDS = {"google_news", "gdelt", "youtube_search", "reddit", "bluesky", "instagram_search"}
