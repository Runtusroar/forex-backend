# Collector Audit Repair Implementation Plan

> For agentic workers: use superpowers:subagent-driven-development and dispatching-parallel-agents for independent file scopes. User authorized execution; continue through verification and deployment.

**Goal:** Correct observed source loss and transaction failures, repair historical data, and make operational status truthful.
**Architecture:** One process, one serialized SQLite writer, isolated read transactions, existing job workers and browser. Add only the metadata needed to represent completeness and retry decisions.
**Tech Stack:** Python 3.12, FastAPI, aiosqlite, Playwright, selectolax, pytest; SwiftUI API compatibility.
**Spec:** `docs/superpowers/specs/2026-09-05-audit-repair-design.md`

## Global constraints

- No production mutations until local tests and a fresh production backup pass.
- No database engine change, publisher capture, video binaries, or new infrastructure service.
- Preserve existing API fields. Use link segments and Watch Video action supported by current iOS.
- Tests live with implementation; never amend assertions merely to hide a failure.
- Independent owners edit only their assigned files; root integrates shared contracts.

## Task 1: News content and comment independence

Files: app/news/detail.py, comments.py, collector.py, models.py; tests/news/test_detail.py, test_collector.py and new test_comments_integrity.py.
Consumes NewsCommentCapture.source_exhausted; produces parse_news_comments(html,article_id,observed_at,source_timezone), additive CommentCollectionObservation metadata, link segments and safe partial detail states.

- [ ] Add failing real-video test from audit evidence: `assert segment.segment_type == 'link'; assert segment.external_action_label == 'Watch Video'`.
- [ ] Add missing-second-segment test: `assert not parse_news_detail_v2(partial,...).is_complete` and ensure collector does not mark partial jobs done.
- [ ] Add video/unsupported-body comment test using independent parser and real repository, asserting comment IDs and text are saved.
- [ ] Add mismatch test: expanded visible comments preserved; source_complete true; original declared count retained; no destructive reconcile; job finishes without tight retries.
- [ ] Implement minimal parsers and collector changes, test affected suite, self-review.

## Task 2: Browser, calendar and backfill correctness

Files: app/collector/*, app/parsers/calendar.py, app/domain.py, app/news/backfill.py; tests/test_workers.py, test_parsers.py, tests/news/test_browser.py and test_backfill.py.
Consumes new calendar repository completion keywords and source_date; produces stable validated browser captures, explicit backfill coverage metadata and rolling calendar lookback.

- [ ] Fail date-shell test with `pytest.raises(SourcePageError)`; preserve genuine verified empty days.
- [ ] Fail missing-event-vs-unavailable-detail test; only mark unavailable when the event exists and truly has no detail control.
- [ ] Fail delayed More test; wait for loading completion and stable DOM before source_exhausted/terminal.
- [ ] Fail known-overlap pagination test with two existing pages and unknown third page: `assert '3' in stored_ids`.
- [ ] Fail completed-checkpoint restart/next-day test; record explicit stop_reason and reached_cutoff instead of false coverage.
- [ ] Implement bounded lookback and source date handling; source captures remain independent from API reads.
- [ ] Run affected suite and self-review; root handles production historical replay.

## Task 3: Persistence, leases, supervision

Files: app/db.py, app/repository.py, app/news/repository.py, app/migrations.py, app/runtime.py and optional app/transactions.py; their corresponding tests.
Produces schema v8 and spec-defined additive repository contracts; root owns API/main wiring.

- [ ] Fail isolation/cancellation test adapted from audit: separate read must not see pending row; cancelled row must remain absent after an unrelated commit.
- [ ] Fail stale-detail-version test: old claimed hash cannot overwrite a newer source version.
- [ ] Fail exhausted-detail retry and zero-comment audit tests; bounded periodic requeue.
- [ ] Fail unavailable-detail hot-loop test; completed unavailable check must wait until scheduled refresh.
- [ ] Fail legacy translation duplicate claim test and unexpected worker exit recovery test.
- [ ] Implement migration and transaction scope, job policies and supervision; run old/new migrations and repository/runtime suite.

## Task 4: Integration, source snapshots, maintenance and status

Files root owns: app/main.py, app/news/api.py, app/config.py, app/news/snapshots.py, scripts/*, compose.yaml, README.md, tests/test_api.py, test_config.py, test_compose.py, tests/news/test_api.py, test_snapshots.py and tests/test_maintenance.py.

- [ ] Fail status test with failed/current jobs and stale timestamp: `assert body['status']=='degraded'`.
- [ ] Wire API request transactions through read_connection without starting request-time collection.
- [ ] Fail semantic snapshot hash test for unchanged source plus dynamic scripts; retain errors and calendar/comment replay evidence.
- [ ] Add bounded maintenance CLI with explicit date interval, safe default preview, summary and backup restore tests. Preserve original values and metadata before history repair.
- [ ] Add consistent SQLite+media backup command and daily scheduling instructions; verify restored database and required media.
- [ ] Full suite, lint, migration on snapshot, real source replay, independent code review, resolve findings.
- [ ] Back up deployed database+media, preserve old image, deploy verified commit, repair retained historical dates, validate IDs/time/counts and APIs. Verify jobs drain and backups restore.

## Progress ledger

Ruling: The audit recommendation followed by “修复” authorizes these bounded fixes and deployment; no repeated design confirmation is needed. Historical deletion is avoided unless source completion is independently validated, and all repair has a rollback backup.

| Interface / scope | Check |
|---|---|
| News ↔ browser | source_exhausted default false preserves old callers; mismatch is not deletion authorization |
| Calendar ↔ persistence | source_date optional and complete_calendar_detail_job optional keywords keep old tests compatible |
| News ↔ persistence | additive collection metadata and desired_source_hash; job completion only after stored true |
| Persistence ↔ root | API gets request-scoped reader while worker repos retain writer |
| Task 1 | Real fixtures, comments independent, current client supports link action |
| Task 2 | Shell rejection must coexist with genuine no-event days; terminal doesn't prove 30-day coverage |
| Task 3 | No network awaits in write critical section; cancellation rollback before release |
| Task 4 | Status and backup describe source completeness truthfully; production mutation follows local proof |
