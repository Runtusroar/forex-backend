# Forex Factory MVP Backend

A personal FastAPI backend that collects the Forex Factory economic calendar and news with a real
Chromium browser, stores the English source data in SQLite, and translates new content to Simplified
Chinese asynchronously with Kimi. The API is consumed by the separate iPhone app.

## Docker deployment

The recommended deployment requires only Docker Engine with Docker Compose. Compose runs two
containers from one image: Chromium under Xvfb and the FastAPI service. Their Chrome profile and
SQLite database live in separate named volumes.

```bash
cp .env.example .env
```

Edit `.env` and replace these two values:

- `APP_API_KEY`: a long random value used by the iPhone app in the `X-API-Key` header.
- `MOONSHOT_API_KEY`: the Kimi Code membership key created in the Kimi Code console.

The default Kimi Code endpoint is `https://api.kimi.com/coding/v1` and the default model is
`k3-256k`. Both remain configurable through `KIMI_BASE_URL` and `KIMI_MODEL`.

Start the stack:

```bash
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8000/health
```

Only the API port is published, and it is bound to host loopback. Chrome's debugging port is not
published. Set `APP_PORT` in `.env` if another local port is needed; point Nginx at
`http://127.0.0.1:${APP_PORT}`.

Useful operations:

```bash
# Follow logs
docker compose logs -f

# Upgrade after pulling repository changes
git pull
docker compose up -d --build

# Stop containers while retaining both named volumes
docker compose down
```

Create a consistent SQLite backup and copy it to the current directory:

```bash
docker compose exec -T api python -c 'import sqlite3; source=sqlite3.connect("/app/data/forex_factory.sqlite3"); backup=sqlite3.connect("/app/data/backup.sqlite3"); source.backup(backup); backup.close(); source.close()'
docker compose cp api:/app/data/backup.sqlite3 ./forex_factory-backup.sqlite3
docker compose exec -T api rm /app/data/backup.sqlite3
```

Do not run `docker compose down -v` unless the stored database and Chrome profile should be
permanently deleted.

## Collection behavior

Playwright connects to the persistent Chromium process instead of launching a disposable browser.
The calendar is committed before news detail enrichment begins. News listing entries are retained
even when an individual detail page cannot be loaded. Existing story bodies are reused unless the
listing title or summary changes.

Translation runs independently after English records are stored. A Kimi outage leaves the English
data and collection loop intact; Chinese fields remain nullable until a later retry succeeds.

Keep the server operating-system timezone aligned with the timezone shown by Forex Factory in the
persistent Chrome profile. Calendar rows show wall-clock time, so the parser converts that source
time to UTC before storing it. Singapore and China are both UTC+8.

## API

Except for `/health`, requests require the `X-API-Key` header.

- `GET /health`
- `GET /api/v1/calendar?from=<ISO8601>&to=<ISO8601>`
- `GET /api/v1/news?limit=50`
- `GET /api/v1/news/{source_id}`
- `GET /api/v1/status`

Chinese fields can be `null` while translation is pending or unavailable. Timestamps are UTC
ISO-8601 values.

## Local development

Local development requires Python 3.12, `uv`, and Chrome or Chromium.

```bash
cp .env.example .env
uv sync
export CHROME_BINARY="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
export CHROME_PROFILE_DIR="$PWD/chrome-profile"
bash scripts/start_chrome.sh
```

In a second terminal:

```bash
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

On Linux, set `CHROME_BINARY` to the installed Chrome or Chromium executable. Keep CDP port `9222`
on loopback and never expose it publicly.

## Verification

```bash
uv sync
.venv/bin/pytest -q
.venv/bin/ruff check app tests
bash -n scripts/start_chrome.sh
docker compose --env-file .env.example config --quiet
git diff --check
```
