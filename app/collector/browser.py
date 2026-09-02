from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

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


async def _source_ids(block: Locator) -> frozenset[str]:
    hrefs = await block.locator("a[href*='/news/']").evaluate_all(
        "links => links.map(link => link.getAttribute('href') || '')"
    )
    return frozenset(
        match.group(1)
        for href in hrefs
        if (match := re.search(r"/news/(\d+)", href))
    )


class BrowserSession:
    def __init__(self, cdp_url: str) -> None:
        self.cdp_url = cdp_url
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.calendar_page: Page | None = None
        self.news_page: Page | None = None

    async def connect(self) -> None:
        if self.browser and self.browser.is_connected():
            return
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.connect_over_cdp(self.cdp_url)
        context = self.browser.contexts[0]
        self.calendar_page = await context.new_page()
        self.news_page = await context.new_page()

    async def calendar_html(self) -> str:
        await self.connect()
        assert self.calendar_page is not None
        await self.calendar_page.goto(
            "https://www.forexfactory.com/calendar?week=this", wait_until="domcontentloaded"
        )
        await self.calendar_page.wait_for_selector(
            "tr.calendar__row", state="attached", timeout=20_000
        )
        return await self.calendar_page.content()

    async def news_html(self) -> str:
        await self.connect()
        assert self.news_page is not None
        await self.news_page.goto(
            "https://www.forexfactory.com/news", wait_until="domcontentloaded"
        )
        await self.news_page.wait_for_selector(
            ".news-block__item, .news__item", state="attached", timeout=20_000
        )
        return await self.news_page.content()

    async def news_detail_html(self, url: str) -> str:
        await self.connect()
        assert self.browser is not None
        page = await self.browser.contexts[0].new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_selector(".news__article", state="attached", timeout=20_000)
            return await page.content()
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
                raise SourcePageError(
                    f"news continuation added no source IDs: {section_slug}"
                )
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
