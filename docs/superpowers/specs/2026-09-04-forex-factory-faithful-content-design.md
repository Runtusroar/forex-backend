# Forex Factory Faithful Content Design

## Status and Scope

This design supersedes `2026-09-03-full-story-acquisition-design.md` for publisher-content
handling. The product remains an English-first, Chinese-subtitle iPhone presentation of the
information shown by Forex Factory. It does not collect, extract, store, translate, or natively
render publisher-site articles.

The change spans the backend collector, SQLite schema and API, and the iPhone news detail view.
Calendar behavior, notification behavior, Forex Factory comments, feed sections, and impact
filtering remain unchanged.

## Verified Forex Factory Behavior

Live inspection established these rules:

1. A normal `.news__article` can contain Forex Factory prose in `.news__copy`. When the prose is
   abbreviated, its terminal `...` is visible content and must be preserved exactly.
2. The terminal `(full story)` presentation is composed of punctuation plus an anchor whose
   visible label is `full story`. Selecting it opens the publisher URL.
3. A detail page can contain multiple `.news__article` blocks from different sources. Their DOM
   order, text, media, author metadata, and links are independent and must not be merged.
4. Social story blocks can expose their complete post text in the Forex Factory DOM while
   visually clamping long content. A live Truth Social example used a 200-pixel, ten-line clamp,
   displayed `Show More`, and opened the social-source URL in a new tab when selected.
5. News cards and detail pages are different Forex Factory presentations. The card teaser must
   not be repeated above successfully loaded detail segments.

These rules are behavior contracts, not assumptions based on one publisher.

## Chosen Approach

Use structured native rendering. The backend stores the Forex Factory text, ordered media, and
semantic links as separate but associated records. The API communicates display semantics, and
SwiftUI reconstructs the same relationship without embedding Forex Factory HTML.

Rejected alternatives:

- Saving arbitrary Forex Factory HTML and rendering it in a `WKWebView` would couple the app to
  remote CSS and JavaScript, complicate bilingual text, and increase security and layout risk.
- Flattening everything into plain text plus a separate action button would lose the inline link
  position and repeat the defect this design fixes.

## Collection and Parsing

The existing real-Chrome/CDP collection path remains the only browser path. It fetches Forex
Factory listing and detail pages; it never requests a publisher URL.

For each `.news__article`, in DOM order, the detail parser emits one segment:

- `text_en` contains only the text supplied by Forex Factory for that content block.
- Whitespace may be normalized, but visible punctuation and terminal `...` or `…` are retained.
- A recognized full-story anchor is excluded from prose and emitted as a structured link with
  kind `full_story`, label, absolute URL, and segment-relative order.
- The client reconstructs a non-breaking ` (` + linked label + `)` after the English prose.
- Images and attachments already exposed by Forex Factory are retained through the existing
  media cache. The collector does not discover extra media on the publisher page.
- Social blocks retain author, handle, time, Forex Factory DOM text, source URL, and a display
  mode. A block marked by Forex Factory as show-more content is emitted as `clamped` with ten
  lines and action label `Show More`; other blocks are `full`.
- Selecting a full-story link, source-linked image, social block, or `Show More` opens its stored
  external URL through the existing in-app Safari presentation.

Comments continue to be collected from the Forex Factory detail page. No new comment behavior is
introduced by this change.

## Storage and Migration

`news_segments` gains explicit presentation fields sufficient for native rendering:

- display mode: `full` or `clamped`;
- optional maximum line count;
- optional external action label.

`news_segment_links` remains the normalized semantic-link table but no longer references a
publisher document. Link URL, label, kind, position, stable identity, and observation history are
preserved.

The migration removes `news_source_documents`, their localized-text jobs, source-fetch runtime
state, and the link foreign-key dependency. Existing Forex Factory article, segment, link, media,
comment, and translation data remain intact. A timestamped SQLite backup is created immediately
before the production migration, making removal of previously collected publisher content
reversible by restoring the backup.

## Translation

Only text present on Forex Factory is queued for Kimi translation. Collection and English API
availability never wait for translation. Translation failure leaves the English content, media,
links, and display rules available and retryable.

English remains authoritative and is rendered first. Chinese is rendered below as a subtitle. A
full-story link is shown once, inline with the English text, because it is an action rather than
translatable prose. Clamped social English and Chinese content use the same line limit so the
screen does not reveal more content in one language than the other.

## API Contract

`GET /api/v2/news/{source_id}` continues to return ordered segments. Each segment returns:

- exact normalized Forex Factory English text and optional Chinese translation;
- author and source metadata;
- ordered media;
- ordered links containing only ID, position, kind, label, and URL;
- presentation information for full or clamped display.

The API stops returning `source_document` summaries. The authenticated source-document endpoint
and source-document status counts are removed. The deployed base URL and authentication scheme do
not change.

## iPhone Presentation

The detail header shows the title and metadata. Once ordered detail segments are available, the
listing teaser is not shown again.

For an article excerpt, SwiftUI creates one English attributed run consisting of the preserved
prose and inline `(full story)` link. Selecting the link presents `SFSafariViewController` for the
stored URL. Chinese text follows below and does not duplicate the action.

For a clamped social segment, English and Chinese use the API-provided line limit. `Show More` is
shown as an external action matching Forex Factory behavior and opens the social-source URL in the
same in-app Safari presentation. Media stays in segment order and keeps its existing native image
display.

The publisher-article reader, loading states, source-document models, requests, and navigation are
deleted.

## Failure Handling

- A missing or malformed publisher URL does not discard the Forex Factory prose.
- A segment without a valid external URL renders its text and media without an inert action.
- One malformed content block does not reorder or overwrite valid sibling blocks.
- A Forex Factory challenge or structurally incomplete page follows the existing safe retry path
  and never replaces the last complete observation.
- Media and Kimi failures remain independent from article collection.

## Verification and Rollout

Backend fixtures and tests cover exact terminal ellipsis preservation, inline full-story link
semantics, multiple ordered article blocks, full and clamped social blocks, absence of publisher
HTTP work, source-document schema removal, and API compatibility. iPhone tests cover simplified
link decoding, inline link construction, Safari routing, clamped social presentation, and removal
of publisher-reader requests.

After both full test suites and static checks pass:

1. create and verify a production database backup;
2. deploy the backend migration and application to the Colorful server;
3. compare representative live API records against their Forex Factory pages;
4. install the rebuilt app on the paired iPhone 15 Pro;
5. verify normal article, multi-source article, image, comments, full-story link, social clamp, and
   bilingual rendering on device;
6. commit and push the backend and iPhone repositories separately.
