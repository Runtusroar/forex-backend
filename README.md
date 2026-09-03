# Forex Factory MVP Backend

A personal FastAPI backend that collects the Forex Factory economic calendar and news with a real
Chromium browser, stores the English source data in SQLite, and translates new content to Simplified
Chinese asynchronously with Kimi. The API is consumed by the separate iPhone app.

## Docker deployment

The recommended deployment requires only Docker Engine with Docker Compose. Compose runs two
containers from one image: Chromium under Xvfb and the FastAPI service. Their Chrome profile and
application data live in separate named volumes. The application-data volume contains SQLite,
validated news media, and compressed source snapshots.

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
docker compose exec -T api python scripts/smoke_news_v2.py
```

Only the API port is published, and it is bound to host loopback. Chrome's debugging port is not
published. Set `APP_PORT` in `.env` if another local port is needed; point Nginx at
`http://127.0.0.1:${APP_PORT}`.

Useful operations:

```bash
# Follow logs
docker compose logs -f

# Back up first, then upgrade after pulling repository changes
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
docker compose cp api:/app/data/media ./forex-media-backup
```

Before every News V2 deployment, keep both the SQLite backup and media directory until the new
container passes `/health` and the V2 smoke check. To roll back a migration, stop the API, restore
the pre-migration database, restore its matching media directory, and start it again:

```bash
docker compose stop api
docker compose cp ./forex_factory-backup.sqlite3 api:/app/data/forex_factory.sqlite3
docker compose cp ./forex-media-backup/. api:/app/data/media
docker compose start api
```

`docker compose down` retains all data. Do not run `docker compose down -v`: it permanently
destroys the SQLite database, cached media, source snapshots, and Chrome profile.

## Collection behavior

Playwright connects to the persistent Chromium process instead of launching a disposable browser.
Calendar collection and News V2 listing collection are independent. News V2 preserves Latest, Hot,
Fundamental, Technical, Industry, Entertainment, Educational, and Latest Comments. Canonical
articles are committed before detail, media, or translation work, so an individual downstream
failure cannot remove the English listing data.

Detail pages are stored as ordered article/social/update segments with genuine content attachments
and visible comment threads. Media is downloaded with type, signature, and size validation, then
deduplicated by SHA-256. Changed/error HTML snapshots are retained for 30 days. A low-priority,
checkpointed browser backfill follows each section's visible `More` control up to 30 days and yields
whenever live detail work is waiting.

Forex Factory `full story` anchors are stored as structured segment links, not flattened into
paragraph text. Visible terminal ellipses remain part of the Forex Factory prose. Selecting the
link in the iPhone app opens the publisher URL; the backend never requests or stores the publisher
article. Social blocks retain Forex Factory's full/clamped display mode and external action.

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

News V2 endpoints used by the new iPhone client:

- `GET /api/v2/news/sections`
- `GET /api/v2/news?section=latest&impact=high&limit=50`
- `GET /api/v2/news/{source_id}`
- `GET /api/v2/news/comments/latest`
- `GET /api/v2/news/{source_id}/comments`
- `GET /api/v2/news/media/{media_id}`
- `GET /api/v2/status`

Chinese fields can be `null` while translation is pending or unavailable. Timestamps are UTC
ISO-8601 values. V1 News remains backed by V2 compatibility serialization until the installed
iPhone client has been migrated and verified; no V1 removal is part of this release.

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
