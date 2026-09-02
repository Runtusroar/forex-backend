import asyncio

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
