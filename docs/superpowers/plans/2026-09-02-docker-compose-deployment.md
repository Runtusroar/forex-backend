# Docker Compose Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the backend deployable with one `docker compose up -d --build` command while publishing only host port `8000`.

**Architecture:** Compose runs full Chromium under Xvfb in one service and FastAPI in another. They share one repository-built image, communicate over the private Compose network, and persist SQLite and the Chromium profile in separate named volumes.

**Tech Stack:** Docker Engine, Docker Compose, Python 3.12, uv, Debian Chromium, Xvfb, FastAPI, SQLite

**Spec:** `docs/superpowers/specs/2026-09-02-docker-compose-deployment-design.md`

## Global Constraints

- Publish only `127.0.0.1:8000` by default; never publish CDP port `9222`.
- Keep local Chrome startup bound to `127.0.0.1`; container network binding must be explicit.
- Persist `/app/data` and `/app/chrome-profile` in distinct named volumes.
- Require only `APP_API_KEY` and `MOONSHOT_API_KEY` edits for the default deployment.
- Keep Nginx, domain, TLS, firewall, PostgreSQL, Redis, Kubernetes, and APNs out of scope.
- Preserve Python 3.12 and all locked production dependency versions from `uv.lock`.

---

### Task 1: Container-Safe Chrome Launcher

**Files:**
- Modify: `scripts/start_chrome.sh`
- Create: `tests/test_start_chrome_script.py`

**Interfaces:**
- Consumes: `CHROME_BINARY` and `CHROME_PROFILE_DIR` environment variables already required by the launcher.
- Produces: optional `CHROME_REMOTE_DEBUGGING_ADDRESS` with default `127.0.0.1`, plus `CHROME_CONTAINER_MODE=1` to add container-only Chromium flags.

- [ ] **Step 1: Write the failing launcher tests**

```python
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
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run: `.venv/bin/pytest -q tests/test_start_chrome_script.py`

Expected: the second test fails because the existing launcher always passes `127.0.0.1` and has no container flags.

- [ ] **Step 3: Implement environment-controlled arguments**

Use a Bash array in `scripts/start_chrome.sh`:

```bash
remote_debugging_address="${CHROME_REMOTE_DEBUGGING_ADDRESS:-127.0.0.1}"
chrome_args=(
  "--remote-debugging-address=${remote_debugging_address}"
  "--remote-debugging-port=9222"
  "--user-data-dir=${CHROME_PROFILE_DIR}"
  "--no-first-run"
  "--no-default-browser-check"
)

if [[ "${CHROME_CONTAINER_MODE:-0}" == "1" ]]; then
  chrome_args+=("--no-sandbox" "--disable-dev-shm-usage")
fi

exec "$CHROME_BINARY" "${chrome_args[@]}"
```

- [ ] **Step 4: Run focused and full tests**

Run: `.venv/bin/pytest -q tests/test_start_chrome_script.py && .venv/bin/pytest -q`

Expected: both launcher tests pass and the full suite reports 24 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/start_chrome.sh tests/test_start_chrome_script.py
git commit -m "feat: support Chrome container runtime"
```

### Task 2: Docker Image and Compose Topology

**Files:**
- Create: `Dockerfile`
- Create: `compose.yaml`
- Create: `.dockerignore`
- Modify: `.env.example`

**Interfaces:**
- Consumes: locked packages from `pyproject.toml` and `uv.lock`; `APP_API_KEY` and `MOONSHOT_API_KEY` from `.env`.
- Produces: host API endpoint `http://127.0.0.1:${APP_PORT:-8000}`, internal CDP endpoint `http://chrome:9222`, named volumes `database` and `chrome-profile`.

- [ ] **Step 1: Verify the deployment contract is absent**

Run: `docker compose config --format json`

Expected: FAIL because `compose.yaml` does not exist.

- [ ] **Step 2: Add the reproducible runtime image**

Create `Dockerfile` using `python:3.12-slim-bookworm`, copy uv `0.11.0` from its official image, install `chromium`, `xvfb`, `curl`, and CJK fonts, install production dependencies with `uv sync --frozen --no-dev`, copy only application runtime files, create writable data/profile directories, and switch to a non-root `app` user.

- [ ] **Step 3: Add the two-service Compose model**

Create `compose.yaml` with:

- one shared local image/build definition;
- `chrome` running `xvfb-run -a bash scripts/start_chrome.sh` with container mode and a `/json/version` health check;
- `api` depending on healthy `chrome`, overriding `DATABASE_PATH=/app/data/forex_factory.sqlite3` and `CDP_URL=http://chrome:9222`, and running Uvicorn;
- only `127.0.0.1:${APP_PORT:-8000}:8000` published;
- separate persistent named volumes and `restart: unless-stopped`.

- [ ] **Step 4: Exclude secrets and local artifacts from the image**

Create `.dockerignore` containing `.env`, `.git`, `.worktrees`, `.venv`, caches, local data/profile directories, tests, and docs. Add `APP_PORT=8000` to `.env.example` while keeping existing local defaults unchanged.

- [ ] **Step 5: Validate resolved Compose behavior**

Run:

```bash
cp .env.example .env.compose-test
docker compose --env-file .env.compose-test config --format json > /tmp/forex-compose.json
python - <<'PY'
import json
from pathlib import Path

model = json.loads(Path("/tmp/forex-compose.json").read_text())
assert model["services"]["api"]["ports"][0]["published"] == "8000"
assert model["services"]["api"]["ports"][0]["host_ip"] == "127.0.0.1"
assert not model["services"]["chrome"].get("ports")
assert model["services"]["api"]["environment"]["CDP_URL"] == "http://chrome:9222"
assert set(model["volumes"]) == {"chrome-profile", "database"}
PY
```

Expected: Compose validation and every contract assertion pass. Remove `.env.compose-test` afterward.

- [ ] **Step 6: Build the image**

Run: `docker compose --env-file .env.example build`

Expected: both services resolve to the shared image and the image build exits successfully.

- [ ] **Step 7: Commit**

```bash
git add Dockerfile compose.yaml .dockerignore .env.example
git commit -m "feat: add Docker Compose deployment"
```

### Task 3: Operator Documentation and End-to-End Verification

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: `compose.yaml`, `.env.example`, `/health`, and the existing authenticated API.
- Produces: copy-paste deployment, upgrade, log, health, backup, and safe shutdown instructions.

- [ ] **Step 1: Document the Docker-first workflow**

Make Docker Compose the recommended deployment path. State that the only published service port is `8000`, list the two required secret edits, and document:

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8000/health
docker compose logs -f
git pull && docker compose up -d --build
docker compose down
```

Warn that `docker compose down -v` deletes the SQLite and Chrome-profile volumes. Retain a compact local-development section.

- [ ] **Step 2: Run static verification**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check app tests
bash -n scripts/start_chrome.sh
docker compose --env-file .env.example config --quiet
git diff --check
```

Expected: 24 tests pass and every command exits zero.

- [ ] **Step 3: Start and probe the full stack**

Create a temporary environment file from `.env.example` with non-production test keys, then run:

```bash
docker compose --env-file .env.compose-test up -d --build
docker compose --env-file .env.compose-test ps
curl --fail --retry 30 --retry-delay 2 http://127.0.0.1:8000/health
curl --fail -H 'X-API-Key: compose-test-api-key' http://127.0.0.1:8000/api/v1/status
docker compose --env-file .env.compose-test down
```

Expected: both services become healthy, `/health` returns `{"status":"ok"}`, and authenticated status returns model `kimi-k2.6`. Remove `.env.compose-test` after shutdown; retain named volumes.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: add one-command backend deployment"
```

- [ ] **Step 5: Final review**

Compare the complete branch against the spec, confirm only port `8000` is published, inspect for embedded secrets, and request an independent code review before integration.
