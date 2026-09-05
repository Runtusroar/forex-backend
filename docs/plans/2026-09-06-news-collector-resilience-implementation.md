# News Collector Resilience Implementation Plan

**Goal:** Make the production news collector self-healing and observable, and make the iOS news timestamp represent actual collection freshness.

**Architecture:** Browser capture owns page-level recovery and attaches source evidence to errors. The news collector owns durable failure counters and logs. The API exposes collection freshness separately from response time. The iOS client consumes that freshness and renders a compact delayed state.

**Tech stack:** Python 3.12, FastAPI, Playwright, SQLite, pytest; Swift 6, SwiftUI, XCTest.

### Task 1: Browser failure evidence and page recovery

**Files:**
- Modify: `app/collector/browser.py`
- Test: `tests/news/test_browser.py`

1. Add failing tests for a challenge page, a selector timeout with readable HTML, retry on a recreated news page, and CDP reconnect.
2. Run the focused tests and confirm the expected failures.
3. Add a private listing capture method, failure evidence attachment, page replacement, and one bounded retry.
4. Run the focused tests until they pass.

### Task 2: Durable failure state and useful logs

**Files:**
- Modify: `app/news/collector.py`
- Modify: `app/news/repository.py`
- Test: `tests/news/test_collector.py`

1. Add failing tests for consecutive failure state, failure timestamp, recovery reset, and emitted log records.
2. Run the focused tests and confirm the expected failures.
3. Replace silent suppression in the listing loop with structured failure handling and throttled logs.
4. Run the focused tests until they pass.

### Task 3: Expose truthful freshness

**Files:**
- Modify: `app/news/api.py`
- Modify: `app/news/repository.py`
- Test: `tests/news/test_api.py`

1. Add failing API tests for `source_updated_at` and status failure metadata.
2. Run the focused tests and confirm the expected failures.
3. Read runtime state through the repository and add the fields to the API responses.
4. Run the focused tests until they pass.

### Task 4: Use source freshness in iOS

**Files:**
- Modify: `ForexFactoryMVP/Models/APIModels.swift`
- Modify: `ForexFactoryMVP/News/NewsViewModel.swift`
- Modify: `ForexFactoryMVP/Components/InterfaceComponents.swift`
- Test: `ForexFactoryMVPTests/APIModelsTests.swift`
- Test: `ForexFactoryMVPTests/ViewModelTests.swift`
- Test: `ForexFactoryMVPTests/EditorialThemeTests.swift`

1. Add failing decoding, timestamp-selection, and delayed-state tests.
2. Run the focused XCTest targets and confirm the expected failures.
3. Decode `source_updated_at` with a legacy fallback, calculate delayed state, and append the compact English marker.
4. Run the focused tests until they pass.

### Task 5: Verify, integrate, deploy, and install

1. Run backend lint and the full pytest suite.
2. Generate the Xcode project and run the full iOS test suite.
3. Review diffs, commit each repository, merge the branches into `main`, and push both repositories.
4. Deploy the backend using the repository deployment procedure and inspect container logs/status.
5. Confirm the production source timestamp advances and current Forex Factory article IDs arrive.
6. Build and install the iOS app on the connected physical device and launch it for review.
