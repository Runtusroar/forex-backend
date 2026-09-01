import os
import subprocess
from pathlib import Path


def run_launcher(tmp_path: Path, **overrides: str) -> list[str]:
    fake_chrome = tmp_path / "fake-chrome"
    fake_chrome.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n")
    fake_chrome.chmod(0o755)
    env = os.environ | {
        "CHROME_BINARY": str(fake_chrome),
        "CHROME_PROFILE_DIR": str(tmp_path / "profile"),
    } | overrides
    result = subprocess.run(
        ["bash", "scripts/start_chrome.sh"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.splitlines()


def test_launcher_keeps_local_cdp_default(tmp_path: Path) -> None:
    arguments = run_launcher(tmp_path)
    assert "--remote-debugging-address=127.0.0.1" in arguments
    assert "--no-sandbox" not in arguments


def test_launcher_enables_private_compose_network_binding(tmp_path: Path) -> None:
    arguments = run_launcher(
        tmp_path,
        CHROME_REMOTE_DEBUGGING_ADDRESS="0.0.0.0",
        CHROME_CONTAINER_MODE="1",
    )
    assert "--remote-debugging-address=0.0.0.0" in arguments
    assert "--no-sandbox" in arguments
    assert "--disable-dev-shm-usage" in arguments
