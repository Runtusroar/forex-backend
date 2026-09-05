from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from playwright.async_api import Browser, Locator, Page, Playwright, async_playwright

from app.parsers.errors import SourcePageError

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


async def _source_ids(block: Locator) -> frozenset[str]:
    hrefs = await block.locator("a[href*='/news/']").evaluate_all(
        "links => links.map(link => link.getAttribute('href') || '')"
    )
    return frozenset(match.group(1) for href in hrefs if (match := re.search(r"/news/(\d+)", href)))


class BrowserSession:
    def __init__(self, cdp_url: str) -> None:
        self.cdp_url = cdp_url
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
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.connect_over_cdp(self.cdp_url)
            context = self.browser.contexts[0]
            self.calendar_page = await context.new_page()
            self.news_page = await context.new_page()

    async def calendar_html(self, day: date) -> str:
        await self.connect()
        async with self._calendar_lock:
            assert self.calendar_page is not None
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
            return await self.calendar_page.content()

    async def calendar_detail_html(self, day: date, source_id: str) -> str:
        await self.connect()
        async with self._calendar_lock:
            assert self.calendar_page is not None
            slug = f"{day:%b}{day.day}.{day.year}".lower()
            await self.calendar_page.goto(
                f"https://www.forexfactory.com/calendar?day={slug}#detail={source_id}",
                wait_until="domcontentloaded",
            )
            await self.calendar_page.wait_for_selector(
                "tr.calendar__details--detail", state="attached", timeout=20_000
            )
            return await self.calendar_page.content()

    async def calendar_details_html(
        self, day: date, source_ids: Sequence[str]
    ) -> dict[str, str | None]:
        await self.connect()
        async with self._calendar_lock:
            assert self.calendar_page is not None
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
            expanded: list[str] = []
            unavailable: list[str] = []
            details = self.calendar_page.locator("tr.calendar__details--detail")
            for source_id in dict.fromkeys(source_ids):
                if not re.fullmatch(r"\d+", source_id):
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
                for _ in range(20):
                    if await details.count() > before:
                        expanded.append(source_id)
                        break
                    await self.calendar_page.wait_for_timeout(100)
            html = await self.calendar_page.content()
            pages: dict[str, str | None] = {source_id: html for source_id in expanded}
            pages.update({source_id: None for source_id in unavailable})
            return pages

    async def news_html(self) -> str:
        await self.connect()
        async with self._news_lock:
            assert self.news_page is not None
            await self.news_page.goto(
                "https://www.forexfactory.com/news", wait_until="domcontentloaded"
            )
            await self.news_page.wait_for_selector(
                ".news-block__item, .news__item", state="attached", timeout=20_000
            )
            return await self.news_page.content()

    async def news_detail_html(self, url: str, expected_comment_count: int | None = None) -> str:
        capture = await self._news_detail_capture(
            url, expected_comment_count, expand_comments=expected_comment_count is not None
        )
        return capture.html

    async def news_comments_html(
        self, url: str, expected_comment_count: int | None = None
    ) -> NewsCommentCapture:
        return await self._news_detail_capture(
            url, expected_comment_count, expand_comments=True
        )

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
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_selector(".news__article", state="attached", timeout=20_000)
            if not expand_comments:
                return NewsCommentCapture(await page.content(), 0, 0)
            await page.wait_for_selector(".news-comments", state="attached", timeout=5_000)
            more = page.locator(".news-comments .foot li.more a")
            declared_count = expected_comment_count or 0
            has_more = bool(await more.count())
            if has_more:
                text = await more.inner_text()
                match = re.search(r"([\d,]+)\s+Comments?", text, re.I)
                if match:
                    declared_count = int(match.group(1).replace(",", ""))
                await more.click()
            comments = page.locator(".news-comments__list .news-comment")
            previous = -1
            stable_reads = 0
            target_count = declared_count if has_more else 0
            for _ in range(60):
                current = await comments.count()
                if current == previous:
                    stable_reads += 1
                else:
                    stable_reads = 0
                reached_target = target_count > 0 and current >= target_count
                if (reached_target and stable_reads >= 1) or (
                    target_count == 0 and stable_reads >= 2
                ):
                    break
                previous = current
                await page.wait_for_timeout(250)
            collected_count = await comments.count()
            if not has_more:
                declared_count = max(expected_comment_count or 0, collected_count)
            return NewsCommentCapture(
                html=await page.content(),
                declared_count=declared_count or collected_count,
                collected_count=collected_count,
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

            source_ids = await _source_ids(block)
            terminal = False
            completed = 0
            for _ in range(continuation_count):
                more = block.get_by_text("More", exact=True).last
                if not await more.count() or not await more.is_visible():
                    terminal = True
                    break
                before = source_ids
                await more.click()
                for _ in range(20):
                    await asyncio.sleep(0.25)
                    source_ids = await _source_ids(block)
                    if source_ids - before:
                        completed += 1
                        break
                    if not await more.is_visible():
                        terminal = True
                        break
                else:
                    raise SourcePageError(f"news continuation added no source IDs: {section_slug}")
                if terminal:
                    break
            if not terminal:
                more = block.get_by_text("More", exact=True).last
                terminal = not await more.count() or not await more.is_visible()
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
