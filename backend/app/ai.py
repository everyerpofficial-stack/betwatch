"""Story summaries.

With ANTHROPIC_API_KEY: an LLM writes a short summary + highlights from the headlines and feed descriptions of
the posts in a story (never from full article text), and judges tone toward the betting industry. Output is
labelled summary_source="ai" in the API and UI.

Without a key: the summary is the longest feed description in the story, labelled summary_source="publisher",
and highlights are distinct headlines from different outlets — i.e. literally what each outlet led with.
"""
import json
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Article, Cluster

log = logging.getLogger("betwatch.ai")

PROMPT = """You summarise news coverage for a media-intelligence dashboard about betting, gaming and regulation.
Below are headlines and short feed descriptions from different outlets about what may be the same event.
Use ONLY this material. Do not add facts, names, numbers or quotes that are not in it. Mark allegations as
allegations. Reply with JSON only:
{{"summary": "2-3 sentence neutral summary", "highlights": ["up to 4 short points outlets emphasised"],
  "tone": "Negative|Neutral|Positive"}}
where tone is how the news reads for the betting/gaming industry (crackdowns, arrests, bans = Negative).

Posts:
{posts}"""


def _fallback(arts: list[Article]) -> tuple[str | None, list[str]]:
    descs = sorted((a.description for a in arts if a.description), key=len, reverse=True)
    summary = descs[0] if descs else None
    seen_outlets, highlights = set(), []
    for a in sorted(arts, key=lambda a: a.published_at or a.collected_at):
        if a.outlet_id in seen_outlets:
            continue
        seen_outlets.add(a.outlet_id)
        highlights.append(f"{a.outlet.name}: {a.title}")
        if len(highlights) == 4:
            break
    return summary, highlights


def _llm(arts: list[Article]) -> dict | None:
    s = get_settings()
    if not s.anthropic_api_key:
        return None
    posts = "\n".join(f"- [{a.outlet.name}] {a.title} — {(a.description or '')[:280]}" for a in arts[:25])
    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages", timeout=60,
            headers={"x-api-key": s.anthropic_api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": s.anthropic_model, "max_tokens": 500,
                  "messages": [{"role": "user", "content": PROMPT.format(posts=posts)}]},
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        if not isinstance(data.get("summary"), str):
            return None
        return data
    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.warning("LLM summary failed: %s", e)
        return None


def summarize_clusters(db: Session, cluster_ids: set[int]) -> int:
    done = 0
    for cid in cluster_ids:
        c = db.get(Cluster, cid)
        if c is None:
            continue
        arts = db.scalars(select(Article).where(Article.cluster_id == cid)).all()
        if not arts or (c.summary and c.summary_article_count == len(arts)):
            continue
        data = _llm(arts) if len(arts) >= 1 else None
        if data:
            c.summary = data["summary"][:1200]
            c.highlights = [str(h)[:300] for h in data.get("highlights", [])][:4]
            c.summary_source = "ai"
            if data.get("tone") in ("Negative", "Neutral", "Positive"):
                for a in arts:
                    if a.tone_source != "ai":
                        a.tone, a.tone_source = data["tone"], "ai"
        else:
            c.summary, c.highlights = _fallback(arts)
            c.summary_source = "publisher"
        c.summary_article_count = len(arts)
        done += 1
    return done
