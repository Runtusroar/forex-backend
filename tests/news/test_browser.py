import asyncio
from datetime import date, datetime

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
        self.day = date(2026, 9, 1)

    async def goto(self, _url: str, **_kwargs) -> None:
        if "?day=" in _url:
            self.day = datetime.strptime(_url.split("?day=")[1], "%b%d.%Y").date()
        self.active_navigations += 1
        self.max_active_navigations = max(self.max_active_navigations, self.active_navigations)
        await asyncio.sleep(0.01)
        self.active_navigations -= 1

    async def wait_for_selector(self, _selector: str, **_kwargs) -> None:
        return None

    async def wait_for_timeout(self, _milliseconds):
        pass

    async def wait_for_load_state(self, *_args, **_kwargs):
        pass

    async def content(self) -> str:
        return (
            f"<table>"
            f'<tr class="calendar__row">'
            f"<td>{self.day:%b} {self.day.day}</td>"
            f"</tr>"
            f"</table>"
            "<script>window.calendarComponentStates[1] = {days: ["
            f'{{"date":"{self.day:%b} {self.day.day}","events":[]}}]}};</script>'
        )


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

    async def evaluate_all(self, _expression):
        return "comments"

    async def is_visible(self) -> bool:
        return bool(await self.count())

    async def inner_text(self) -> str:
        if self.selector == ".news-comments .foot li.more a":
            return f"Show All {self.page.declared_count} Comments"
        return ""


class CommentPage:
    def __init__(self) -> None:
        self.expanded = False
        self.clicks = 0
        self.count_reads = 0
        self.expansion_delay_reads = 0
        self.initial_count = 64
        self.expanded_count = 184
        self.declared_count = 184
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

    async def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None

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
        if "data-event-id='" in self.selector:
            source_id = self.selector.split("data-event-id='", 1)[1].split("'", 1)[0]
            if self.selector.endswith(" .calendar__cell.calendar__detail .calendar__detail-link"):
                return int(
                    source_id not in self.page.unavailable and source_id not in self.page.missing
                )
            return int(source_id not in self.page.missing)
        return 1

    async def click(self) -> None:
        marker = "data-event-id='"
        source_id = self.selector.split(marker, 1)[1].split("'", 1)[0]
        self.page.expanded.append(source_id)


class CalendarBatchPage:
    def __init__(self) -> None:
        self.goto_calls = 0
        self.expanded: list[str] = []
        self.unavailable: set[str] = set()
        self.missing: set[str] = set()

    async def goto(self, _url: str, **_kwargs) -> None:
        self.goto_calls += 1

    async def wait_for_selector(self, _selector: str, **_kwargs) -> None:
        return None

    def locator(self, selector: str) -> CalendarDetailLocator:
        return CalendarDetailLocator(self, selector)

    async def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    async def content(self) -> str:
        rows = "".join(
            (
                f'<tr class="calendar__row" data-event-id="{source_id}">'
                f'<td class="calendar__date">Sep 7</td>'
                f'<td class="calendar__time">8:00am</td>'
                f'<td class="calendar__currency">USD</td>'
                f'<td class="calendar__impact">'
                f"</td>"
                f'<td class="calendar__event">Event</td>'
                f"</tr>"
            )
            for source_id in ("148126", "149662", "151187")
            if source_id not in self.missing
        )
        import json

        days = [
            {
                "date": "Sep 7",
                "events": [
                    {"id": source_id}
                    for source_id in ("148126", "149662", "151187")
                    if source_id not in self.missing
                ],
            }
        ]
        payload = (
            "<script>window.calendarComponentStates[1] = {days: " + json.dumps(days) + "};</script>"
        )
        return "<table>" + rows + "</table><!--" + ",".join(self.expanded) + "-->" + payload


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
    expected_html = await page.content()
    assert all(html == expected_html for html in pages.values())


async def test_calendar_detail_batch_marks_event_without_detail_link_unavailable() -> None:
    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(FakeContext())  # type: ignore[assignment]
    page = CalendarBatchPage()
    page.unavailable.add("149662")
    session.calendar_page = page  # type: ignore[assignment]

    pages = await session.calendar_details_html(date(2026, 9, 7), ["148126", "149662"])

    assert pages["148126"] == await page.content()
    assert pages["149662"] is None


async def test_calendar_detail_batch_missing_event_remains_retryable() -> None:
    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(FakeContext())
    page = CalendarBatchPage()
    page.missing.add("149662")
    session.calendar_page = page
    pages = await session.calendar_details_html(date(2026, 9, 7), ["148126", "149662"])
    assert "149662" not in pages


async def test_comment_capture_reports_exhaustion_despite_stale_declared_count() -> None:
    page = CommentPage()
    page.declared_count = 615
    page.expanded_count = 612
    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(CommentContext(page))
    capture = await session.news_comments_html("https://example.test/news/1", 615)
    assert capture.declared_count == 615
    assert capture.collected_count == 612
    assert capture.source_exhausted is True


async def test_comment_expansion_without_progress_is_not_source_exhaustion() -> None:
    page = CommentPage()
    page.expansion_delay_reads = 10_000
    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(CommentContext(page))
    capture = await session.news_comments_html("https://example.test/news/1", 184)
    assert capture.source_exhausted is False


class MoreLocator:
    def __init__(self, page, kind="block"):
        self.page = page
        self.kind = kind

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    def nth(self, _index):
        return self

    def locator(self, selector):
        return MoreLocator(self.page, selector)

    def get_by_text(self, _text, **_kwargs):
        return MoreLocator(self.page, "more")

    async def count(self):
        if "loading" in self.kind:
            return int(self.page.loading)
        return 1

    async def inner_text(self):
        return "News / Latest Stories"

    async def evaluate_all(self, _expression):
        return [f"/news/{i}" for i in self.page.ids]

    async def is_visible(self):
        return not self.page.loading and not self.page.terminal

    async def click(self):
        self.page.loading = True
        self.page.ticks = 0


class MorePage:
    def __init__(self):
        self.ids = {"1"}
        self.loading = False
        self.terminal = False
        self.ticks = 0

    async def goto(self, *_args, **_kwargs):
        pass

    async def wait_for_selector(self, *_args, **_kwargs):
        pass

    async def wait_for_load_state(self, *_args, **_kwargs):
        pass

    def locator(self, selector):
        return MoreLocator(self, selector)

    async def wait_for_timeout(self, _milliseconds):
        if self.loading:
            self.ticks += 1
            if self.ticks >= 16:
                self.ids.add("2")
                self.loading = False

    async def content(self):
        return ",".join(sorted(self.ids))


async def test_news_more_does_not_treat_transient_hidden_button_as_terminal(monkeypatch) -> None:
    async def tick(_seconds):
        await page.wait_for_timeout(250)

    page = MorePage()
    monkeypatch.setattr(browser_module.asyncio, "sleep", tick)
    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(FakeContext())
    session.news_page = page
    capture = await session.news_more_html("latest", 1)
    assert capture.source_ids == frozenset({"1", "2"})
    assert capture.continuation_count == 1
    assert capture.terminal is False


async def test_calendar_capture_waits_past_date_shell() -> None:
    class DelayedCalendarPage(ConcurrencyProbePage):
        def __init__(self):
            super().__init__()
            self.reads = 0

        async def content(self):
            self.reads += 1
            shell = '<table><tr class="calendar__row"><td>Sep 7</td></tr>'
            if self.reads < 5:
                return shell + "</table>"
            return shell + (
                '<tr class="calendar__row" data-event-id="1">'
                '<td class="calendar__time">8:00am</td>'
                '<td class="calendar__currency">USD</td>'
                '<td class="calendar__impact">'
                "</td>"
                '<td class="calendar__event">Event</td>'
                "</tr>"
                "</table>"
                "<script>window.calendarComponentStates[1] = "
                '{days: [{"date":"Sep 7","events":[{"id":1}]}]};</script>'
            )

        async def wait_for_timeout(self, _milliseconds):
            pass

        async def wait_for_load_state(self, *_args, **_kwargs):
            pass

    page = DelayedCalendarPage()
    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(FakeContext())
    session.calendar_page = page
    html = await session.calendar_html(date(2026, 9, 7))
    assert 'data-event-id="1"' in html


async def test_comment_capture_repeats_more_when_expansion_is_paginated() -> None:
    class PaginatedLocator(CommentLocator):
        async def count(self):
            if self.selector == ".news-comments .foot li.more a":
                return int(self.page.clicks < 2)
            if self.selector == ".news-comments__list .news-comment":
                return 64 if not self.page.clicks else 120 if self.page.clicks == 1 else 184
            return 0

    class PaginatedPage(CommentPage):
        def locator(self, selector):
            return PaginatedLocator(self, selector)

    page = PaginatedPage()
    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(CommentContext(page))
    capture = await session.news_comments_html("https://example.test/news/1", 184)
    assert capture.collected_count == 184
    assert capture.source_exhausted is True
    assert page.clicks == 2


async def test_comment_capture_waits_for_content_stability_with_unchanged_count() -> None:
    class ChangingLocator(CommentLocator):
        async def evaluate_all(self, _expression):
            self.page.text_reads += 1
            return str(min(self.page.text_reads, 12))

    class ChangingPage(CommentPage):
        text_reads = 0

        def locator(self, selector):
            return ChangingLocator(self, selector)

        async def content(self):
            return "loaded" if self.text_reads >= 12 else "placeholder"

    page = ChangingPage()
    page.expanded = True
    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(CommentContext(page))
    capture = await session.news_comments_html("https://example.test/news/1", 184)
    assert capture.html == "loaded"
    assert capture.source_exhausted is True


async def test_unavailable_calendar_details_keep_source_page_evidence() -> None:
    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(FakeContext())
    page = CalendarBatchPage()
    page.unavailable.update({"148126", "149662"})
    session.calendar_page = page
    pages = await session.calendar_details_html(date(2026, 9, 7), ["148126", "149662"])
    assert pages == {"148126": None, "149662": None}
    assert pages.source_html == await page.content()


async def test_calendar_capture_waits_for_inflight_value_update() -> None:
    class UpdatingPage(CalendarBatchPage):
        updated = False

        async def wait_for_load_state(self, *_args, **_kwargs):
            self.updated = True

        async def content(self):
            html = await super().content()
            return html.replace("Event</td>", ("Updated" if self.updated else "Pending") + "</td>")

    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(FakeContext())
    session.calendar_page = UpdatingPage()
    html = await session.calendar_html(date(2026, 9, 7))
    assert "Updated</td>" in html


async def test_calendar_browser_rejects_truncated_source_without_payload() -> None:
    from pathlib import Path

    from selectolax.parser import HTMLParser

    from app.parsers.errors import SourcePageError

    complete = (Path(__file__).parents[1] / "fixtures/calendar_source_2026-09-01.html").read_text()
    tree = HTMLParser(complete)
    for node in tree.css("script") + tree.css("tr.calendar__row[data-event-id]")[1:]:
        node.decompose()

    class TruncatedPage(CalendarBatchPage):
        async def content(self):
            return tree.html

    session = BrowserSession("http://chrome:9222")
    session.browser = FakeBrowser(FakeContext())
    session.calendar_page = TruncatedPage()
    with pytest.raises(SourcePageError, match="source payload"):
        await session.calendar_html(date(2026, 9, 1))
