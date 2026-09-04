# Calendar Source Accuracy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the calendar API reproduce Forex Factory's UTC+8 daily calendar accurately without accepting lazy-loaded weekly skeletons or retaining stale events.

**Architecture:** Fetch explicit Forex Factory day pages instead of the lazy weekly page. Parse their wall-clock values with a configured `Asia/Singapore` source timezone, transactionally replace only validated day windows, refresh today every 30 seconds, and refresh the eight-day schedule every ten minutes. Expose the last successful collection time and last error without changing the existing calendar item schema.

**Tech Stack:** Python 3.12, Playwright, Selectolax, FastAPI, SQLite/aiosqlite, pytest, Swift/SwiftUI/XCTest

**Spec:** `docs/design.md` plus the user-approved calendar repair requirements from 2026-09-04

## Global Constraints

- Keep the backend and iOS app as separate repositories.
- Preserve English-first bilingual calendar titles and existing translations when the English title is unchanged.
- Store timestamps in UTC and display/query calendar days in UTC+8.
- A failed or incomplete source page must leave the last complete database snapshot intact.
- Keep the MVP lightweight: no new service or external calendar dependency.

---

### Task 1: Source-timezone and complete daily-page parsing

**Files:**
- Modify: `tests/test_parsers.py`
- Modify: `app/parsers/calendar.py`

**Interfaces:**
- Consumes: Forex Factory daily HTML and an explicit source `tzinfo`.
- Produces: `parse_calendar(html, now, source_timezone, expected_date)` which returns events for the requested day, permits a validated empty day, and rejects an incomplete/wrong-day page.

- [x] **Step 1: Write failing parser tests**

  Add literal UTC expectations proving `4:00pm` in UTC+8 becomes `08:00Z`, a matching empty daily page returns `[]`, and a page without the requested day marker raises `SourcePageError`.

- [x] **Step 2: Run the focused parser tests and verify RED**

  Run `pytest -q tests/test_parsers.py` and confirm the new `expected_date` calls fail because that contract is not implemented.

- [x] **Step 3: Implement the parser contract**

  Track day-breaker date text, validate it against the literal requested date, retain the existing challenge rejection, and only permit an empty result after the requested day is proven present.

- [x] **Step 4: Run the parser tests and verify GREEN**

  Run `pytest -q tests/test_parsers.py` and confirm all parser cases pass.

### Task 2: Daily collection and transactional snapshots

**Files:**
- Modify: `tests/test_workers.py`
- Modify: `tests/test_repository.py`
- Modify: `app/collector/browser.py`
- Modify: `app/collector/service.py`
- Modify: `app/repository.py`

**Interfaces:**
- Consumes: `BrowserSource.calendar_html(day: date) -> str` and configured source timezone.
- Produces: `Repository.replace_calendar_window(items, start, end)` plus a collector that refreshes the current UTC+8 day every cycle and the current eight-day horizon every ten minutes.

- [x] **Step 1: Write failing collector and repository tests**

  Assert the browser is asked for explicit dates, the UTC+8 source time is stored correctly, stale rows inside a validated window are removed, rows outside it remain, and a failed day fetch makes no partial replacement.

- [x] **Step 2: Run focused tests and verify RED**

  Run `pytest -q tests/test_workers.py tests/test_repository.py` and confirm failures name the missing daily-source and snapshot behavior.

- [x] **Step 3: Implement minimal daily collection**

  Format day URLs as `calendar?day=sep4.2026`, wait for a real event or day-breaker row, parse every requested page before writing, deduplicate by source ID, and replace the validated UTC window in one transaction.

- [x] **Step 4: Add collection freshness state**

  Persist `calendar_last_success`, `calendar_last_count`, and `calendar_last_error`; record exceptions in the background loop while keeping the previous database snapshot.

- [x] **Step 5: Run focused tests and verify GREEN**

  Run `pytest -q tests/test_workers.py tests/test_repository.py` and confirm all cases pass.

### Task 3: Configuration and API freshness

**Files:**
- Modify: `tests/test_config.py`
- Modify: `tests/test_compose.py`
- Modify: `tests/test_api.py`
- Modify: `app/config.py`
- Modify: `app/main.py`
- Modify: `.env.example`
- Modify: `compose.yaml`

**Interfaces:**
- Consumes: `CALENDAR_SOURCE_TIMEZONE=Asia/Singapore`, `CALENDAR_HORIZON_DAYS=8`, and `CALENDAR_SCHEDULE_INTERVAL_SECONDS=600`.
- Produces: correctly configured collector, calendar `generated_at` derived from the last successful snapshot when present, and status fields describing calendar freshness.

- [x] **Step 1: Write failing config/API contract tests**

  Assert production defaults, Compose propagation, UTC+8 default API range, and a status response containing the persisted calendar freshness values.

- [x] **Step 2: Run focused tests and verify RED**

  Run `pytest -q tests/test_config.py tests/test_compose.py tests/test_api.py` and confirm failures are caused by the absent configuration and freshness contract.

- [x] **Step 3: Wire configuration and API behavior**

  Construct the collector with `ZoneInfo(settings.calendar_source_timezone)`, use UTC+8 midnight for the default calendar range, and return persisted freshness state while remaining backward compatible with existing clients.

- [x] **Step 4: Run focused tests and verify GREEN**

  Run `pytest -q tests/test_config.py tests/test_compose.py tests/test_api.py` and confirm all cases pass.

### Task 4: Fixed UTC+8 client query window

**Files:**
- Modify: `ForexFactoryMVPTests/ViewModelTests.swift` in the iOS repository
- Modify: `ForexFactoryMVP/Calendar/CalendarViewModel.swift` in the iOS repository

**Interfaces:**
- Consumes: device-independent UTC+8 calendar boundaries.
- Produces: API requests whose `from` and `to` values always cover eight UTC+8 calendar days.

- [x] **Step 1: Write a failing XCTest**

  Capture the requested range in the stub API and assert the literal UTC instants for UTC+8 midnight and the eight-day exclusive end.

- [x] **Step 2: Run the focused XCTest and verify RED**

  Run the project test target for `ViewModelTests` and confirm the request currently follows the device calendar instead of the fixed UTC+8 calendar.

- [x] **Step 3: Implement fixed UTC+8 boundaries**

  Build a Gregorian calendar with `TimeZone(secondsFromGMT: 8 * 3600)` and use it for the request range.

- [x] **Step 4: Run iOS tests and verify GREEN**

  Run the full iOS test target and confirm the calendar and editorial time tests pass.

### Task 5: Verification and deployment

**Files:**
- Modify: `README.md` only if the documented environment variables or deployment commands are incomplete.

**Interfaces:**
- Consumes: passing backend/iOS changes.
- Produces: pushed branches, updated local-server containers, repaired live calendar data, and an updated app installed on the paired iPhone 15 Pro.

- [x] **Step 1: Run backend verification**

  Run the full pytest suite, Ruff, Compose config validation, and `git diff --check`.

- [x] **Step 2: Run iOS verification**

  Build/test for the paired development environment and run `git diff --check`.

- [ ] **Step 3: Commit and push both repositories**

  Commit focused changes on `codex/news-v2` and `codex/editorial-ui`, then push both existing branches.

- [ ] **Step 4: Back up and deploy the backend**

  Back up the SQLite volume, update `/srv/forex-backend`, rebuild/restart Compose, and wait for healthy containers.

- [ ] **Step 5: Validate live source parity**

  Compare representative event IDs, UTC+8 times, actual/forecast/previous values, collection freshness, and API results against the corresponding Forex Factory daily pages.

- [ ] **Step 6: Install the iOS app**

  Build for device `00008130-000E69241412001C`, install it on the paired iPhone 15 Pro, and launch it.
