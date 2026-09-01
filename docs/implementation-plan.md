# Forex Factory Lightweight Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one Python service that collects Forex Factory through persistent Chrome, stores English data in SQLite immediately, translates asynchronously with Kimi, and exposes a small authenticated read API.

**Architecture:** Uvicorn runs FastAPI while application-lifespan tasks supervise a single collector loop and a durable SQLite-backed translation worker. Playwright connects to an externally launched Chrome over loopback CDP; parsers emit typed observations; repository transactions upsert source content and enqueue hash-bound translations.

**Tech Stack:** Python 3.12, FastAPI, Uvicorn, Playwright, aiosqlite, httpx, Pydantic Settings, pytest, Ruff

**Spec:** `docs/superpowers/specs/2026-09-01-forex-factory-lightweight-mvp-design.md`

## Global Constraints

- The backend is an MVP and must not include PostgreSQL, Redis, Celery, APNs, pairing, notification rules, an upload collector, or Docker.
- Chrome is launched outside Playwright with a dedicated persistent profile; Playwright only calls `connect_over_cdp`.
- English source data commits before Kimi is called; failed translation never rolls back or hides English.
- Use `kimi-k2.6` by default and send `thinking: {"type": "disabled"}`.
- Bind the API to `127.0.0.1:8000` by default; Nginx, DNS, and TLS are outside the project.
- Never commit `.env`, API keys, cookies, Chrome profiles, raw production HTML, or screenshots.
- Every implementation step follows red-green-refactor and every task ends with focused tests.

---

### Task 1: Replace the legacy backend with the MVP package and configuration

**Files:**
- Preserve unchanged: `backend/.env`
- Replace: `backend/pyproject.toml`
- Create: `backend/.env.example`
- Create: `backend/.gitignore`
- Create: `backend/README.md`
- Create: `backend/app/__init__.py`
- Create: `backend/app/config.py`
- Test: `backend/tests/test_config.py`

**Interfaces:**
- Produces: `Settings` with `database_path`, `cdp_url`, `collect_interval_seconds`, `app_api_key`, `moonshot_api_key`, `kimi_base_url`, and `kimi_model`.
- Produces: `get_settings() -> Settings` cached for production and directly instantiable in tests.

- [ ] **Step 1: Preserve `backend/.env`, remove the legacy backend directory, recreate it, and restore `.env` with owner-only permissions**

Verify first that the backup exists and is non-empty without printing it. The cleanup removes the old PostgreSQL, APNs, pairing, sync, Docker, Alembic, and worker implementation.

- [ ] **Step 2: Write the failing configuration tests**

```python
def test_settings_have_mvp_defaults(tmp_path):
    settings = Settings(
        _env_file=None,
        DATABASE_PATH=tmp_path / "db.sqlite3",
        APP_API_KEY="test-api-key",
        MOONSHOT_API_KEY="test-kimi-key",
    )
    assert settings.cdp_url == "http://127.0.0.1:9222"
    assert settings.collect_interval_seconds == 30
    assert settings.kimi_model == "kimi-k2.6"


def test_interval_must_be_positive(tmp_path):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            DATABASE_PATH=tmp_path / "db.sqlite3",
            APP_API_KEY="x",
            MOONSHOT_API_KEY="y",
            COLLECT_INTERVAL_SECONDS=0,
        )
```

- [ ] **Step 3: Run `uv run pytest tests/test_config.py -q` and verify it fails because `app.config` is absent**

- [ ] **Step 4: Add the minimal package and settings implementation**

Use `pydantic-settings` with `env_file=".env"`, `extra="ignore"`, positive interval validation, and `SecretStr` for both keys. `.env.example` contains fake values only. Runtime dependencies are exactly FastAPI, Uvicorn, Playwright, aiosqlite, httpx, and pydantic-settings; development dependencies are pytest, pytest-asyncio, respx, and Ruff.

- [ ] **Step 5: Run `uv sync`, then `uv run pytest tests/test_config.py -q` and `uv run ruff check .`**

- [ ] **Step 6: Commit `chore: reset backend to lightweight MVP`**

---

### Task 2: Add typed observations and the durable SQLite repository

**Files:**
- Create: `backend/app/domain.py`
- Create: `backend/app/db.py`
- Create: `backend/app/repository.py`
- Test: `backend/tests/test_repository.py`

**Interfaces:**
- Produces: `CalendarObservation`, `NewsObservation`, `CalendarRecord`, `NewsRecord`, and `TranslationJob` dataclasses.
- Produces: `Database.open()`, `Database.close()`, and `Database.initialize()`.
- Produces: `Repository.upsert_calendar(items)`, `upsert_news(items)`, `claim_translation_jobs(limit)`, `complete_translation(job, translated)`, `fail_translation(job, error, next_attempt_at)`, `list_calendar(start, end)`, `list_news(limit, before)`, and `get_news(source_id)`.

- [ ] **Step 1: Write failing repository tests for source-first commits and enqueue-on-change**

```python
async def test_upsert_commits_english_and_enqueues_translation(repository):
    item = CalendarObservation(
        source_id="142001",
        event_at=datetime(2026, 9, 1, 12, 30, tzinfo=UTC),
        currency="USD",
        impact="high",
        title_en="ISM Manufacturing PMI",
        actual="51.2",
        forecast="50.5",
        previous="49.8",
    )
    await repository.upsert_calendar([item])
    stored = (await repository.list_calendar(item.event_at, item.event_at))[0]
    jobs = await repository.claim_translation_jobs(limit=10)
    assert stored.title_en == "ISM Manufacturing PMI"
    assert stored.title_zh is None
    assert jobs[0].source_hash == stored.source_hash


async def test_unchanged_content_does_not_duplicate_job(repository, calendar_item):
    await repository.upsert_calendar([calendar_item])
    await repository.upsert_calendar([calendar_item])
    assert await repository.translation_job_count() == 1
```

- [ ] **Step 2: Run `uv run pytest tests/test_repository.py -q` and verify missing domain/database imports fail**

- [ ] **Step 3: Implement the schema and transactional upserts**

Enable `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, and a 5-second busy timeout. Use SHA-256 over normalized translatable English fields. Use uniqueness `(entity_type, entity_id, source_hash)` for jobs and never include relative news-age labels in a hash.

- [ ] **Step 4: Add and run a stale-translation test**

```python
async def test_stale_translation_cannot_overwrite_changed_source(repository, news_item):
    await repository.upsert_news([news_item])
    old_job = (await repository.claim_translation_jobs(1))[0]
    await repository.upsert_news([replace(news_item, title_en="Updated title")])
    applied = await repository.complete_translation(
        old_job,
        {"title_zh": "旧标题", "summary_zh": None, "body_zh": None},
    )
    assert applied is False
    assert (await repository.get_news(news_item.source_id)).title_zh is None
```

- [ ] **Step 5: Run `uv run pytest tests/test_repository.py -q` and `uv run ruff check app tests`**

- [ ] **Step 6: Commit `feat: add SQLite content and translation queue`**

---

### Task 3: Implement fixture-tested Forex Factory DOM parsers

**Files:**
- Create: `backend/app/parsers/__init__.py`
- Create: `backend/app/parsers/calendar.py`
- Create: `backend/app/parsers/news.py`
- Create: `backend/app/parsers/errors.py`
- Create: `backend/tests/fixtures/calendar.html`
- Create: `backend/tests/fixtures/news.html`
- Create: `backend/tests/fixtures/news_article.html`
- Create: `backend/tests/fixtures/news_social.html`
- Create: `backend/tests/fixtures/challenge.html`
- Test: `backend/tests/test_calendar_parser.py`
- Test: `backend/tests/test_news_parser.py`

**Interfaces:**
- Produces: `parse_calendar(html: str, now: datetime) -> list[CalendarObservation]`.
- Produces: `parse_news_listing(html: str, now: datetime) -> list[NewsObservation]`.
- Produces: `parse_news_detail(html: str) -> NewsDetail`.
- Produces: `SourcePageError` and `ChallengePageError`.

- [ ] **Step 1: Write failing calendar tests covering carried date/time and all value columns**

```python
def test_calendar_carries_grouped_date_and_time(calendar_html):
    rows = parse_calendar(calendar_html, datetime(2026, 9, 1, tzinfo=UTC))
    first, second = rows
    assert first.event_at == datetime(2026, 9, 1, 12, 30, tzinfo=UTC)
    assert second.event_at == datetime(2026, 9, 1, 12, 30, tzinfo=UTC)
    assert second.currency == "USD"
    assert (second.actual, second.forecast, second.previous) == ("51.2", "50.5", "49.8")
```

- [ ] **Step 2: Run the calendar parser tests and verify they fail**

- [ ] **Step 3: Implement calendar parsing with explicit carry-forward state and strict identity validation**

Use standard-library HTML parsing or `selectolax`, normalize whitespace, map impact CSS classes to `low|medium|high|holiday`, and reject an empty page or rows without source IDs. Store parsed timestamps as aware UTC values.

- [ ] **Step 4: Write failing news tests for ordinary and X/Twitter-style content**

```python
def test_listing_excludes_relative_age_from_source_content(news_html, fixed_now):
    a = parse_news_listing(news_html.replace("5 min ago", "6 min ago"), fixed_now)[0]
    b = parse_news_listing(news_html, fixed_now)[0]
    assert a.translatable_hash == b.translatable_hash


def test_social_detail_uses_post_text(news_social_html):
    detail = parse_news_detail(news_social_html)
    assert detail.kind == "social"
    assert detail.body_en == "US manufacturing activity expanded..."
```

- [ ] **Step 5: Implement news listing/detail adapters and challenge detection**

Select the unique Forex Factory article container, branch ordinary article versus social preview, normalize source URLs, preserve first-seen time when no absolute publication time exists, and reject known challenge titles or total selector loss.

- [ ] **Step 6: Run `uv run pytest tests/test_calendar_parser.py tests/test_news_parser.py -q` and Ruff**

- [ ] **Step 7: Commit `feat: parse calendar and news DOM`**

---

### Task 4: Connect Playwright to persistent Chrome and schedule collection

**Files:**
- Create: `backend/app/collector/__init__.py`
- Create: `backend/app/collector/browser.py`
- Create: `backend/app/collector/service.py`
- Create: `backend/scripts/start_chrome.sh`
- Test: `backend/tests/test_collector.py`

**Interfaces:**
- Produces: `BrowserSession.connect(cdp_url)`, `calendar_html()`, `news_html()`, `news_detail_html(url)`, and `close()`.
- Produces: `Collector.run_cycle() -> CollectionResult` and `Collector.run(stop_event)`.
- Consumes: Task 2 `Repository` and Task 3 parser functions.

- [ ] **Step 1: Write failing tests using a fake browser session**

```python
async def test_cycle_commits_calendar_even_when_news_detail_fails(repository, fake_browser):
    fake_browser.news_detail_error = TimeoutError()
    result = await Collector(fake_browser, repository).run_cycle()
    assert result.calendar_count == 2
    assert len(await repository.list_calendar(DAY_START, DAY_END)) == 2
    assert result.degraded is True


async def test_collector_never_overlaps_cycles(repository, fake_browser):
    collector = Collector(fake_browser, repository)
    await asyncio.gather(collector.run_cycle(), collector.run_cycle())
    assert fake_browser.maximum_simultaneous_cycles == 1
```

- [ ] **Step 2: Run `uv run pytest tests/test_collector.py -q` and verify missing collector classes fail**

- [ ] **Step 3: Implement `BrowserSession` with `async_playwright().chromium.connect_over_cdp()`**

Reuse a bounded set of pages, use explicit DOM-content readiness plus expected-selector waits, and place timeouts around navigation. Never call `chromium.launch` or expose the CDP URL through the public API.

- [ ] **Step 4: Implement the locked collector cycle and 30-second monotonic schedule**

Commit successful page types independently so a news detail failure cannot discard calendar data. Record sanitized last-success and last-error state. Stop cleanly when the application stop event is set.

- [ ] **Step 5: Add `start_chrome.sh`**

The script requires explicit `CHROME_BINARY` and `CHROME_PROFILE_DIR`, validates the profile path is not empty or `/`, creates it with owner-only permissions, and launches Chrome with loopback remote debugging on port 9222. It prints no cookies or profile contents.

- [ ] **Step 6: Run collector tests and `bash -n scripts/start_chrome.sh`**

- [ ] **Step 7: Commit `feat: collect through persistent Chrome CDP`**

---

### Task 5: Add the asynchronous Kimi translation worker

**Files:**
- Create: `backend/app/translation/__init__.py`
- Create: `backend/app/translation/kimi.py`
- Create: `backend/app/translation/worker.py`
- Test: `backend/tests/test_kimi.py`
- Test: `backend/tests/test_translation_worker.py`

**Interfaces:**
- Produces: `KimiTranslator.translate(jobs: Sequence[TranslationJob]) -> dict[int, dict[str, str | None]]`.
- Produces: `TranslationWorker.run_once() -> TranslationRunResult` and `run(stop_event)`.
- Consumes: Task 2 job claim/complete/fail operations.

- [ ] **Step 1: Write a failing request-shape test with `respx`**

```python
async def test_kimi_uses_k26_without_thinking(settings, job, respx_mock):
    route = respx_mock.post("https://api.moonshot.ai/v1/chat/completions").mock(
        return_value=Response(200, json=VALID_TRANSLATION_RESPONSE)
    )
    await KimiTranslator(settings).translate([job])
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "kimi-k2.6"
    assert body["thinking"] == {"type": "disabled"}
    assert body["response_format"]["type"] == "json_schema"
```

- [ ] **Step 2: Run Kimi tests and verify failure**

- [ ] **Step 3: Implement strict batched translation**

Send at most ten jobs per request. The system message requires faithful Simplified Chinese financial translation, preservation of numbers/currency abbreviations, no commentary, and one result per input ID. Validate JSON, exact ID set, allowed fields, and non-empty strings before returning.

- [ ] **Step 4: Write failing worker retry and source-isolation tests**

```python
async def test_rate_limit_schedules_retry_without_changing_english(repository, translator_429):
    result = await TranslationWorker(repository, translator_429).run_once()
    assert result.failed == 1
    assert (await repository.get_news("9001")).title_en == "Dollar rises"
    assert (await repository.get_news("9001")).title_zh is None
    assert (await repository.peek_job()).next_attempt_at > datetime.now(UTC)
```

- [ ] **Step 5: Implement retry classification and stale-hash completion guards**

Use the exact retry schedule in the spec. Sanitize stored errors to status code and error class only. Authentication/payment errors update translator health and retain jobs. Invalid output fails after five attempts. A new source hash creates a fresh pending job.

- [ ] **Step 6: Run translation tests and Ruff**

- [ ] **Step 7: Commit `feat: translate asynchronously with Kimi`**

---

### Task 6: Expose the authenticated read API and supervise background loops

**Files:**
- Create: `backend/app/api.py`
- Create: `backend/app/schemas.py`
- Create: `backend/app/main.py`
- Test: `backend/tests/test_api.py`
- Test: `backend/tests/test_lifespan.py`

**Interfaces:**
- Produces: `create_app(settings: Settings | None = None) -> FastAPI`.
- Produces the five HTTP endpoints defined in the spec.
- Consumes: Task 2 repository, Task 4 collector, and Task 5 translation worker.

- [ ] **Step 1: Write failing authentication and bilingual response tests**

```python
async def test_calendar_requires_api_key(client):
    assert (await client.get("/api/v1/calendar")).status_code == 401


async def test_calendar_allows_missing_translation(auth_client, seeded_repository):
    response = await auth_client.get(
        "/api/v1/calendar?from=2026-09-01T00:00:00Z&to=2026-09-02T00:00:00Z"
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["title_en"] == "ISM Manufacturing PMI"
    assert response.json()["items"][0]["title_zh"] is None
```

- [ ] **Step 2: Run API tests and verify missing app factory failure**

- [ ] **Step 3: Implement Pydantic response schemas and API-key dependency**

Use `hmac.compare_digest` and return a generic 401. Validate date ranges, cap news limits at 100, order calendar ascending and news descending, and emit UTC timestamps with `Z`.

- [ ] **Step 4: Implement the lifespan supervisor**

Initialize the database before serving, start collector and translator tasks, cancel and await them during shutdown, then close browser and database resources. A crashed background loop records degraded health and is restarted with bounded delay rather than terminating the API.

- [ ] **Step 5: Run `uv run pytest -q`, Ruff, and a local API smoke test with collector disabled**

- [ ] **Step 6: Commit `feat: expose lightweight calendar and news API`**

---

### Task 7: Add operator commands and end-to-end verification documentation

**Files:**
- Create: `backend/app/cli.py`
- Modify: `backend/README.md`
- Create: `backend/tests/test_cli.py`

**Interfaces:**
- Produces: `python -m app.cli init-db`, `retry-translations`, `collect-once`, and `status`.

- [ ] **Step 1: Write failing CLI tests for safe status and retry behavior**

```python
def test_status_never_prints_secrets(cli_runner, configured_env):
    result = cli_runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert configured_env["MOONSHOT_API_KEY"] not in result.output


def test_retry_translations_only_requeues_failed_jobs(cli_runner, repository):
    result = cli_runner.invoke(cli, ["retry-translations"])
    assert result.exit_code == 0
    assert "requeued=1" in result.output
```

- [ ] **Step 2: Implement commands using the same settings and repository code**

`collect-once` exits nonzero on challenge/empty parse while preserving prior data. `status` prints only counts and timestamps. `retry-translations` changes failed jobs to pending without modifying source records.

- [ ] **Step 3: Document exact development and production-local commands**

Include environment setup, `uv sync`, `uv run playwright install chromium` only for Playwright protocol support, external Chrome startup, profile initialization, `uv run uvicorn app.main:app --host 127.0.0.1 --port 8000`, tests, API examples, and the explicit statement that Nginx/TLS are user-managed.

- [ ] **Step 4: Run final verification**

Run `uv run pytest -q`, `uv run ruff check .`, `bash -n scripts/start_chrome.sh`, and `git diff --check`. Then run two live collection cycles at least 30 seconds apart if the dedicated Chrome profile is available; record counts and verify a browser restart reuses the same profile.

- [ ] **Step 5: Commit `docs: add backend operation guide`**
