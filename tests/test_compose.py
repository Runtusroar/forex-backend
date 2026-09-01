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


def test_api_has_a_healthcheck() -> None:
    compose = Path("compose.yaml").read_text()

    api_section = compose.split("  api:\n", maxsplit=1)[1]
    assert 'test: ["CMD", "curl", "--fail", "http://127.0.0.1:8000/health"]' in api_section
