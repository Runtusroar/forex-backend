# Collector Data Integrity Design

## Goal

Make Calendar details and News comments complete, deduplicated, observable, and available from local storage without request-time scraping.

## Confirmed Failures

- News detail HTML is captured before Forex Factory's `Show All N Comments` action, so a page declaring 184 comments stores only the default 64 while being marked complete.
- Listing comment counts can trigger a detail refresh, but articles outside story panels are not reliably refreshed when a new Latest Comment appears.
- Latest Comments uses `.news-block__commenter` and `.news-block__preview`; the stale parser produces `Unknown` and empty text and overwrites richer detail rows.
- Comment count is persisted with `max()`, so source-side deletion cannot be represented.
- Comments have no current/deleted marker, source order, completeness state, or dedicated collection job.
- Calendar details are fetched during API requests. Only 12 of the current 78 events have cached details.
- Calendar detail pages can be expanded in a single page per day; opening every detail as a separate page is unnecessary.

## News Collection

Article body collection and comment collection use separate jobs. Listing observations store the current source comment count exactly. A comment job is enqueued when an article is new, the observed count changes, a Latest Comment references the article, a previous fetch is partial, or the article is due for an audit.

The browser opens the article, waits for the comments area, clicks `Show All N Comments` when present, then waits until the button disappears and the number of comment nodes stabilizes. A result is complete only when the stable DOM count equals the latest declared count. A source race may produce a partial result; partial results are stored without deactivating previously current comments and are retried.

A complete result is reconciled atomically. Parsed comment IDs are upserted and marked current; comments absent from the complete source result are marked inactive. Comment identity provides deduplication. Source position and nesting depth preserve Forex Factory thread order.

Latest Comments is a feed observation, not a canonical comment replacement. It may insert a placeholder for a new comment, but must never replace non-empty author, text, time, parent, or reaction data collected from a detail page.

Recent active articles are audited periodically even when the listing count is no longer visible. Latest Comment activity schedules a high-priority audit. Backoff limits repeated source failures.

## Calendar Collection

The Calendar API becomes cache-only. Schedule collection creates detail jobs for new, missing, changed, or stale events. A background worker claims jobs grouped by source date, opens that date once, expands all requested event details, parses each expanded row, and stores each detail independently.

The default policy prefetches every event in the configured eight-day horizon. Static future details refresh daily. Events near release refresh more frequently so actual, previous, history, and related stories converge after publication. Failed jobs retain cached data and retry with backoff.

## Schema Version 7

- Add `comments_state`, `comments_checked_at`, and `comments_completed_at` to `news_articles`; stop applying `max()` to `comment_count`.
- Add `position`, `depth`, `is_current`, and `observation_quality` to `news_comments`.
- Add `news_comment_jobs` with expected count, state, priority, attempts, lease, retry time, and error.
- Add `calendar_detail_jobs` with event hash, state, priority, attempts, lease, retry time, and error.
- Add `source_hash` and `last_success_at` to `calendar_event_details`.
- Retain SQLite, WAL, foreign keys, and the shared write lock. PostgreSQL is deferred until collectors run as multiple processes or market tick history creates materially concurrent writes.

## API Contract

- Calendar detail requests never access Forex Factory and return cached data or `404` while a background job is pending.
- News detail reports observed, collected-current, and completeness state from storage.
- Comment pagination uses current comments in source thread order.
- Status exposes pending/failed Calendar detail and News comment job counts plus last successful collection times.

## Verification

- Live-shaped fixtures include `Show All`, current Latest Comments selectors, relative time with an absolute `title`, nested replies, deletion reconciliation, and count mismatch.
- Browser tests prove full-comment expansion and same-day Calendar detail batching.
- Repository migration and job tests prove exact counts, deduplication, quality-preserving upserts, complete-only deactivation, leases, and retries.
- API tests prove no request-time scraping.

