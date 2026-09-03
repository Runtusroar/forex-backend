# Full Story Acquisition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve Forex Factory full-story links as structured data and asynchronously collect safe, provenance-aware publisher documents.

**Architecture:** Forex Factory excerpts, semantic links, and publisher documents live in separate tables. A bounded source worker fetches public HTTP(S) documents, snapshots raw HTML, extracts readable ordered text, and queues hash-bound translation without blocking the existing collector.

**Tech Stack:** Python 3.12, FastAPI, SQLite/aiosqlite, httpx, selectolax, pytest, Docker Compose

**Spec:** `docs/superpowers/specs/2026-09-03-full-story-acquisition-design.md`

## Global Constraints

- Never overwrite Forex Factory listing or detail content with publisher content.
- Never bypass a login, paywall, CAPTCHA, or publisher block.
- Validate every redirect destination against SSRF before requesting it.
- Store English and provenance before asynchronous Chinese translation.
- Preserve the current API key, hostname, database volume, and media volume.

---

### Task 1: Semantic Full-Story Links

**Files:**
- Modify: `app/news/models.py`
- Modify: `app/news/detail.py`
- Modify: `tests/fixtures/news_v2/detail_excerpt.html`
- Modify: `tests/news/test_detail.py`

**Interfaces:**
- Produces: `SegmentLinkObservation(stable_key, segment_key, position, kind, label, url)` and `DetailObservation.links`.

- [ ] Add a fixture with a full-story anchor inside the final paragraph and a second segment link.
- [ ] Add tests asserting link order/URL and that neither `(full story)` nor surrounding ellipsis remains in segment prose.
- [ ] Run `./.venv/bin/pytest -q tests/news/test_detail.py` and confirm the new tests fail because links are flattened.
- [ ] Implement anchor removal before text extraction and emit normalized absolute link observations.
- [ ] Run the focused tests and the parser suite.
- [ ] Commit `fix: preserve full-story link semantics`.

### Task 2: Durable Link and Source-Document Storage

**Files:**
- Modify: `app/migrations.py`
- Modify: `app/news/models.py`
- Modify: `app/news/repository.py`
- Modify: `tests/news/test_migrations.py`
- Modify: `tests/news/test_repository.py`

**Interfaces:**
- Produces: schema version 3, `SourceDocumentJob`, `replace_detail()` link upserts, `claim_source_document_jobs()`, `complete_source_document()`, `fail_source_document()`, and `source_document_data()`.

- [ ] Add failing migration tests for `news_segment_links` and `news_source_documents`, including seeding from existing excerpt `source_url` rows.
- [ ] Add failing repository tests for idempotent link upserts, independent source-document state, lease recovery, retry state, and prior-good-content preservation.
- [ ] Implement migration 3 with indexed foreign keys, constrained states, source timestamps, content hashes, and retry metadata.
- [ ] Extend `replace_detail()` to upsert links/documents in the same transaction and enqueue source translation only after successful English extraction.
- [ ] Implement repository claim/complete/fail/read operations with bounded exponential backoff.
- [ ] Run focused migration/repository tests and commit `feat: store publisher source documents`.

### Task 3: Safe Publisher Fetch and Readable Extraction

**Files:**
- Create: `app/news/source_document.py`
- Create: `tests/fixtures/news_v2/source_jsonld.html`
- Create: `tests/fixtures/news_v2/source_dom.html`
- Create: `tests/news/test_source_document.py`

**Interfaces:**
- Produces: `validate_public_url(url)`, `extract_source_document(html, final_url)`, and `SourceDocumentWorker.run_once()`.

- [ ] Add failing extraction tests with literal expected title, author, lead image, publication text, and ordered paragraphs for JSON-LD and DOM fallback fixtures.
- [ ] Add failing HTTP tests for public redirects, private/loopback destinations, HTML byte limits, 403 blocked state, malformed content, and successful persistence.
- [ ] Implement DNS/IP validation, manual redirects, bounded streaming, and content-type checks.
- [ ] Implement JSON-LD-first and scored-DOM extraction with boilerplate removal and quality thresholds.
- [ ] Snapshot fetched HTML and update repository state without changing Forex Factory rows.
- [ ] Run the focused tests and commit `feat: collect publisher full stories`.

### Task 4: Runtime, Translation, and API Contract

**Files:**
- Modify: `app/config.py`
- Modify: `app/main.py`
- Modify: `app/translation/worker.py`
- Modify: `app/news/api.py`
- Modify: `compose.yaml`
- Modify: `.env.example`
- Modify: `tests/news/test_translation.py`
- Modify: `tests/news/test_api.py`
- Modify: `tests/test_deployment_contract.py`

**Interfaces:**
- Produces: segment `links`, `GET /api/v2/news/source-documents/{id}`, source state counts, and `NEWS_SOURCE_*` configuration.

- [ ] Add failing API tests for complete, pending, blocked, missing, and unauthorized source documents.
- [ ] Add failing translation tests proving source English commits first and stale Chinese cannot overwrite new content.
- [ ] Add failing Compose contract tests for source interval, timeout, byte limit, redirect limit, and attempts.
- [ ] Wire the worker into `BackgroundRuntime`, translation priorities, status reporting, and API serializers.
- [ ] Add compatibility cleaning so old stored segments never expose terminal full-story labels as prose.
- [ ] Run all backend tests, Ruff, Compose config validation, and diff checks.
- [ ] Commit `feat: expose bilingual publisher documents`.

### Task 5: Live Deployment Acceptance

**Files:**
- Modify: `README.md`
- Modify: `docs/api-contract.md`

**Interfaces:**
- Consumes: existing Colorful Docker deployment and Cloudflare Tunnel.
- Produces: migrated live V3 storage and verified source-document API.

- [ ] Document provenance, source states, retry behavior, browser fallback, and new endpoint.
- [ ] Push the backend branch and update the Colorful checkout without printing secrets.
- [ ] Rebuild/restart with the persistent database volume and confirm all three containers are healthy.
- [ ] Verify a live excerpt has clean prose, a structured link, a distinct source document, and either complete native content or an explicit blocked/failed state.
- [ ] Commit/push any deployment-only documentation corrections.
