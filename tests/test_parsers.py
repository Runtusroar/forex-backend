from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.parsers import (
    ChallengePageError,
    SourcePageError,
    parse_calendar,
    parse_calendar_detail,
    parse_news_detail,
    parse_news_listing,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_calendar_reads_ids_values_and_grouped_rows() -> None:
    rows = parse_calendar(fixture("calendar.html"), datetime(2026, 9, 1, tzinfo=UTC))

    assert [row.source_id for row in rows] == ["1001", "1002"]
    assert rows[0].event_at == rows[1].event_at
    assert rows[1].currency == "USD"
    assert rows[1].impact == "medium"
    assert (rows[1].actual, rows[1].forecast, rows[1].previous) == ("0.2%", "0.1%", "-0.3%")


def test_calendar_skips_current_day_breakers_and_converts_source_timezone() -> None:
    rows = parse_calendar(
        fixture("calendar_current.html"),
        datetime(2026, 9, 1, tzinfo=UTC),
        source_timezone=timezone(timedelta(hours=8)),
    )

    assert len(rows) == 1
    assert rows[0].source_id == "149673"
    assert rows[0].event_at == datetime(2026, 8, 30, 23, 50, tzinfo=UTC)
    assert rows[0].title_en == "Prelim Industrial Production m/m"


def test_calendar_accepts_a_validated_empty_requested_day() -> None:
    rows = parse_calendar(
        """
        <table><tr class="calendar__row calendar__row--day-breaker">
          <td>Sun <span>Sep 6</span></td>
        </tr></table>
        <script>window.calendarComponentStates[1] = {days: [{"date":"Sep 6","events":[]}]};</script>
        """,
        datetime(2026, 9, 4, tzinfo=UTC),
        source_timezone=timezone(timedelta(hours=8)),
        expected_date=date(2026, 9, 6),
        require_source_payload=True,
    )

    assert rows == []


def test_calendar_rejects_a_page_without_the_requested_day() -> None:
    with pytest.raises(SourcePageError, match="requested day"):
        parse_calendar(
            """
            <table><tr class="calendar__row calendar__row--day-breaker">
              <td>Sun <span>Sep 6</span></td>
            </tr></table>
            """,
            datetime(2026, 9, 4, tzinfo=UTC),
            source_timezone=timezone(timedelta(hours=8)),
            expected_date=date(2026, 9, 7),
        )


def test_calendar_preserves_non_clock_source_labels() -> None:
    rows = parse_calendar(
        """
        <table>
          <tr class="calendar__row calendar__row--day-breaker"><td>Wed Sep 9</td></tr>
          <tr class="calendar__row" data-event-id="153363">
            <td class="calendar__date">Wed Sep 9</td>
            <td class="calendar__time">8:15pm</td>
            <td class="calendar__currency">USD</td>
            <td class="calendar__impact"><span class="icon--ff-impact-yel"></span></td>
            <td class="calendar__event">ADP Weekly Employment Change</td>
          </tr>
          <tr class="calendar__row" data-event-id="151045">
            <td class="calendar__date"></td>
            <td class="calendar__time">Aug 23rd</td>
            <td class="calendar__currency">USD</td>
            <td class="calendar__impact"><span class="icon--ff-impact-yel"></span></td>
            <td class="calendar__event">ADP Weekly Employment Change</td>
            <td class="calendar__previous">11.8K</td>
          </tr>
        </table>
        """,
        datetime(2026, 9, 4, tzinfo=UTC),
        source_timezone=timezone(timedelta(hours=8)),
        expected_date=date(2026, 9, 9),
    )

    assert rows[0].source_time_text is None
    assert rows[0].source_position == 0
    assert rows[1].source_time_text == "Aug 23rd"
    assert rows[1].source_position == 1
    assert rows[1].event_at == datetime(2026, 9, 8, 16, tzinfo=UTC)


def test_calendar_detail_reads_specs_history_and_related_stories() -> None:
    detail = parse_calendar_detail(
        fixture("calendar_detail.html"),
        "149673",
        datetime(2026, 8, 31, 12, tzinfo=UTC),
    )

    assert detail.source_id == "149673"
    assert detail.currency == "JPY"
    assert detail.currency_name == "Japanese yen"
    assert detail.title_en == "Prelim Industrial Production m/m"
    assert detail.actual_state == "better"
    assert detail.previous_revised_from == "1.3%"
    assert (
        detail.ff_url
        == "https://www.forexfactory.com/calendar/225-jn-prelim-industrial-production-mm"
    )
    assert detail.source_name == "METI"
    assert detail.source_url == "https://www.meti.go.jp/english/"
    assert (
        detail.latest_release_url == "http://www.meti.go.jp/english/statistics/tyo/iip/index.html"
    )
    assert detail.measures == "Change in total output;"
    assert detail.usual_effect == "'Actual' greater than 'Forecast' is good for currency;"
    assert detail.frequency == "Released monthly;"
    assert detail.next_release_text == "Sep 30, 2026"
    assert (
        detail.next_release_url
        == "https://www.forexfactory.com/calendar?day=sep30.2026#detail=149674"
    )
    assert detail.ff_notes == "Preliminary release tends to have the most impact;"
    assert detail.why_traders_care == "It is a leading indicator of economic health;"
    assert len(detail.history) == 2
    assert (
        detail.history[0].event_url
        == "https://www.forexfactory.com/calendar?day=aug31.2026#detail=149673"
    )
    assert detail.history[0].actual_state == "better"
    assert detail.history[0].previous_revised_from == "1.3%"
    assert len(detail.related_stories) == 1
    assert detail.related_stories[0].title_en.startswith("Japan: Indices")
    assert detail.related_stories[0].source_name == "meti.go.jp"


def test_calendar_detail_accepts_history_values_with_empty_class() -> None:
    html = fixture("calendar_detail.html").replace(
        '<td class="calendarhistory__row calendarhistory__row--actual">'
        '<span class="better">0.1%</span></td>',
        '<td class="calendarhistory__row calendarhistory__row--actual">'
        '<span class="">0.1%</span></td>',
        1,
    )

    detail = parse_calendar_detail(
        html,
        "149673",
        datetime(2026, 8, 31, 12, tzinfo=UTC),
    )

    assert detail.history[0].actual == "0.1%"
    assert detail.history[0].actual_state is None
    assert detail.history[1].actual_state == "better"


def test_calendar_detail_accepts_empty_class_inside_impact_cell() -> None:
    html = fixture("calendar_detail.html").replace(
        '<span title="Low Impact Expected" class="icon icon--ff-impact-yel"></span>',
        '<span class=""></span>'
        '<span title="Low Impact Expected" class="icon icon--ff-impact-yel"></span>',
        1,
    )

    detail = parse_calendar_detail(
        html,
        "149673",
        datetime(2026, 8, 31, 12, tzinfo=UTC),
    )

    assert detail.impact == "low"


def test_calendar_detail_selects_row_belonging_to_requested_event() -> None:
    html = """
    <table>
      <tr class="calendar__row" data-event-id="1">
        <td class="calendar__currency">USD</td>
        <td class="calendar__impact"><span class="icon--ff-impact-yel"></span></td>
        <td class="calendar__event">First</td>
      </tr>
      <tr class="calendar__row calendar__details calendar__details--detail"><td>
        <div class="overlay__title">USD First</div>
        <table class="calendarspecs"><tr><td class="calendarspecs__spec">Source</td>
          <td class="calendarspecs__specdescription">
            <a href="https://first.test">First Source</a>
          </td>
        </tr></table>
      </td></tr>
      <tr class="calendar__row" data-event-id="2">
        <td class="calendar__currency">EUR</td>
        <td class="calendar__impact"><span class="icon--ff-impact-yel"></span></td>
        <td class="calendar__event">Second</td>
      </tr>
      <tr class="calendar__row calendar__details calendar__details--detail"><td>
        <div class="overlay__title">EUR Second</div>
        <table class="calendarspecs"><tr><td class="calendarspecs__spec">Source</td>
          <td class="calendarspecs__specdescription">
            <a href="https://second.test">Second Source</a>
          </td>
        </tr></table>
      </td></tr>
    </table>
    """

    detail = parse_calendar_detail(html, "2", datetime(2026, 9, 5, tzinfo=UTC))

    assert detail.title_en == "Second"
    assert detail.source_name == "Second Source"


def test_news_relative_age_does_not_change_translatable_data() -> None:
    html = fixture("news.html")
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    first = parse_news_listing(html, now)[0]
    second = parse_news_listing(html.replace("5 min ago", "6 min ago"), now)[0]

    assert first.source_id == "9001"
    assert first.title_en == second.title_en
    assert first.summary_en == second.summary_en
    assert first.first_seen_at == second.first_seen_at


def test_news_supports_current_blocks_and_skips_comment_cards() -> None:
    rows = parse_news_listing(fixture("news_current.html"), datetime(2026, 9, 1, 12, tzinfo=UTC))

    assert len(rows) == 1
    assert rows[0].source_id == "1415933"
    assert rows[0].title_en == "Bond markets sell off"
    assert rows[0].source == "Reuters"
    assert rows[0].summary_en == "Government borrowing costs rose."
    assert rows[0].image_url == "https://assets.example/bonds.png"


def test_news_detail_supports_article_and_social_content() -> None:
    article = parse_news_detail(fixture("news_article.html"))
    social = parse_news_detail(fixture("news_social.html"))

    assert article.kind == "article"
    assert article.body_en == "The dollar advanced.\n\nYields rose."
    assert social.kind == "social"
    assert social.body_en == "US manufacturing activity expanded..."


@pytest.mark.parametrize("parser", [parse_calendar, parse_news_listing])
def test_challenge_page_is_rejected(parser) -> None:
    with pytest.raises(ChallengePageError):
        parser(fixture("challenge.html"), datetime(2026, 9, 1, tzinfo=UTC))


def test_calendar_rejects_date_only_loading_shell() -> None:
    with pytest.raises(SourcePageError, match=r"empty|incomplete"):
        parse_calendar(
            '<table><tr class="calendar__row calendar__row--day-breaker">'
            "<td>Sep 6</td></tr></table>",
            datetime(2026, 9, 6, tzinfo=UTC),
            expected_date=date(2026, 9, 6),
        )


def test_calendar_rejects_missing_embedded_source_event() -> None:
    html = (
        fixture("calendar.html")
        + """<script>
    window.calendarComponentStates[1] = {days: [
        {"date":"Sep 1","events":[{"id":1001},{"id":1002},{"id":1003}]}]};
    </script>"""
    )
    with pytest.raises(SourcePageError, match="incomplete"):
        parse_calendar(html, datetime(2026, 9, 1, tzinfo=UTC), expected_date=date(2026, 9, 1))


def test_calendar_source_date_is_independent_of_timestamp_timezone() -> None:
    html = fixture("calendar.html").replace(
        'data-timestamp="1788265800"', 'data-timestamp="1788190000"'
    )
    rows = parse_calendar(html, datetime(2026, 9, 1, tzinfo=UTC), expected_date=date(2026, 9, 1))
    assert rows[0].source_date == date(2026, 9, 1)


@pytest.mark.parametrize(("day", "count"), [(date(2026, 9, 1), 39), (date(2026, 9, 2), 16)])
def test_calendar_replays_audited_source_with_all_ids(day, count) -> None:
    rows = parse_calendar(
        fixture(f"calendar_source_{day}.html"),
        datetime(2026, 9, 5, tzinfo=UTC),
        expected_date=day,
    )
    assert len({row.source_id for row in rows}) == count
    assert {row.source_date for row in rows} == {day}


def test_calendar_rejects_audited_source_with_one_missing_row() -> None:
    from selectolax.parser import HTMLParser

    tree = HTMLParser(fixture("calendar_source_2026-09-01.html"))
    tree.css_first("tr.calendar__row[data-event-id]").decompose()
    with pytest.raises(SourcePageError, match="incomplete"):
        parse_calendar(tree.html, datetime(2026, 9, 5, tzinfo=UTC), expected_date=date(2026, 9, 1))


def test_calendar_detail_loading_row_is_retryable() -> None:
    with pytest.raises(SourcePageError, match="incomplete"):
        parse_calendar_detail(
            '<table><tr class="calendar__row" data-event-id="1">'
            '<td class="calendar__event">Event</td><td class="calendar__impact"></td></tr>'
            '<tr class="calendar__details--detail"><td>Loading...</td></tr></table>',
            "1",
            datetime(2026, 9, 1, tzinfo=UTC),
        )


def test_calendar_source_date_without_expected_day_uses_label() -> None:
    html = fixture("calendar.html").replace(
        'data-timestamp="1788265800"', 'data-timestamp="1788190000"'
    )
    rows = parse_calendar(html, datetime(2026, 9, 1, tzinfo=UTC), source_timezone=UTC)
    assert rows[0].source_date == date(2026, 9, 1)


def test_calendar_explicit_historical_date_controls_year() -> None:
    rows = parse_calendar(
        fixture("calendar.html").replace('data-timestamp="1788265800"', ""),
        datetime(2026, 9, 5, tzinfo=UTC),
        source_timezone=UTC,
        expected_date=date(2025, 9, 1),
    )
    assert rows[0].event_at.year == 2025
