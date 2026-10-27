"""Database models. All datetimes are stored as naive UTC."""
from datetime import UTC, datetime

from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Table, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0, tzinfo=None)


article_subjects = Table(
    "article_subjects",
    Base.metadata,
    Column("article_id", ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True),
    Column("subject_id", ForeignKey("subjects.id", ondelete="CASCADE"), primary_key=True),
)


class Outlet(Base):
    """A publisher / channel / account that posts content (Times of India, Aaj Tak YouTube, a subreddit...)."""

    __tablename__ = "outlets"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    domain: Mapped[str | None] = mapped_column(String(200), index=True)
    kind: Mapped[str] = mapped_column(String(40), default="News site")  # News site, Wire agency, TV / video, Trade press, Social
    country: Mapped[str | None] = mapped_column(String(2))
    language: Mapped[str | None] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Source(Base):
    """A configured collector. kind decides the adapter; per-subject kinds run once for every enabled subject."""

    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(40))  # rss | youtube_channel | google_news | gdelt | youtube_search | reddit | bluesky
    url: Mapped[str | None] = mapped_column(Text)
    outlet_name: Mapped[str | None] = mapped_column(String(200))  # fixed outlet for rss / youtube_channel
    outlet_kind: Mapped[str | None] = mapped_column(String(40))
    country: Mapped[str | None] = mapped_column(String(2))
    language: Mapped[str | None] = mapped_column(String(10), default="en")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=15)
    notes: Mapped[str | None] = mapped_column(Text)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Subject(Base):
    """Something the user tracks: a person, company, app or law (e.g. "Mahadev", "Online Gaming Act")."""

    __tablename__ = "subjects"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    query: Mapped[str] = mapped_column(Text)  # search query sent to search adapters
    aliases: Mapped[list] = mapped_column(JSON, default=list)  # extra match terms, lowercase compare
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Cluster(Base):
    """A story: several posts about the same event, grouped automatically."""

    __tablename__ = "clusters"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    first_seen: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime, index=True)
    article_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str | None] = mapped_column(Text)
    summary_source: Mapped[str | None] = mapped_column(String(20))  # "ai" | "publisher" (excerpt of a feed description)
    highlights: Mapped[list] = mapped_column(JSON, default=list)
    summary_article_count: Mapped[int] = mapped_column(Integer, default=0)
    articles: Mapped[list["Article"]] = relationship(back_populates="cluster")


class Article(Base):
    """One post: an article, a video or a social post."""

    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("canonical_url", name="uq_article_canonical_url"),
        UniqueConstraint("fingerprint", name="uq_article_fingerprint"),
        Index("ix_articles_published", "published_at"),
        Index("ix_articles_outlet_pub", "outlet_id", "published_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), default="article")  # article | video | social
    url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(String(1000))
    fingerprint: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)  # short feed description, truncated
    author: Mapped[str | None] = mapped_column(String(300), index=True)
    outlet_id: Mapped[int] = mapped_column(ForeignKey("outlets.id"))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"))
    cluster_id: Mapped[int | None] = mapped_column(ForeignKey("clusters.id", ondelete="SET NULL"), index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    published_is_estimate: Mapped[bool] = mapped_column(Boolean, default=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    language: Mapped[str | None] = mapped_column(String(10))
    scope: Mapped[str] = mapped_column(String(20), default="National")  # National | International
    categories: Mapped[list] = mapped_column(JSON, default=list)
    tone: Mapped[str] = mapped_column(String(10), default="Neutral")  # Negative | Neutral | Positive
    tone_score: Mapped[float] = mapped_column(Float, default=0.0)
    tone_source: Mapped[str] = mapped_column(String(10), default="vader")
    is_live: Mapped[bool] = mapped_column(Boolean, default=False)
    platform: Mapped[str | None] = mapped_column(String(40))  # YouTube, Reddit, Bluesky...
    thumbnail: Mapped[str | None] = mapped_column(Text)
    enriched: Mapped[bool] = mapped_column(Boolean, default=False)

    outlet: Mapped[Outlet] = relationship()
    cluster: Mapped[Cluster | None] = relationship(back_populates="articles")
    subjects: Mapped[list[Subject]] = relationship(secondary=article_subjects)


class CollectionRun(Base):
    __tablename__ = "collection_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("subjects.id", ondelete="SET NULL"))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | ok | error
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    new: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    median_delay_minutes: Mapped[float | None] = mapped_column(Float)  # collected_at - published_at for new items
    error: Mapped[str | None] = mapped_column(Text)


class SavedSearch(Base):
    __tablename__ = "saved_searches"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Bookmark(Base):
    __tablename__ = "bookmarks"
    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    action: Mapped[str] = mapped_column(String(100))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class OutletProfile(Base):
    __tablename__ = "outlet_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    outlet_id: Mapped[int] = mapped_column(ForeignKey("outlets.id", ondelete="CASCADE"), unique=True, index=True)
    wikidata_qid: Mapped[str | None] = mapped_column(String(40), index=True)
    founded: Mapped[str | None] = mapped_column(String(100))
    owner: Mapped[str | None] = mapped_column(String(300))
    headquarters: Mapped[str | None] = mapped_column(String(300))
    official_website: Mapped[str | None] = mapped_column(Text)
    stock_exchange: Mapped[str | None] = mapped_column(String(100))
    ticker: Mapped[str | None] = mapped_column(String(40))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime)
    confirmed_by_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    outlet: Mapped[Outlet] = relationship()


class OutletLink(Base):
    __tablename__ = "outlet_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    outlet_id: Mapped[int] = mapped_column(ForeignKey("outlets.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(40))  # website | youtube | instagram | facebook | twitter | linkedin | telegram
    url: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(40), default="wikidata")  # wikidata | admin | feed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    outlet: Mapped[Outlet] = relationship()


class OutletMetric(Base):
    __tablename__ = "outlet_metrics"
    id: Mapped[int] = mapped_column(primary_key=True)
    outlet_id: Mapped[int] = mapped_column(ForeignKey("outlets.id", ondelete="CASCADE"), index=True)
    metric: Mapped[str] = mapped_column(String(60), index=True)
    value: Mapped[float | None] = mapped_column(Float)
    value_text: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(40), default="available")  # available | not_available
    reason: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    outlet: Mapped[Outlet] = relationship()


class OutletManualFact(Base):
    __tablename__ = "outlet_manual_facts"
    id: Mapped[int] = mapped_column(primary_key=True)
    outlet_id: Mapped[int] = mapped_column(ForeignKey("outlets.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(60))  # share_capital | offices | legal_cases | wins_losses
    fact: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    as_of_date: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    outlet: Mapped[Outlet] = relationship()

