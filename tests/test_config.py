from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_have_mvp_defaults(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "db.sqlite3",
        app_api_key="test-api-key",
        moonshot_api_key="test-kimi-key",
    )

    assert settings.cdp_url == "http://127.0.0.1:9222"
    assert settings.collect_interval_seconds == 30
    assert settings.kimi_base_url == "https://api.kimi.com/coding/v1"
    assert settings.kimi_model == "k3-256k"
    assert settings.kimi_timeout_seconds == 120


def test_interval_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_path=tmp_path / "db.sqlite3",
            app_api_key="x",
            moonshot_api_key="y",
            collect_interval_seconds=0,
        )
