import asyncio
from datetime import date

import pytest

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
        self.max_active_navigations = max(self.max_active_navigations, self.active_navigations)
        await asyncio.sleep(0.01)
        self.active_navigations -= 1

    async def wait_for_selector(self, _selector: str, **_kwargs) -> None:
        return None

    async def content(self) -> str:
        return "<html></html>"


class CommentLocator:
    def __init__(self, page: "CommentPage", selector: str) -> None:
        self.page = page
        self.selector = selector

    async def count(self) -> int:
        if self.selector == ".news-comments .foot li.more a":
            return int(not self.page.expanded)
        if self.selector == ".news-comments__list .news-comment":
            self.page.count_reads += 1
            if self.page.expanded and self.page.count_reads > self.page.expansion_delay_reads:
                return self.page.expanded_count
            return self.page.initial_count
        return 0

    async def click(self) -> None:
        assert self.selector == ".news-comments .foot li.more a"
        self.page.expanded = True
        self.page.clicks += 1

    async def inner_text(self) -> str:
        if self.selector == ".news-comments .foot li.more a":
            return "Show All 184 Comments"
        return ""


class CommentPage:
    def __init__(self) -> None:
        self.expanded = False
        self.clicks = 0
        self.count_reads = 0
        self.expansion_delay_reads = 0
        self.initial_count = 64
        self.expanded_count = 184
        self.comments_available = True
        self.closed = False

    async def goto(self, _url: str, **_kwargs) -> None:
        return None

    async def wait_for_selector(self, selector: str, **_kwargs) -> None:
        if selector == ".news-comments" and not self.comments_available:
            raise TimeoutError("comments missing")
        return None

    def locator(self, selector: str) -> CommentLocator:
        return CommentLocator(self, selector)

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    async def content(self) -> str:
        return "<html>184 comments</html>" if self.expanded else "<html>64 comments</html>"

    async def close(self) -> None:
        self.closed = True


class CommentContext(FakeContext):
    def __init__(self, page: CommentPage) -> None:
        super().__init__()
        self.page = page

    async def new_page(self) -> CommentPage:
        self.created += 1
        return self.page


class CalendarDetailLocator:
    def __init__(self, page: "CalendarBatchPage", selector: str) -> None:
        self.page = page
        self.selector = selector

    async def count(self) -> int:
        if self.selector == "tr.calendar__details--detail":
            return len(self.page.expanded)
        return 1

    async def click(self) -> None:
        marker = "data-event-id='"
        source_id = self.selector.split(marker, 1)[1].split("'", 1)[0]
        self.page.expanded.append(source_id)


class CalendarBatchPage:
    def __init__(self) -> None:
        self.goto_calls = 0
        self.expanded: list[str] = []

    async def goto(self, _url: str, **_kwargs) -> None:
        self.goto_calls += 1

    async def wait_for_selector(self, _selector: str, **_kwargs) -> None:
        return None

    def locator(self, selector: str) -> CalendarDetailLocator:
        return CalendarDetailLocator(self, selector)

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    async def content(self) -> str:
        return "<html>" + ",".join(self.expanded) + "</html>"


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


async def test_news_detail_expands_all_comments_before_capturing_html() -> None:
    page = CommentPage()
    context = CommentContext(page)
    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(context)  # type: ignore[assignment]

    html = await session.news_detail_html("https://example.test/news/1", 184)

    assert html == "<html>184 comments</html>"
    assert page.clicks == 1
    assert page.count_reads >= 2
    assert page.closed is True


async def test_article_detail_capture_does_not_expand_comments_without_expected_count() -> None:
    page = CommentPage()
    context = CommentContext(page)
    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(context)  # type: ignore[assignment]

    html = await session.news_detail_html("https://example.test/news/1")

    assert html == "<html>64 comments</html>"
    assert page.clicks == 0


async def test_news_comment_capture_reports_source_declared_count() -> None:
    page = CommentPage()
    context = CommentContext(page)
    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(context)  # type: ignore[assignment]

    capture = await session.news_comments_html("https://example.test/news/1", 0)

    assert capture.declared_count == 184
    assert capture.collected_count == 184


async def test_news_comment_capture_waits_for_delayed_show_all_result() -> None:
    page = CommentPage()
    page.expansion_delay_reads = 5
    context = CommentContext(page)
    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(context)  # type: ignore[assignment]

    capture = await session.news_comments_html("https://example.test/news/1", 184)

    assert capture.declared_count == 184
    assert capture.collected_count == 184


async def test_news_comment_capture_uses_stable_dom_count_when_no_more_button_exists() -> None:
    page = CommentPage()
    page.expanded = True
    context = CommentContext(page)
    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(context)  # type: ignore[assignment]

    capture = await session.news_comments_html("https://example.test/news/1", 50)

    assert capture.declared_count == 184
    assert capture.collected_count == 184


async def test_news_comment_capture_keeps_higher_expected_count_without_more_button() -> None:
    page = CommentPage()
    page.expanded = True
    page.expanded_count = 64
    context = CommentContext(page)
    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(context)  # type: ignore[assignment]

    capture = await session.news_comments_html("https://example.test/news/1", 184)

    assert capture.declared_count == 184
    assert capture.collected_count == 64


async def test_news_comment_capture_fails_closed_when_comment_section_is_missing() -> None:
    page = CommentPage()
    page.comments_available = False
    context = CommentContext(page)
    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(context)  # type: ignore[assignment]

    with pytest.raises(TimeoutError, match="comments missing"):
        await session.news_comments_html("https://example.test/news/1", 184)


async def test_calendar_detail_batch_uses_one_day_navigation() -> None:
    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(FakeContext())  # type: ignore[assignment]
    page = CalendarBatchPage()
    session.calendar_page = page  # type: ignore[assignment]

    pages = await session.calendar_details_html(date(2026, 9, 7), ["148126", "149662", "151187"])

    assert page.goto_calls == 1
    assert page.expanded == ["148126", "149662", "151187"]
    assert set(pages) == {"148126", "149662", "151187"}
    assert all(html == "<html>148126,149662,151187</html>" for html in pages.values())
