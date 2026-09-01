# Forex Factory Lightweight MVP Design

Date: 2026-09-01
Status: Approved by the user's standing instruction to use the recommended option unless a product choice is required

## 1. Goal

Build two small, independently runnable projects for one personal iPhone:

1. `backend`: collect Forex Factory calendar and news data with the proven persistent-Chrome route, store it in SQLite, translate new English content through Kimi, and expose a read-only HTTPS-ready API.
2. `ios`: display the API as an English-first, Chinese-subtitle economic calendar and news app.

The previous PostgreSQL, pairing, APNs, notification rules, outbox, sync cursor, collector-upload topology, and deployment bundle are discarded. Nginx, the public domain, and TLS are owned by the user and are outside these projects.

## 2. Product Scope

The first release includes:

- current economic calendar rows with time, currency, impact, English and Chinese event names, Actual, Forecast, and Previous;
- Forex Factory news listing and readable details, with English text first and Chinese directly below;
- foreground refresh every 30 seconds and pull-to-refresh;
- a small settings screen for API base URL and API key;
- last-successful-response caching for viewing during a temporary network failure;
- English-only system/service messages.

The first release excludes:

- APNs, background remote notifications, and background fetch;
- Apple paid-developer features;
- user accounts, device pairing, multi-user preferences, and public distribution;
- PostgreSQL, Redis, Celery, Docker, Nginx, domain, TLS, analytics, and an admin panel;
- scraping external publisher pages linked by Forex Factory.

## 3. System Shape

```text
Normal Chrome process with dedicated persistent profile
                  |
             local CDP only
                  v
Playwright page control -> Python DOM adapters -> SQLite
                                              |       |
                                              |       +-> durable translation jobs
                                              |                    |
                                              v                    v
                                      FastAPI read API       Kimi API worker
                                              |
                                      local HTTP port only
                                              |
                                  user-managed Nginx + TLS
                                              |
                                        SwiftUI iPhone app
```

Chrome executes JavaScript and retains the browser session. Playwright attaches to the already-running Chrome over CDP and reads the rendered DOM. Python adapters turn that DOM into typed records. Playwright must not launch Chrome because the feasibility test showed different source behavior when it did.

## 4. Backend Runtime

### 4.1 Technology

- Python 3.12
- FastAPI and Uvicorn
- Playwright Python, connecting with `connect_over_cdp`
- SQLite in WAL mode through `aiosqlite`
- `httpx` for Kimi HTTP requests
- Pydantic for configuration and API schemas
- pytest for unit and API tests

One command starts the API plus two supervised asynchronous loops: collector and translator. The browser itself is started separately by an included script using a dedicated profile and loopback CDP port. The API binds to `127.0.0.1:8000` by default so Nginx can proxy it.

### 4.2 Collection

The normal interval is 30 seconds. Only one collection cycle may run at once. A cycle:

1. connects to or reuses the persistent Chrome CDP session;
2. loads/reloads the calendar and news pages;
3. parses normalized records;
4. commits English source data in one short SQLite transaction;
5. creates translation jobs only for new or changed English content;
6. records counts, duration, and the last error without deleting the last good data.

Calendar rows may omit visually repeated date/time cells; the adapter carries the last explicit values forward. Source IDs are the primary identities. Relative news labels are display metadata and do not participate in content hashes. News detail pages are fetched only for new or changed listing items with concurrency one.

Challenge/selector loss is reported as degraded health. The system preserves existing records and does not attempt CAPTCHA solving, fingerprint spoofing, or other access-control bypasses.

### 4.3 Storage

SQLite owns four tables:

- `calendar_events`: source identity, schedule, fields, English/Chinese title, source hash, timestamps;
- `news_items`: source identity, URL, source, published/first-seen time, English/Chinese title, summary and body, image URL, source hash, timestamps;
- `translation_jobs`: entity type/id, source hash, state, attempts, next-attempt time, and sanitized last error;
- `runtime_state`: schema version and collector/translator health timestamps.

Schema creation is idempotent at process startup. WAL and a busy timeout allow short concurrent reads and writes. There is no migration framework in the MVP; schema version mismatch fails startup with a clear message.

### 4.4 Kimi Translation

Use `k3-256k` through the Kimi Code membership endpoint by default, with low reasoning effort for short translation. Kimi Code routes thinking-disabled K3 requests to an older model, so reasoning remains enabled. `KIMI_BASE_URL` and `KIMI_MODEL` remain configurable so a different Kimi credential type or future model change requires no code edit.

English ingestion never waits for translation. After the source transaction commits, a separate worker claims durable pending jobs from SQLite. It translates a bounded batch into strict JSON, validates IDs and non-empty Chinese output, then updates translated fields only when the source hash still matches. A stale result can never overwrite newer English content.

Retry policy:

- timeout, HTTP 429, and HTTP 5xx: exponential delay of 1, 5, 30, 120, then 360 minutes;
- authentication/payment errors: leave jobs pending, expose degraded translator health, and retry after 30 minutes;
- malformed model output: retry up to five times, then mark the job failed until the English source changes or the operator invokes a retry command.

Missing or failed Chinese text is represented as `null`. It never removes or hides the English record. The API key is read only from `MOONSHOT_API_KEY`, is never sent to iOS, and is redacted from logs.

### 4.5 HTTP API

All `/api/v1/*` endpoints require `X-API-Key`, compared against `APP_API_KEY` with a timing-safe comparison. `/health` is unauthenticated and reveals only process/database state.

- `GET /health`
- `GET /api/v1/calendar?from=<ISO8601>&to=<ISO8601>`
- `GET /api/v1/news?limit=50&before=<ISO8601 optional>`
- `GET /api/v1/news/{source_id}`
- `GET /api/v1/status`

Responses are stable JSON with UTC ISO-8601 timestamps. List endpoints return `{items, generated_at}`. Translation fields are nullable. API errors never contain secrets, SQL, raw HTML, or Chrome profile paths.

## 5. iPhone App

### 5.1 Technology and Structure

- iOS 17+
- Swift 6 and SwiftUI
- URLSession and Codable only; no third-party runtime libraries
- XcodeGen project definition for reproducible project generation
- XCTest for decoding, client, refresh, and view-model behavior

The app has three tabs: Calendar, News, and Settings. It has no login or pairing flow. Settings stores the base URL in `UserDefaults` and the API key in Keychain.

### 5.2 Display

Calendar rows group by local date and show local time, currency, impact, event English title, optional smaller Chinese title, and aligned Actual/Forecast/Previous values. News cards and details show English title/body first and optional smaller Chinese text immediately below. Missing translations do not produce empty placeholder rows.

The app displays plain text and remote images. Arbitrary server HTML and Markdown are not rendered in this MVP.

### 5.3 Refresh and Cache

When the scene is active, each visible content view performs an immediate refresh and then refreshes every 30 seconds. Moving to the background cancels timers. Pull-to-refresh remains available. Requests do not overlap.

Each successful list response is atomically cached as JSON under Application Support. On launch or network failure, cached data is shown with a small stale indicator. API errors do not erase visible data.

## 6. Configuration

Backend environment:

- `DATABASE_PATH=./data/forex_factory.sqlite3`
- `CDP_URL=http://127.0.0.1:9222`
- `COLLECT_INTERVAL_SECONDS=30`
- `APP_API_KEY=<random secret>`
- `MOONSHOT_API_KEY=<Kimi server-side secret>`
- `KIMI_BASE_URL=https://api.kimi.com/coding/v1`
- `KIMI_MODEL=k3-256k`

No real secret is committed. `.env.example` contains names and safe defaults only.

## 7. Verification and Acceptance

Backend acceptance:

- fixture tests cover calendar grouping, event fields, ordinary news, X/Twitter-style news, stable hashes, and challenge/empty-page rejection;
- repository tests prove English is committed before translation and stale translations cannot overwrite changed content;
- Kimi tests cover valid JSON, malformed JSON, timeout/429/auth failures, and retry scheduling;
- API tests cover authentication, bilingual nullable fields, filtering, ordering, and status;
- a live manual smoke test parses two cycles at least 30 seconds apart through the dedicated Chrome profile and survives a Chrome restart with the same profile.

iOS acceptance:

- decoding tests cover translated and untranslated records;
- URL tests cover query construction and API-key headers;
- refresh tests prove immediate plus 30-second foreground refresh without overlap and cancellation in background;
- the app builds for an iOS simulator and unit tests pass;
- calendar and news screens display fixture content in English-first/Chinese-subtitle order;
- invalid API settings and offline cached state are understandable without crashing.

## 8. Operational Boundary

The repository supplies backend startup commands and the exact local listening port. The user supplies Nginx reverse proxying, DNS, TLS certificates, and server firewall configuration. Deployment must never expose the Chrome CDP port publicly.
