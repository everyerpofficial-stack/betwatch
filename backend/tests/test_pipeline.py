"""Adapters (with a mocked HTTP transport + TEST FIXTURES) and the store/dedup/cluster pipeline."""
from datetime import datetime

import httpx
from sqlalchemy import func, select

from app.ingest import adapters
from app.ingest.adapters import RawItem
from app.ingest.pipeline import store_items
from app.models import Article, Cluster, Outlet, Source, Subject


def mock_client(body: bytes, status=200):
    return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status, content=body)))


def make_source(db, **kw):
    s = Source(name=kw.pop("name", "test source"), kind=kw.pop("kind", "rss"), **kw)
    db.add(s)
    db.commit()
    return s


def test_rss_adapter_parses_fixture(db, fixture_bytes, monkeypatch):
    monkeypatch.setattr(adapters, "STALE_FEED_DAYS", 100_000)  # the fixture's dates are fixed; don't let it age out
    src = make_source(db, url="https://www.fixturetimes.example/feed", outlet_name="Fixture Times", country="IN")
    items = adapters.rss(src, None, mock_client(fixture_bytes("outlet_feed.xml")))
    assert len(items) == 2
    first = items[0]
    assert first.author == "Test Reporter"
    assert first.published_at == datetime(2026, 10, 24, 8, 0)
    assert "TEST FIXTURE" in first.description and "<p>" not in first.description


def test_google_news_adapter_strips_suffix_and_reads_source(db, fixture_bytes):
    src = make_source(db, kind="google_news", url="https://news.google.com/rss/search?q={q}&gl=IN")
    subj = Subject(name="Testapp betting", query="Testapp betting", aliases=[])
    items = adapters.google_news(src, subj, mock_client(fixture_bytes("google_news.xml")))
    assert [i.outlet_name for i in items] == ["The Times of India", "The Hindu", "Dhaka Tribune"]
    assert items[0].title == "TEST: Testapp betting promoters arrested in Dubai"
    assert items[0].outlet_country == "IN"


def test_stale_feed_is_reported_not_ok(db):
    # A CDN-frozen feed (CalvinAyre served a Feb 2021 snapshot) must show as failing, not as a healthy source.
    body = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Frozen</title>
      <item><title>TEST: old story</title><link>https://x.example/old</link>
      <pubDate>Wed, 24 Feb 2021 03:46:00 +0000</pubDate></item></channel></rss>"""
    src = make_source(db, url="https://x.example/feed", outlet_name="Frozen")
    try:
        adapters.rss(src, None, mock_client(body))
        assert False, "expected a stale-feed error"
    except adapters.AdapterError as e:
        assert "stale" in str(e) and "24 Feb 2021" in str(e)


def test_rate_limit_raises(db):
    src = make_source(db, url="https://x.example/feed")
    try:
        adapters.rss(src, None, mock_client(b"", status=429))
        assert False, "expected RateLimited"
    except adapters.RateLimited:
        pass


def _items():
    return [
        RawItem(title="TEST: Testapp betting promoters arrested in Dubai", url="https://news.google.com/rss/articles/A",
                outlet_name="The Times of India", outlet_domain="timesofindia.indiatimes.com", published_at=datetime(2026, 10, 23, 7)),
        RawItem(title="TEST: Testapp betting promoters held in Dubai, says agency", url="https://news.google.com/rss/articles/B",
                outlet_name="The Hindu", outlet_domain="thehindu.com", published_at=datetime(2026, 10, 23, 9, 30)),
        RawItem(title="TEST: Bangladesh passes new gambling law", url="https://news.google.com/rss/articles/C",
                outlet_name="Dhaka Tribune", outlet_domain="dhakatribune.com", published_at=datetime(2026, 10, 22, 10)),
    ]


def test_store_dedups_links_subjects_and_clusters(db):
    s1 = Subject(name="Testapp betting", query="Testapp betting", aliases=["testapp"])
    s2 = Subject(name="Dubai", query="Dubai", aliases=[])
    db.add_all([s1, s2])
    db.commit()
    st = store_items(db, None, s1, _items())
    db.commit()
    assert st["new"] == 3 and st["duplicates"] == 0
    # The two Testapp headlines are one story; Bangladesh is another.
    assert db.scalar(select(func.count(Cluster.id))) == 2
    big = db.scalar(select(Cluster).order_by(Cluster.article_count.desc()))
    assert big.article_count == 2
    # Registry merged "The Times of India" into canonical "Times of India"; Bangladesh outlet is International.
    assert db.scalar(select(Outlet).where(Outlet.name == "Times of India")) is not None
    bd = db.scalar(select(Article).where(Article.title.like("%Bangladesh%")))
    assert bd.scope == "International"
    # Same post again from an RSS feed with a direct URL: duplicate by fingerprint, URL upgraded, subject linked.
    again = RawItem(title="TEST: Testapp betting promoters arrested in Dubai", url="https://timesofindia.indiatimes.com/a/123.cms",
                    outlet_name="Times of India", outlet_domain="timesofindia.indiatimes.com", author="A Reporter",
                    published_at=datetime(2026, 10, 23, 7))
    st2 = store_items(db, None, s2, [again])
    db.commit()
    assert st2["new"] == 0 and st2["duplicates"] == 1
    toi = db.scalar(select(Article).where(Article.title.like("%arrested in Dubai%")))
    assert toi.url.startswith("https://timesofindia") and toi.author == "A Reporter"
    assert {s.name for s in toi.subjects} == {"Testapp betting", "Dubai"}


def test_relevance_gate_on_general_feeds(db):
    src = make_source(db, url="https://x.example/feed", outlet_name="Fixture Times", outlet_kind="News site")
    items = [RawItem(title="TEST: Monsoon to hit coast tomorrow", url="https://x.example/1", outlet_name="Fixture Times"),
             RawItem(title="TEST: Police bust illegal betting racket", url="https://x.example/2", outlet_name="Fixture Times")]
    st = store_items(db, src, None, items)
    assert st["new"] == 1 and st["skipped"] == 1
    trade = make_source(db, name="trade", url="https://y.example/feed", outlet_name="Trade Wire", outlet_kind="Trade press")
    st2 = store_items(db, trade, None, [RawItem(title="TEST: Slot studio signs new deal", url="https://y.example/1", outlet_name="Trade Wire")])
    assert st2["new"] == 1  # trade press is relevant by construction


def test_estimated_youtube_date_is_replaced_and_story_refiled(db):
    from app.ingest.enrich import enrich_video_dates
    from app.models import utcnow

    now = utcnow()
    subj = Subject(name="Testapp betting", query="Testapp betting", aliases=[])
    db.add(subj)
    db.commit()
    # Today's article, plus a years-old video whose scraped date could not be parsed (so it looked like today's).
    store_items(db, None, subj, [
        RawItem(title="TEST: Testapp betting app banned by ministry", url="https://news.example/1",
                outlet_name="Fixture Times", published_at=now),
        RawItem(title="TEST: Testapp betting app banned by ministry explained", url="https://www.youtube.com/watch?v=TESTVID0001",
                outlet_name="Fixture Channel", kind="video", platform="YouTube", published_at=None, published_is_estimate=True),
    ])
    db.commit()
    video = db.scalar(select(Article).where(Article.kind == "video"))
    assert video.enriched is False  # queued for the exact-date lookup
    assert db.scalar(select(func.count(Cluster.id))) == 1  # wrongly grouped with today's story

    watch = b'<html><head><meta itemprop="datePublished" content="2023-06-12T04:50:34-07:00"></head></html>'
    def handler(req):
        return httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(200, content=watch)
    fixed = enrich_video_dates(db, httpx.Client(transport=httpx.MockTransport(handler)))
    db.commit()

    assert fixed == 1
    db.refresh(video)
    assert video.published_at == datetime(2023, 6, 12, 11, 50, 34) and not video.published_is_estimate
    # The 2023 video is its own story now, and today's story no longer claims a 2023 post.
    assert db.scalar(select(func.count(Cluster.id))) == 2
    today_story = db.scalar(select(Cluster).join(Article).where(Article.kind == "article"))
    assert today_story.article_count == 1 and today_story.first_seen == now
