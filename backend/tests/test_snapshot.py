"""How a deployment picks up the database the scheduled collector publishes."""
import sqlite3
import time

import httpx
import pytest
from sqlalchemy import create_engine, text

from app import snapshot
from app.ai import summarize_clusters
from app.config import get_settings
from app.models import Article, Cluster, Outlet, utcnow

URL = "https://example.test/betwatch.db"


def make_db(path, posts: int) -> bytes:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY)")
    con.executemany("INSERT INTO articles (id) VALUES (?)", [(i,) for i in range(1, posts + 1)])
    con.commit()
    con.close()
    return path.read_bytes()


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A deployment serving a 3-post copy, with the download mocked by setting `live.reply`."""
    class Live:
        path = tmp_path / "live.db"
        reply = None
        calls = []

        def posts(self):
            with self.engine.connect() as c:
                return c.scalar(text("SELECT COUNT(*) FROM articles"))

    d = Live()
    d.calls = []
    make_db(d.path, 3)
    d.engine = create_engine(f"sqlite:///{d.path}")
    monkeypatch.setattr(snapshot, "engine", d.engine)
    monkeypatch.setattr(snapshot, "_next_check", 0.0)
    monkeypatch.setattr(snapshot, "_etag", None)
    monkeypatch.setattr(get_settings(), "snapshot_url", URL)

    def fake_get(url, headers=None, **kw):
        d.calls.append(headers or {})
        if isinstance(d.reply, Exception):
            raise d.reply
        return httpx.Response(d.reply[0], content=d.reply[1], headers=d.reply[2], request=httpx.Request("GET", url))

    monkeypatch.setattr(snapshot.httpx, "get", fake_get)
    yield d
    d.engine.dispose()


def leftovers(live):
    return [p.name for p in live.path.parent.iterdir() if p.name.endswith(".new")]


def test_newer_snapshot_replaces_the_copy_and_next_check_is_conditional(live, tmp_path, monkeypatch):
    assert live.posts() == 3  # an open pooled connection must not keep serving the old file
    live.reply = (200, make_db(tmp_path / "pub.db", 5), {"etag": '"v2"'})
    assert snapshot.refresh() is True
    assert live.posts() == 5 and not leftovers(live)

    monkeypatch.setattr(snapshot, "_next_check", 0.0)
    live.reply = (304, b"", {})
    assert snapshot.refresh() is False
    assert live.calls[-1] == {"If-None-Match": '"v2"'} and live.posts() == 5


def test_damaged_download_is_rejected(live):
    live.reply = (200, b"<html>not a database</html>", {})
    assert snapshot.refresh() is False
    assert live.posts() == 3 and not leftovers(live)


def test_a_database_of_another_shape_is_rejected(live, tmp_path):
    con = sqlite3.connect(tmp_path / "other.db")
    con.execute("CREATE TABLE something_else (id INTEGER)")
    con.commit()
    con.close()
    live.reply = (200, (tmp_path / "other.db").read_bytes(), {})
    assert snapshot.refresh() is False
    assert live.posts() == 3


def test_snapshot_with_fewer_posts_is_rejected(live, tmp_path):
    live.reply = (200, make_db(tmp_path / "pub.db", 2), {"etag": '"old"'})
    assert snapshot.refresh() is False
    assert live.posts() == 3 and snapshot._etag is None  # not remembered, so it is looked at again later


def test_failures_keep_the_copy_and_retry_sooner(live):
    for reply in (httpx.ConnectError("down"), (404, b"", {}), (429, b"", {})):
        snapshot._next_check = 0.0
        live.reply = reply
        assert snapshot.refresh() is False
        assert live.posts() == 3
        assert snapshot._next_check <= time.monotonic() + snapshot.RETRY_SECONDS


def test_checks_are_throttled(live, tmp_path):
    live.reply = (304, b"", {})
    snapshot.refresh()
    snapshot.refresh()
    assert len(live.calls) == 1 and not snapshot.due()


def test_without_a_url_nothing_happens(live, monkeypatch):
    monkeypatch.setattr(get_settings(), "snapshot_url", "")
    assert snapshot.due() is False and snapshot.refresh() is False
    assert live.calls == []


def test_export_includes_the_write_ahead_log(live, tmp_path, monkeypatch):
    writer = sqlite3.connect(live.path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("INSERT INTO articles (id) VALUES (99)")
    writer.commit()  # still open, so the row has not reached the main file
    out = tmp_path / "out.db"
    assert snapshot.export(out) == 4
    writer.close()
    con = sqlite3.connect(out)
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    con.close()


def test_collector_without_llm_keeps_existing_ai_summary(db):
    outlet = Outlet(name="Fixture Times", domain="fixture.test")
    cluster = Cluster(title="Story", first_seen=utcnow(), last_seen=utcnow(), summary="Written by AI", summary_source="ai",
                      highlights=["point"], summary_article_count=1)
    db.add_all([outlet, cluster])
    db.flush()
    for i in range(2):
        db.add(Article(title=f"Post {i}", description="Publisher text", url=f"https://fixture.test/{i}", canonical_url=f"https://fixture.test/{i}",
                       fingerprint=f"fp{i}", outlet_id=outlet.id, cluster_id=cluster.id))
    db.flush()
    assert summarize_clusters(db, {cluster.id}) == 0
    assert (cluster.summary, cluster.summary_source, cluster.summary_article_count) == ("Written by AI", "ai", 1)
