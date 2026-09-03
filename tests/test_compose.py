from pathlib import Path


def test_chrome_has_writable_x11_socket_directory() -> None:
    compose = Path("compose.yaml").read_text()

    assert "/tmp/.X11-unix:mode=1777" in compose


def test_chrome_waits_for_xvfb_without_xvfb_run_signal_race() -> None:
    compose = Path("compose.yaml").read_text()

    assert "xvfb-run" not in compose
    assert "xdpyinfo -display :99" in compose


def test_api_shares_chrome_loopback_for_restricted_cdp_listener() -> None:
    compose = Path("compose.yaml").read_text()

    assert 'network_mode: "service:chrome"' in compose
    assert "CDP_URL: http://127.0.0.1:9222" in compose
    assert "CHROME_REMOTE_DEBUGGING_ADDRESS: 127.0.0.1" in compose


def test_chrome_removes_only_its_stale_runtime_locks() -> None:
    compose = Path("compose.yaml").read_text()

    assert "rm -f /tmp/.X99-lock /app/chrome-profile/Singleton*" in compose


def test_api_receives_configurable_kimi_endpoint_and_model() -> None:
    compose = Path("compose.yaml").read_text()

    assert "KIMI_BASE_URL: ${KIMI_BASE_URL:-https://api.kimi.com/coding/v1}" in compose
    assert "KIMI_MODEL: ${KIMI_MODEL:-k3-256k}" in compose
    assert "KIMI_TIMEOUT_SECONDS: ${KIMI_TIMEOUT_SECONDS:-120}" in compose


def test_api_receives_configurable_collection_interval() -> None:
    compose = Path("compose.yaml").read_text()

    assert "COLLECT_INTERVAL_SECONDS: ${COLLECT_INTERVAL_SECONDS:-30}" in compose


def test_api_receives_all_news_worker_settings_under_persistent_data() -> None:
    compose = Path("compose.yaml").read_text()

    assert "NEWS_SOURCE_TIMEZONE: ${NEWS_SOURCE_TIMEZONE:-Asia/Shanghai}" in compose
    assert "NEWS_DETAIL_INTERVAL_SECONDS: ${NEWS_DETAIL_INTERVAL_SECONDS:-2}" in compose
    assert "NEWS_DETAIL_MAX_ATTEMPTS: ${NEWS_DETAIL_MAX_ATTEMPTS:-8}" in compose
    assert "NEWS_SOURCE_INTERVAL_SECONDS: ${NEWS_SOURCE_INTERVAL_SECONDS:-5}" in compose
    assert "NEWS_SOURCE_TIMEOUT_SECONDS: ${NEWS_SOURCE_TIMEOUT_SECONDS:-20}" in compose
    assert "NEWS_SOURCE_MAX_BYTES: ${NEWS_SOURCE_MAX_BYTES:-2000000}" in compose
    assert "NEWS_SOURCE_MAX_REDIRECTS: ${NEWS_SOURCE_MAX_REDIRECTS:-5}" in compose
    assert "NEWS_SOURCE_MAX_ATTEMPTS: ${NEWS_SOURCE_MAX_ATTEMPTS:-5}" in compose
    assert "NEWS_SNAPSHOT_DIR: /app/data/snapshots" in compose
    assert "NEWS_SNAPSHOT_RETENTION_DAYS: ${NEWS_SNAPSHOT_RETENTION_DAYS:-30}" in compose
    assert "NEWS_MEDIA_DIR: /app/data/media" in compose
    assert "NEWS_MEDIA_MAX_BYTES: ${NEWS_MEDIA_MAX_BYTES:-10485760}" in compose
    assert "NEWS_BACKFILL_DAYS: ${NEWS_BACKFILL_DAYS:-30}" in compose


def test_api_has_a_healthcheck() -> None:
    compose = Path("compose.yaml").read_text()

    api_section = compose.split("  api:\n", maxsplit=1)[1]
    assert 'test: ["CMD", "curl", "--fail", "http://127.0.0.1:8000/health"]' in api_section


def test_image_explicitly_installs_xvfb_readiness_dependency() -> None:
    dockerfile = Path("Dockerfile").read_text()

    assert "x11-utils" in dockerfile
