from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    Locator,
    Page,
    Playwright,
    Request,
    Response,
    async_playwright,
)

from app.parsers.calendar import parse_calendar
from app.parsers.errors import SourcePageError, reject_challenge

logger = logging.getLogger(__name__)

NEWS_SECTION_HEADINGS = {
    "latest": "News / Latest Stories",
    "fundamental": "Fundamental Analysis / Latest Stories",
    "technical": "Technical Analysis / Latest Stories",
    "industry": "Forex Industry News / Latest Stories",
    "entertainment": "Entertainment News / Latest Stories",
    "educational": "Educational News / Latest Stories",
}


@dataclass(frozen=True, slots=True)
class NewsContinuationPage:
    html: str
    continuation_count: int
    source_ids: frozenset[str]
    terminal: bool


@dataclass(frozen=True, slots=True)
class NewsCommentCapture:
    html: str
    declared_count: int
    collected_count: int
    source_exhausted: bool = False


class SourceAccessRestrictedError(SourcePageError):
    """The source explicitly requires authentication to show the requested article."""


class CalendarDetailPages(dict[str, str | None]):
    """A compatible mapping that also retains evidence for unavailable details."""

    def __init__(self, pages: dict[str, str | None], source_html: str) -> None:
        super().__init__(pages)
        self.source_html = source_html


async def _source_ids(block: Locator) -> frozenset[str]:
    hrefs = await block.locator("a[href*='/news/']").evaluate_all(
        "links => links.map(link => link.getAttribute('href') || '')"
    )
    return frozenset(match.group(1) for href in hrefs if (match := re.search(r"/news/(\d+)", href)))


LOADING_SELECTOR = ".loading:visible, .is-loading:visible, [aria-busy='true']:visible"


class _SourceActivity:
    """Track source AJAX, excluding third-party advertising and analytics traffic."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.pending: set[Request] = set()
        self.failed = False

    def _started(self, request: Request) -> None:
        target = urlsplit(request.url)
        if (
            request.resource_type in {"xhr", "fetch"}
            and target.hostname in {"www.forexfactory.com", "forexfactory.com"}
            and (target.path.startswith("/news/") or target.path.startswith("/calendar"))
        ):
            self.pending.add(request)

    def _finished(self, request: Request) -> None:
        self.pending.discard(request)

    def _failed(self, request: Request) -> None:
        if request in self.pending:
            self.failed = True
        self.pending.discard(request)

    def _response(self, response: Response) -> None:
        if response.request in self.pending and response.status >= 400:
            self.failed = True

    @property
    def loading(self) -> bool:
        if self.failed:
            raise SourcePageError("source request failed before content stabilized")
        return bool(self.pending)

    def __enter__(self) -> _SourceActivity:
        self.page.on("request", self._started)
        self.page.on("requestfinished", self._finished)
        self.page.on("requestfailed", self._failed)
        self.page.on("response", self._response)
        return self

    def __exit__(self, *_args: object) -> None:
        self.page.remove_listener("request", self._started)
        self.page.remove_listener("requestfinished", self._finished)
        self.page.remove_listener("requestfailed", self._failed)
        self.page.remove_listener("response", self._response)


async def _settled_markup(page: Page, selector: str, activity: _SourceActivity) -> None:
    previous = None
    stable = 0
    for _ in range(120):
        markup = await page.locator(selector).evaluate_all(
            "nodes => nodes.map(node => node.outerHTML).join('')"
        )
        loading = activity.loading or await page.locator(LOADING_SELECTOR).count()
        stable = stable + 1 if markup and markup == previous and not loading else 0
        if stable >= 8:
            return
        previous = markup
        await page.wait_for_timeout(250)
    error = SourcePageError("source content did not stabilize")
    error.source_html = await page.content()
    raise error


async def _visible(locator: Locator) -> bool:
    return bool(await locator.count()) and await locator.is_visible()


async def _settled_news_block(
    page: Page,
    block: Locator,
    activity: _SourceActivity,
    before_ids: frozenset[str] | None = None,
) -> tuple[frozenset[str], bool]:
    # The More control disappears while XHR is in flight. Its absence alone is
    # never a terminal signal; wait for source AJAX and a stable DOM.
    previous = None
    stable = 0
    for _ in range(120):
        ids = await _source_ids(block)
        more_visible = await _visible(block.get_by_text("More", exact=True).last)
        loading = activity.loading or bool(await block.locator(LOADING_SELECTOR).count())
        state = (ids, more_visible)
        stable = stable + 1 if state == previous and not loading else 0
        # After a click, an absent button without new IDs may still be a delayed render.
        # Without explicit empty-response evidence, leave that case retryable at the deadline.
        progressed = before_ids is None or bool(ids - before_ids)
        if stable >= 8 and progressed:
            if (
                ids == await _source_ids(block)
                and more_visible == await _visible(block.get_by_text("More", exact=True).last)
                and not await block.locator(LOADING_SELECTOR).count()
                and not activity.loading
            ):
                return ids, not more_visible
            stable = 0
        previous = state
        await page.wait_for_timeout(250)
    raise SourcePageError("news continuation did not stabilize")


class BrowserSession:
    def __init__(self, cdp_url: str, news_retry_delay: float = 1.0) -> None:
        self.cdp_url = cdp_url
        self.news_retry_delay = news_retry_delay
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.calendar_page: Page | None = None
        self.news_page: Page | None = None
        self._connect_lock = asyncio.Lock()
        self._calendar_lock = asyncio.Lock()
        self._news_lock = asyncio.Lock()

    async def connect(self) -> None:
        if self.browser and self.browser.is_connected():
            return
        async with self._connect_lock:
            if self.browser and self.browser.is_connected():
                return
            await self._clear_disconnected_connection()
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.connect_over_cdp(self.cdp_url)
            context = self.browser.contexts[0]
            self.calendar_page = await context.new_page()
            self.news_page = await context.new_page()

    async def _clear_disconnected_connection(self) -> None:
        self.calendar_page = None
        self.news_page = None
        self.browser = None
        if self.playwright:
            with suppress(Exception):
                await self.playwright.stop()
        self.playwright = None

    async def calendar_html(self, day: date) -> str:
        await self.connect()
        async with self._calendar_lock:
            assert self.calendar_page is not None
            with _SourceActivity(self.calendar_page) as activity:
                slug = f"{day:%b}{day.day}.{day.year}".lower()
                await self.calendar_page.goto(
                    f"https://www.forexfactory.com/calendar?day={slug}",
                    wait_until="domcontentloaded",
                )
                await self.calendar_page.wait_for_selector(
                    ".calendar__row--day-breaker, tr.calendar__row[data-event-id]",
                    state="attached",
                    timeout=20_000,
                )
                return await self._calendar_capture(day, activity)

    async def _calendar_capture(self, day: date, activity: _SourceActivity) -> str:
        assert self.calendar_page is not None
        previous = None
        stable = 0
        error = SourcePageError("calendar capture is incomplete")
        for _ in range(80):
            html = await self.calendar_page.content()
            try:
                rows = parse_calendar(
                    html,
                    datetime.combine(day, datetime.min.time(), UTC),
                    source_timezone=UTC,
                    expected_date=day,
                    require_source_payload=True,
                    validate_timezone=False,
                )
            except SourcePageError as cause:
                error = cause
                stable = 0
            else:
                loading = (
                    activity.loading or await self.calendar_page.locator(LOADING_SELECTOR).count()
                )
                stable = stable + 1 if rows == previous and not loading else 0
                if stable >= 8:
                    return html
                previous = rows
            await self.calendar_page.wait_for_timeout(250)
        # Keep the last source evidence available to the collector snapshot hook.
        error.source_html = html
        raise error

    async def calendar_detail_html(self, day: date, source_id: str) -> str:
        await self.connect()
        async with self._calendar_lock:
            assert self.calendar_page is not None
            with _SourceActivity(self.calendar_page) as activity:
                slug = f"{day:%b}{day.day}.{day.year}".lower()
                await self.calendar_page.goto(
                    f"https://www.forexfactory.com/calendar?day={slug}#detail={source_id}",
                    wait_until="domcontentloaded",
                )
                await self.calendar_page.wait_for_selector(
                    "tr.calendar__details--detail", state="attached", timeout=20_000
                )
                await _settled_markup(self.calendar_page, "tr.calendar__details--detail", activity)
                return await self.calendar_page.content()

    async def calendar_details_html(
        self, day: date, source_ids: Sequence[str]
    ) -> dict[str, str | None]:
        await self.connect()
        async with self._calendar_lock:
            assert self.calendar_page is not None
            with _SourceActivity(self.calendar_page) as activity:
                slug = f"{day:%b}{day.day}.{day.year}".lower()
                await self.calendar_page.goto(
                    f"https://www.forexfactory.com/calendar?day={slug}",
                    wait_until="domcontentloaded",
                )
                await self.calendar_page.wait_for_selector(
                    ".calendar__row--day-breaker, tr.calendar__row[data-event-id]",
                    state="attached",
                    timeout=20_000,
                )
                await self._calendar_capture(day, activity)
                expanded: list[str] = []
                unavailable: list[str] = []
                details = self.calendar_page.locator("tr.calendar__details--detail")
                for source_id in dict.fromkeys(source_ids):
                    if not re.fullmatch(r"\d+", source_id):
                        continue
                    event = self.calendar_page.locator(
                        f"tr.calendar__row[data-event-id='{source_id}']"
                    )
                    if not await event.count():
                        continue
                    link = self.calendar_page.locator(
                        f"tr.calendar__row[data-event-id='{source_id}'] "
                        ".calendar__cell.calendar__detail .calendar__detail-link"
                    )
                    if not await link.count():
                        unavailable.append(source_id)
                        continue
                    before = await details.count()
                    await link.click()
                    for _ in range(100):
                        if await details.count() > before:
                            expanded.append(source_id)
                            break
                        await self.calendar_page.wait_for_timeout(200)
                await _settled_markup(
                    self.calendar_page, "tr.calendar__row, tr.calendar__details--detail", activity
                )
                html = await self.calendar_page.content()
                pages: dict[str, str | None] = {source_id: html for source_id in expanded}
                pages.update({source_id: None for source_id in unavailable})
                return CalendarDetailPages(pages, html)

    async def news_html(self) -> str:
        await self.connect()
        async with self._news_lock:
            first_error: Exception | None = None
            for attempt in range(2):
                try:
                    html = await self._news_html_once()
                    if attempt:
                        logger.info("News listing page recovered after page replacement")
                    return html
                except Exception as error:
                    if attempt:
                        if not getattr(error, "source_html", None) and first_error is not None:
                            error.source_html = getattr(first_error, "source_html", None)
                        raise
                    first_error = error
                    logger.warning(
                        "News listing page capture failed; replacing page and retrying "
                        "error_type=%s message=%s",
                        type(error).__name__,
                        str(error),
                    )
                    try:
                        await self._replace_news_page()
                    except Exception as recovery_error:
                        detail = str(recovery_error).strip()
                        suffix = f": {detail}" if detail else ""
                        captured = SourcePageError(
                            "news listing page recovery failed "
                            f"({type(recovery_error).__name__}){suffix}"
                        )
                        captured.source_html = getattr(first_error, "source_html", None)
                        raise captured from recovery_error
                    if self.news_retry_delay:
                        await asyncio.sleep(self.news_retry_delay)
            raise AssertionError("unreachable")

    async def _news_html_once(self) -> str:
        assert self.news_page is not None
        page = self.news_page
        html: str | None = None
        try:
            await page.goto(
                "https://www.forexfactory.com/news", wait_until="domcontentloaded"
            )
            html = await page.content()
            await page.wait_for_selector(
                ".news-block__item, .news__item", state="attached", timeout=30_000
            )
            html = await page.content()
            reject_challenge(html)
            return html
        except Exception as error:
            with suppress(Exception):
                html = await page.content()
            if html and not isinstance(error, SourcePageError):
                try:
                    reject_challenge(html)
                except SourcePageError as classified:
                    classified.source_html = html
                    raise classified from error
            if isinstance(error, SourcePageError):
                captured = error
            else:
                detail = str(error).strip()
                suffix = f": {detail}" if detail else ""
                captured = SourcePageError(
                    f"news listing capture failed ({type(error).__name__}){suffix}"
                )
            if html:
                captured.source_html = html
            raise captured from error

    async def _replace_news_page(self) -> None:
        old_page = self.news_page
        self.news_page = None
        if old_page:
            with suppress(Exception):
                await old_page.close()
        if self.browser and self.browser.is_connected():
            try:
                self.news_page = await self.browser.contexts[0].new_page()
                return
            except Exception as error:
                logger.warning(
                    "News page replacement failed; reconnecting CDP error_type=%s message=%s",
                    type(error).__name__,
                    str(error),
                )
        await self._clear_disconnected_connection()
        await self.connect()

    async def news_detail_html(self, url: str, expected_comment_count: int | None = None) -> str:
        capture = await self._news_detail_capture(
            url, expected_comment_count, expand_comments=expected_comment_count is not None
        )
        return capture.html

    async def news_comments_html(
        self, url: str, expected_comment_count: int | None = None
    ) -> NewsCommentCapture:
        return await self._news_detail_capture(url, expected_comment_count, expand_comments=True)

    async def _news_detail_capture(
        self,
        url: str,
        expected_comment_count: int | None,
        *,
        expand_comments: bool,
    ) -> NewsCommentCapture:
        await self.connect()
        assert self.browser is not None
        page = await self.browser.contexts[0].new_page()
        try:
            with _SourceActivity(page) as activity:
                response = await page.goto(url, wait_until="domcontentloaded")
                notice = page.locator(".error__body")
                if await _visible(notice):
                    text = " ".join((await notice.inner_text()).lower().split())
                    if "only accessible to registered traders" in text:
                        status = response.status if response is not None else None
                        error = SourceAccessRestrictedError(
                            f"source article requires registered-trader login (HTTP {status})"
                        )
                        error.source_html = await page.content()
                        raise error
                await page.wait_for_selector(".news__article", state="attached", timeout=20_000)
                if not expand_comments:
                    await _settled_markup(page, ".news__article", activity)
                    return NewsCommentCapture(await page.content(), 0, 0)
                await page.wait_for_selector(".news-comments", state="attached", timeout=5_000)
                await _settled_markup(page, ".news-comments", activity)
                more = page.locator(".news-comments .foot li.more a")
                comments = page.locator(".news-comments__list .news-comment")
                declared_count = expected_comment_count or 0
                has_more = await _visible(more)
                if has_more:
                    text = await more.inner_text()
                    match = re.search(r"([\d,]+)\s+Comments?", text, re.I)
                    if match:
                        declared_count = int(match.group(1).replace(",", ""))
                initial_count = await comments.count()
                if has_more:
                    await more.click()
                previous = None
                stable_reads = 0
                source_exhausted = False
                collected_count = initial_count
                for _ in range(120):
                    current = await comments.count()
                    markup = await comments.evaluate_all(
                        "nodes => nodes.map(node => node.outerHTML).join('')"
                    )
                    state = (current, markup)
                    loading = activity.loading or bool(await page.locator(LOADING_SELECTOR).count())
                    stable_reads = stable_reads + 1 if state == previous and not loading else 0
                    more_visible = await _visible(more)
                    progressed = not has_more or current > initial_count
                    if stable_reads >= 8 and progressed and more_visible:
                        initial_count = current
                        has_more = True
                        await more.click()
                        stable_reads = 0
                    elif stable_reads >= 8 and progressed and not more_visible:
                        if (
                            current == await comments.count()
                            and markup
                            == await comments.evaluate_all(
                                "nodes => nodes.map(node => node.outerHTML).join('')"
                            )
                            and not await _visible(more)
                            and not await page.locator(LOADING_SELECTOR).count()
                            and not activity.loading
                        ):
                            source_exhausted = True
                            collected_count = current
                            break
                    collected_count = current
                    previous = state
                    await page.wait_for_timeout(250)
                if not has_more:
                    declared_count = max(expected_comment_count or 0, collected_count)
                return NewsCommentCapture(
                    html=await page.content(),
                    declared_count=declared_count or collected_count,
                    collected_count=collected_count,
                    source_exhausted=source_exhausted,
                )
        finally:
            await page.close()

    async def news_more_html(
        self, section_slug: str, continuation_count: int
    ) -> NewsContinuationPage:
        if section_slug not in NEWS_SECTION_HEADINGS:
            raise ValueError(f"unsupported news section: {section_slug}")
        if continuation_count < 0:
            raise ValueError("continuation_count must not be negative")
        await self.connect()
        async with self._news_lock:
            assert self.news_page is not None
            with _SourceActivity(self.news_page) as activity:
                await self.news_page.goto(
                    "https://www.forexfactory.com/news", wait_until="domcontentloaded"
                )
                await self.news_page.wait_for_selector(".news-block", timeout=20_000)
                blocks = self.news_page.locator(".news-block")
                block: Locator | None = None
                for index in range(await blocks.count()):
                    candidate = blocks.nth(index)
                    heading = candidate.locator("h2").first
                    expected_heading = NEWS_SECTION_HEADINGS[section_slug]
                    if (
                        await heading.count()
                        and (await heading.inner_text()).strip() == expected_heading
                    ):
                        block = candidate
                        break
                if block is None:
                    raise SourcePageError(f"news section missing: {section_slug}")

                source_ids, terminal = await _settled_news_block(self.news_page, block, activity)
                completed = 0
                for _ in range(continuation_count):
                    if terminal:
                        break
                    more = block.get_by_text("More", exact=True).last
                    before = source_ids
                    # Category controls prefetch on hover and can ignore a click
                    # while that response is still loading.
                    await more.hover()
                    await _settled_news_block(self.news_page, block, activity)
                    await more.click()
                    source_ids, terminal = await _settled_news_block(
                        self.news_page, block, activity, before_ids=before
                    )
                    if source_ids - before:
                        completed += 1
                    elif not terminal:
                        raise SourcePageError(
                            f"news continuation added no source IDs: {section_slug}"
                        )
                return NewsContinuationPage(
                    html=await self.news_page.content(),
                    continuation_count=completed,
                    source_ids=source_ids,
                    terminal=terminal,
                )

    async def close(self) -> None:
        for page in (self.calendar_page, self.news_page):
            if page:
                await page.close()
        self.calendar_page = None
        self.news_page = None
        if self.browser:
            await self.browser.close()
        self.browser = None
        if self.playwright:
            await self.playwright.stop()
        self.playwright = None
