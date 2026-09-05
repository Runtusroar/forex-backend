import asyncio
from datetime import date

from app.collector import browser as browser_module
from app.collector.browser import BrowserSession


class FakeContext:
    def __init__(self) -> None:
        self.created = 0

    async def new_page(self):
        await asyncio.sleep(0)
        self.created += 1
        return object()


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self.contexts = [context]

    def is_connected(self) -> bool:
        return True


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.connects = 0

    async def connect_over_cdp(self, _url: str) -> FakeBrowser:
        self.connects += 1
        await asyncio.sleep(0)
        return self.browser


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium


class FakeStarter:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright
        self.starts = 0

    async def start(self) -> FakePlaywright:
        self.starts += 1
        await asyncio.sleep(0)
        return self.playwright


class ConcurrencyProbePage:
    def __init__(self) -> None:
        self.active_navigations = 0
        self.max_active_navigations = 0

    async def goto(self, _url: str, **_kwargs) -> None:
        self.active_navigations += 1
        self.max_active_navigations = max(
            self.max_active_navigations, self.active_navigations
        )
        await asyncio.sleep(0.01)
        self.active_navigations -= 1

    async def wait_for_selector(self, _selector: str, **_kwargs) -> None:
        return None

    async def content(self) -> str:
        return "<html></html>"


async def test_concurrent_connect_creates_only_one_page_pair(monkeypatch) -> None:
    context = FakeContext()
    chromium = FakeChromium(FakeBrowser(context))
    starter = FakeStarter(FakePlaywright(chromium))
    monkeypatch.setattr(browser_module, "async_playwright", lambda: starter)
    session = BrowserSession("http://chrome:9222")

    await asyncio.gather(session.connect(), session.connect())

    assert starter.starts == 1
    assert chromium.connects == 1
    assert context.created == 2


async def test_calendar_navigation_is_serialized_on_shared_page() -> None:
    context = FakeContext()
    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(context)  # type: ignore[assignment]
    page = ConcurrencyProbePage()
    session.calendar_page = page  # type: ignore[assignment]

    await asyncio.gather(
        session.calendar_html(date(2026, 9, 1)),
        session.calendar_html(date(2026, 9, 2)),
    )

    assert page.max_active_navigations == 1


async def test_news_navigation_is_serialized_on_shared_page() -> None:
    context = FakeContext()
    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(context)  # type: ignore[assignment]
    page = ConcurrencyProbePage()
    session.news_page = page  # type: ignore[assignment]

    await asyncio.gather(session.news_html(), session.news_html())

    assert page.max_active_navigations == 1
