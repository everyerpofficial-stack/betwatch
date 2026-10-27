"""BetWatch API. Serves the dashboard (frontend/) and the JSON API (/api/...). Docs at /docs."""
import asyncio
import csv
import io
import json
import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .config import get_settings
from .frontend_assets import ADMIN_HTML, APP_JS, CHANNEL_HTML, INDEX_HTML, STORY_HTML, STYLES_CSS
from .db import Base, SessionLocal, engine, get_db
from .ingest.adapters import ADAPTERS, PER_SUBJECT_KINDS, parse_instagram_post
from .ingest.collector import run_source
from .ingest.pipeline import store_items
from .models import (
    Article, AuditLog, Bookmark, Cluster, CollectionRun, Outlet,
    OutletLink, OutletManualFact, OutletProfile, SavedSearch, Source, Subject,
    article_subjects, utcnow
)
from .security import is_public_http_url, require_admin
from .services.channel_profile import build_channel_profile_data, sync_outlet_wikidata
from .services.dashboard import Filters, build_dashboard, fmt, iso, meta, rankings, subjects_az, _first_ids, _majority_tone
from .services.pdf_report import generate_channel_pdf, generate_subject_pdf

log = logging.getLogger("betwatch.api")
FRONTEND = None
for _cand in [
    Path(__file__).resolve().parents[2] / "frontend",
    Path(__file__).resolve().parents[2] / "public",
    Path.cwd() / "frontend",
    Path.cwd() / "public",
    Path(__file__).resolve().parents[1] / "frontend",
    Path(__file__).resolve().parents[1] / "public",
    Path("/var/task/frontend"),
    Path("/var/task/public"),
]:
    if _cand.exists() and (_cand / "index.html").exists():
        FRONTEND = _cand
        break
if FRONTEND is None:
    FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if engine.url.get_backend_name() == "sqlite":
        try:
            Base.metadata.create_all(engine)
        except Exception as e:
            log.warning("create_all failed: %s", e)
        try:
            from .seed import seed
            seed(only_missing=True)
        except Exception as e:
            log.warning("seed failed: %s", e)
    sched = None
    if get_settings().embedded_worker and not os.environ.get("VERCEL"):
        from .worker import start_background
        sched = start_background()
        log.info("embedded collector started")
    yield
    if sched:
        sched.shutdown(wait=False)


app = FastAPI(title="BetWatch API", version="1.0.0", lifespan=lifespan,
              description="News, video and social coverage of betting, gaming and regulation.")


@app.middleware("http")
async def vercel_path_rewrite_middleware(request: Request, call_next):
    matched = request.headers.get("x-matched-path")
    if matched:
        clean_path = matched.split("?")[0]
        if clean_path and clean_path != "/api/index.py":
            request.scope["path"] = clean_path
    elif request.scope.get("path") in ("/api/index.py", "/api/index"):
        request.scope["path"] = "/"
    return await call_next(request)


Tab = Literal["day", "week", "month", "year", "all"]


def filters(q: str = "", cat: str = "All", time: Tab = "all", scope: Literal["Both", "National", "International"] = "Both",
            outlet: str = "", journo: str = "", tone: Literal["All", "Negative", "Neutral", "Positive"] = "All",
            limit: int = Query(60, ge=1, le=500), offset: int = Query(0, ge=0)) -> Filters:
    return Filters(q=q[:200], cat=cat, time=time, scope=scope, outlet=outlet[:200], journo=journo[:300], tone=tone,
                   limit=limit, offset=offset)


# ---------------------------------------------------------------- public read API
@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok", "time": iso(utcnow())}


@app.get("/api/dashboard", summary="Everything the dashboard renders, for the given filters")
def dashboard(f: Filters = Depends(filters), db: Session = Depends(get_db)):
    return build_dashboard(db, f)


@app.get("/api/stories")
def stories(f: Filters = Depends(filters), db: Session = Depends(get_db)):
    d = build_dashboard(db, f)
    return {"total": d["stories_total"], "offset": f.offset, "items": d["stories"]}


@app.get("/api/stories/{cluster_id}")
def story(cluster_id: int, db: Session = Depends(get_db)):
    c = db.scalar(
        select(Cluster)
        .options(
            selectinload(Cluster.articles).selectinload(Article.outlet),
            selectinload(Cluster.articles).selectinload(Article.subjects),
        )
        .where(Cluster.id == cluster_id)
    )
    if not c:
        raise HTTPException(404, "Story not found")
    arts = sorted(c.articles, key=lambda a: a.published_at or a.collected_at)
    firsts = _first_ids(arts)
    tone = _majority_tone(arts) if arts else "Neutral"
    cats = sorted({cat for a in arts for cat in (a.categories or [])})
    first_names = sorted({a.outlet.name for a in arts if a.id in firsts})

    seen_outlets = {}
    for a in arts:
        o = a.outlet
        if o.id not in seen_outlets:
            seen_outlets[o.id] = {
                "id": o.id,
                "name": o.name,
                "kind": o.kind,
                "domain": o.domain,
                "country": o.country,
                "is_first": any(x.id in firsts and x.outlet_id == o.id for x in arts),
            }
    channels = sorted(seen_outlets.values(), key=lambda x: (not x["is_first"], x["name"].lower()))

    summary_source = c.summary_source if c.summary else "none"
    if summary_source not in ("ai", "publisher"):
        summary_source = "ai" if get_settings().anthropic_api_key and c.summary else ("publisher" if c.summary else "none")

    posts = [
        {
            "id": a.id,
            "outlet_id": a.outlet.id,
            "outlet": a.outlet.name,
            "outlet_kind": a.outlet.kind,
            "title": a.title,
            "author": a.author or "Byline not captured",
            "url": a.url,
            "published_at": iso(a.published_at),
            "published_at_fmt": fmt(a.published_at or a.collected_at, a.published_is_estimate),
            "collected_at": iso(a.collected_at),
            "tone": a.tone,
            "kind": a.kind,
            "platform": a.platform,
            "thumbnail": a.thumbnail,
            "is_first": a.id in firsts,
            "scope": a.scope,
            "categories": a.categories or [],
        }
        for a in arts
    ]

    return {
        "id": c.id,
        "title": c.title,
        "summary": c.summary or "The feeds for this story carry no description text. The headlines below show what each outlet led with. Add ANTHROPIC_API_KEY for AI summaries.",
        "summary_source": summary_source,
        "highlights": c.highlights or [],
        "tone": tone,
        "categories": cats or ["General"],
        "first_seen": iso(c.first_seen),
        "first_seen_fmt": fmt(c.first_seen),
        "last_seen": iso(c.last_seen),
        "last_seen_fmt": fmt(c.last_seen),
        "first_name": " & ".join(first_names) if first_names else "—",
        "post_count": len(arts),
        "channel_count": len(channels),
        "channels": channels,
        "posts": posts,
    }


@app.get("/api/channels")
def channels(f: Filters = Depends(filters), db: Session = Depends(get_db)):
    d = build_dashboard(db, f)
    return {"items": d["channels"], "detail": d["channel_detail"]}


@app.get("/api/channels/{outlet_id}", summary="Full channel profile (Wikidata, audience, track record, coverage)")
def channel_profile_endpoint(
    outlet_id: int,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    q: str = "",
    db: Session = Depends(get_db),
):
    data = build_channel_profile_data(db, outlet_id, page=page, page_size=limit, q=q)
    if not data:
        raise HTTPException(404, "Channel not found")
    return data


@app.get("/api/journalists")
def journalists(f: Filters = Depends(filters), db: Session = Depends(get_db)):
    d = build_dashboard(db, f)
    return {"items": d["journalists"], "detail": d["journalist_detail"]}


@app.get("/api/subjects", summary="All tracked subjects A–Z with coverage counts")
def subjects(db: Session = Depends(get_db)):
    return subjects_az(db)


@app.get("/api/rankings")
def get_rankings(db: Session = Depends(get_db)):
    return {"betting": rankings(db, "betting"), "mahadev": rankings(db, "mahadev"),
            "views": None, "views_note": "Needs a traffic-data provider; not available from free sources."}


@app.get("/api/sources/health", summary="Public freshness and per-source status")
def sources_health(db: Session = Depends(get_db)):
    out = []
    for s in db.scalars(select(Source).order_by(Source.name)).all():
        last = db.scalar(select(CollectionRun).where(CollectionRun.source_id == s.id).order_by(CollectionRun.id.desc()).limit(1))
        out.append({"name": s.name, "kind": s.kind, "enabled": s.enabled, "interval_minutes": s.interval_minutes,
                    "last_run": fmt(s.last_run_at) if s.last_run_at else None, "last_success": fmt(s.last_success_at) if s.last_success_at else None,
                    "failures": s.consecutive_failures, "last_error": s.last_error,
                    "last_latency_ms": last.latency_ms if last else None,
                    "last_median_delay_minutes": last.median_delay_minutes if last else None})
    return {"meta": meta(db), "sources": out}


@app.get("/api/stream", summary="Server-Sent Events: pushes a message when new posts are collected")
async def stream(request: Request, since: int = 0):
    async def gen():
        last = since
        if not last:
            with SessionLocal() as db:
                last = db.scalar(select(func.max(Article.id))) or 0
        yield f"event: hello\ndata: {json.dumps({'max_id': last})}\n\n"
        while not await request.is_disconnected():
            await asyncio.sleep(10)
            with SessionLocal() as db:
                mx = db.scalar(select(func.max(Article.id))) or 0
                if mx > last:
                    new = db.scalars(select(Article).where(Article.id > last).order_by(Article.id.desc()).limit(5)).all()
                    n = db.scalar(select(func.count(Article.id)).where(Article.id > last))
                    payload = {"max_id": mx, "new": n, "latest": [{"title": a.title, "outlet": a.outlet.name} for a in new]}
                    last = mx
                    yield f"event: new\ndata: {json.dumps(payload)}\n\n"
                else:
                    yield ": keep-alive\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- saved searches & bookmarks (single workspace)
class SavedSearchIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    params: dict


@app.get("/api/saved-searches")
def list_saved(db: Session = Depends(get_db)):
    return [{"id": s.id, "name": s.name, "params": s.params} for s in db.scalars(select(SavedSearch).order_by(SavedSearch.id))]


@app.post("/api/saved-searches", status_code=201)
def add_saved(body: SavedSearchIn, db: Session = Depends(get_db)):
    allowed = {"q", "cat", "time", "scope", "outlet", "journo", "tone"}
    s = SavedSearch(name=body.name, params={k: str(v)[:200] for k, v in body.params.items() if k in allowed})
    db.add(s)
    db.commit()
    return {"id": s.id, "name": s.name, "params": s.params}


@app.delete("/api/saved-searches/{sid}", status_code=204)
def del_saved(sid: int, db: Session = Depends(get_db)):
    s = db.get(SavedSearch, sid)
    if s:
        db.delete(s)
        db.commit()


# ---------------------------------------------------------------- saved keywords (Task 5)
_kw_rate_limit: list[float] = []


def _check_kw_rate_limit():
    now = time.time()
    _kw_rate_limit[:] = [t for t in _kw_rate_limit if now - t < 60]
    if len(_kw_rate_limit) >= 20:
        raise HTTPException(429, "Too many keyword requests. Please wait a moment.")
    _kw_rate_limit.append(now)


class KeywordIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    query: str | None = Field(default=None, max_length=300)
    aliases: list[str] = Field(default_factory=list, max_length=20)


class KeywordPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    query: str | None = None
    aliases: list[str] | None = None
    enabled: bool | None = None


@app.get("/api/keywords", summary="List tracked keywords with live post count and collection status")
def list_keywords(db: Session = Depends(get_db)):
    subs = db.scalars(select(Subject).order_by(Subject.name)).all()
    out = []
    now = utcnow()
    for s in subs:
        post_count = db.scalar(
            select(func.count(article_subjects.c.article_id))
            .where(article_subjects.c.subject_id == s.id)
        ) or 0
        last_run = db.scalar(
            select(CollectionRun)
            .where(CollectionRun.subject_id == s.id)
            .order_by(CollectionRun.id.desc())
            .limit(1)
        )
        running = db.scalar(
            select(CollectionRun.id)
            .where(CollectionRun.subject_id == s.id, CollectionRun.status == "running")
            .limit(1)
        )
        if running or (last_run is None and s.created_at and (now - s.created_at).total_seconds() < 180):
            status = "Collecting…"
        elif last_run and last_run.status == "error":
            status = f"Failed: {last_run.error or 'Source error'}"
        else:
            status = "Ready"

        out.append({
            "id": s.id,
            "name": s.name,
            "query": s.query,
            "aliases": s.aliases or [],
            "enabled": s.enabled,
            "posts": post_count,
            "status": status,
        })
    return out


@app.post("/api/keywords", status_code=201, summary="Save a tracked keyword and start collecting immediately")
def create_keyword(body: KeywordIn, bg: BackgroundTasks, db: Session = Depends(get_db)):
    _check_kw_rate_limit()
    name = body.name.strip()
    existing = db.scalar(select(Subject).where(func.lower(Subject.name) == name.lower()))
    if existing:
        if existing.enabled:
            bg.add_task(_collect_subject, existing.id)
        return {"id": existing.id, "name": existing.name, "query": existing.query,
                "aliases": existing.aliases or [], "enabled": existing.enabled, "status": "Collecting…"}
    s = Subject(name=name, query=(body.query or name).strip(),
                aliases=[a.strip().lower() for a in body.aliases if a.strip()])
    db.add(s)
    audit(db, "keyword.add", name=s.name)
    db.commit()
    bg.add_task(_collect_subject, s.id)
    return {"id": s.id, "name": s.name, "query": s.query, "aliases": s.aliases or [],
            "enabled": s.enabled, "status": "Collecting…"}


@app.patch("/api/keywords/{kid}")
def update_keyword(kid: int, body: KeywordPatch, db: Session = Depends(get_db)):
    s = db.get(Subject, kid)
    if not s:
        raise HTTPException(404, "Keyword not found")
    if body.name is not None:
        s.name = body.name.strip()
    if body.query is not None:
        s.query = body.query.strip()
    if body.aliases is not None:
        s.aliases = [a.strip().lower() for a in body.aliases if a.strip()]
    if body.enabled is not None:
        s.enabled = body.enabled
    audit(db, "keyword.update", id=kid, **body.model_dump(exclude_none=True))
    db.commit()
    return {"id": s.id, "name": s.name, "query": s.query, "aliases": s.aliases, "enabled": s.enabled}


@app.delete("/api/keywords/{kid}", status_code=204)
def delete_keyword(kid: int, db: Session = Depends(get_db)):
    s = db.get(Subject, kid)
    if s:
        audit(db, "keyword.delete", id=kid, name=s.name)
        db.delete(s)
        db.commit()


@app.get("/api/bookmarks")
def list_bookmarks(db: Session = Depends(get_db)):
    rows = db.scalars(select(Bookmark).order_by(Bookmark.id.desc())).all()
    out = []
    for b in rows:
        a = db.get(Article, b.article_id)
        if a:
            out.append({"id": b.id, "article_id": a.id, "title": a.title, "outlet": a.outlet.name, "url": a.url})
    return out


@app.post("/api/bookmarks/{article_id}", status_code=201)
def add_bookmark(article_id: int, db: Session = Depends(get_db)):
    if not db.get(Article, article_id):
        raise HTTPException(404)
    if not db.scalar(select(Bookmark).where(Bookmark.article_id == article_id)):
        db.add(Bookmark(article_id=article_id))
        db.commit()
    return {"ok": True}


@app.delete("/api/bookmarks/{article_id}", status_code=204)
def del_bookmark(article_id: int, db: Session = Depends(get_db)):
    b = db.scalar(select(Bookmark).where(Bookmark.article_id == article_id))
    if b:
        db.delete(b)
        db.commit()


# ---------------------------------------------------------------- admin API (Bearer ADMIN_TOKEN)
admin = [Depends(require_admin)]


def audit(db: Session, action: str, **detail):
    db.add(AuditLog(action=action, detail=detail))


class SubjectIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    query: str | None = Field(default=None, max_length=300)
    aliases: list[str] = Field(default_factory=list, max_length=20)


def _collect_subject(subject_id: int):
    with SessionLocal() as db:
        ids = [s.id for s in db.scalars(select(Source).where(Source.enabled.is_(True), Source.kind.in_(PER_SUBJECT_KINDS)))]
    for sid in ids:
        try:
            run_source(sid, subject_ids=[subject_id])
        except Exception as e:
            log.warning("subject backfill failed on source %s: %s", sid, e)


@app.post("/api/admin/subjects", dependencies=admin, status_code=201, summary="Track a new subject and start collecting it")
def add_subject(body: SubjectIn, bg: BackgroundTasks, db: Session = Depends(get_db)):
    if db.scalar(select(Subject).where(func.lower(Subject.name) == body.name.strip().lower())):
        raise HTTPException(409, "Subject already tracked")
    s = Subject(name=body.name.strip(), query=(body.query or body.name).strip(), aliases=[a.strip().lower() for a in body.aliases if a.strip()])
    db.add(s)
    audit(db, "subject.add", name=s.name)
    db.commit()
    bg.add_task(_collect_subject, s.id)  # runs after the response, in the threadpool
    return {"id": s.id, "name": s.name, "status": "collecting"}


class SubjectPatch(BaseModel):
    query: str | None = None
    aliases: list[str] | None = None
    enabled: bool | None = None


@app.patch("/api/admin/subjects/{sid}", dependencies=admin)
def patch_subject(sid: int, body: SubjectPatch, db: Session = Depends(get_db)):
    s = db.get(Subject, sid)
    if not s:
        raise HTTPException(404)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(s, k, v)
    audit(db, "subject.update", id=sid, **body.model_dump(exclude_none=True))
    db.commit()
    return {"ok": True}


@app.delete("/api/admin/subjects/{sid}", dependencies=admin, status_code=204)
def delete_subject(sid: int, db: Session = Depends(get_db)):
    s = db.get(Subject, sid)
    if s:
        audit(db, "subject.delete", name=s.name)
        db.delete(s)
        db.commit()


class SourceIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    kind: str
    url: HttpUrl | None = None
    outlet_name: str | None = Field(default=None, max_length=200)
    outlet_kind: str | None = Field(default="News site", max_length=40)
    country: str | None = Field(default=None, max_length=2)
    language: str | None = Field(default="en", max_length=10)
    interval_minutes: int = Field(default=15, ge=2, le=1440)
    enabled: bool = True
    notes: str | None = None


class SourcePatch(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=2, le=1440)
    url: HttpUrl | None = None
    notes: str | None = None
    outlet_name: str | None = None


def _source_json(s: Source) -> dict:
    return {"id": s.id, "name": s.name, "kind": s.kind, "url": s.url, "outlet_name": s.outlet_name, "country": s.country,
            "enabled": s.enabled, "interval_minutes": s.interval_minutes, "notes": s.notes,
            "last_run": fmt(s.last_run_at) if s.last_run_at else None, "last_success": fmt(s.last_success_at) if s.last_success_at else None,
            "failures": s.consecutive_failures, "last_error": s.last_error}


@app.get("/api/admin/outlets", dependencies=admin)
def list_admin_outlets(db: Session = Depends(get_db)):
    return [{"id": o.id, "name": o.name, "kind": o.kind, "domain": o.domain} for o in db.scalars(select(Outlet).order_by(Outlet.name))]


@app.get("/api/admin/sources", dependencies=admin)
def list_sources(db: Session = Depends(get_db)):
    return [_source_json(s) for s in db.scalars(select(Source).order_by(Source.name))]


@app.post("/api/admin/sources", dependencies=admin, status_code=201)
def add_source(body: SourceIn, db: Session = Depends(get_db)):
    if body.kind not in ADAPTERS:
        raise HTTPException(422, f"kind must be one of {sorted(ADAPTERS)}")
    url = str(body.url) if body.url else None
    if url and body.kind in ("rss", "youtube_channel") and not is_public_http_url(url):
        raise HTTPException(422, "URL must be a public http(s) address")
    s = Source(**body.model_dump(exclude={"url"}), url=url)
    db.add(s)
    audit(db, "source.add", name=s.name, kind=s.kind)
    db.commit()
    return _source_json(s)


@app.patch("/api/admin/sources/{sid}", dependencies=admin)
def patch_source(sid: int, body: SourcePatch, db: Session = Depends(get_db)):
    s = db.get(Source, sid)
    if not s:
        raise HTTPException(404)
    data = body.model_dump(exclude_none=True)
    if "url" in data:
        data["url"] = str(data["url"])
        if s.kind in ("rss", "youtube_channel") and not is_public_http_url(data["url"]):
            raise HTTPException(422, "URL must be a public http(s) address")
    for k, v in data.items():
        setattr(s, k, v)
    audit(db, "source.update", id=sid, **{k: str(v) for k, v in data.items()})
    db.commit()
    return _source_json(s)


@app.delete("/api/admin/sources/{sid}", dependencies=admin, status_code=204)
def delete_source(sid: int, db: Session = Depends(get_db)):
    s = db.get(Source, sid)
    if s:
        audit(db, "source.delete", name=s.name)
        db.delete(s)
        db.commit()


@app.post("/api/admin/sources/{sid}/run", dependencies=admin, summary="Run a source now (in the background)")
def run_now(sid: int, db: Session = Depends(get_db)):
    if not db.get(Source, sid):
        raise HTTPException(404)
    audit(db, "source.run", id=sid)
    db.commit()
    threading.Thread(target=run_source, args=(sid,), daemon=True).start()
    return {"status": "started"}


class IngestUrlIn(BaseModel):
    url: str


@app.post("/api/admin/ingest-url", dependencies=admin, summary="Directly ingest an Instagram reel/post URL")
def ingest_url(body: IngestUrlIn, db: Session = Depends(get_db)):
    url = body.url.strip()
    if not url:
        raise HTTPException(422, "URL is required")
    raw_item = None
    if "instagram.com" in url:
        raw_item = parse_instagram_post(url)
        if not raw_item:
            raise HTTPException(400, "Could not extract metadata from Instagram URL. Ensure the post is public.")
    else:
        raise HTTPException(400, "Currently direct ingestion supports Instagram reel and post URLs.")

    stats = store_items(db, None, None, [raw_item])
    db.commit()
    audit(db, "url.ingest", url=url, new=stats["new"], duplicates=stats["duplicates"])
    return {"status": "ok", "url": url, "stats": stats, "title": raw_item.title, "outlet": raw_item.outlet_name}


class WikidataOverrideIn(BaseModel):
    qid: str | None = None


@app.post("/api/admin/channels/{outlet_id}/wikidata", dependencies=admin, summary="Confirm or override Wikidata QID for outlet")
def admin_sync_wikidata(outlet_id: int, body: WikidataOverrideIn, db: Session = Depends(get_db)):
    outlet = db.get(Outlet, outlet_id)
    if not outlet:
        raise HTTPException(404, "Channel not found")
    prof = sync_outlet_wikidata(db, outlet, override_qid=body.qid)
    audit(db, "channel.wikidata_sync", outlet_id=outlet_id, qid=prof.wikidata_qid)
    return {"ok": True, "qid": prof.wikidata_qid, "founded": prof.founded, "owner": prof.owner, "headquarters": prof.headquarters}


class ManualFactIn(BaseModel):
    category: Literal["share_capital", "offices", "legal_cases", "wins_losses"]
    fact: str = Field(min_length=2)
    source_url: str = Field(min_length=4)
    as_of_date: str = Field(min_length=4)


@app.post("/api/admin/channels/{outlet_id}/manual-facts", dependencies=admin, status_code=201, summary="Add manual fact (share capital, offices, legal cases)")
def admin_add_manual_fact(outlet_id: int, body: ManualFactIn, db: Session = Depends(get_db)):
    outlet = db.get(Outlet, outlet_id)
    if not outlet:
        raise HTTPException(404, "Channel not found")
    mf = OutletManualFact(
        outlet_id=outlet.id,
        category=body.category,
        fact=body.fact.strip(),
        source_url=body.source_url.strip(),
        as_of_date=body.as_of_date.strip(),
    )
    db.add(mf)
    audit(db, "channel.manual_fact_add", outlet_id=outlet_id, category=body.category)
    db.commit()
    return {"id": mf.id, "category": mf.category, "fact": mf.fact, "source_url": mf.source_url, "as_of_date": mf.as_of_date}


@app.delete("/api/admin/channels/{outlet_id}/manual-facts/{fact_id}", dependencies=admin, status_code=204)
def admin_delete_manual_fact(outlet_id: int, fact_id: int, db: Session = Depends(get_db)):
    mf = db.get(OutletManualFact, fact_id)
    if mf and mf.outlet_id == outlet_id:
        audit(db, "channel.manual_fact_delete", outlet_id=outlet_id, fact_id=fact_id)
        db.delete(mf)
        db.commit()


@app.get("/api/admin/runs", dependencies=admin)
def runs(limit: int = Query(100, le=1000), db: Session = Depends(get_db)):
    rows = db.scalars(select(CollectionRun).order_by(CollectionRun.id.desc()).limit(limit)).all()
    names = {s.id: s.name for s in db.scalars(select(Source))}
    subj = {s.id: s.name for s in db.scalars(select(Subject))}
    return [{"id": r.id, "source": names.get(r.source_id), "subject": subj.get(r.subject_id), "started": fmt(r.started_at),
             "status": r.status, "fetched": r.fetched, "new": r.new, "duplicates": r.duplicates, "latency_ms": r.latency_ms,
             "median_delay_minutes": r.median_delay_minutes, "error": r.error} for r in rows]


@app.get("/api/admin/audit", dependencies=admin)
def audit_log(db: Session = Depends(get_db)):
    return [{"at": fmt(a.at), "action": a.action, "detail": a.detail} for a in db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(300))]


@app.get("/api/admin/export", dependencies=admin, summary="Export post metadata (no article bodies)")
def export(format: Literal["csv", "json"] = "csv", f: Filters = Depends(filters), db: Session = Depends(get_db)):
    d = build_dashboard(db, Filters(**{**f.__dict__, "limit": 100000, "offset": 0}))
    rows = [{"story_id": s["id"], "story": s["title"], **{k: p[k] for k in ("o", "by", "h", "published_at", "collected_at", "tone", "url", "is_first", "kind", "scope")}}
            for s in d["stories"] for p in s["posts"]]
    if format == "json":
        return JSONResponse(rows)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else ["story_id"])
    w.writeheader()
    w.writerows(rows)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=betwatch-export.csv"})


@app.get("/api/reports/subject.pdf", summary="Export dashboard PDF intelligence report")
def export_subject_pdf(f: Filters = Depends(filters), db: Session = Depends(get_db)):
    try:
        pdf_bytes = generate_subject_pdf(db, f)
        raw_name = f.q.strip() if f.q else (f.outlet.strip() if f.outlet else "all-tracked")
        safe_ascii = re.sub(r"[^\w\-\.]+", "_", raw_name.encode("ascii", "replace").decode("ascii")).strip("_") or "report"
        filename_ascii = f"betwatch-subject-{safe_ascii}.pdf"
        quoted_utf8 = quote(f"betwatch-subject-{raw_name}.pdf")
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{filename_ascii}"; filename*=UTF-8\'\'{quoted_utf8}'},
        )
    except Exception as e:
        log.exception("Failed to generate subject PDF")
        raise HTTPException(status_code=500, detail=f"Failed to generate PDF: {e}")


@app.get("/api/reports/channel/{outlet_id}.pdf", summary="Export channel intelligence dossier PDF")
def export_channel_pdf(outlet_id: int, db: Session = Depends(get_db)):
    try:
        outlet = db.get(Outlet, outlet_id)
        raw_name = outlet.name if outlet else f"channel-{outlet_id}"
        safe_ascii = re.sub(r"[^\w\-\.]+", "_", raw_name.encode("ascii", "replace").decode("ascii")).strip("_") or f"channel-{outlet_id}"
        filename_ascii = f"betwatch-channel-{safe_ascii}.pdf"
        quoted_utf8 = quote(f"betwatch-{raw_name}.pdf")

        pdf_bytes = generate_channel_pdf(db, outlet_id)
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{filename_ascii}"; filename*=UTF-8\'\'{quoted_utf8}'},
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        log.exception("Failed to generate channel PDF")
        raise HTTPException(status_code=500, detail=f"Failed to generate PDF: {e}")


# ---------------------------------------------------------------- frontend routes
@app.get("/", include_in_schema=False)
@app.get("/api/index.py", include_in_schema=False)
def index():
    if INDEX_HTML:
        return HTMLResponse(content=INDEX_HTML, media_type="text/html")
    if FRONTEND and (FRONTEND / "index.html").exists():
        return FileResponse(FRONTEND / "index.html")
    return HTMLResponse("<h1>BetWatch</h1><p>Dashboard loading...</p>")


@app.get("/admin", include_in_schema=False)
def admin_page():
    if ADMIN_HTML:
        return HTMLResponse(content=ADMIN_HTML, media_type="text/html")
    if FRONTEND and (FRONTEND / "admin.html").exists():
        return FileResponse(FRONTEND / "admin.html")
    return HTMLResponse("<h1>BetWatch Admin</h1>")


@app.get("/story/{story_id}", include_in_schema=False)
def story_page(story_id: int):
    if STORY_HTML:
        return HTMLResponse(content=STORY_HTML, media_type="text/html")
    if FRONTEND and (FRONTEND / "story.html").exists():
        return FileResponse(FRONTEND / "story.html")
    return HTMLResponse("<h1>BetWatch Story</h1>")


@app.get("/channel/{outlet_id}", include_in_schema=False)
def channel_page(outlet_id: int):
    if CHANNEL_HTML:
        return HTMLResponse(content=CHANNEL_HTML, media_type="text/html")
    if FRONTEND and (FRONTEND / "channel.html").exists():
        return FileResponse(FRONTEND / "channel.html")
    return HTMLResponse("<h1>BetWatch Channel Profile</h1>")


@app.get("/static/styles.css", include_in_schema=False)
@app.get("/styles.css", include_in_schema=False)
def styles_asset():
    if STYLES_CSS:
        return Response(content=STYLES_CSS, media_type="text/css")
    if FRONTEND and (FRONTEND / "styles.css").exists():
        return FileResponse(FRONTEND / "styles.css")
    return Response(content="", media_type="text/css")


@app.get("/static/app.js", include_in_schema=False)
@app.get("/app.js", include_in_schema=False)
def app_js_asset():
    if APP_JS:
        return Response(content=APP_JS, media_type="application/javascript")
    if FRONTEND and (FRONTEND / "app.js").exists():
        return FileResponse(FRONTEND / "app.js")
    return Response(content="", media_type="application/javascript")
