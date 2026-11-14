import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Where .github/workflows/collect.yml publishes the database. The default applies on Vercel whichever entrypoint it
# runs (the FastAPI preset loads app/main.py directly and never runs api/index.py), so it must live here.
PUBLISHED_SNAPSHOT = "https://github.com/everyerpofficial-stack/betwatch/releases/download/data/betwatch.db"


class Settings(BaseSettings):
    """All configuration comes from environment variables (see .env.example)."""

    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    database_url: str = "sqlite:////tmp/betwatch.db" if os.environ.get("VERCEL") else "sqlite:///./betwatch.db"
    admin_token: str = "change-me"
    home_country: str = "IN"  # outlets from this country count as "National"
    display_timezone: str = "Asia/Kolkata"
    snapshot_url: str = PUBLISHED_SNAPSHOT if os.environ.get("VERCEL") else ""  # serverless only (app/snapshot.py)
    # GitHub token allowed to run the Collect news workflow (fine-grained, Actions: read and write). With it, a visit to
    # a deployment whose data is old starts a collection, because GitHub fires the workflow's schedule only every few hours.
    github_dispatch_token: str = ""

    # Worker
    embedded_worker: bool = False  # run the collector inside the API process (single-container dev)
    scheduler_tick_seconds: int = 60
    http_timeout_seconds: float = 20.0
    user_agent: str = "BetWatchBot/1.0 (+https://example.com/bot; contact@example.com)"
    relevance_filter: bool = True  # general feeds: keep only betting/gaming items or tracked-subject matches
    enrich_bylines: bool = True  # fetch article <meta> for author when the feed has none
    enrich_max_per_run: int = 30  # ~2 s each (resolve link + read page metadata), run once per worker tick

    # Optional providers (leave blank to disable)
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-5-5"
    youtube_api_key: str = ""
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    bluesky_handle: str = ""
    bluesky_app_password: str = ""
    meta_access_token: str = ""
    ig_business_account_id: str = ""
    market_data_api_key: str = ""
    factcheck_api_key: str = ""

    # Clustering
    cluster_window_hours: int = 96
    cluster_threshold: int = 62  # rapidfuzz token_set_ratio 0-100


@lru_cache
def get_settings() -> Settings:
    return Settings()
