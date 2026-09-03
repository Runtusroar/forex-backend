# Full Story Acquisition Design

## Goal

Preserve the meaning and provenance of Forex Factory News pages, including ordered detail
segments and their `full story` links, while asynchronously collecting readable source-site
documents when possible. The source document must never overwrite the Forex Factory excerpt.

## Confirmed Root Causes

Forex Factory renders `full story` as an anchor inside `.news__copy`. The current parser correctly
copies its `href` into `news_segments.source_url`, but paragraph `.text()` also flattens the anchor
label into `text_en`. The API therefore loses link semantics and the phone displays
`...(full story)` as ordinary text.

The iPhone detail header also renders the list `teaser` before rendering detail segments. Live
data shows recent teasers are prefixes of the detail text, so this duplicates the first part of
the story.

## Source-of-Truth Boundaries

The system has three distinct sources that must remain distinguishable:

1. `news_articles` stores Forex Factory listing metadata: title, teaser, thumbnail, source, time,
   impact, comments, categories, and feed membership.
2. `news_segments` stores the ordered content shown on the Forex Factory detail page. Link labels
   are not part of segment prose.
3. `news_source_documents` stores a separately fetched publisher document. It has its own URL,
   title, author, publication time, body, extraction state, hashes, and timestamps.

No collector or migration may replace a Forex Factory teaser/segment with publisher content.

## Link Model

Add `news_segment_links` with one row per observed semantic link:

- stable identity within a segment;
- segment and article foreign keys;
- DOM order;
- kind (`full_story`, with room for later kinds);
- visible label;
- original absolute URL;
- linked source-document ID;
- first/last observation and current-state fields.

The detail parser removes recognized `full story` anchors before extracting prose, then emits the
link separately. Multiple segments and multiple links remain independently attributable.

Existing V2 rows are bootstrapped from excerpt segments that already contain `source_url`.
Known terminal `full story` labels are removed from API prose as a compatibility guard until every
old detail has been re-observed.

## Publisher Document Model

`news_source_documents` is keyed by a normalized original URL and records:

- original URL and final URL after safe redirects;
- source host, title, author, publication source text, lead image URL;
- ordered readable paragraphs serialized as JSON plus joined `body_en`;
- extraction method and content hash;
- state: `pending`, `processing`, `complete`, `blocked`, or `failed`;
- attempts, retry time, HTTP status, error type, first seen, last fetched, and updated time.

The original HTML is stored through the existing compressed snapshot subsystem under page type
`source`. Snapshots make future parser improvements possible without re-downloading publisher
pages. Parsed English commits before translation; translations use the existing hash-bound
`localized_texts` queue with entity type `source_document`.

## Safe Fetching and Extraction

A low-concurrency worker claims source documents independently from Forex Factory collection.
It uses bounded HTTP GETs with a browser-like user agent, manual redirects, maximum byte size,
HTML content-type validation, and retry backoff.

Every initial and redirect URL must use HTTP(S), have no credentials, and resolve exclusively to
public IP addresses. Loopback, private, link-local, reserved, multicast, and unspecified addresses
are rejected before any request. This prevents a publisher link from becoming an SSRF path into
the home network or container services.

Extraction first checks `NewsArticle`/`Article` JSON-LD, then scores visible article-like DOM
containers. It removes scripts, styles, navigation, adverts, forms, related-story blocks, and
footers, and preserves headings, paragraphs, list items, and block quotes in DOM order. A document
is `complete` only when the result passes minimum length and paragraph-quality checks. HTTP 401,
403, and 451 become `blocked`; unsupported/malformed pages remain retryable then become `failed`.

The MVP does not bypass authentication, paywalls, CAPTCHAs, or publisher restrictions. A blocked
source remains useful because its original URL and Forex Factory excerpt are preserved.

## API

Each V2 detail segment adds `links`. A link includes ID, kind, label, URL, position, and a source
document summary: ID, state, title, author, source host, published text, lead image URL, and whether
native readable content is available.

Add authenticated `GET /api/v2/news/source-documents/{id}`. It returns provenance, state,
English-first localized title/body, ordered English/Chinese paragraphs when complete, and the
original/final URL. Pending, blocked, and failed records still return metadata and state so the
client can choose a browser fallback.

Operational status adds source-document state counts and the latest successful/error timestamps.

## iPhone Behavior

The News card continues to show title, teaser, and thumbnail. Once detail has loaded, its header
shows title and metadata but not the listing teaser; ordered Forex Factory segments follow.

Each `full_story` link renders as a clear `Read full story` action rather than ordinary prose.
If its source document is complete, the action opens a native English-first/Chinese-below source
article screen. Otherwise it opens the original publisher URL in `SFSafariViewController`, keeping
the user inside the app while respecting publisher behavior.

## Failure and Freshness Rules

- Forex Factory collection never waits for a source fetch or translation.
- Source extraction failure never changes a Forex Factory detail from complete to failed.
- Publisher content is updated only after a successful parse; prior good content remains visible
  during refresh failures.
- Content hashes prevent duplicate translations and snapshots.
- Parser and repository tests cover multiple links, stripped labels, provenance, restart-safe job
  claims, safe redirects, blocked pages, JSON-LD, DOM fallback, and stale translation protection.
- API and iOS tests cover complete and blocked source documents plus browser fallback.

## Rollout

Migrate the database in place, seed link/document jobs from existing excerpt rows, deploy the
backend, verify live link and source-document state without logging secrets, then update and
install the iPhone client. The existing Cloudflare URL and API key remain unchanged.
