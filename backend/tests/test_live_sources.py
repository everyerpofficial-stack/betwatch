"""Integration tests against REAL sources. Skipped unless BETWATCH_NETWORK_TESTS=1 (they need internet)."""
import os

import pytest
from sqlalchemy import func, select

from app.ingest.collector import run_source
from app.models import Article, Source, Subject
from app.seed import seed

pytestmark = pytest.mark.skipif(os.environ.get("BETWATCH_NETWORK_TESTS") != "1", reason="set BETWATCH_NETWORK_TESTS=1")


def test_real_rss_and_google_news(db):
    seed()
    rss = db.scalar(select(Source).where(Source.name == "RSS · Times of India"))
    res = run_source(rss.id)
    assert res[0]["status"] == "ok" and res[0]["fetched"] > 0
    gn = db.scalar(select(Source).where(Source.kind == "google_news", Source.language == "en"))
    subj = db.scalar(select(Subject).where(Subject.name == "Online Gaming Act"))
    res = run_source(gn.id, subject_ids=[subj.id])
    assert res[0]["status"] == "ok" and res[0]["fetched"] > 0
    assert db.scalar(select(func.count(Article.id))) > 0


def test_real_youtube_channel_feed(db):
    seed()
    yt = db.scalar(select(Source).where(Source.kind == "youtube_channel"))
    res = run_source(yt.id)
    assert res[0]["status"] == "ok" and res[0]["fetched"] > 0
