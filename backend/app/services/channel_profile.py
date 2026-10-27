"""Channel profile service: Wikidata integration, audience metrics, track record, and coverage statistics."""
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote_plus

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    Article,
    Outlet,
    OutletLink,
    OutletManualFact,
    OutletMetric,
    OutletProfile,
    Subject,
    article_subjects,
    utcnow,
)
from ..services.dashboard import fmt, iso

log = logging.getLogger("betwatch.channel_profile")

WIKIDATA_SEARCH_URL = "https://www.wikidata.org/w/api.php?action=wbsearchentities&search={query}&language=en&format=json"
WIKIDATA_ENTITY_URL = "https://www.wikidata.org/w/api.php?action=wbgetentities&ids={ids}&props=claims|labels|descriptions&languages=en&format=json"


def _format_wikidata_time(time_str: str) -> str:
    """Format Wikidata timestamp like '+1838-11-03T00:00:00Z' into '1838' or date string."""
    if not time_str:
        return ""
    m = re.match(r"^[+-]?(\d{4})", time_str)
    return m.group(1) if m else time_str.split("T")[0]


def parse_wikidata_entity(entity_data: dict, labels_cache: dict[str, str] | None = None) -> dict:
    """Parse a single Wikidata entity dictionary into clean profile fields and official links.

    Pure function with no external I/O: safe for testing with labelled fixture JSON files.
    """
    labels_cache = labels_cache or {}
    claims = entity_data.get("claims", {})

    def claim_val(prop: str) -> str | None:
        cl_list = claims.get(prop)
        if not cl_list:
            return None
        snak = cl_list[0].get("mainsnak", {})
        dv = snak.get("datavalue", {})
        val = dv.get("value")
        if isinstance(val, dict):
            # Check for label inside fixture or resolve ID
            if "label" in val:
                return val["label"]
            if "id" in val:
                return labels_cache.get(val["id"])  # an unresolved ID ("Q112247183") is not a readable fact
            if "time" in val:
                return _format_wikidata_time(val["time"])
        elif isinstance(val, str):
            return val
        return None

    founded = claim_val("P571")
    owner = claim_val("P127") or claim_val("P749")
    headquarters = claim_val("P159")
    official_website = claim_val("P856")
    stock_exchange = claim_val("P414")

    # Ticker as qualifier P249 on P414
    ticker = None
    p414_claims = claims.get("P414", [])
    if p414_claims:
        quals = p414_claims[0].get("qualifiers", {}).get("P249", [])
        if quals:
            dv = quals[0].get("datavalue", {})
            ticker = dv.get("value") if isinstance(dv.get("value"), str) else None

    # Social & platform links
    links = []
    if official_website:
        links.append({"platform": "website", "url": official_website, "source": "wikidata"})

    handle_map = [
        ("P2002", "twitter", "https://x.com/{}"),
        ("P2003", "instagram", "https://instagram.com/{}"),
        ("P2013", "facebook", "https://facebook.com/{}"),
        ("P2397", "youtube", "https://www.youtube.com/channel/{}"),
        ("P4264", "linkedin", "https://www.linkedin.com/company/{}"),
        ("P3789", "telegram", "https://t.me/{}"),
    ]

    for prop, platform, url_tpl in handle_map:
        val = claim_val(prop)
        if val:
            full_url = val if val.startswith("http") else url_tpl.format(val.lstrip("@"))
            links.append({"platform": platform, "url": full_url, "source": "wikidata"})

    return {
        "qid": entity_data.get("id"),
        "label": entity_data.get("labels", {}).get("en", {}).get("value"),
        "description": entity_data.get("descriptions", {}).get("en", {}).get("value"),
        "founded": founded,
        "owner": owner,
        "headquarters": headquarters,
        "official_website": official_website,
        "stock_exchange": stock_exchange,
        "ticker": ticker,
        "links": links,
    }


def clean_outlet_name(name: str) -> str:
    """Clean outlet name to base publication name for Wikidata & public registry resolution."""
    if not name:
        return ""
    n = re.sub(r"\s*[·\-–|]\s*(YouTube|Twitter|Instagram|Facebook|Video|Digital|Live).*$", "", name, flags=re.IGNORECASE)
    n = re.sub(r"\b(Hindi|English|Tamil|Telugu|Bengali|Marathi|Gujarati|Urdu|Live TV|News Channel)\b.*$", "", n, flags=re.IGNORECASE).strip()
    return n or name


# Wikidata IDs for outlets whose name search is ambiguous ("Mint" finds the plant genus first). Each was checked
# with wbgetentities on 27 Oct 2026. Only the ID lives here: founded / owner / HQ / links are read from Wikidata, and
# audience numbers come from live APIs or show "Not available". Never hard-code figures in this file.
KNOWN_QIDS: dict[str, str] = {
    "india tv": "Q6019415",
    "aaj tak": "Q3396358",
    "ndtv": "Q2985356",
    "times of india": "Q46807",
    "hindustan times": "Q41595",
    "indian express": "Q1954843",
    "india today": "Q1661219",
    "mint": "Q2271133",
    "economic times": "Q1954757",
    "zee news": "Q5287969",
    "dainik bhaskar": "Q1872524",
    "the hindu": "Q926175",
    "dream11": "Q53080990",
    "igaming business": "Q18350679",
    "business today": "Q5001804",
}

# Channels and accounts hosted on these sites are not the site itself: searching Wikidata for "youtube.com" returns
# YouTube (Q866), which gave every YouTube channel YouTube Inc.'s founding date, owner and social links.
PLATFORM_DOMAINS = {"youtube.com", "instagram.com", "reddit.com", "bsky.app", "facebook.com", "x.com", "twitter.com",
                    "t.me", "linkedin.com", "tiktok.com"}

# A name-search hit is accepted only if its label equals the outlet name and its description says it is media.
MEDIA_DESC_RE = re.compile(r"\b(news|newspaper|channel|television|tv|magazine|media|broadcast\w*|publication|publisher|"
                           r"website|portal|daily|journal|agency|radio|youtuber|tabloid)\b", re.IGNORECASE)


def known_qid(name: str) -> str | None:
    """Verified QID for an outlet by exact (cleaned, case-insensitive) name. No substring matching: "NDTV Profit"
    is not NDTV and "DREAM11 EXPERT" is not Dream11."""
    return KNOWN_QIDS.get(clean_outlet_name(name).lower().strip())


def site_domain(url_or_domain: str | None) -> str:
    d = (url_or_domain or "").lower().strip()
    d = re.sub(r"^https?://", "", d).split("/", 1)[0]
    return d.removeprefix("www.")


def is_platform_outlet(outlet: Outlet) -> bool:
    d = site_domain(outlet.domain)
    return any(d == p or d.endswith("." + p) for p in PLATFORM_DOMAINS)


def _norm_label(s: str | None) -> str:
    s = re.sub(r"[^\w]+", " ", (s or "").lower()).strip()
    return s.removeprefix("the ").strip()


def search_wikidata(query: str) -> list[dict]:
    """Search Wikidata for candidate entities matching the query."""
    settings = get_settings()
    headers = {"User-Agent": settings.user_agent}
    url = WIKIDATA_SEARCH_URL.format(query=quote_plus(query))
    try:
        with httpx.Client(timeout=10.0, headers=headers) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                results = []
                for item in data.get("search", [])[:8]:
                    results.append({
                        "id": item.get("id"),
                        "label": item.get("label"),
                        "description": item.get("description"),
                        "url": f"https://www.wikidata.org/wiki/{item.get('id')}",
                    })
                return results
    except Exception as e:
        log.warning("Wikidata search failed for %s: %s", query, e)
    return []


def fetch_wikidata_entity(qid: str) -> dict | None:
    """Fetch entity JSON from Wikidata API and resolve entity references."""
    settings = get_settings()
    headers = {"User-Agent": settings.user_agent}
    url = WIKIDATA_ENTITY_URL.format(ids=qid)
    try:
        with httpx.Client(timeout=10.0, headers=headers) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                entity = data.get("entities", {}).get(qid)
                if not entity or "claims" not in entity:
                    return None

                # Find referenced QIDs in claims to resolve human-readable labels
                ref_qids = set()
                for prop in ("P127", "P749", "P159", "P414"):
                    for c in entity.get("claims", {}).get(prop, []):
                        val = c.get("mainsnak", {}).get("datavalue", {}).get("value")
                        if isinstance(val, dict) and "id" in val:
                            ref_qids.add(val["id"])

                labels_cache = {}
                if ref_qids:
                    resolve_url = WIKIDATA_ENTITY_URL.format(ids="|".join(sorted(ref_qids)))
                    res_resp = client.get(resolve_url)
                    if res_resp.status_code == 200:
                        res_data = res_resp.json().get("entities", {})
                        for r_qid, r_ent in res_data.items():
                            lbl = r_ent.get("labels", {}).get("en", {}).get("value")
                            if lbl:
                                labels_cache[r_qid] = lbl

                return parse_wikidata_entity(entity, labels_cache=labels_cache)
    except Exception as e:
        log.warning("Wikidata fetch failed for %s: %s", qid, e)
    return None


def _search_outlet_qid(outlet: Outlet) -> str | None:
    """Use a Wikidata search hit only when it is clearly this outlet, never just because it ranked first."""
    # Strip only the platform suffix: clean_outlet_name also drops language words, and "NTV Telugu" -> "NTV" matched
    # the Bangladeshi channel NTV.
    cleaned = re.sub(r"\s*[·|]\s*(YouTube|Instagram|Reddit|Bluesky|Facebook|Twitter|X)\s*$", "", outlet.name,
                     flags=re.IGNORECASE).strip()
    target = _norm_label(cleaned)
    if not target:
        return None
    hits = [c["id"] for c in search_wikidata(cleaned)
            if _norm_label(c.get("label")) == target and MEDIA_DESC_RE.search(c.get("description") or "")]
    # Several media entities share the name ("Business Today": a BBC programme, an Indian magazine, a US magazine…):
    # don't guess. Add the right one to KNOWN_QIDS, or set it in Admin.
    return hits[0] if len(hits) == 1 else None


def _reset_profile(db: Session, prof: OutletProfile) -> None:
    """Forget a Wikidata match and the links it brought, so the outlet can be matched again."""
    for link in db.scalars(select(OutletLink).where(OutletLink.outlet_id == prof.outlet_id,
                                                    OutletLink.source.in_(("wikidata", "domain")))).all():
        db.delete(link)
    prof.wikidata_qid = prof.founded = prof.owner = prof.headquarters = None
    prof.official_website = prof.stock_exchange = prof.ticker = prof.fetched_at = None
    db.flush()


def profile_looks_wrong(outlet: Outlet, prof: OutletProfile) -> bool:
    """Profiles saved by the old matcher: a wrong hard-coded QID, or a channel given its platform's profile
    (official website youtube.com / instagram.com). Admin-confirmed profiles are never touched."""
    if prof.confirmed_by_admin:
        return False
    known = known_qid(outlet.name)
    if known and prof.wikidata_qid and prof.wikidata_qid != known:
        return True
    return is_platform_outlet(outlet) and site_domain(prof.official_website) in PLATFORM_DOMAINS


def sync_outlet_wikidata(db: Session, outlet: Outlet, override_qid: str | None = None) -> OutletProfile:
    """Fetch and sync Wikidata profile and links for an outlet."""
    prof = db.scalar(select(OutletProfile).where(OutletProfile.outlet_id == outlet.id))
    if prof and override_qid:
        _reset_profile(db, prof)  # don't keep the previous entity's links
    qid = override_qid or (prof.wikidata_qid if prof else None) or known_qid(outlet.name)
    searched = False
    if not qid:
        qid, searched = _search_outlet_qid(outlet), True

    parsed = fetch_wikidata_entity(qid) if qid else None
    platform = is_platform_outlet(outlet)
    if parsed and searched and not platform:
        # A searched match that names a different website is a different organisation.
        own, theirs = site_domain(outlet.domain), site_domain(parsed["official_website"])
        if own and theirs and not (own.endswith(theirs) or theirs.endswith(own)):
            parsed = None

    if not prof:
        prof = OutletProfile(outlet_id=outlet.id)
        db.add(prof)

    if parsed:
        prof.wikidata_qid = parsed["qid"]
        prof.founded = parsed["founded"]
        prof.owner = parsed["owner"]
        prof.headquarters = parsed["headquarters"]
        prof.official_website = parsed["official_website"] or (f"https://{outlet.domain}" if outlet.domain and not platform else None)
        prof.stock_exchange = parsed["stock_exchange"]
        prof.ticker = parsed["ticker"]
        prof.fetched_at = utcnow()
        if override_qid:
            prof.confirmed_by_admin = True

        # Sync links
        existing_urls = {l.url for l in db.scalars(select(OutletLink).where(OutletLink.outlet_id == outlet.id)).all()}
        for link in parsed["links"]:
            if link["url"] not in existing_urls:
                db.add(OutletLink(
                    outlet_id=outlet.id,
                    platform=link["platform"],
                    url=link["url"],
                    source="wikidata"
                ))
    else:
        # If no Wikidata match yet, at least provide official website from outlet domain (not for a channel on a
        # platform: youtube.com is not "NTV Telugu · YouTube"'s website)
        if outlet.domain and not platform and not prof.official_website:
            prof.official_website = f"https://{outlet.domain}"
            db.add(OutletLink(outlet_id=outlet.id, platform="website", url=prof.official_website, source="domain"))

    db.commit()
    return prof


def _not_available(platform: str, metric: str, reason: str, source: str) -> dict:
    return {"platform": platform, "metric": metric, "value": None, "status": "Not available", "reason": reason,
            "source": source, "as_of": None}


def get_channel_audience(db: Session, outlet: Outlet, links: list[OutletLink]) -> list[dict]:
    """Returns audience statistics per platform with live status, reason, source, and as_of date.

    GOLDEN RULE: only numbers fetched live from an official API are shown; everything else is a clear
    'Not available' with the exact requirement. Nothing is hard-coded or estimated.
    """
    settings = get_settings()
    out = []

    # 1. YouTube
    yt_link = next((l.url for l in links if l.platform == "youtube"), None)
    if settings.youtube_api_key and yt_link:
        ref = yt_link.rstrip("/").split("/")[-1]
        param = {"forHandle": ref} if ref.startswith("@") else {"id": ref}
        try:
            with httpx.Client(timeout=8.0) as client:
                resp = client.get("https://www.googleapis.com/youtube/v3/channels",
                                  params={"part": "statistics", "key": settings.youtube_api_key, **param})
                if resp.status_code == 200:
                    items = resp.json().get("items", [])
                    if items:
                        stats = items[0].get("statistics", {})
                        subs = int(stats.get("subscriberCount", 0))
                        views = int(stats.get("viewCount", 0))
                        out.append({
                            "platform": "YouTube",
                            "metric": "Subscribers & Views",
                            "value": f"{subs:,} subscribers · {views:,} views",
                            "status": "Available",
                            "source": "YouTube Data API v3",
                            "as_of": fmt(utcnow()),
                        })
        except Exception as e:
            log.warning("YouTube API stats error: %s", e)

    if not any(x["platform"] == "YouTube" for x in out):
        reason = ("needs YOUTUBE_API_KEY" if not settings.youtube_api_key
                  else "no verified YouTube channel link (Wikidata)" if not yt_link else "YouTube API returned no statistics")
        out.append(_not_available("YouTube", "Subscribers & Views", reason, "YouTube Data API v3"))

    # 2. Instagram
    if settings.meta_access_token and settings.ig_business_account_id:
        out.append(_not_available("Instagram", "Followers", "outlet username not connected to business discovery",
                                  "Meta Graph API (Business Discovery)"))
    else:
        out.append(_not_available("Instagram", "Followers",
                                  "Meta Graph API access required (META_ACCESS_TOKEN and IG_BUSINESS_ACCOUNT_ID)",
                                  "Meta Graph API"))

    # 3. Facebook
    out.append(_not_available("Facebook", "Page followers", "needs Meta API access (Page Public Metadata)",
                              "Meta Graph API"))

    # 4. X / Twitter
    out.append(_not_available("X (Twitter)", "Followers", "needs X API access (paid tier required)", "X API v2"))

    # 5. Website traffic
    out.append(_not_available("Website Traffic", "Monthly visits", "needs a licensed traffic-data provider",
                              "Similarweb / Comscore (licensed)"))

    # 6. Telegram
    out.append(_not_available("Telegram", "Channel members", "no public API for channel subscriber counts", "Telegram"))

    return out


def get_channel_track_record(db: Session, outlet: Outlet) -> dict:
    """Returns legal cases, wins/losses, and fact-check records."""
    settings = get_settings()

    manuals = db.scalars(select(OutletManualFact).where(OutletManualFact.outlet_id == outlet.id)).all()
    legal_cases = [
        {"id": m.id, "fact": m.fact, "source_url": m.source_url, "as_of": m.as_of_date}
        for m in manuals if m.category == "legal_cases"
    ]
    wins_losses = [
        {"id": m.id, "fact": m.fact, "source_url": m.source_url, "as_of": m.as_of_date}
        for m in manuals if m.category == "wins_losses"
    ]

    fact_checks = {
        "status": "Not available",
        "reason": "needs FACTCHECK_API_KEY",
        "source": "Google Fact Check Tools API",
        "claims": [],
        "total": 0,
    }

    if settings.factcheck_api_key:
        try:
            fc_url = f"https://factchecktools.googleapis.com/v1alpha1/claims:search?query={quote_plus(outlet.name)}&key={settings.factcheck_api_key}"
            with httpx.Client(timeout=8.0) as client:
                resp = client.get(fc_url)
                if resp.status_code == 200:
                    claims = resp.json().get("claims", [])
                    fact_checks = {
                        "status": "Available",
                        "source": "Google Fact Check Tools API",
                        "total": len(claims),
                        "claims": [
                            {
                                "text": c.get("text"),
                                "claimant": c.get("claimant"),
                                "date": c.get("claimDate"),
                                "rating": (c.get("claimReview", [{}])[0].get("textualRating") if c.get("claimReview") else "Reviewed"),
                                "review_url": (c.get("claimReview", [{}])[0].get("url") if c.get("claimReview") else ""),
                            }
                            for c in claims[:10]
                        ],
                    }
        except Exception as e:
            log.warning("Fact check API error: %s", e)

    return {
        "legal_cases": legal_cases,
        "wins_losses": wins_losses,
        "fact_checks": fact_checks,
    }


def get_channel_coverage_stats(db: Session, outlet_id: int) -> dict:
    """Returns coverage stats: total posts, first/latest post, tone split, top subjects, monthly counts."""
    arts = db.scalars(
        select(Article)
        .where(Article.outlet_id == outlet_id)
        .order_by(func.coalesce(Article.published_at, Article.collected_at).desc())
    ).all()

    if not arts:
        return {
            "total_posts": 0,
            "first_post": None,
            "latest_post": None,
            "tone_split": {"Negative": 0, "Neutral": 0, "Positive": 0},
            "top_subjects": [],
            "posts_per_month": [],
        }

    first_art = arts[-1]
    latest_art = arts[0]

    tone_counts = {"Negative": 0, "Neutral": 0, "Positive": 0}
    for a in arts:
        t = a.tone or "Neutral"
        if t in tone_counts:
            tone_counts[t] += 1

    # Top subjects
    subj_rows = db.execute(
        select(Subject.name, func.count(Article.id))
        .join(article_subjects, article_subjects.c.subject_id == Subject.id)
        .join(Article, Article.id == article_subjects.c.article_id)
        .where(Article.outlet_id == outlet_id)
        .group_by(Subject.id)
        .order_by(func.count(Article.id).desc())
        .limit(6)
    ).all()
    top_subjects = [{"name": name, "count": count} for name, count in subj_rows]

    # Posts per month timeline (small bar chart)
    month_counts = {}
    for a in arts:
        dt = a.published_at or a.collected_at
        if dt:
            m_key = f"{dt.year}-{dt.month:02d}"
            month_counts[m_key] = month_counts.get(m_key, 0) + 1

    sorted_months = sorted(month_counts.items())[-12:]
    posts_per_month = [{"month": m, "count": c} for m, c in sorted_months]

    return {
        "total_posts": len(arts),
        "first_post": {
            "title": first_art.title,
            "url": first_art.url,
            "date": fmt(first_art.published_at or first_art.collected_at),
        },
        "latest_post": {
            "title": latest_art.title,
            "url": latest_art.url,
            "date": fmt(latest_art.published_at or latest_art.collected_at),
        },
        "tone_split": tone_counts,
        "top_subjects": top_subjects,
        "posts_per_month": posts_per_month,
    }


def build_channel_profile_data(
    db: Session, outlet_id: int, page: int = 1, page_size: int = 50, q: str = ""
) -> dict:
    """Full data payload for the Channel Profile page."""
    outlet = db.get(Outlet, outlet_id)
    if not outlet:
        return {}

    prof = db.scalar(select(OutletProfile).where(OutletProfile.outlet_id == outlet.id))
    if prof and profile_looks_wrong(outlet, prof):
        _reset_profile(db, prof)
        prof = sync_outlet_wikidata(db, outlet)
    if not prof:
        prof = sync_outlet_wikidata(db, outlet)

    links = db.scalars(select(OutletLink).where(OutletLink.outlet_id == outlet.id)).all()
    manuals = db.scalars(select(OutletManualFact).where(OutletManualFact.outlet_id == outlet.id)).all()

    # Manual facts for profile card
    share_cap = next((m.fact for m in manuals if m.category == "share_capital"), None)
    share_cap_meta = next((f"Added manually from {m.source_url}, {m.as_of_date}" for m in manuals if m.category == "share_capital"), "Not available — add manually in Admin")
    offices = next((m.fact for m in manuals if m.category == "offices"), None)
    offices_meta = next((f"Added manually from {m.source_url}, {m.as_of_date}" for m in manuals if m.category == "offices"), "Not available — add manually in Admin")

    wd_src = f"Wikidata ({prof.wikidata_qid})" if prof.wikidata_qid else None
    no_wd = "No Wikidata entry matched this channel" if not prof.wikidata_qid else "Not available"

    founded_val = prof.founded or "Not available from Wikidata"
    founded_src = wd_src if prof.founded else no_wd

    owner_val = prof.owner or "Not available from Wikidata"
    owner_src = wd_src if prof.owner else no_wd

    hq_val = prof.headquarters or "Not available from Wikidata"
    hq_src = wd_src if prof.headquarters else no_wd

    stock_val = prof.stock_exchange or "Not listed / Not available"
    ticker_val = prof.ticker or "—"

    profile_card = {
        "founded": founded_val,
        "founded_source": founded_src,
        "owner": owner_val,
        "owner_source": owner_src,
        "headquarters": hq_val,
        "headquarters_source": hq_src,
        "official_website": prof.official_website or (f"https://{outlet.domain}" if outlet.domain and not is_platform_outlet(outlet) else "Not available"),
        "stock_exchange": stock_val,
        "ticker": ticker_val,
        "share_capital": share_cap or "Not available",
        "share_capital_source": share_cap_meta,
        "share_price": "Not available",
        "share_price_reason": "needs MARKET_DATA_API_KEY",
        "offices": offices or "Not available",
        "offices_source": offices_meta,
    }

    audience = get_channel_audience(db, outlet, links)
    track_record = get_channel_track_record(db, outlet)
    coverage = get_channel_coverage_stats(db, outlet.id)

    # Paginated articles published by this outlet
    stmt = (
        select(Article)
        .where(Article.outlet_id == outlet_id)
        .order_by(func.coalesce(Article.published_at, Article.collected_at).desc())
    )
    count_stmt = select(func.count(Article.id)).where(Article.outlet_id == outlet_id)
    if q.strip():
        stmt = stmt.where(Article.title.ilike(f"%{q.strip()}%"))
        count_stmt = count_stmt.where(Article.title.ilike(f"%{q.strip()}%"))  # pages must match the search

    total_filtered_posts = db.scalar(count_stmt) or 0
    arts = db.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).all()

    posts = [
        {
            "id": a.id,
            "title": a.title,
            "url": a.url,
            "journalist": a.author or "Byline not captured",
            "published_at_fmt": fmt(a.published_at or a.collected_at, a.published_is_estimate),
            "tone": a.tone,
            "kind": a.kind,
            "story_id": a.cluster_id,
            "story_title": a.cluster.title if a.cluster else "Standalone post",
        }
        for a in arts
    ]

    return {
        "outlet": {
            "id": outlet.id,
            "name": outlet.name,
            "kind": outlet.kind,
            "domain": outlet.domain,
            "country": outlet.country,  # None = unknown; never assume India
            "language": outlet.language or "en",
        },
        "wikidata": {
            "qid": prof.wikidata_qid,
            "url": f"https://www.wikidata.org/wiki/{prof.wikidata_qid}" if prof.wikidata_qid else None,
            "confirmed": prof.confirmed_by_admin,
            "fetched_at": iso(prof.fetched_at),
        },
        "links": [{"platform": l.platform, "url": l.url, "source": l.source} for l in links],
        "profile": profile_card,
        "audience": audience,
        "track_record": track_record,
        "coverage": coverage,
        "posts": posts,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total_filtered_posts,
            "pages": (total_filtered_posts + page_size - 1) // page_size if page_size else 1,
        },
    }
