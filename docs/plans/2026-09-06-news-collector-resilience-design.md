# News Collector Resilience Design

## Problem

The public API can continue returning HTTP 200 while the production Chromium collector has stopped ingesting new Forex Factory news. The current listing loop suppresses every exception, reuses the same page after a timeout, and records only an exception class. A selector timeout also happens before the collector receives HTML, so the diagnostic snapshot is lost. API `generated_at` values describe response creation time rather than the age of the collected source data.

## Goals

- Preserve enough evidence to distinguish a Cloudflare challenge, invalid source markup, a navigation timeout, a stuck page, and a disconnected CDP browser.
- Recover automatically from a failed or stuck news page without discarding Chromium's persistent profile.
- Keep the last valid database contents when the source cannot be collected.
- Expose consecutive failures and truthful source freshness through the status and news APIs.
- Keep the iOS header compact and animation-free while showing the actual collection time and a delayed marker.

## Browser recovery

`BrowserSession.news_html()` performs one normal capture attempt. The attempt navigates to the news page, waits for either valid listing content or a bounded challenge grace period, and validates the returned HTML for a Cloudflare challenge.

If the attempt fails, the exception receives the current page HTML whenever Playwright can still read it. The shared news page is then closed and replaced. The call retries once on the fresh page after a short delay. The calendar page and Chromium user profile remain intact. If CDP is disconnected, the Playwright connection and both shared page references are cleared so `connect()` can establish a new connection.

After the retry fails, the final exception retains the latest available HTML. The collector snapshot hook stores it and leaves the existing database rows unchanged.

## Runtime state and logs

Every failed listing cycle records:

- `news_last_listing_error`
- `news_last_listing_error_at`
- `news_listing_consecutive_failures`

The first failure and periodic repeated failures are logged with the error type, failure count, and message. A later success logs recovery and resets the error fields and counter. Logs never contain the full source HTML or credentials; HTML remains in the bounded snapshot store.

The existing `/api/v2/status` response includes the failure timestamp and consecutive count. Its existing `listing_stale` calculation continues to use the last successful listing collection.

## API freshness contract

News list and latest-comment envelopes retain `generated_at` as the response-generation time for compatibility and add `source_updated_at` as the latest successful listing collection time. The latest-comments feed membership and rank are produced by that listing capture, so comment-audit success cannot make a stale feed appear fresh. The article detail contract is unchanged because the detail screen does not show a last-updated label.

The iOS models accept `source_updated_at`, falling back to `generated_at` for older servers. `NewsViewModel.lastUpdatedAt` uses the source timestamp. If a successful API response contains a source timestamp older than five minutes, the model marks the current data delayed instead of clearing its stale state.

The header remains one line and displays `Last updated <date> <time> UTC+8 · Delayed` when the source is stale. It has no spinner or loading animation.

## Verification

Backend tests cover challenge evidence, selector timeout evidence, fresh-page retry, reconnect behavior, runtime failure counters, recovery logs/state, status fields, and source timestamps. iOS tests cover decoding with and without `source_updated_at`, using the source timestamp after refresh, and delayed-state calculation. Full backend and iOS suites run before deployment.
