"""Builds everything the dashboard shows from the database.

This is a direct port of the design's renderVals() logic: the same filters (subject, category, timeframe,
coverage, channel, journalist, tone), the same story/post rules (first publisher, tone majority, counts) and
the same views (feed, channels, journalists, A–Z, live, video, social, rankings).
"""
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..models import Article, Cluster, CollectionRun, Outlet, Source, Subject, article_subjects, utcnow
from ..ingest.text import contains_term

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


def _matches_q(a: Article, q: str) -> bool:
    if not q:
        return True
    c_title = a.cluster.title if getattr(a, "cluster", None) else ""
    hay = " ".join([a.title or "", a.description or "", a.author or "", a.outlet.name or "", c_title] +
                   [s.name for s in a.subjects] + [x for s in a.subjects for x in (s.aliases or [])])
    return contains_term(hay, q) or q.lower() in hay.lower()


def _post_ok(a: Article, f: Filters, ignore: str = "") -> bool:
    if ignore != "outlet" and f.outlet and a.outlet.name != f.outlet:
        return False
    if ignore != "journo" and f.journo and (a.author or NO_BYLINE) != f.journo:
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
    Post-level semantics: q must match the post (headline, description, byline, outlet, tagged subjects), cat must
    be one of the post's own categories, and time applies to the post's own date.
    """
    q = (f.q or "").strip()
    stmt = select(Article).join(Outlet, Outlet.id == Article.outlet_id).options(
        selectinload(Article.outlet), selectinload(Article.subjects),
        selectinload(Article.cluster).selectinload(Cluster.articles))
    if outlet_id is not None:
        stmt = stmt.where(Article.outlet_id == outlet_id)
    elif f.outlet and "outlet" not in ignore:
        stmt = stmt.where(Outlet.name == f.outlet)
    since = _since(f.time) if f.time in WINDOWS else None
    if since is not None:
        stmt = stmt.where(func.coalesce(Article.published_at, Article.collected_at) >= since)
    if f.cat and f.cat != "All":
        stmt = stmt.where(cast(Article.categories, String).like(f'%"{f.cat}"%'))
    if f.tone and f.tone != "All":
        stmt = stmt.where(Article.tone == f.tone)
    if f.scope and f.scope != "Both":
        stmt = stmt.where(Article.scope == f.scope)
    if f.journo and "journo" not in ignore:
        stmt = stmt.where(Article.author.is_(None) if f.journo == NO_BYLINE else Article.author == f.journo)
    if q:
        like = f"%{q}%"
        tagged = select(article_subjects.c.article_id).join(Subject, Subject.id == article_subjects.c.subject_id) \
            .where(or_(Subject.name.ilike(like), cast(Subject.aliases, String).ilike(like)))
        # SQL pre-filter is a superset; _matches_q below is the exact rule the feed uses.
        stmt = stmt.outerjoin(Cluster, Cluster.id == Article.cluster_id).where(
            or_(Article.title.ilike(like), Article.description.ilike(like), Article.author.ilike(like),
                Outlet.name.ilike(like), Cluster.title.ilike(like), Article.id.in_(tagged)))
    rows = list(db.scalars(stmt).all())
    if q:
        rows = [a for a in rows if _matches_q(a, q)]
    return rows


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


def load_window(db: Session, f: Filters) -> list[Cluster]:
    now = utcnow()
    q_str = (f.q or "").strip()
    stmt = select(Cluster).options(
        selectinload(Cluster.articles).selectinload(Article.outlet),
        selectinload(Cluster.articles).selectinload(Article.subjects),
    )
    if q_str:
        like = f"%{q_str}%"
        tagged = select(article_subjects.c.article_id).join(Subject, Subject.id == article_subjects.c.subject_id) \
            .where(or_(Subject.name.ilike(like), cast(Subject.aliases, String).ilike(like)))
        matching_cluster_ids = select(Article.cluster_id).join(Outlet, Outlet.id == Article.outlet_id).where(
            Article.cluster_id.is_not(None),
            or_(
                Article.title.ilike(like),
                Article.description.ilike(like),
                Article.author.ilike(like),
                Outlet.name.ilike(like),
                Article.id.in_(tagged)
            )
        ).distinct()
        stmt = stmt.where(or_(Cluster.title.ilike(like), Cluster.id.in_(matching_cluster_ids)))
    win = WINDOWS.get(f.time)
    if win is not None:
        stmt = stmt.where(Cluster.last_seen >= now - win)
    stmt = stmt.order_by(Cluster.last_seen.desc()).limit(MAX_CLUSTERS)
    return list(db.scalars(stmt).all())


def build_dashboard(db: Session, f: Filters) -> dict:
    s = get_settings()
    q = f.q.strip()
    clusters = load_window(db, f)

    # Story-level filters: subject search + category (union of post categories).
    def story_ok(c: Cluster, ignore_cat=False) -> bool:
        if q and not (contains_term(c.title or "", q) or q.lower() in (c.title or "").lower() or any(_matches_q(a, q) for a in c.articles)):
            return False
        if not ignore_cat and f.cat != "All" and not any(f.cat in (a.categories or []) for a in c.articles):
            return False
        return bool(c.articles)

    in_scope = [c for c in clusters if story_ok(c)]
    scoped_posts: list[tuple[Article, Cluster]] = [(a, c) for c in in_scope for a in c.articles]

    # ---- feed --------------------------------------------------------------------------------------------------
    stories, visible = [], []
    for c in in_scope:
        arts = sorted(c.articles, key=_when)
        shown = [a for a in arts if _post_ok(a, f)]
        if not shown:
            continue
        visible.extend((a, c) for a in shown)
        firsts = _first_ids(arts)
        ordered = sorted(arts, key=lambda a: (a.id not in firsts, _when(a)))
        first_names = sorted({a.outlet.name for a in arts if a.id in firsts})
        tone = _majority_tone(arts)
        cats = sorted({x for a in arts for x in (a.categories or [])}, key=CATEGORIES.index)
        subjects = sorted({sj.name for a in arts for sj in a.subjects})
        scopes = Counter(a.scope for a in arts)
        stories.append({
            "id": c.id, "title": c.title, "date_label": fmt(c.last_seen, with_time=False),
            "first_seen": iso(c.first_seen), "last_seen": iso(c.last_seen),
            "first_name": " & ".join(first_names) + (" (same time)" if len(first_names) > 1 else ""),
            "outlet_count": len({a.outlet_id for a in arts}), "post_count": len(arts), "shown_count": len(shown),
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
    base_for_cats = [c for c in clusters if story_ok(c, ignore_cat=True)]
    cats = [{"label": cat, "count": len(base_for_cats) if cat == "All" else
             sum(1 for c in base_for_cats if any(cat in (a.categories or []) for a in c.articles))} for cat in CATEGORIES]

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
    for a, _ in scoped_posts:
        if not _post_ok(a, f, ignore="journo"):
            continue
        name = a.author or NO_BYLINE
        jm[name]["n"] += 1
        jm[name]["outlets"].add(a.outlet.name)
        jm[name]["tones"][a.tone] += 1
    journalists = [{"name": n, "n": v["n"], "outlets": ", ".join(sorted(v["outlets"])[:4]) +
                    (" …" if len(v["outlets"]) > 4 else "")} for n, v in jm.items()]
    journalists.sort(key=lambda j: (j["name"] == NO_BYLINE, -j["n"], j["name"].lower()))
    journalist_detail = None
    if f.journo:
        jp = [(a, c) for a, c in scoped_posts if (a.author or NO_BYLINE) == f.journo and _post_ok(a, f, ignore="journo")]
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
    def count(cat: str | None, key: str, subject_like: str | None = None) -> int:
        q = select(func.count(func.distinct(Article.cluster_id)))
        if subject_like:
            q = q.join(article_subjects, article_subjects.c.article_id == Article.id).join(Subject, Subject.id == article_subjects.c.subject_id) \
                .where(Subject.name.ilike(f"%{subject_like}%"))
        since = _since(key)
        if since is not None:
            q = q.where(func.coalesce(Article.published_at, Article.collected_at) >= since)
        if cat:
            # categories is a JSON list; a LIKE on its text form works on both SQLite and PostgreSQL
            q = q.where(cast(Article.categories, String).like(f'%"{cat}"%'))
        return db.scalar(q) or 0

    return {"d-bet": count("Betting", "day"), "w-bet": count("Betting", "week"), "m-bet": count("Betting", "month"),
            "m-law": count("Law", "month"), "y-law": count("Law", "year"), "mahadev": count(None, "all", "mahadev")}


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
    key = (kind_key, limit, db.scalar(select(func.max(Article.id))) or 0, db.scalar(select(func.count(Article.id))) or 0)
    if key in _rank_cache:
        return _rank_cache[key]
    f = Filters(q=spec["q"], cat=spec["cat"], time=spec["time"], scope=spec["scope"], tone=spec["tone"], journo=spec["journo"])
    counts = Counter((a.outlet.id, a.outlet.name) for a in matching_posts(db, f))
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][1].lower()))[:limit]
    top = rows[0][1] if rows else 1
    out = [{"rank": f"{i + 1:02d}", "outlet_id": oid, "name": n, "n": c, "pct": round(c / top * 100),
            "goto": {**spec, "outlet": n}} for i, ((oid, n), c) in enumerate(rows)]
    _rank_cache[key] = out
    return out


def subjects_az(db: Session) -> list[dict]:
    rows = db.execute(
        select(Subject.id, Subject.name, Subject.query, Subject.enabled, func.count(Article.id), func.count(func.distinct(Article.cluster_id)),
               func.count(func.distinct(Article.outlet_id)),
               func.min(func.coalesce(Article.published_at, Article.collected_at)),
               func.max(func.coalesce(Article.published_at, Article.collected_at)))
        .outerjoin(article_subjects, article_subjects.c.subject_id == Subject.id)
        .outerjoin(Article, Article.id == article_subjects.c.article_id)
        .group_by(Subject.id).order_by(Subject.name)
    ).all()
    return [{"id": i, "name": n, "query": qy, "enabled": en, "letter": n[:1].upper(), "posts": p, "stories": st, "outlets": o,
             "first": fmt(fs, with_time=False) if fs else "—", "last": fmt(ls, with_time=False) if ls else "—"}
            for i, n, qy, en, p, st, o, fs, ls in rows]
