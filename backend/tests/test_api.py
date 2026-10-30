"""API tests on TEST FIXTURE data: dashboard filters, channel coverage check, journalists, admin auth & validation."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.ingest.adapters import RawItem
from app.ingest.pipeline import store_items
from app.main import app
from app.models import Subject, utcnow

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(db):
    now = utcnow()
    s = Subject(name="Testapp betting", query="Testapp betting", aliases=["testapp"])
    db.add(s)
    db.commit()
    items = [
        RawItem(title="TEST: Testapp betting promoters arrested in Dubai", url="https://timesofindia.indiatimes.com/t/1.cms",
                outlet_name="Times of India", outlet_domain="timesofindia.indiatimes.com", author="Asha Rao",
                published_at=now - timedelta(hours=5)),
        RawItem(title="TEST: Testapp betting promoters held in Dubai, says agency", url="https://www.thehindu.com/t/2",
                outlet_name="The Hindu", outlet_domain="thehindu.com", published_at=now - timedelta(hours=3)),
        RawItem(title="TEST: Evening studio debate on cricket fixtures LIVE", url="https://www.youtube.com/watch?v=TEST1", kind="video",
                outlet_name="Fixture TV", platform="YouTube", published_at=now - timedelta(hours=1)),
    ]
    store_items(db, None, s, items)  # found by searching the subject
    other = RawItem(title="TEST: Court grants relief to gaming firm, ban quashed", url="https://indianexpress.com/t/3",
                    outlet_name="Indian Express", outlet_domain="indianexpress.com", author="Ravi Kumar",
                    published_at=now - timedelta(days=10))
    store_items(db, None, None, [other])  # general feed item, unrelated to the subject
    db.commit()
    return TestClient(app)


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_dashboard_subject_kpis_and_first_publisher(client):
    d = client.get("/api/dashboard", params={"q": "Testapp", "time": "all"}).json()
    assert d["k"]["posts"] == 3 and d["k"]["videos"] == 1
    story = next(s for s in d["stories"] if s["post_count"] == 2)
    assert story["first_name"] == "Times of India"
    assert story["posts"][0]["is_first"] and story["posts"][0]["o"] == "Times of India"
    assert d["k"]["first"] == "Times of India"
    assert len(d["live"]) == 1


def test_timeframe_filter(client):
    week = client.get("/api/dashboard", params={"time": "week"}).json()
    month = client.get("/api/dashboard", params={"time": "month"}).json()
    assert week["k"]["posts"] == 3 and month["k"]["posts"] == 4


def test_channel_coverage_yes_and_no(client):
    yes = client.get("/api/dashboard", params={"q": "Testapp", "time": "all", "outlet": "The Hindu"}).json()["channel_detail"]
    assert yes["covered"] and yes["count"] == 1
    no = client.get("/api/dashboard", params={"q": "Testapp", "time": "all", "outlet": "Indian Express"}).json()["channel_detail"]
    assert not no["covered"] and no["total"] == 1


def test_journalist_and_tone_filters(client):
    d = client.get("/api/dashboard", params={"time": "all", "journo": "Ravi Kumar"}).json()
    assert d["k"]["posts"] == 1 and d["journalist_detail"]["count"] == 1
    neg = client.get("/api/dashboard", params={"time": "all", "tone": "Negative"}).json()
    assert neg["k"]["posts"] >= 1 and neg["k"]["neg"] == neg["k"]["posts"]  # KPIs count only matching posts
    pos = client.get("/api/dashboard", params={"time": "all", "tone": "Positive"}).json()
    assert [s["title"] for s in pos["stories"]] == ["TEST: Court grants relief to gaming firm, ban quashed"]


def test_subjects_and_rankings(client):
    subs = client.get("/api/subjects").json()
    assert subs[0]["name"] == "Testapp betting" and subs[0]["posts"] == 3
    r = client.get("/api/rankings").json()
    assert r["views"] is None


def test_ranking_click_count_equals_ranking_count(client):
    d = client.get("/api/dashboard", params={"time": "all"}).json()
    for rank_key in ("rank_betting", "rank_mahadev"):
        ranks = d.get(rank_key, [])
        for r in ranks:
            goto = r["goto"]
            res = client.get("/api/dashboard", params=goto).json()
            cd = res["channel_detail"]
            assert cd is not None, f"channel_detail missing for {r['name']}"
            assert cd["count"] == r["n"], f"Post count {cd['count']} != ranking {r['n']} for {r['name']}"
            assert len(cd["posts"]) == r["n"]
            assert cd["covered"] is True

    # Test 0-post reason when filtered out
    filt_res = client.get("/api/dashboard", params={"outlet": "Times of India", "cat": "Stock market", "time": "all"}).json()
    cd_filt = filt_res["channel_detail"]
    assert cd_filt["count"] == 0
    assert cd_filt["reason_kind"] == "filters"
    assert "Your filters exclude" in cd_filt["reason"]

    # Test 0-post reason when outlet not found
    missing_res = client.get("/api/dashboard", params={"outlet": "Unknown Ghost Outlet", "time": "all"}).json()
    cd_miss = missing_res["channel_detail"]
    assert cd_miss["count"] == 0
    assert cd_miss["reason_kind"] == "missing"



def test_admin_requires_token(client):
    assert client.get("/api/admin/sources").status_code == 401
    assert client.get("/api/admin/sources", headers=AUTH).status_code == 200


def test_admin_rejects_private_feed_url(client):
    r = client.post("/api/admin/sources", headers=AUTH, json={"name": "evil", "kind": "rss", "url": "http://127.0.0.1:8000/x", "outlet_name": "x"})
    assert r.status_code == 422


def test_admin_add_source_and_audit(client):
    r = client.post("/api/admin/sources", headers=AUTH, json={"name": "RSS · Example", "kind": "rss", "url": "https://example.com/feed", "outlet_name": "Example"})
    assert r.status_code == 201
    assert client.get("/api/admin/audit", headers=AUTH).json()[0]["action"] == "source.add"


def test_saved_search_roundtrip(client):
    r = client.post("/api/saved-searches", json={"name": "Mine", "params": {"q": "Testapp", "evil": "x"}})
    assert r.status_code == 201 and r.json()["params"] == {"q": "Testapp"}
    assert len(client.get("/api/saved-searches").json()) == 1


def test_export_csv(client):
    r = client.get("/api/admin/export", headers=AUTH, params={"time": "all"})
    assert r.status_code == 200 and "Times of India" in r.text


def test_story_details_extended(client):
    d = client.get("/api/dashboard", params={"time": "all"}).json()
    assert d["stories"]
    story_id = d["stories"][0]["id"]
    res = client.get(f"/api/stories/{story_id}")
    assert res.status_code == 200
    st = res.json()
    assert st["id"] == story_id
    assert st["title"]
    assert "channels" in st and len(st["channels"]) >= 1
    assert "posts" in st and len(st["posts"]) >= 1
    p0 = st["posts"][0]
    assert "outlet_id" in p0 and "published_at_fmt" in p0 and "is_first" in p0


def test_search_lists_only_matching_posts_of_a_loose_story(client, db):
    now = utcnow()
    store_items(db, None, None, [
        RawItem(title="TEST: Online betting racket busted in Pune, Zorbet app seized", url="https://indianexpress.com/t/z1",
                outlet_name="Indian Express", outlet_domain="indianexpress.com", published_at=now - timedelta(hours=2)),
        RawItem(title="TEST: Online betting racket busted in Nagpur", url="https://www.thehindu.com/t/z2",
                outlet_name="The Hindu", outlet_domain="thehindu.com", published_at=now - timedelta(hours=1)),
    ])
    db.commit()
    all_posts = client.get("/api/dashboard", params={"time": "all"}).json()
    loose = next(s for s in all_posts["stories"] if "Pune" in " ".join(p["h"] for p in s["posts"]))
    assert loose["post_count"] == 2  # both posts are clustered into one story

    d = client.get("/api/dashboard", params={"q": "Zorbet", "time": "all"}).json()
    listed = [p["h"] for s in d["stories"] for p in s["posts"]]
    assert listed == ["TEST: Online betting racket busted in Pune, Zorbet app seized"]
    assert d["k"]["posts"] == d["stories"][0]["post_count"] == sum(c["n"] for c in d["channels"]) == 1
    assert "Zorbet" in d["stories"][0]["title"]


def test_keyword_and_subject_counts_equal_search_results(client):
    d = client.get("/api/dashboard", params={"q": "Testapp betting", "time": "all"}).json()
    kw = next(k for k in client.get("/api/keywords").json() if k["name"] == "Testapp betting")
    sub = next(s for s in client.get("/api/subjects").json() if s["name"] == "Testapp betting")
    assert kw["posts"] == sub["posts"] == d["k"]["posts"] == 3
    assert sub["stories"] == d["stories_total"]


def test_saved_keyword_lifecycle(client):
    # 1. Saving keyword creates Subject and triggers collection
    res = client.post("/api/keywords", json={"name": "Casino Regulation India", "aliases": ["casino law", "goa casinos"]})
    assert res.status_code == 201
    kw = res.json()
    assert kw["name"] == "Casino Regulation India"
    assert kw["status"] == "Collecting…"

    # 2. Appears in /api/subjects
    subs = client.get("/api/subjects").json()
    assert any(s["name"] == "Casino Regulation India" for s in subs)

    # 3. Appears in /api/keywords
    kws = client.get("/api/keywords").json()
    matched = next((k for k in kws if k["id"] == kw["id"]), None)
    assert matched is not None
    assert matched["name"] == "Casino Regulation India"
    assert matched["enabled"] is True

    # 4. Patch keyword
    patch_res = client.patch(f"/api/keywords/{kw['id']}", json={"enabled": False})
    assert patch_res.status_code == 200
    assert patch_res.json()["enabled"] is False

    # 5. Delete keyword
    del_res = client.delete(f"/api/keywords/{kw['id']}")
    assert del_res.status_code == 204
    subs_after = client.get("/api/subjects").json()
    assert not any(s["name"] == "Casino Regulation India" for s in subs_after)


def test_wikidata_parsing_fixture(fixture_bytes):
    import json
    from app.services.channel_profile import parse_wikidata_entity
    data = json.loads(fixture_bytes("wikidata_entity.json").decode("utf-8"))
    entity = data["entities"]["Q468087"]
    parsed = parse_wikidata_entity(entity)

    assert parsed["qid"] == "Q468087"
    assert parsed["founded"] == "1838"
    assert parsed["owner"] == "The Times Group"
    assert parsed["headquarters"] == "Mumbai, Maharashtra, India"
    assert parsed["official_website"] == "https://timesofindia.indiatimes.com/"
    assert parsed["stock_exchange"] == "Bombay Stock Exchange"
    assert parsed["ticker"] == "500000"

    link_map = {l["platform"]: l["url"] for l in parsed["links"]}
    assert link_map["twitter"] == "https://x.com/timesofindia"
    assert link_map["instagram"] == "https://instagram.com/timesofindia"
    assert link_map["facebook"] == "https://facebook.com/TimesofIndia"
    assert link_map["youtube"] == "https://www.youtube.com/channel/UCt4t-jeY85JegMlZ-E5UWtA"
    assert link_map["linkedin"] == "https://www.linkedin.com/company/the-times-of-india"
    assert link_map["telegram"] == "https://t.me/timesofindia_official"


def test_channel_profile_and_manual_facts(client):
    # 1. Fetch channel profile for Times of India (present in fixture)
    d = client.get("/api/dashboard", params={"time": "all"}).json()
    toi = next(c for c in d["channels"] if c["name"] == "Times of India")
    res = client.get(f"/api/channels/{toi['id']}")
    assert res.status_code == 200
    prof = res.json()
    assert prof["outlet"]["name"] == "Times of India"
    assert "profile" in prof
    assert "audience" in prof and len(prof["audience"]) >= 4
    # Check NO FAKE DATA: audience entries have status and source
    for aud in prof["audience"]:
        assert aud["source"] and aud["status"]
        if aud["status"] == "Not available":
            assert aud["reason"]

    assert "coverage" in prof and prof["coverage"]["total_posts"] >= 1
    assert "posts" in prof and len(prof["posts"]) >= 1

    # 2. Add manual fact via Admin
    fact_res = client.post(
        f"/api/admin/channels/{toi['id']}/manual-facts",
        headers=AUTH,
        json={
            "category": "legal_cases",
            "fact": "Delhi High Court dismissed trademark infringement claim against the outlet",
            "source_url": "https://delhihighcourt.nic.in/order/123",
            "as_of_date": "10 Oct 2026",
        },
    )
    assert fact_res.status_code == 201
    fact_id = fact_res.json()["id"]

    # 3. Fact appears in track record
    prof2 = client.get(f"/api/channels/{toi['id']}").json()
    cases = prof2["track_record"]["legal_cases"]
    assert any(c["id"] == fact_id and "Delhi High Court" in c["fact"] for c in cases)

    # 4. Delete manual fact
    del_res = client.delete(f"/api/admin/channels/{toi['id']}/manual-facts/{fact_id}", headers=AUTH)
    assert del_res.status_code == 204


def test_pdf_reports(client, fresh_db):
    # 1. Subject PDF report
    res = client.get("/api/reports/subject.pdf?time=all")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/pdf"
    assert res.content.startswith(b"%PDF-")
    assert len(res.content) > 1000

    # 2. Channel PDF report for existing outlet
    d = client.get("/api/dashboard?time=all").json()
    toi = next(c for c in d["channels"] if c["name"] == "Times of India")
    ch_res = client.get(f"/api/reports/channel/{toi['id']}.pdf")
    assert ch_res.status_code == 200
    assert ch_res.headers["content-type"] == "application/pdf"
    assert ch_res.content.startswith(b"%PDF-")
    assert len(ch_res.content) > 1000

    # 3. Channel PDF 404 on nonexistent outlet
    err_res = client.get("/api/reports/channel/999999.pdf")
    assert err_res.status_code == 404


