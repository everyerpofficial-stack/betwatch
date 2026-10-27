import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (see .env.example)."""

    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    database_url: str = "sqlite:////tmp/betwatch.db" if os.environ.get("VERCEL") else "sqlite:///./betwatch.db"
    admin_token: str = "change-me"
    home_country: str = "IN"  # outlets from this country count as "National"
    display_timezone: str = "Asia/Kolkata"

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
