# Audit repair design

User approved implementation by replying “修复” to the completed audit. Binding audit:
`/Users/wangcaixian/Documents/ChatGPT/forexfactory/audit-2026-09-05/README.md`.

Retain SQLite, one FastAPI process, persistent browser, database jobs and file media storage. No queues, PostgreSQL migration, video downloading or publisher full-text crawling. Preserve installed iOS API compatibility through additive fields and existing link segments.

Fix video metadata capture and independent comment parsing; reject partial detail reconciliation. Distinguish source count mismatch from unfinished DOM collection, retaining both declared and visible counts without deleting comments on mismatch. Retry exhausted details through bounded low-frequency audits and audit recent zero-comment articles.

Validate calendar page completion against source page evidence, distinguish empty day from loading shell, persist source date, refresh a small lookback and support explicit historical repair. Record unavailable details with a reason/time to avoid tight retry loops. Backfill checkpoints record cutoff, coverage and reason; known IDs do not mean source exhaustion; completed runs reopen periodically and after downtime. Preserve data on suspect decreases and retain source snapshots for replay.

Use separate read connections for API responses with transaction snapshots; keep one serialized writer. Roll back BaseException/cancellation before releasing writes. Add translation leases and worker supervision. Status must reflect stale collection, active failures, source mismatches and coverage, excluding obsolete media failures.

Schema v8 additive contract (owned by persistence implementation):
- calendar_events.source_date TEXT nullable; CalendarObservation and CalendarDetailJob source_date: date | None = None.
- calendar_detail_jobs.unavailable_reason TEXT, last_checked_at TEXT. complete_calendar_detail_job accepts unavailable_reason: str | None and checked_at: datetime | None optional keywords.
- news_articles.comments_source_complete INTEGER NOT NULL DEFAULT 0; comments_visible_count INTEGER nullable.
- CommentCollectionObservation adds source_complete: bool = False and visible_count: int | None = None.
- NewsCommentCapture adds source_exhausted: bool = False (browser owner). It means verified expansion ended and DOM stabilized, not merely matching the expected count.
- NewsRepository.replace_detail optionally accepts desired_source_hash and returns bool, rejecting obsolete writes. Collector only completes complete detail captures; partial observations remain retryable.
- Database.read_connection(): async context manager yielding a read-only aiosqlite connection in a consistent read transaction, closed afterwards. Root wires API dependencies to repositories on this connection; workers keep writer repositories.
- Repository write guard protects cancellation; Repository read methods use an optional reader connection defaulting to a Database-managed read connection. NewsRepository can accept an optional reader connection; root passes it for API request scope. Write methods must always use the writer connection.

Maintain complete visible comment collection with stale declared count as partial content plus source_complete=true and a normally completed job; status exposes the count mismatch separately and periodic audit remains active. Do not deactivate stored comments until declared/parsed/DOM evidence agrees. Preserve the original declared number.

Verification: tests fail before fixes; source fixtures from authorized local audit evidence; migration of production copy; historical replay before live writes; fresh deployment backup, source validation, controlled repair, full suite and live APIs. Preserve production data and a rollback image. Daily local backups plus verified restore, with a documented off-host copy. No secret output.
