from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_path: Path = Path("data/forex_factory.sqlite3")
    cdp_url: str = "http://127.0.0.1:9222"
    collect_interval_seconds: int = Field(default=30, gt=0)
    news_source_timezone: str = "Asia/Shanghai"
    news_detail_interval_seconds: int = Field(default=2, gt=0)
    news_detail_max_attempts: int = Field(default=8, gt=0)
    news_snapshot_dir: Path = Path("data/snapshots")
    news_snapshot_retention_days: int = Field(default=30, gt=0)
    news_media_dir: Path = Path("data/media")
    news_media_max_bytes: int = Field(default=10_485_760, gt=0)
    news_backfill_days: int = Field(default=30, gt=0)
    app_api_key: SecretStr
    moonshot_api_key: SecretStr
    kimi_base_url: str = "https://api.kimi.com/coding/v1"
    kimi_model: str = "k3-256k"
    kimi_timeout_seconds: float = Field(default=120, gt=0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
