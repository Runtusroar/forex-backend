# Forex Factory News V2 Backend Design

Date: 2026-09-03

Status: Proposed, based on the approved recommended architecture

## 1. Purpose

Replace the current proof-of-concept News collector with a durable representation of the information exposed by Forex Factory News. The backend must preserve article identity, classifications, dynamic feed placement, ordered story content, real media, breaking-news impact, source metadata, and observed comments without coupling source collection to translation success.

This specification covers the backend collector, storage model, media cache, translation integration, and read-only API contract. The iPhone application will be redesigned against the stable V2 API in a separate follow-up specification.

## 2. Current Problems

The existing collector opens only `https://www.forexfactory.com/news` and scans every `.news-block__item` on the page as if all items belonged to one feed. It skips comment cards, deduplicates by source ID, and discards where each item appeared.

The live page currently exposes distinct structures:

- Hot Stories;
- News / Latest Stories;
- News / Latest Comments;
- Fundamental Analysis / Latest Stories;
- Technical Analysis / Latest Stories;
- Forex Industry News / Latest Stories;
- Entertainment News / Latest Stories;
- Educational News / Latest Stories;
- a Breaking News impact legend with high, medium, and low values.

The existing detail parser selects only the first `.news__article` containing an `h1`. A Forex Factory detail page can contain several ordered article, social, update, chart, and attachment segments, so later segments are lost. The current first-`img` rule can store UI icons, impact SVGs, avatars, or source logos as article media. Absolute publication timestamps and comment counts are also available in the DOM but are not stored.

## 3. Design Decisions

### 3.1 Replicate information semantics, not the desktop layout

The backend will model the information relationships visible on Forex Factory. It will not reproduce Forex Factory HTML or its visual layout. The iPhone client may present the same information with native navigation and English-first, Chinese-subtitle formatting.

### 3.2 Separate classifications, feeds, and comments

The following concepts are deliberately distinct:

- **Article:** the canonical Forex Factory news identity.
- **Category:** a relatively stable content classification: fundamental, technical, industry, entertainment, or educational.
- **Feed:** a dynamic placement such as latest or hot, including current rank.
- **Comment feed:** recent comment activity, whose entries point to comment entities and related articles.
- **Breaking impact:** `high`, `medium`, `low`, or `null`; this is independent of categories and feeds.

An article is stored once and can have multiple categories and feed placements.

### 3.3 Preserve changes, not every identical 30-second page

The system will record entry, exit, and rank changes for dynamic feeds. It will not store a complete historical copy of the page every 30 seconds. Raw compressed HTML is retained only when content changes, when a detail is first seen, or when parsing fails.

### 3.4 Keep source facts independent from translations

English source data is committed before translation. Translation is asynchronous, versioned by the source-content hash, and cannot block or roll back collection. A failed or stale translation remains retryable without changing the source record.

### 3.5 Do not recursively crawl external publishers in V2

Forex Factory sometimes exposes only an excerpt and a `full story` link. V2 stores the external source URL and an excerpt marker but does not crawl arbitrary publisher sites. This keeps the collector bounded and avoids per-publisher anti-bot, paywall, copyright, and parsing behavior.

## 4. Scope

### 4.1 In scope

- Every news block visible on the Forex Factory News page.
- Latest and Hot feed membership and rank changes.
- Fundamental, Technical, Industry, Entertainment, and Educational category membership.
- Latest Comments entries visible on the News page.
- Absolute publication timestamps, source name, source URL, comment count, and breaking impact.
- Every ordered `.news__article` segment on a fetched detail page.
- Ordinary article excerpts, social posts, updates, quotes, content links, charts, images, and attachments.
- Real content-media caching with hash deduplication.
- Comment and nested-reply data exposed in the initially loaded detail DOM.
- English source text and asynchronous Simplified Chinese translation.
- Thirty-day best-effort historical backfill, followed by permanent incremental collection.
- V1 API compatibility during the iPhone migration.

### 4.2 Out of scope for this phase

- Exact historical reconstruction of the full page at every collection instant.
- Crawling complete articles from arbitrary external publishers.
- Pixel-for-pixel reproduction of Forex Factory.
- Posting comments, subscribing to Forex Factory alerts, or other write interactions.
- Exhaustively expanding every historical comment page or hidden reply tree.
- Backfilling the entire Forex Factory archive beyond the initial 30-day window.
- APNs changes; push policy remains a separate feature after the News V2 data contract is stable.

## 5. Components

### 5.1 Browser source

The persistent Chrome/CDP session remains the acquisition mechanism because it is already proven against the live site. Browser responsibilities are limited to navigation, waiting for valid content, and returning rendered HTML. It does not contain parsing or persistence logic.

### 5.2 Listing adapter

The listing adapter parses a rendered `/news` page into one observation batch:

- canonical article observations;
- category memberships;
- latest and hot feed placements with rank;
- latest-comment observations;
- source-page metadata and content hash.

The adapter identifies blocks from their semantic heading and known structure rather than treating all `.news-block__item` nodes identically. It rejects challenge pages and selector loss before producing mutations.

When an article occurs in several blocks during one cycle, the adapter merges observations by Forex Factory news ID. It unions categories and placements, prefers valid non-empty fields, and reports conflicting non-empty values rather than silently choosing by DOM order.

### 5.3 Detail adapter

The detail adapter parses all `.news__article` elements in DOM order. Each source element becomes a normalized segment with a stable fingerprint. A segment can be an article excerpt, social post, update, quote, or link-bearing content block.

Media extraction is scoped to content and attachment containers. UI SVGs, impact icons, avatars, emoji assets, and source logos are excluded. Captions and source URLs are retained. The adapter records whether Forex Factory content is an excerpt that links to a full external story.

### 5.4 Collector coordinator

The coordinator performs short, idempotent stages:

1. Fetch and validate the listing page.
2. Parse a complete observation batch.
3. Commit articles, classifications, feed changes, and latest-comment entries in one transaction.
4. Queue new or changed articles for detail collection.
5. Process detail work independently, prioritizing high-impact and newest articles.
6. Queue changed source text for translation.
7. Queue eligible content media for download.

A detail or media failure never discards a successful listing observation.

### 5.5 Media cache

The media worker downloads only normalized content media. Files are stored under the persistent data volume using a SHA-256-derived name. The database retains both the original URL and local serving path.

Downloads use bounded timeouts, content-type validation, a size limit, and atomic temporary-file replacement. Unsupported or failed media remains represented by its original URL and error status, so article ingestion continues.

### 5.6 Translation worker

The Kimi worker translates article titles, teasers, segment text, and optionally collected comment text. Jobs are keyed by entity, field, language, and source hash. Source changes create a new job and make older work stale. Batches remain bounded and retry with backoff.

Comments are stored immediately. Comment translation is lower priority than article and story-segment translation.

## 6. Storage Model

SQLite in WAL mode remains appropriate for one user and one collector. Media is stored in the persistent filesystem rather than as database blobs.

### 6.1 `news_articles`

Canonical article facts:

- `source_id` primary key;
- `ff_url` unique;
- `title_en`;
- `teaser_en` nullable;
- `source_name` nullable;
- `source_url` nullable;
- `published_at` nullable only when the source provides no absolute timestamp;
- `published_at_source_text` nullable, preserving the displayed source value;
- `source_timezone` nullable, preserving the timezone used for normalization;
- `breaking_impact` constrained to high, medium, low, or null;
- `comment_count` non-negative;
- `detail_state`: pending, complete, partial, or failed;
- `is_excerpt`;
- `source_hash`;
- `first_seen_at`, `last_seen_at`, and `updated_at`.

The old single `image_url` and `body_en` fields are not the V2 source of truth. They may be populated in a compatibility view or serializer for V1 clients until migration completes.

### 6.2 `news_category_memberships`

- `article_id`;
- `category` constrained to fundamental, technical, industry, entertainment, or educational;
- `first_seen_at`;
- `last_seen_at`;
- unique `(article_id, category)`.

Category membership is durable once observed. A category panel shows only its newest subset, so later absence from that panel is not evidence that the article lost its classification. V2 does not remove category membership unless the source provides an explicit contradictory signal.

### 6.3 `news_feed_placements`

Current latest/hot state:

- `article_id`;
- `feed_type`: latest or hot;
- `rank`;
- `first_seen_at`;
- `last_seen_at`;
- `is_current`;
- unique `(article_id, feed_type)`.

A placement is marked inactive only after it is absent from three consecutive successful observations of that same feed. Failed, challenged, or structurally incomplete page loads do not count as absence.

### 6.4 `news_feed_events`

Change-only history:

- generated ID;
- `article_id`;
- `feed_type`;
- `event_type`: entered, moved, or left;
- `previous_rank` nullable;
- `new_rank` nullable;
- `observed_at`.

No event is written when a successful cycle observes the same state and rank.

### 6.5 `news_segments`

- generated ID;
- `article_id`;
- `position`;
- `segment_type`: article, social, update, quote, or link;
- `author_name` nullable;
- `author_handle` nullable;
- `published_at` nullable;
- `text_en` nullable;
- `source_url` nullable;
- `is_excerpt`;
- `source_hash`;
- `first_seen_at`, `last_seen_at`, and `updated_at`;
- stable unique segment key within the article.

Segment order is part of the rendered story but not the identity of an otherwise unchanged segment. Reordering updates `position` without duplicating the content.

### 6.6 `news_media`

- generated ID;
- `article_id`;
- `segment_id` nullable for article-level media;
- `position`;
- `media_type`: image, chart, or attachment;
- `original_url`;
- `local_path` nullable;
- `mime_type` nullable;
- `byte_size` nullable;
- `sha256` nullable;
- `caption` nullable;
- `download_state` and sanitized `last_error`;
- unique normalized source URL per owning segment.

### 6.7 `news_comments`

- Forex Factory comment ID primary key;
- `article_id`;
- `parent_comment_id` nullable;
- `author_name`;
- `published_at` nullable;
- `text_en`;
- `permalink`;
- `reaction_count` nullable;
- `first_seen_at`, `last_seen_at`, and `updated_at`;
- `source_hash`.

Only comments exposed in the initially rendered detail page and the Latest Comments block are collected in this phase. The schema supports later full pagination without migration.

### 6.8 `news_comment_feed`

- `comment_id`;
- `rank`;
- `first_seen_at`;
- `last_seen_at`;
- `is_current`;
- unique `comment_id`.

### 6.9 `localized_texts` and translation jobs

Localized values are stored separately:

- entity type and ID;
- field name;
- language;
- source hash;
- translated text;
- model;
- status and timestamps;
- unique `(entity_type, entity_id, field_name, language, source_hash)`.

The existing translation-job mechanism is migrated to reference these V2 entities. Existing calendar translation behavior remains unchanged.

### 6.10 Source snapshots

`source_snapshots` records:

- page type and page key;
- content hash;
- compressed-file path;
- capture time;
- parse status and sanitized error type.

Listing snapshots are retained for 30 days. Detail snapshots are written on first observation, meaningful source change, or parse failure and retained for 30 days. Snapshot cleanup never deletes normalized article, segment, comment, or media data.

## 7. Timing and Priorities

- Main News listing: every 30 seconds.
- Detail queue: continuous, concurrency one initially to avoid source pressure.
- New high-impact details: highest priority.
- Other new details: newest first.
- Existing details with changed comment count or incomplete state: rechecked at a slower bounded interval.
- Media downloads: after source text is committed, with low bounded concurrency.
- Translation: asynchronous; article title and high-impact segments before comments.
- Historical backfill: throttled, resumable, and lower priority than live collection.

The regular listing cycle does not wait for detail, media, translation, or backfill queues.

Source timestamps are normalized to aware UTC values using an explicit configured Forex Factory display timezone. The original displayed text and the applied timezone are preserved. Relative-only values remain nullable when they cannot be resolved safely; first-observed time is never presented as an exact publication time.

The 30-day backfill uses the site's visible, browser-driven `More` continuation for each relevant story block. It stops when the oldest valid publication time is beyond the cutoff, the site reports no continuation, or two successive successful continuations yield no new source IDs. Progress is checkpointed per block so restart does not repeat completed work.

## 8. API V2

All endpoints remain read-only and protected by the existing API-key mechanism.

### 8.1 Sections

`GET /api/v2/news/sections`

Returns stable display order, identifiers, names, current item counts, and supported capabilities for latest, hot, fundamental, technical, industry, entertainment, educational, and latest-comments.

### 8.2 Article lists

`GET /api/v2/news?section=<slug>&impact=<optional>&limit=<n>&cursor=<optional>`

The backend maps `latest` and `hot` to feed placements and category slugs to category memberships. Responses include title translations, source, true publication time, breaking impact, comment count, a valid content thumbnail when available, and the article's category list.

Pagination uses an opaque stable cursor derived from the section's ordering keys and article ID. It does not depend solely on first-observed time.

### 8.3 Detail

`GET /api/v2/news/{source_id}`

Returns canonical article metadata, categories, current feed states, ordered segments with translations, normalized media, original-source links, excerpt markers, comment summary, and completeness state.

### 8.4 Comments

- `GET /api/v2/news/comments/latest?limit=<n>&cursor=<optional>`
- `GET /api/v2/news/{source_id}/comments?limit=<n>&cursor=<optional>`

Replies include `parent_comment_id`. The API reports whether the backend has only the initially visible subset, so the client never implies that a partial collection is complete.

### 8.5 Media

`GET /api/v2/news/media/{media_id}`

Streams a successfully cached media object with the existing API-key protection, a validated content type, cache headers, and no exposure of filesystem paths. If caching failed, article responses retain the original URL and download state so the client can show a placeholder or explicitly open the source.

### 8.6 Compatibility

`/api/v1/news` and `/api/v1/news/{source_id}` continue working while the current iPhone build is in use. Their flattened body and image values are derived best-effort from V2 data. V1 is removed only after the V2 iPhone build has been installed and verified.

## 9. Failure Handling and Data Integrity

- A challenged, empty, or structurally invalid listing produces no membership exits or destructive updates.
- Listing observations are committed transactionally.
- Duplicate source IDs in one page are merged before persistence.
- Missing optional fields do not overwrite previously valid values unless the source explicitly removes them across confirmed observations.
- A detail parse can be partial; successfully parsed segments remain available and the article stays retryable.
- Detail, media, translation, comment, and backfill failures do not block live listing collection.
- All persistent jobs are idempotent and safe to retry after process restart.
- Errors stored in the database and status API contain types and bounded messages, never secrets or full request headers.
- Before schema migration, deployment takes a recoverable backup of the SQLite database and media directory.

## 10. Migration

The migration is versioned and idempotent.

1. Back up the live SQLite database.
2. Create V2 tables without dropping calendar data or V1 news data.
3. Import each legacy news row into `news_articles` as partial data.
4. Mark imported articles for live detail refresh and V2 translation.
5. Serve V1 from compatibility serialization while the V2 collector enriches records.
6. Keep legacy tables until the V2 backend and iPhone app have both passed acceptance testing.

No destructive legacy-table removal is part of this phase.

## 11. Observability and Operations

The status endpoint will expose, without secrets:

- last successful listing time;
- last listing error type;
- counts by section and impact;
- detail queue counts by state;
- media queue counts by state;
- translation queue counts by state;
- last successful detail and translation times;
- current database schema version.

Docker persists SQLite, compressed source snapshots, Chrome profile data, and cached media in named volumes. Backup documentation includes both database and media paths. Snapshot cleanup and backfill are bounded maintenance tasks.

## 12. Testing Strategy

### Parser fixtures

- all News page sections and their headings;
- duplicate articles appearing in multiple sections;
- high, medium, low, and absent breaking impact;
- absolute timestamps and comment counts;
- Hot Stories and Latest Comments;
- ordinary detail pages;
- social-only details;
- multi-segment alloy stories;
- article excerpts with external full-story links;
- content images, charts, attachments, and excluded UI assets;
- nested visible comments;
- challenge and selector-loss pages.

### Persistence tests

- idempotent upsert behavior;
- category union without duplicate articles;
- entered, moved, and left feed events;
- three-success absence rule;
- segment update and reorder behavior;
- stale translation rejection;
- media URL and hash deduplication;
- migration from the current schema without calendar loss.

### Service and API tests

- listing success survives detail failure;
- source collection survives Kimi failure;
- stable cursor pagination;
- filters and section ordering;
- partial-detail and partial-comment completeness flags;
- V1 compatibility during migration;
- API authentication.

### Live smoke test

An opt-in live test uses the persistent Chrome route and verifies non-empty sections, valid source IDs, true publication timestamps where exposed, sensible impact values, and at least one parseable detail. It never becomes a deterministic unit-test dependency.

## 13. Acceptance Criteria

- The live News page produces the complete expected set of named sections without mixing Latest Comments into articles.
- A source ID exists only once even when it appears in several categories or feeds.
- Category memberships and latest/hot positions remain queryable independently.
- Breaking impact, absolute time, source, source URL, and comment count match the rendered page.
- Known multi-segment details preserve every story segment in DOM order.
- Only genuine content images, charts, and attachments are exposed as media.
- A translation outage leaves new English source content available through the API.
- A challenge page cannot erase or deactivate current section membership.
- Thirty-day backfill can stop and resume without duplicates.
- Existing calendar endpoints remain unchanged.
- Existing V1 News endpoints continue serving the installed phone build until V2 client verification is complete.

## 14. Delivery Order

1. Schema migration and repository primitives.
2. Complete listing parser and observation merger.
3. Feed/category persistence and tests.
4. Multi-segment detail and comment parsing.
5. Media cache.
6. V2 translation integration.
7. V2 API and V1 compatibility adapter.
8. Thirty-day resumable backfill.
9. Docker migration, backup, live smoke verification, and deployment.
10. Separate iPhone News V2 UI design and implementation against the verified API.
