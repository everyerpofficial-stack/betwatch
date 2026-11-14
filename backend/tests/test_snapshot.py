"""How a deployment picks up the database the scheduled collector publishes."""
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, text

from app import snapshot
from app.ai import summarize_clusters
from app.config import get_settings
from app.models import Article, Cluster, Outlet, Source, utcnow

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


@pytest.fixture
def github(db, monkeypatch):
    """A deployment with a dispatch token, GitHub mocked: set `github.last_run` to the newest workflow run (or None),
    or `github.error` to make every call fail. `github.calls` records (method, url)."""
    class GitHub:
        last_run = None
        error = None
        calls = []

        def collected(self, minutes_ago):
            db.add(Source(name="Feed", kind="rss", last_success_at=utcnow() - timedelta(minutes=minutes_ago)))
            db.commit()

        def run(self, status, minutes_ago=0):
            at = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.last_run = {"status": status, "updated_at": at}

        def dispatched(self):
            return [u for m, u in self.calls if m == "POST" and u.endswith("/dispatches")]

    g = GitHub()
    g.calls = []

    class FakeClient:
        def __init__(self, headers=None, **kw):
            assert headers["Authorization"] == "Bearer gh-token"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def _reply(self, method, url, body):
            g.calls.append((method, url))
            if g.error:
                raise g.error
            return httpx.Response(200 if method == "GET" else 204, json=body, request=httpx.Request(method, url))

        def get(self, url, **kw):
            return self._reply("GET", url, {"workflow_runs": [g.last_run] if g.last_run else []})

        def post(self, url, **kw):
            assert kw["json"] == {"ref": "main"}
            return self._reply("POST", url, None)

    monkeypatch.setattr(snapshot.httpx, "Client", FakeClient)
    monkeypatch.setattr(snapshot, "_next_collect_check", 0.0)
    monkeypatch.setattr(get_settings(), "github_dispatch_token", "gh-token")
    return g


def test_old_data_starts_a_collection_once(github):
    github.collected(minutes_ago=200)
    github.run("completed", minutes_ago=180)
    assert snapshot.request_collection() is True
    assert len(github.dispatched()) == 1 and github.dispatched()[0].endswith("/collect.yml/dispatches")
    assert not snapshot.collect_due() and snapshot.request_collection() is False  # waits for that run's data


def test_fresh_data_asks_github_nothing(github):
    github.collected(minutes_ago=5)
    assert snapshot.request_collection() is False and github.calls == []


def test_no_second_run_while_one_is_under_way_or_just_finished(github):
    github.collected(minutes_ago=200)
    for status, ago in (("in_progress", 0), ("queued", 0), ("completed", 3)):
        snapshot._next_collect_check = 0.0
        github.run(status, ago)
        assert snapshot.request_collection() is False
    assert github.dispatched() == []


def test_never_collected_starts_a_collection(github):
    assert snapshot.request_collection() is True and len(github.dispatched()) == 1


def test_github_failure_is_retried_later(github):
    github.collected(minutes_ago=200)
    github.error = httpx.ConnectError("down")
    assert snapshot.request_collection() is False
    assert snapshot._next_collect_check <= time.monotonic() + snapshot.CHECK_EVERY_SECONDS


def test_without_a_token_no_collection_is_started(github, monkeypatch):
    monkeypatch.setattr(get_settings(), "github_dispatch_token", "")
    github.collected(minutes_ago=200)
    assert snapshot.collect_due() is False and snapshot.request_collection() is False and github.calls == []


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
