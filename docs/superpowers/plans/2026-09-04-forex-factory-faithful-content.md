# Forex Factory Faithful Content Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the backend store and serve only content present on Forex Factory while preserving its ellipsis, full-story links, ordered blocks, media, and social clamp behavior.

**Architecture:** Keep the real-Chrome Forex Factory collector and normalized segment/media/link model. Remove the independent publisher-document pipeline, add explicit segment presentation metadata, and migrate schema v3 to v4 without losing Forex Factory links or content.

**Tech Stack:** Python 3.12, FastAPI, aiosqlite/SQLite, selectolax, Playwright CDP, pytest, Ruff, Docker Compose

**Spec:** `docs/superpowers/specs/2026-09-04-forex-factory-faithful-content-design.md`

## Global Constraints

- Never request, extract, store, translate, or expose publisher-site article bodies.
- Preserve visible terminal `...` and `…` in Forex Factory prose.
- Preserve every `.news__article` block in DOM order.
- Full-story and social actions remain structured external URLs.
- Kimi failures never block English collection.
- Create a timestamped production SQLite backup before schema v4 is applied.

---

### Task 1: Parser Presentation Contract

**Files:**
- Modify: `app/news/models.py`
- Modify: `app/news/detail.py`
- Modify: `tests/fixtures/news_v2/detail_alloy.html`
- Create: `tests/fixtures/news_v2/detail_truth_social.html`
- Modify: `tests/news/test_detail.py`

**Interfaces:**
- Consumes: Forex Factory `.news__article`, `.news__copy`, `.x-twitter-post-preview__*`, and `.truthsocial-post__*` nodes.
- Produces: `SegmentObservation(display_mode, max_lines, external_action_label)` and independent `SegmentLinkObservation` values.

- [ ] **Step 1: Write failing parser tests**

Add assertions equivalent to:

```python
assert article.text_en == "Full Forex Factory excerpt..."
assert article.display_mode == "full"
assert detail.links[0].label == "full story"
assert social.text_en == expected_complete_ff_dom_text
assert social.display_mode == "clamped"
assert social.max_lines == 10
assert social.external_action_label == "Show More"
```

- [ ] **Step 2: Verify parser tests fail**

Run: `.venv/bin/pytest -q tests/news/test_detail.py`

Expected: failures show the ellipsis is removed and presentation fields/Truth Social parsing are absent.

- [ ] **Step 3: Add presentation fields and faithful parsing**

Define:

```python
SegmentDisplayMode = Literal["full", "clamped"]

@dataclass(frozen=True, slots=True)
class SegmentObservation:
    # existing fields
    display_mode: SegmentDisplayMode = "full"
    max_lines: int | None = None
    external_action_label: str | None = None
```

Use whitespace normalization that retains punctuation. Remove only the recognized anchor before prose extraction. Parse Truth Social author, handle, source URL, timestamp, and `.truthsocial-post__content`; when the post has `truthsocial-post--show-more`, emit `clamped`, `10`, and `Show More`.

- [ ] **Step 4: Verify parser tests pass**

Run: `.venv/bin/pytest -q tests/news/test_detail.py`

- [ ] **Step 5: Commit parser contract**

```bash
git add app/news/models.py app/news/detail.py tests/fixtures/news_v2 tests/news/test_detail.py
git commit -m "fix: preserve Forex Factory detail presentation"
```

### Task 2: Schema v4 Migration

**Files:**
- Modify: `app/migrations.py`
- Modify: `tests/news/test_migrations.py`

**Interfaces:**
- Consumes: schema versions 1, 2, and 3.
- Produces: schema version 4 with segment presentation columns and publisher-independent `news_segment_links`.

- [ ] **Step 1: Write failing v3-to-v4 migration test**

Create a v3 database containing a source document, its translation rows, a linked segment, and a full-story link. Assert after migration:

```python
assert schema_version == 4
assert "news_source_documents" not in table_names
assert source_document_translation_count == 0
assert link == ("full_story", "full story", "https://publisher.example/story")
assert segment_presentation == ("full", None, None)
```

- [ ] **Step 2: Verify migration test fails**

Run: `.venv/bin/pytest -q tests/news/test_migrations.py`

- [ ] **Step 3: Implement migration 4**

Add presentation columns to `news_segments`. Rebuild `news_segment_links` without `source_document_id`, copy current link identity/order/URL data, drop the old child table and `news_source_documents`, remove `localized_texts.entity_type='source_document'`, recreate the ordering index, and update `runtime_state.schema_version` to `4` in one transaction.

- [ ] **Step 4: Verify all migration entry paths**

Run: `.venv/bin/pytest -q tests/news/test_migrations.py`

Expected: clean databases and v2/v3 upgrades all end at schema version 4.

- [ ] **Step 5: Commit schema migration**

```bash
git add app/migrations.py tests/news/test_migrations.py
git commit -m "refactor: remove publisher documents from schema"
```

### Task 3: Repository and Translation Simplification

**Files:**
- Modify: `app/news/repository.py`
- Modify: `app/translation/worker.py`
- Modify: `tests/news/test_repository.py`
- Modify: `tests/news/test_translation.py`
- Delete: `tests/news/test_source_document.py`

**Interfaces:**
- Consumes: `DetailObservation` segments and links.
- Produces: stored Forex Factory segments, media, links, comments, and translation jobs only.

- [ ] **Step 1: Replace publisher-job tests with faithful storage tests**

Assert `complete_detail()` persists presentation fields and a full-story link without creating a publisher job. Assert translation claims never return `source_document` work.

- [ ] **Step 2: Verify repository tests fail**

Run: `.venv/bin/pytest -q tests/news/test_repository.py tests/news/test_translation.py`

- [ ] **Step 3: Remove publisher repository paths**

Update segment INSERT/UPDATE statements for `display_mode`, `max_lines`, and `external_action_label`. Insert links directly into the rebuilt table. Remove source-document claim/complete/fail/refresh/read methods and source-document translation table mappings.

- [ ] **Step 4: Verify focused tests pass**

Run: `.venv/bin/pytest -q tests/news/test_repository.py tests/news/test_translation.py`

- [ ] **Step 5: Commit repository simplification**

```bash
git add app/news/repository.py app/translation/worker.py tests/news
git commit -m "refactor: store only Forex Factory content"
```

### Task 4: API Contract Without Publisher Documents

**Files:**
- Modify: `app/news/api.py`
- Modify: `tests/news/test_api.py`
- Modify: `tests/news/test_smoke_contract.py`

**Interfaces:**
- Consumes: repository detail data.
- Produces: `segments[].presentation` and simplified `segments[].links[]`; removes `/api/v2/news/source-documents/{id}`.

- [ ] **Step 1: Write failing API assertions**

Assert detail JSON retains `...`, includes:

```json
{
  "presentation": {"mode": "full", "max_lines": null, "action_label": null},
  "links": [{"id": 1, "position": 0, "kind": "full_story", "label": "full story", "url": "https://publisher.example/story"}]
}
```

Assert the removed source-document route returns 404 and status output has no source-document counters.

- [ ] **Step 2: Verify API tests fail**

Run: `.venv/bin/pytest -q tests/news/test_api.py tests/news/test_smoke_contract.py`

- [ ] **Step 3: Implement the simplified response**

Delete terminal full-story text stripping and source-document response helpers/routes. Return presentation fields and links without nested publisher metadata.

- [ ] **Step 4: Verify API contract tests pass**

Run: `.venv/bin/pytest -q tests/news/test_api.py tests/news/test_smoke_contract.py`

- [ ] **Step 5: Commit API contract**

```bash
git add app/news/api.py tests/news/test_api.py tests/news/test_smoke_contract.py
git commit -m "fix: expose faithful Forex Factory segments"
```

### Task 5: Runtime and Deployment Cleanup

**Files:**
- Modify: `app/main.py`
- Modify: `app/config.py`
- Delete: `app/news/source_document.py`
- Modify: `compose.yaml`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `docs/design.md`
- Modify: `tests/test_config.py`
- Modify: `tests/test_compose.py`

**Interfaces:**
- Consumes: the existing calendar, listing, detail, media, translation, snapshot-cleanup, and backfill workers.
- Produces: runtime with no publisher HTTP client or source-document worker/configuration.

- [ ] **Step 1: Update config and Compose contract tests**

Assert `NEWS_SOURCE_INTERVAL_SECONDS`, `NEWS_SOURCE_TIMEOUT_SECONDS`, `NEWS_SOURCE_MAX_BYTES`, `NEWS_SOURCE_MAX_REDIRECTS`, and `NEWS_SOURCE_MAX_ATTEMPTS` are absent while Forex Factory timezone/detail settings remain.

- [ ] **Step 2: Verify cleanup tests fail**

Run: `.venv/bin/pytest -q tests/test_config.py tests/test_compose.py`

- [ ] **Step 3: Remove source runtime and documentation**

Delete source-worker construction/shutdown, source-only settings and environment keys, module and documentation claims. Keep `httpx` if still required by the media worker.

- [ ] **Step 4: Verify focused and import tests**

Run: `.venv/bin/pytest -q tests/test_config.py tests/test_compose.py`

Run: `.venv/bin/python -m compileall -q app`

- [ ] **Step 5: Commit runtime cleanup**

```bash
git add -A
git commit -m "refactor: remove publisher collection runtime"
```

### Task 6: Backend Verification

**Files:**
- Modify only if verification reveals a defect in files already listed above.

**Interfaces:**
- Consumes: complete backend changes.
- Produces: a releasable backend commit.

- [ ] **Step 1: Run full tests**

Run: `.venv/bin/pytest -q`

- [ ] **Step 2: Run static checks**

Run: `.venv/bin/ruff check .`

Run: `git diff --check`

- [ ] **Step 3: Validate Docker Compose rendering**

Run: `docker compose --env-file .env.example config --quiet`

- [ ] **Step 4: Inspect source-pipeline absence**

Run: `rg -n "SourceDocument|source_document|NEWS_SOURCE_(INTERVAL|TIMEOUT|MAX_BYTES|MAX_REDIRECTS|MAX_ATTEMPTS)" app tests compose.yaml .env.example README.md docs/design.md`

Expected: no runtime/config/API/source-model matches.

- [ ] **Step 5: Commit any verification fixes**

```bash
git add -A
git commit -m "test: verify faithful Forex Factory content"
```

### Task 7: Production Migration and Live Contract Check

**Files:**
- No repository files unless a live contract defect is reproduced in a test first.

**Interfaces:**
- Consumes: tested backend image and Colorful server deployment.
- Produces: schema-v4 production service with a restorable pre-migration database backup.

- [ ] **Step 1: Inspect server paths, running Compose project, disk space, and health without mutation**

- [ ] **Step 2: Copy the SQLite database to a timestamped backup and verify the backup file size and SQLite integrity**

- [ ] **Step 3: Push the backend branch and deploy the exact tested commit**

- [ ] **Step 4: Verify `/health`, authenticated `/api/v2/status`, and representative normal, multi-source, image, comment, and social-detail responses**

- [ ] **Step 5: Compare response text, ellipsis, links, order, and presentation metadata with current Forex Factory pages**

- [ ] **Step 6: Record the deployed commit and backup path in the handoff**
