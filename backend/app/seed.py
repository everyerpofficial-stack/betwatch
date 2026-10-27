"""Seed sources and tracked subjects. Safe to run repeatedly (upserts by name). `python -m app.seed`"""
from sqlalchemy import select

from .config import get_settings
from .db import Base, engine, session_scope
from .models import Source, Subject

RSS = [
    # name, url, country, kind, interval
    ("The Hindu", "https://www.thehindu.com/news/national/feeder/default.rss", "IN", "News site", 10),
    ("NDTV", "https://feeds.feedburner.com/ndtvnews-top-stories", "IN", "TV / video", 10),
    ("Indian Express", "https://indianexpress.com/feed/", "IN", "News site", 10),
    ("Hindustan Times", "https://www.hindustantimes.com/feeds/rss/india-news/rssfeed.xml", "IN", "News site", 10),
    ("Times of India", "https://timesofindia.indiatimes.com/rssfeedstopstories.cms", "IN", "News site", 10),
    ("India Today", "https://www.indiatoday.in/rss/home", "IN", "TV / video", 10),
    ("Mint", "https://www.livemint.com/rss/news", "IN", "News site", 15),
    ("Economic Times", "https://economictimes.indiatimes.com/rssfeedstopstories.cms", "IN", "News site", 15),
    ("Free Press Journal", "https://www.freepressjournal.in/stories.rss", "IN", "News site", 15),
    ("BBC News", "https://feeds.bbci.co.uk/news/world/rss.xml", "GB", "News site", 15),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "QA", "News site", 15),
    ("The Guardian", "https://www.theguardian.com/world/rss", "GB", "News site", 15),
    ("CalvinAyre", "https://calvinayre.com/feed/", "CA", "Trade press", 30),
    ("Asia Gaming Brief", "https://agbrief.com/feed/", "MO", "Trade press", 30),
    ("iGaming Business", "https://igamingbusiness.com/feed/", "GB", "Trade press", 30),
]
YOUTUBE_CHANNELS = [
    ("NDTV", "UCZFMm1mMw0F81Z37aaEzTUA"),
    ("Aaj Tak", "UCt4t-jeY85JegMlZ-E5UWtA"),
    ("India Today", "UCYPvAwZP8pZhSMW8qs7cVCw"),
    ("WION", "UC_gUM8rL-Lrg6O3adPW9K1g"),
]
SUBJECTS = [
    # name, search query, aliases
    ("Mahadev betting", "Mahadev betting app", ["mahadev app", "mahadev book", "mahaadev", "mahadev online book", "mahadev betting app"]),
    ("Online Gaming Act", "Online Gaming Act India", ["proga", "online gaming rules", "online gaming law", "promotion and regulation of online gaming", "online gaming authority"]),
    ("Online betting", "online betting India", ["betting app", "illegal betting", "sports betting", "betting apps", "online gambling", "betting racket"]),
    ("Betting law", "betting law OR gambling law", ["gambling act", "betting ban", "gambling bill", "anti-gambling"]),
    ("Parimatch", "Parimatch", []),
    ("Dream11", "Dream11", []),
]


def seed(only_missing: bool = True) -> None:
    s = get_settings()
    if engine.url.get_backend_name() == "sqlite":
        Base.metadata.create_all(engine)  # PostgreSQL: run `alembic upgrade head` first
    with session_scope() as db:
        def upsert(name, **kw):
            src = db.scalar(select(Source).where(Source.name == name))
            if src is None:
                db.add(Source(name=name, **kw))
            elif not only_missing:
                for k, v in kw.items():
                    setattr(src, k, v)

        for name, url, cc, kind, mins in RSS:
            upsert(f"RSS · {name}", kind="rss", url=url, outlet_name=name, outlet_kind=kind, country=cc,
                   language="en", interval_minutes=mins)
        for name, cid in YOUTUBE_CHANNELS:
            upsert(f"YouTube · {name}", kind="youtube_channel",
                   url=f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}", outlet_name=name,
                   outlet_kind="TV / video", country="IN", language="en", interval_minutes=20)
        upsert("Google News search · English (India)", kind="google_news",
               url="https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en", language="en",
               interval_minutes=30, notes="Per subject. Google limits this feed to personal, non-commercial use.")
        upsert("Google News search · Hindi (India)", kind="google_news",
               url="https://news.google.com/rss/search?q={q}&hl=hi&gl=IN&ceid=IN:hi", language="hi",
               interval_minutes=60, notes="Per subject. Google limits this feed to personal, non-commercial use.")
        upsert("GDELT global news search", kind="gdelt", interval_minutes=60, notes="1week")
        # 2 searches (normal + live) x 100 units x subjects x runs/day must stay under the 10,000/day free quota:
        # 6 subjects every 4 h = 7,200 units/day. Raise the interval if you track more subjects.
        upsert("YouTube search + live", kind="youtube_search", interval_minutes=240, enabled=bool(s.youtube_api_key),
               notes="Per subject. Needs YOUTUBE_API_KEY. Quota: 200 units per subject per run; free tier 10,000/day.")
        upsert("Reddit search", kind="reddit", interval_minutes=60,
               enabled=bool(s.reddit_client_id and s.reddit_client_secret), notes="Needs REDDIT_CLIENT_ID / SECRET.")
        upsert("Bluesky search", kind="bluesky", interval_minutes=60,
               enabled=bool(s.bluesky_handle and s.bluesky_app_password), notes="Needs BLUESKY_HANDLE / APP_PASSWORD.")

        for name, query, aliases in SUBJECTS:
            if db.scalar(select(Subject).where(Subject.name == name)) is None:
                db.add(Subject(name=name, query=query, aliases=aliases))


if __name__ == "__main__":
    seed()
    print("Seeded sources and subjects.")
