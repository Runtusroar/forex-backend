# Forex Factory MVP Backend

A small personal backend that collects the Forex Factory economic calendar and news, stores source
data in SQLite, translates new English content asynchronously with Kimi, and exposes a read-only
FastAPI API for the iPhone app.

## Architecture

```text
Persistent Chrome -> Playwright over local CDP -> parsers -> SQLite -> FastAPI
                                                        \-> Kimi translation worker
```

Playwright connects to a separately started Chrome. It does not launch the browser. English source
data is committed before translation starts, so a Kimi failure never blocks collection or removes
existing data.

## Requirements

- Python 3.12
- `uv`
- Google Chrome or Chromium
- a Kimi API key

## Setup

```bash
cp .env.example .env
uv sync
```

Set a long random `APP_API_KEY` and your `MOONSHOT_API_KEY` in `.env`. The default translation model
is `kimi-k2.6` with thinking disabled.

Start a dedicated Chrome profile on macOS:

```bash
export CHROME_BINARY="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
export CHROME_PROFILE_DIR="$PWD/chrome-profile"
bash scripts/start_chrome.sh
```

On Linux, set `CHROME_BINARY` to the installed Chrome/Chromium executable. Keep CDP port `9222`
bound to loopback; never expose it publicly.

Start the service:

```bash
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

## API

Except for `/health`, requests require `X-API-Key`.

- `GET /health`
- `GET /api/v1/calendar?from=<ISO8601>&to=<ISO8601>`
- `GET /api/v1/news?limit=50`
- `GET /api/v1/news/{source_id}`
- `GET /api/v1/status`

Chinese fields are nullable while translation is pending or unavailable. Timestamps are UTC
ISO-8601 values.

## Verification

```bash
uv sync
.venv/bin/pytest -q
.venv/bin/ruff check app tests
bash -n scripts/start_chrome.sh
```

The service listens only on `127.0.0.1:8000`. Nginx reverse proxying, the domain, TLS certificate,
and firewall are managed separately by the owner.
