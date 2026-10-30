"""Builds everything the dashboard shows from the database.

This is a direct port of the design's renderVals() logic: the same filters (subject, category, timeframe,
coverage, channel, journalist, tone), the same story/post rules (first publisher, tone majority, counts) and
the same views (feed, channels, journalists, A–Z, live, video, social, rankings).
"""
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import String, and_, cast, false, func, or_, select, true
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..models import Article, Cluster, CollectionRun, Outlet, Source, Subject, article_subjects, utcnow
from ..ingest.text import SearchQuery, search_words

WINDOWS = {"day": timedelta(days=1), "week": timedelta(days=7), "month": timedelta(days=31),
           "year": timedelta(days=365), "all": None}
TIME_NAMES = {"day": "Today", "week": "This week", "month": "This month", "year": "Past 12 months", "all": "All time"}
CATEGORIES = ["All", "Betting", "Law", "Gaming app", "Company", "Stock market"]
NO_BYLINE = "Byline not captured"
MAX_CLUSTERS = 3000  # aggregate over the most recent N stories in the window
STALE_AFTER_MINUTES = 120  # sources run every 10-60 min; no success for 2 h means the collector is not running


@dataclass
class Filters:
    q: str = ""
    cat: str = "All"
    time: str = "week"
    scope: str = "Both"
    outlet: str = ""
    journo: str = ""
    tone: str = "All"
    limit: int = 60
    offset: int = 0


def _tz():
    return ZoneInfo(get_settings().display_timezone)


def fmt(dt: datetime | None, estimate: bool = False, with_time: bool = True) -> str:
    if dt is None:
        return "date n/a"
    local = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(_tz())
    s = f"{local.day} {local:%b %Y}"
    if with_time:
        s += f", {local:%H:%M}"
    return s + (" ~" if estimate else "")


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def _when(a: Article) -> datetime:
    return a.published_at or a.collected_at


class PostMatcher:
    """The one post-level rule for every filter. The feed, KPIs, category chips, channels, journalists, rankings,
    keyword and A–Z counts all use it, so a number shown anywhere equals the posts listed on click.

    A post matches the search when its own headline, description, byline or channel name contains it. When the
    search names a tracked subject, a post also matches if it is tagged with that subject (the subject's news
    search found it, maybe in the article body) or its text contains one of the subject's aliases, so adding an
    alias (e.g. a Hindi spelling) covers posts collected before it was added.
    The story it was clustered into does not count: a loose story must not pull unrelated posts into a search.
    """

    def __init__(self, f: Filters):
        self.f = f
        self.sq = SearchQuery(f.q) if (f.q or "").strip() else None
        self.since = _since(f.time) if f.time in WINDOWS else None
        self.synonyms = SearchQuery.any_phrase([])  # names + aliases of the subjects the search names; set by sql_filter
        self._q_hits: dict[int, bool] = {}
        self._subject_hits: dict[int, bool] = {}
        # Set by load_window to the posts passing sql_filter, so the feed judges exactly the posts that
        # matching_posts (channel counts) judges, even where Python would match text the SQL LIKE cannot.
        self.candidates: set[int] | None = None

    def subject_ok(self, s: Subject) -> bool:
        hit = self._subject_hits.get(s.id)
        if hit is None:
            hit = self._subject_hits[s.id] = self.sq.names_subject(s.name, s.aliases or [])
        return hit

    def q_ok(self, a: Article) -> bool:
        if self.sq is None:
            return True
        if self.candidates is not None and a.id not in self.candidates:
            return False
        hit = self._q_hits.get(a.id)
        if hit is None:
            own = search_words(" ".join([a.title or "", a.description or "", a.author or "", a.outlet.name or ""]))
            hit = self._q_hits[a.id] = (self.sq.matches_words(own) or self.synonyms.matches_words(own)
                                        or any(self.subject_ok(s) for s in a.subjects))
        return hit

    def ok(self, a: Article, ignore: tuple[str, ...] = ()) -> bool:
        f = self.f
        if self.since is not None and "time" not in ignore and _when(a) < self.since:
            return False
        if f.cat and f.cat != "All" and "cat" not in ignore and f.cat not in (a.categories or []):
            return False
        return _post_ok(a, f, ignore) and self.q_ok(a)

    def sql_filter(self, db: Session):
        """SQL pre-filter for the search: a superset of q_ok (LIKE on each word), refined in Python by q_ok.

        Needs Outlet joined to Article."""
        named = [s for s in db.scalars(select(Subject)).all() if self.subject_ok(s)]
        tagged_ids = [s.id for s in named]
        self.synonyms = SearchQuery.any_phrase([t for s in named for t in [s.name, *(s.aliases or [])]])
        conds = [and_(*[or_(Article.title.ilike(p), Article.description.ilike(p), Article.author.ilike(p),
                            Outlet.name.ilike(p)) for p in pats]) if pats else true()
                 for pats in self.sq.sql_patterns() + self.synonyms.sql_patterns()]
        if tagged_ids:
            conds.append(Article.id.in_(select(article_subjects.c.article_id)
                                        .where(article_subjects.c.subject_id.in_(tagged_ids))))
        return or_(*conds) if conds else false()


def _post_ok(a: Article, f: Filters, ignore: tuple[str, ...] = ()) -> bool:
    if "outlet" not in ignore and f.outlet and a.outlet.name != f.outlet:
        return False
    if "journo" not in ignore and f.journo and (a.author or NO_BYLINE) != f.journo:
        return False
    if f.tone != "All" and a.tone != f.tone:
        return False
    if f.scope != "Both" and a.scope != f.scope:
        return False
    return True


def _post_json(a: Article, first_ids: set[int], story_title: str | None = None) -> dict:
    return {
        "id": a.id, "o": a.outlet.name, "outlet_kind": a.outlet.kind, "by": a.author or "byline n/a",
        "h": a.title, "dl": fmt(a.published_at or a.collected_at, a.published_is_estimate),
        "published_at": iso(a.published_at), "collected_at": iso(a.collected_at),
        "tone": a.tone, "tone_source": a.tone_source, "url": a.url, "is_first": a.id in first_ids,
        "kind": a.kind, "platform": a.platform, "thumbnail": a.thumbnail, "is_live": a.is_live,
        "scope": a.scope, "story": story_title,
    }


def _first_ids(arts: list[Article]) -> set[int]:
    """First publisher = earliest real publish time (estimates only if nothing better). Ties share 1ST."""
    real = [a for a in arts if a.published_at and not a.published_is_estimate]
    pool = real or [a for a in arts if a.published_at] or arts
    earliest = min(_when(a) for a in pool)
    return {a.id for a in pool if _when(a) == earliest}


def _majority_tone(arts) -> str:
    c = Counter(a.tone for a in arts)
    return max(["Negative", "Neutral", "Positive"], key=lambda t: (c[t], t == "Neutral"))


def matching_posts(db: Session, f: Filters, outlet_id: int | None = None, ignore: tuple[str, ...] = ()) -> list[Article]:
    """Every post that itself matches the filters, queried directly (no MAX_CLUSTERS cap).

    Single source of truth for per-channel counts: the rankings, the Channels list numbers, the News channel
    dropdown and the channel detail all use this, so a number shown anywhere equals the posts listed on click.
    SQL narrows the rows; PostMatcher.ok, the same rule the feed applies, decides.
    """
    m = PostMatcher(f)
    stmt = select(Article).join(Outlet, Outlet.id == Article.outlet_id).options(
        selectinload(Article.outlet), selectinload(Article.subjects),
        selectinload(Article.cluster).selectinload(Cluster.articles))
    if outlet_id is not None:
        stmt = stmt.where(Article.outlet_id == outlet_id)
        ignore = (*ignore, "outlet")
    elif f.outlet and "outlet" not in ignore:
        stmt = stmt.where(Outlet.name == f.outlet)
    if m.since is not None:
        stmt = stmt.where(func.coalesce(Article.published_at, Article.collected_at) >= m.since)
    if f.cat and f.cat != "All":
        stmt = stmt.where(cast(Article.categories, String).like(f'%"{f.cat}"%'))
    if f.tone and f.tone != "All":
        stmt = stmt.where(Article.tone == f.tone)
    if f.scope and f.scope != "Both":
        stmt = stmt.where(Article.scope == f.scope)
    if f.journo and "journo" not in ignore:
        stmt = stmt.where(Article.author.is_(None) if f.journo == NO_BYLINE else Article.author == f.journo)
    if m.sq is not None:
        stmt = stmt.where(m.sql_filter(db))
    return [a for a in db.scalars(stmt).all() if m.ok(a, ignore)]


def channel_detail_for(db: Session, f: Filters, outlet: Outlet | None, name: str) -> dict:
    """Every post this outlet published that matches the filters, plus a reason when there are none."""
    if outlet is None:
        return {"name": name, "outlet_id": None, "kind": "", "covered": False, "count": 0, "total": 0, "posts": [],
                "reason": "No channel with this name is in the database. Its posts were removed when their source was "
                          "deleted, or it has not been collected yet.", "reason_kind": "missing"}
    posts = matching_posts(db, f, outlet_id=outlet.id)
    posts.sort(key=_when, reverse=True)
    total = db.scalar(select(func.count(Article.id)).where(Article.outlet_id == outlet.id)) or 0
    reason, reason_kind = None, None
    if not posts:
        if total == 0:
            reason, reason_kind = ("This channel has no collected posts. They were removed when their source was "
                                   "deleted, or nothing has been collected from it yet."), "removed"
        else:
            reason, reason_kind = (f"Your filters exclude all {total} posts from this channel."), "filters"
    out = []
    for a in posts:
        c = a.cluster
        firsts = _first_ids(c.articles) if c and c.articles else set()
        pj = _post_json(a, firsts, c.title if c else None)
        pj["story_id"] = c.id if c else None
        out.append(pj)
    return {"name": outlet.name, "outlet_id": outlet.id, "kind": outlet.kind, "covered": bool(posts), "count": len(posts),
            "total": total, "posts": out, "reason": reason, "reason_kind": reason_kind}


def load_window(db: Session, f: Filters, m: PostMatcher) -> list[Cluster]:
    """Stories that can hold a matching post: active in the timeframe and, when searching, with a candidate post."""
    stmt = select(Cluster).options(
        selectinload(Cluster.articles).selectinload(Article.outlet),
        selectinload(Cluster.articles).selectinload(Article.subjects),
    )
    if m.sq is not None:
        cand = select(Article.id, Article.cluster_id).join(Outlet, Outlet.id == Article.outlet_id).where(m.sql_filter(db))
        m.candidates = {i for i, _ in db.execute(cand).all()}
        stmt = stmt.where(Cluster.id.in_(cand.with_only_columns(Article.cluster_id)))
    if m.since is not None:
        stmt = stmt.where(Cluster.last_seen >= m.since)
    stmt = stmt.order_by(Cluster.last_seen.desc()).limit(MAX_CLUSTERS)
    return list(db.scalars(stmt).all())


def build_dashboard(db: Session, f: Filters) -> dict:
    s = get_settings()
    q = f.q.strip()
    m = PostMatcher(f)
    clusters = load_window(db, f, m)

    # ---- feed --------------------------------------------------------------------------------------------------
    # A story is listed when at least one of its posts matches every filter. Its card describes only the posts
    # that match the search (all of them when there is no search); KPIs count the posts matching every filter.
    stories, visible = [], []
    cat_base: list[list[Article]] = []  # per story, posts matching every filter except category (category chips)
    journo_base: list[tuple[Article, Cluster]] = []  # posts matching every filter except journalist
    for c in clusters:
        arts = sorted(c.articles, key=_when)
        rel = [a for a in arts if m.q_ok(a)]
        if not rel:
            continue
        cb = [a for a in rel if m.ok(a, ignore=("cat",))]
        if cb:
            cat_base.append(cb)
        journo_base.extend((a, c) for a in rel if m.ok(a, ignore=("journo",)))
        shown = [a for a in rel if m.ok(a)]
        if not shown:
            continue
        visible.extend((a, c) for a in shown)
        firsts = _first_ids(rel)
        ordered = sorted(rel, key=lambda a: (a.id not in firsts, _when(a)))
        first_names = sorted({a.outlet.name for a in rel if a.id in firsts})
        tone = _majority_tone(rel)
        cats = sorted({x for a in rel for x in (a.categories or [])}, key=CATEGORIES.index)
        subjects = sorted({sj.name for a in rel for sj in a.subjects})
        scopes = Counter(a.scope for a in rel)
        # A loose story's headline may not mention the search; lead with its first matching headline instead
        title = c.title if m.sq is None or m.sq.matches(c.title or "") else ordered[0].title
        stories.append({
            "id": c.id, "title": title, "date_label": fmt(c.last_seen, with_time=False),
            "first_seen": iso(c.first_seen), "last_seen": iso(c.last_seen),
            "first_name": " & ".join(first_names) + (" (same time)" if len(first_names) > 1 else ""),
            "outlet_count": len({a.outlet_id for a in rel}), "post_count": len(rel), "shown_count": len(shown),
            "tone": tone, "scope": scopes.most_common(1)[0][0], "cats": cats or ["General"],
            "summary": c.summary or ("The feeds for this story carry no description text. The headlines below show what "
                                     "each outlet led with. Add ANTHROPIC_API_KEY for AI summaries."),
            "summary_source": c.summary_source if c.summary else "none",
            "highlights": c.highlights or [], "subjects": subjects,
            "date_note": f"First seen {fmt(c.first_seen)} · latest {fmt(c.last_seen)} ({s.display_timezone})",
            "posts": [_post_json(a, firsts) for a in ordered],
        })

    # ---- KPIs (over visible posts) ------------------------------------------------------------------------------
    vp = [a for a, _ in visible]
    tones = Counter(a.tone for a in vp)
    tot = len(vp) or 1
    firstp = min((a for a in vp if a.published_at and not a.published_is_estimate), key=_when, default=None) \
        or min((a for a in vp if a.published_at), key=_when, default=None)
    first_story = next((c.title for a, c in visible if firstp is not None and a.id == firstp.id), "")
    k = {
        "posts": len(vp), "stories": len(stories), "outlets": len({a.outlet_id for a in vp}),
        "journos": len({a.author for a in vp if a.author}), "noByline": sum(1 for a in vp if not a.author),
        "videos": sum(1 for a in vp if a.kind == "video"), "social": sum(1 for a in vp if a.kind == "social"),
        "first": firstp.outlet.name if firstp else "—",
        "firstDate": f"{fmt(_when(firstp), firstp.published_is_estimate)} · {first_story}" if firstp else "No dated posts",
        "neg": tones["Negative"], "neu": tones["Neutral"], "pos": tones["Positive"],
        "negPct": round(tones["Negative"] / tot * 100), "neuPct": round(tones["Neutral"] / tot * 100),
        "posPct": round(tones["Positive"] / tot * 100),
    }

    # ---- category chips (ignore category filter, like the design) --------------------------------------------
    # Each chip's count is the number of stories listed after clicking it.
    cats = [{"label": cat, "count": len(cat_base) if cat == "All" else
             sum(1 for posts in cat_base if any(cat in (a.categories or []) for a in posts))} for cat in CATEGORIES]

    # ---- channels ------------------------------------------------------------------------------------------------
    # Counts come from matching_posts (post-level, uncapped) so each number equals the posts listed on click.
    all_outlets = db.execute(
        select(Outlet.id, Outlet.name, Outlet.kind, func.count(Article.id)).join(Article, Article.outlet_id == Outlet.id)
        .group_by(Outlet.id).order_by(Outlet.name)
    ).all()
    per_outlet = Counter(a.outlet.name for a in matching_posts(db, f, ignore=("outlet",)))
    channels = [{"id": i, "name": n, "kind": kd, "n": per_outlet.get(n, 0), "total": t} for i, n, kd, t in all_outlets]
    # Channels with coverage on this subject appear first, then alphabetical
    channels.sort(key=lambda x: (-x["n"], x["name"].lower()))
    channel_detail = None
    if f.outlet:
        o = db.scalar(select(Outlet).where(Outlet.name == f.outlet))
        channel_detail = channel_detail_for(db, f, o, f.outlet)

    # ---- journalists ---------------------------------------------------------------------------------------------
    jm: dict[str, dict] = defaultdict(lambda: {"n": 0, "outlets": set(), "tones": Counter()})
    for a, _ in journo_base:
        name = a.author or NO_BYLINE
        jm[name]["n"] += 1
        jm[name]["outlets"].add(a.outlet.name)
        jm[name]["tones"][a.tone] += 1
    journalists = [{"name": n, "n": v["n"], "outlets": ", ".join(sorted(v["outlets"])[:4]) +
                    (" …" if len(v["outlets"]) > 4 else "")} for n, v in jm.items()]
    journalists.sort(key=lambda j: (j["name"] == NO_BYLINE, -j["n"], j["name"].lower()))
    journalist_detail = None
    if f.journo:
        jp = [(a, c) for a, c in journo_base if (a.author or NO_BYLINE) == f.journo]
        jp.sort(key=lambda ac: _when(ac[0]), reverse=True)
        t = Counter(a.tone for a, _ in jp)
        journalist_detail = {"name": f.journo, "count": len(jp), "neg": t["Negative"], "neu": t["Neutral"], "pos": t["Positive"],
                             "posts": [_post_json(a, set(), c.title) for a, c in jp[:200]]}

    # ---- video / social / live -----------------------------------------------------------------------------------
    videos = [_post_json(a, set(), c.title) for a, c in sorted(visible, key=lambda ac: _when(ac[0]), reverse=True) if a.kind == "video"][:100]
    social = [_post_json(a, set(), c.title) for a, c in sorted(visible, key=lambda ac: _when(ac[0]), reverse=True) if a.kind == "social"][:100]
    pages_c = Counter((a.outlet.name, a.platform or a.outlet.kind) for a, _ in visible if a.kind in ("social", "video"))
    pages = [{"handle": n, "net": p, "n": cnt} for (n, p), cnt in pages_c.most_common(50)]
    live_since = utcnow() - timedelta(hours=24)
    live = [_post_json(a, set(), c.title) for a, c in visible if a.is_live and _when(a) >= live_since]

    # ---- options for selects (filtered to current scope so clicking always shows matching stories) -----------
    # Channels dropdown: show ONLY channels that covered this subject/scope,
    # with the actual in-scope post count for that subject!
    outlet_options = [
        {"name": n, "count": per_outlet[n]}
        for n in sorted(per_outlet.keys(), key=lambda x: (-per_outlet[x], x.lower()))
        if per_outlet[n] > 0 or n == f.outlet
    ]
    if f.outlet and not any(o["name"] == f.outlet for o in outlet_options):
        outlet_options.insert(0, {"name": f.outlet, "count": 0})
    if not outlet_options:
        outlet_options = [{"name": n, "count": t} for _, n, _, t in all_outlets]

    # Journalists dropdown: show ONLY journalists who covered this subject/scope
    journo_options = [
        {"name": n, "count": v["n"]}
        for n, v in sorted(jm.items(), key=lambda kv: (kv[0] == NO_BYLINE, -kv[1]["n"], kv[0].lower()))
        if v["n"] > 0 or n == f.journo
    ]
    if not journo_options:
        journo_opts = [(a or NO_BYLINE, n) for a, n in db.execute(
            select(Article.author, func.count(Article.id)).group_by(Article.author)
            .order_by(func.count(Article.id).desc()).limit(500)).all()]
        journo_options = [{"name": n, "count": c} for n, c in journo_opts]

    total_posts = db.scalar(select(func.count(Article.id))) or 0
    heading = (f"“{q}”" if q else ("All betting & gaming news" if f.cat == "All" else f"{f.cat} news")) + " · " + TIME_NAMES.get(f.time, "")
    if f.outlet:
        heading += f" · {f.outlet}"
    if f.journo:
        heading += f" · {f.journo}"

    page = stories[f.offset: f.offset + f.limit]
    return {
        "meta": meta(db) | {"total_posts": total_posts},
        "heading": heading, "k": k, "cats": cats,
        "stories": page, "stories_total": len(stories), "offset": f.offset, "limit": f.limit,
        "channels": channels, "channel_detail": channel_detail,
        "journalists": journalists, "journalist_detail": journalist_detail,
        "videos": videos, "social": social, "pages": pages, "live": live,
        "outlet_options": outlet_options,
        "journo_options": journo_options,
        "presets": preset_counts(db),
        "rank_betting": rankings(db, "betting"),
        "rank_mahadev": rankings(db, "subject", query=f.q.strip() if f.q and f.q.strip() else "Mahadev"),
        "rank_subject": rankings(db, "subject", query=f.q.strip() if f.q and f.q.strip() else "Mahadev"),
        "subject_heading": f"Most {f.q.strip()} betting posts" if f.q and f.q.strip() else "Most Mahadev betting posts",
        "subject_name": q or "all tracked subjects",
    }


def meta(db: Session) -> dict:
    s = get_settings()
    last_ok = db.scalar(select(func.max(Source.last_success_at)))
    srcs = db.scalars(select(Source).where(Source.enabled.is_(True))).all()
    delay = db.scalar(select(func.avg(CollectionRun.median_delay_minutes))
                      .where(CollectionRun.median_delay_minutes.isnot(None), CollectionRun.started_at >= utcnow() - timedelta(days=1)))
    # "Live" only if some source succeeded recently; otherwise the collector is not running and the data is old.
    mins_since = int((utcnow() - last_ok).total_seconds() // 60) if last_ok else None
    return {
        "as_of": fmt(utcnow()), "timezone": s.display_timezone, "last_success": fmt(last_ok) if last_ok else "never",
        "last_success_iso": iso(last_ok), "minutes_since_success": mins_since,
        "is_live": mins_since is not None and mins_since <= STALE_AFTER_MINUTES, "sources_enabled": len(srcs),
        "sources_failing": sum(1 for x in srcs if x.consecutive_failures), "median_delay_minutes": round(delay, 1) if delay else None,
        "total_stories": db.scalar(select(func.count(Cluster.id))) or 0,
        "total_outlets": db.scalar(select(func.count(func.distinct(Article.outlet_id)))) or 0,
        "max_article_id": db.scalar(select(func.max(Article.id))) or 0,
        "ai_enabled": bool(s.anthropic_api_key),
        "youtube_enabled": True,  # YouTube scraping & search active
        "social_enabled": True,   # Instagram search & collection active
    }


def _since(key: str):
    w = WINDOWS[key]
    return None if w is None else utcnow() - w


def preset_counts(db: Session) -> dict:
    """Story counts for the watchlists (category within timeframe)."""
    def count(cat: str, key: str) -> int:
        q = select(func.count(func.distinct(Article.cluster_id)))
        since = _since(key)
        if since is not None:
            q = q.where(func.coalesce(Article.published_at, Article.collected_at) >= since)
        if cat:
            # categories is a JSON list; a LIKE on its text form works on both SQLite and PostgreSQL
            q = q.where(cast(Article.categories, String).like(f'%"{cat}"%'))
        return db.scalar(q) or 0

    return {"d-bet": count("Betting", "day"), "w-bet": count("Betting", "week"), "m-bet": count("Betting", "month"),
            "m-law": count("Law", "month"), "y-law": count("Law", "year"),
            "mahadev": keyword_coverage(db, "Mahadev")["stories"]}


def _data_version(db: Session) -> tuple:
    """Changes whenever posts, their subject tags, or subject names/aliases change (keys the count caches)."""
    subjects = tuple(db.execute(select(Subject.id, Subject.name, cast(Subject.aliases, String))).all())
    return (db.scalar(select(func.max(Article.id))) or 0, db.scalar(select(func.count(Article.id))) or 0,
            db.scalar(select(func.count()).select_from(article_subjects)) or 0, hash(subjects))


_coverage_cache: dict[tuple, dict] = {}


def keyword_coverage(db: Session, q: str, version: tuple | None = None) -> dict:
    """What an all-time search for q lists: posts, stories, channels, first and latest post dates."""
    key = (q.strip().casefold(), version or _data_version(db))
    hit = _coverage_cache.get(key)
    if hit is None:
        posts = matching_posts(db, Filters(q=q, cat="All", time="all"))
        whens = [_when(a) for a in posts]
        if len(_coverage_cache) > 500:
            _coverage_cache.clear()
        hit = _coverage_cache[key] = {
            "posts": len(posts), "stories": len({a.cluster_id for a in posts if a.cluster_id is not None}),
            "outlets": len({a.outlet_id for a in posts}), "first": min(whens, default=None), "last": max(whens, default=None)}
    return hit


# The exact filters a ranking click applies. rankings() counts with these same filters via matching_posts,
# so the number shown equals the number of posts listed after the click.
RANKING_FILTERS = {
    "betting": {"tab": "channels", "time": "all", "cat": "Betting", "q": "", "scope": "Both", "tone": "All", "journo": ""},
    "mahadev": {"tab": "channels", "time": "all", "cat": "All", "q": "Mahadev", "scope": "Both", "tone": "All", "journo": ""},
}
_rank_cache: dict[tuple, list[dict]] = {}


def rankings(db: Session, kind: str, query: str = "", limit: int = 5) -> list[dict]:
    if kind == "subject" or query:
        q_val = query.strip() if query else "Mahadev"
        spec = {"tab": "channels", "time": "all", "cat": "All", "q": q_val, "scope": "Both", "tone": "All", "journo": ""}
        kind_key = f"subject:{q_val.lower()}"
    else:
        spec = RANKING_FILTERS[kind]
        kind_key = kind
    key = (kind_key, limit, _data_version(db))
    if key in _rank_cache:
        return _rank_cache[key]
    if len(_rank_cache) > 500:
        _rank_cache.clear()
    f = Filters(q=spec["q"], cat=spec["cat"], time=spec["time"], scope=spec["scope"], tone=spec["tone"], journo=spec["journo"])
    counts = Counter((a.outlet.id, a.outlet.name) for a in matching_posts(db, f))
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][1].lower()))[:limit]
    top = rows[0][1] if rows else 1
    out = [{"rank": f"{i + 1:02d}", "outlet_id": oid, "name": n, "n": c, "pct": round(c / top * 100),
            "goto": {**spec, "outlet": n}} for i, ((oid, n), c) in enumerate(rows)]
    _rank_cache[key] = out
    return out


def subjects_az(db: Session) -> list[dict]:
    """Tracked subjects A–Z; each count is what clicking the subject (an all-time search for its name) lists."""
    version = _data_version(db)
    out = []
    for sj in db.scalars(select(Subject).order_by(Subject.name)).all():
        cov = keyword_coverage(db, sj.name, version)
        out.append({"id": sj.id, "name": sj.name, "query": sj.query, "enabled": sj.enabled, "letter": sj.name[:1].upper(),
                    "posts": cov["posts"], "stories": cov["stories"], "outlets": cov["outlets"],
                    "first": fmt(cov["first"], with_time=False) if cov["first"] else "—",
                    "last": fmt(cov["last"], with_time=False) if cov["last"] else "—"})
    return out
