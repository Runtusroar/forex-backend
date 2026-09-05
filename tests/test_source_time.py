import json
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.news.detail import parse_news_comments, parse_news_detail_v2
from app.news.listing import parse_news_listing_v2
from app.parsers.calendar import parse_calendar
from app.parsers.errors import SourcePageError

NOW = datetime(2026, 9, 5, tzinfo=UTC)
SG = ZoneInfo("Asia/Singapore")
SH = ZoneInfo("Asia/Shanghai")


def metadata(zone: str) -> str:
    return f"<script>window.FF = {{timezone_name: '{zone}'}};</script>"


def calendar(clock="9:45am", epoch=None, day="Sep 1", zone="Asia/Singapore"):
    event = {"id": 150105}
    if epoch is not None:
        event["dateline"] = epoch
    payload = json.dumps([{"date": day, "events": [event]}])
    return metadata(zone) + f"""
    <table><tr class="calendar__row" data-event-id="150105">
      <td class="calendar__date">{day}</td><td class="calendar__time">{clock}</td>
      <td class="calendar__currency">CNY</td><td class="calendar__impact"></td>
      <td class="calendar__event">Manufacturing PMI</td>
    </tr></table>
    <script>window.calendarComponentStates[1] = {{days: {payload}}};</script>
    """


def listing(raw="Sep 1, 2026, 9:45am"):
    return f"""<div class="news-block"><h2>News / Latest Stories</h2>
    <div class="news-block__item"><div class="news-block__title">
    <a href="/news/1-probe">Probe</a></div><div class="news-block__details">
    <span class="nowrap" title="{raw}">ago</span></div></div></div>"""


def detail(raw="Sep 1, 2026, 9:45am"):
    return f"""<article class="news__article"><div class="x-twitter-post-preview">
    <span class="x-twitter-post-preview__text">Story</span>
    <span class="x-twitter-post-preview__datetime">{raw}</span>
    </div></article><div class="news-comments__list"><div class="news-comment">
    <a href="/news/1-probe/comment/7#post7">Permalink</a>
    <span class="news-comment__header-date"><span title="{raw}">ago</span></span>
    <div class="news-comment__comment-message">Comment</div></div></div>"""


@pytest.mark.parametrize("kind", ["calendar", "listing", "detail", "comments"])
def test_source_metadata_drift_is_rejected_before_time_can_be_stored(kind):
    with pytest.raises(SourcePageError, match="timezone"):
        if kind == "calendar":
            parse_calendar(calendar("1:45am", 1788227100, zone="UTC"), NOW, SG)
        elif kind == "listing":
            parse_news_listing_v2(metadata("UTC") + listing(), NOW, SH)
        elif kind == "detail":
            parse_news_detail_v2(metadata("UTC") + detail(), "1", NOW, SH)
        else:
            parse_news_comments(metadata("UTC") + detail(), "1", NOW, SH)


def test_calendar_structure_validation_can_skip_timezone_check():
    rows = parse_calendar(
        calendar(epoch=1788227100), NOW, UTC, date(2026, 9, 1),
        require_source_payload=True, validate_timezone=False,
    )
    assert rows[0].source_id == "150105"


def test_calendar_prefers_payload_epoch_over_rendered_clock():
    rows = parse_calendar(calendar("9:46am", 1788227100), NOW, SG)
    assert rows[0].event_at == datetime(2026, 9, 1, 1, 45, tzinfo=UTC)
    assert rows[0].source_date == date(2026, 9, 1)


def test_calendar_untimed_label_keeps_source_date_despite_payload_epoch():
    rows = parse_calendar(calendar("Tentative", 1788227100), NOW, SG)
    assert rows[0].event_at == datetime(2026, 8, 31, 16, tzinfo=UTC)
    assert rows[0].source_time_text == "Tentative"
    assert rows[0].source_date == date(2026, 9, 1)


def test_page_zone_is_used_when_calendar_config_is_omitted():
    rows = parse_calendar(calendar(), NOW)
    assert rows[0].event_at == datetime(2026, 9, 1, 1, 45, tzinfo=UTC)


def test_equal_offset_zone_names_are_accepted_and_actual_source_zone_is_recorded():
    batch = parse_news_listing_v2(metadata("Asia/Singapore") + listing(), NOW, SH)
    assert batch.articles[0].published_at == datetime(2026, 9, 1, 1, 45, tzinfo=UTC)
    assert batch.articles[0].source_timezone == "Asia/Singapore"
    assert batch.source_timezone == "Asia/Singapore"


def test_historical_offset_is_validated_even_when_current_offsets_match():
    # London and UTC match in December, but not for an August publication.
    with pytest.raises(SourcePageError, match="timezone"):
        parse_news_listing_v2(
            metadata("Europe/London") + listing("Aug 1, 2026, 9:45am"),
            datetime(2026, 12, 1, tzinfo=UTC), ZoneInfo("UTC"),
        )


@pytest.mark.parametrize("raw,day,clock", [
    ("Nov 1, 2026, 1:30am", "Nov 1", "1:30am"),
    ("Mar 8, 2026, 2:30am", "Mar 8", "2:30am"),
])
def test_dst_wall_clock_ambiguity_is_not_silently_guessed(raw, day, clock):
    zone = ZoneInfo("America/New_York")
    with pytest.raises(SourcePageError, match=r"ambiguous|nonexistent"):
        parse_calendar(
            calendar(clock, day=day, zone=zone.key), NOW, zone,
            date(2026, 11, 1) if day == "Nov 1" else date(2026, 3, 8),
        )
    batch = parse_news_listing_v2(metadata(zone.key) + listing(raw), NOW, zone)
    result = parse_news_detail_v2(metadata(zone.key) + detail(raw), "1", NOW, zone)
    comments = parse_news_comments(metadata(zone.key) + detail(raw), "1", NOW, zone)
    assert batch.articles[0].published_at is None
    assert batch.articles[0].published_at_source_text == raw
    assert result.segments[0].published_at is None
    assert result.segments[0].published_at_source_text == raw
    assert comments[0].published_at is None
    assert comments[0].published_at_source_text == raw


def test_epoch_disambiguates_dst_calendar_event():
    rows = parse_calendar(
        calendar("1:30am", 1793514600, "Nov 1", "America/New_York"),
        NOW, ZoneInfo("America/New_York"),
    )
    assert rows[0].event_at == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


def test_real_calendar_fixture_preserves_known_epoch_and_source_day():
    html = (Path(__file__).parent / "fixtures/calendar_source_2026-09-01.html").read_text()
    rows = parse_calendar(metadata("Asia/Singapore") + html, NOW, SH, date(2026, 9, 1))
    assert len(rows) == 39
    row = next(row for row in rows if row.source_id == "150105")
    assert row.event_at == datetime(2026, 9, 1, 1, 45, tzinfo=UTC)
    assert row.source_date == date(2026, 9, 1)


def test_invalid_declared_zone_is_not_silently_replaced_with_config():
    with pytest.raises(SourcePageError, match="timezone"):
        parse_news_listing_v2(metadata("Invalid/Timezone") + listing(), NOW, SH)


def test_non_script_text_cannot_override_source_timezone():
    html = "<p>window.FF = {timezone_name: 'UTC'};</p>" + listing()
    batch = parse_news_listing_v2(html, NOW, SH)
    assert batch.articles[0].published_at == datetime(2026, 9, 1, 1, 45, tzinfo=UTC)
