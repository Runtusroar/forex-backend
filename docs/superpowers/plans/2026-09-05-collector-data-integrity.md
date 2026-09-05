# Collector Data Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Calendar details and News comments complete, deduplicated, observable, and served entirely from local storage.

**Architecture:** Separate comment freshness from article-body freshness, and add durable jobs for News comments and Calendar details. Browser collectors perform the Forex Factory interactions required to expose complete DOM content, while repositories reconcile only verified-complete observations.

**Tech Stack:** Python 3.12, FastAPI, Playwright async API, selectolax, aiosqlite, pytest.

**Spec:** `docs/superpowers/specs/2026-09-05-collector-data-integrity-design.md`

## Global Constraints

- Keep SQLite schema version 7 and the existing single-process write lock.
- Calendar detail API paths must not perform source network access.
- Partial News comment fetches must never delete previously current comments.
- Latest Comments observations must never downgrade canonical detail data.
- Every production behavior is introduced through a failing test.

---

### Task 1: Live-shaped comment parsing and browser expansion

**Files:**
- Modify: `tests/fixtures/news_v2/listing_all_sections.html`
- Create: `tests/fixtures/news_v2/detail_many_comments.html`
- Modify: `tests/news/test_listing.py`
- Modify: `tests/news/test_detail.py`
- Modify: `tests/test_browser.py`
- Modify: `app/news/listing.py`
- Modify: `app/news/detail.py`
- Modify: `app/collector/browser.py`

**Interfaces:**
- Produces: `BrowserSession.news_detail_html(url: str, expected_comment_count: int | None = None) -> str`
- Produces: detail comments with `position`, `depth`, absolute `published_at`, and reaction count.

- [ ] **Step 1: Write failing tests for current Latest Comments classes and quality fields.** Assert `.news-block__commenter`, `.news-block__preview`, permalink, and article ID parse correctly.
- [ ] **Step 2: Run `../../.venv/bin/pytest -q tests/news/test_listing.py` and verify the new selector test fails with `Unknown` or empty text.**
- [ ] **Step 3: Implement current selectors with backward-compatible fallbacks and rerun the test to green.**
- [ ] **Step 4: Write failing detail-parser tests for source position, nesting depth, reaction count, and the absolute `title` inside a relative date element.**
- [ ] **Step 5: Run `../../.venv/bin/pytest -q tests/news/test_detail.py` and verify the new fields or values fail.**
- [ ] **Step 6: Extend `CommentObservation` and `parse_news_detail_v2`, then rerun the tests to green.**
- [ ] **Step 7: Write a failing fake-Playwright browser test where `Show All 184 Comments` must be clicked and the DOM count must stabilize before HTML capture.**
- [ ] **Step 8: Implement full-comment expansion with an expected-count check and rerun browser tests to green.**
- [ ] **Step 9: Commit with `fix: collect complete Forex Factory comments`.**

### Task 2: Schema v7 and quality-preserving comment storage

**Files:**
- Modify: `app/migrations.py`
- Modify: `app/news/models.py`
- Modify: `app/news/repository.py`
- Modify: `tests/news/test_migrations.py`
- Modify: `tests/news/test_repository.py`

**Interfaces:**
- Produces: `NewsRepository.enqueue_comment_job(...)`, `claim_comment_jobs(...)`, `complete_comment_job(...)`, and `fail_comment_job(...)`.
- Produces: `NewsRepository.replace_comments(article_id, observation, expected_count)` with complete-only reconciliation.

- [ ] **Step 1: Write a failing migration test asserting schema version 7 and all new columns/tables/indexes.**
- [ ] **Step 2: Run the migration test and verify it fails at version 6.**
- [ ] **Step 3: Add migration 7 and model fields, then rerun migration tests to green.**
- [ ] **Step 4: Write failing repository tests for exact source count decreases, ID deduplication, listing placeholders not overwriting detail data, and complete-only `is_current=0`.**
- [ ] **Step 5: Run the repository tests and verify each behavior fails for the intended reason.**
- [ ] **Step 6: Implement quality-aware upsert and complete reconciliation, then rerun repository tests to green.**
- [ ] **Step 7: Write failing job tests for count-change enqueue, Latest Comment enqueue, leases, priority, retry, and periodic audit eligibility.**
- [ ] **Step 8: Implement durable comment jobs and rerun job tests to green.**
- [ ] **Step 9: Commit with `feat: add durable comment collection state`.**

### Task 3: Dedicated comment worker and scheduled audits

**Files:**
- Create: `app/news/comments.py`
- Modify: `app/news/collector.py`
- Modify: `app/main.py`
- Modify: `app/config.py`
- Modify: `.env.example`
- Modify: `compose.yaml`
- Modify: `tests/news/test_collector.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: `CommentCollector.run_cycle(now: datetime | None = None) -> int` and `run(stop, interval)`.
- Consumes: Task 1 browser expansion and Task 2 comment jobs/reconciliation.

- [ ] **Step 1: Write failing collector tests for complete fetch, partial mismatch retry, and Latest Comment-triggered scheduling.**
- [ ] **Step 2: Run collector tests and verify `CommentCollector` is missing.**
- [ ] **Step 3: Implement the worker and remove comment persistence from the article-body worker.**
- [ ] **Step 4: Add configurable audit and loop intervals, wire the runtime task, and rerun collector/config tests to green.**
- [ ] **Step 5: Commit with `feat: schedule complete comment refreshes`.**

### Task 4: Calendar detail jobs and same-day batch prefetch

**Files:**
- Modify: `app/migrations.py`
- Modify: `app/domain.py`
- Modify: `app/repository.py`
- Modify: `app/collector/browser.py`
- Create: `app/collector/calendar_details.py`
- Modify: `app/collector/service.py`
- Modify: `app/main.py`
- Modify: `tests/test_repository.py`
- Modify: `tests/test_workers.py`
- Modify: `tests/test_api.py`

**Interfaces:**
- Produces: `BrowserSession.calendar_details_html(day: date, source_ids: Sequence[str]) -> dict[str, str]`.
- Produces: `CalendarDetailCollector.run_cycle(now: datetime | None = None) -> int`.

- [ ] **Step 1: Write failing repository tests for missing/changed Calendar job enqueue, date grouping, lease, retry, and completion.**
- [ ] **Step 2: Run the tests and verify the job API is missing.**
- [ ] **Step 3: Implement Calendar detail jobs and enqueue them during schedule replacement.**
- [ ] **Step 4: Write a failing browser test proving one date navigation expands and returns multiple event details.**
- [ ] **Step 5: Implement same-day detail expansion and rerun browser tests to green.**
- [ ] **Step 6: Write failing worker and API tests proving all horizon events are prefetched and API requests never call the browser.**
- [ ] **Step 7: Implement the worker, runtime wiring, refresh policy, and cache-only API; rerun tests to green.**
- [ ] **Step 8: Commit with `feat: prefetch calendar details in background`.**

### Task 5: API completeness, status, and end-to-end verification

**Files:**
- Modify: `app/news/api.py`
- Modify: `app/news/repository.py`
- Modify: `app/main.py`
- Modify: `tests/news/test_api.py`
- Modify: `tests/test_api.py`
- Modify: `README.md`

**Interfaces:**
- Produces: stored comment completeness fields in News detail responses and job health in status responses.

- [ ] **Step 1: Write failing API tests for observed count, current collected count, completeness state, stable comment order, and collector job status.**
- [ ] **Step 2: Run API tests and verify hard-coded `comments_complete: false` and missing status fields fail.**
- [ ] **Step 3: Implement repository projections and API serialization, then rerun API tests to green.**
- [ ] **Step 4: Document collection intervals, completeness semantics, and SQLite-to-PostgreSQL migration thresholds.**
- [ ] **Step 5: Run `../../.venv/bin/pytest -q`, `../../.venv/bin/ruff check app tests`, and `git diff --check`.**
- [ ] **Step 6: Run live POCs against one high-comment article and one eight-day Calendar window without writing source secrets to logs.**
- [ ] **Step 7: Commit with `docs: document collector integrity guarantees`.**

