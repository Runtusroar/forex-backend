from playwright.async_api import Browser, Page, Playwright, async_playwright


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
        await self.calendar_page.wait_for_selector("tr.calendar__row", timeout=20_000)
        return await self.calendar_page.content()

    async def news_html(self) -> str:
        await self.connect()
        assert self.news_page is not None
        await self.news_page.goto(
            "https://www.forexfactory.com/news", wait_until="domcontentloaded"
        )
        await self.news_page.wait_for_selector(".news__item", timeout=20_000)
        return await self.news_page.content()

    async def news_detail_html(self, url: str) -> str:
        await self.connect()
        assert self.browser is not None
        page = await self.browser.contexts[0].new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_selector(".news__article", timeout=20_000)
            return await page.content()
        finally:
            await page.close()

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
