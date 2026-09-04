import re
from datetime import UTC, date, datetime, timedelta, tzinfo
from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node

from app.domain import (
    CalendarDetailObservation,
    CalendarHistoryObservation,
    CalendarObservation,
    CalendarRelatedStoryObservation,
)
from app.parsers.errors import SourcePageError, reject_challenge

CLOCK_PATTERN = re.compile(r"\d{1,2}:\d{2}(?:am|pm)", re.IGNORECASE)
SOURCE_ROOT = "https://www.forexfactory.com"


def _text(node: Node, selector: str) -> str | None:
    found = node.css_first(selector)
    value = _node_text(found) if found else ""
    return value or None


def _node_text(node: Node | None) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.text(separator=" ", strip=True)).strip()


def _absolute_url(value: str | None) -> str | None:
    if not value or value == "null":
        return None
    return urljoin(SOURCE_ROOT, value)


def _impact(node: Node) -> str:
    impact = node.css_first(".calendar__impact")
    classes = " ".join(child.attributes.get("class", "") for child in impact.css("*") if impact)
    if "red" in classes:
        return "high"
    if "ora" in classes:
        return "medium"
    if "yel" in classes:
        return "low"
    if "gry" in classes:
        return "holiday"
    return "unknown"


def _value_state(node: Node | None) -> str | None:
    if node is None:
        return None
    classes = " ".join(
        child.attributes.get("class") or "" for child in [node, *node.css("*")]
    )
    if "better" in classes:
        return "better"
    if "worse" in classes:
        return "worse"
    return None


def _revised_from(node: Node | None) -> str | None:
    if node is None:
        return None
    title = node.attributes.get("title", "")
    match = re.search(r"Revised from\s+(.+)", title)
    return match.group(1).strip() if match else None


def _specs(detail: Node) -> dict[str, tuple[str, list[Node]]]:
    values: dict[str, tuple[str, list[Node]]] = {}
    for row in detail.css(".calendarspecs tr"):
        label = _node_text(row.css_first(".calendarspecs__spec"))
        description = row.css_first(".calendarspecs__specdescription")
        if not label or description is None:
            continue
        values[label] = (_node_text(description), description.css("a"))
    return values


def _spec_url(links: list[Node], index: int) -> str | None:
    if len(links) <= index:
        return None
    return _absolute_url(links[index].attributes.get("href"))


def _source_name(spec_value: str | None) -> str | None:
    if not spec_value:
        return None
    return spec_value.split("(", 1)[0].strip() or None


def _history(detail: Node) -> tuple[CalendarHistoryObservation, ...]:
    rows: list[CalendarHistoryObservation] = []
    for row in detail.css("table.calendarhistory tbody tr"):
        date_node = row.css_first(".calendarhistory__row--history")
        actual_node = row.css_first(".calendarhistory__row--actual")
        forecast_node = row.css_first(".calendarhistory__row--forecast")
        previous_node = row.css_first(".calendarhistory__row--previous")
        release_date = _node_text(date_node)
        if not release_date:
            continue
        previous_value = previous_node.css_first("span") if previous_node else None
        rows.append(
            CalendarHistoryObservation(
                release_date_text=release_date,
                event_url=_absolute_url(
                    date_node.css_first("a").attributes.get("href")
                    if date_node and date_node.css_first("a")
                    else None
                ),
                actual=_node_text(actual_node) or None,
                forecast=_node_text(forecast_node) or None,
                previous=_node_text(previous_node) or None,
                actual_state=_value_state(actual_node),
                previous_state=_value_state(previous_node),
                previous_revised_from=_revised_from(previous_value),
            )
        )
    return tuple(rows)


def _related_stories(detail: Node) -> tuple[CalendarRelatedStoryObservation, ...]:
    story_nodes = detail.css(".news-block__item") or detail.css(".news-block")
    stories: list[CalendarRelatedStoryObservation] = []
    for story in story_nodes:
        title_link = story.css_first(".news-block__title a[href*='/news/']")
        title = _node_text(title_link)
        ff_url = _absolute_url(title_link.attributes.get("href")) if title_link else None
        if not title or not ff_url:
            continue
        source_link = story.css_first(".news-block__details a[href*='/hit']")
        source_text = _node_text(source_link)
        if source_text.lower().startswith("from "):
            source_text = source_text[5:].strip()
        details_text = _node_text(story.css_first(".news-block__details"))
        published = None
        if "|" in details_text:
            published = details_text.split("|", 1)[1].strip() or None
        stories.append(
            CalendarRelatedStoryObservation(
                title_en=title,
                ff_url=ff_url,
                source_name=source_text or None,
                source_url=_absolute_url(source_link.attributes.get("href"))
                if source_link
                else None,
                published_at_source_text=published,
                preview=_node_text(story.css_first(".news-block__preview")) or None,
            )
        )
    return tuple(stories)


def _source_date(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*(\d{1,2})", value)
    return f"{match.group(1)} {match.group(2)}" if match else None


def _event_time(
    row: Node,
    date_text: str | None,
    time_text: str | None,
    now: datetime,
    source_timezone: tzinfo,
) -> datetime:
    timestamp = row.attributes.get("data-timestamp")
    if timestamp and timestamp.isdigit():
        return datetime.fromtimestamp(int(timestamp), tz=UTC)
    date_text = _source_date(date_text)
    if not date_text or not time_text:
        raise SourcePageError("calendar row has no timestamp")
    clock = time_text if CLOCK_PATTERN.fullmatch(time_text) else "12:00am"
    parsed = datetime.strptime(f"{date_text} {now.year} {clock}", "%b %d %Y %I:%M%p")
    parsed = parsed.replace(tzinfo=source_timezone)
    local_now = now.astimezone(source_timezone)
    if parsed - local_now > timedelta(days=180):
        parsed = parsed.replace(year=parsed.year - 1)
    elif local_now - parsed > timedelta(days=180):
        parsed = parsed.replace(year=parsed.year + 1)
    return parsed.astimezone(UTC)


def parse_calendar(
    html: str,
    now: datetime,
    source_timezone: tzinfo | None = None,
    expected_date: date | None = None,
) -> list[CalendarObservation]:
    reject_challenge(html)
    tree = HTMLParser(html)
    results: list[CalendarObservation] = []
    source_timezone = source_timezone or datetime.now().astimezone().tzinfo or UTC
    last_date: str | None = None
    last_time: str | None = None
    expected_marker = f"{expected_date:%b} {expected_date.day}" if expected_date else None
    requested_day_seen = False
    source_position = 0
    for row in tree.css("tr.calendar__row"):
        source_id = row.attributes.get("data-event-id", "").strip()
        if not source_id:
            structural_date = _text(row, "td")
            if _source_date(structural_date):
                last_date = structural_date
                last_time = None
                requested_day_seen |= _source_date(structural_date) == expected_marker
            continue
        last_date = _text(row, ".calendar__date") or last_date
        row_time = _text(row, ".calendar__time")
        last_time = row_time or last_time
        requested_day_seen |= _source_date(last_date) == expected_marker
        title = _text(row, ".calendar__event")
        currency = _text(row, ".calendar__currency")
        if not title or not currency:
            raise SourcePageError("calendar row missing required fields")
        results.append(
            CalendarObservation(
                source_id=source_id,
                event_at=_event_time(row, last_date, last_time, now, source_timezone),
                currency=currency,
                impact=_impact(row),
                title_en=title,
                actual=_text(row, ".calendar__actual"),
                forecast=_text(row, ".calendar__forecast"),
                previous=_text(row, ".calendar__previous"),
                source_time_text=(
                    row_time
                    if row_time is not None and not CLOCK_PATTERN.fullmatch(row_time)
                    else None
                ),
                source_position=source_position,
            )
        )
        source_position += 1
    if expected_date and not requested_day_seen:
        raise SourcePageError("calendar page does not contain requested day")
    if not results and expected_date is None:
        raise SourcePageError("calendar page contains no event rows")
    return results


def parse_calendar_detail(
    html: str,
    source_id: str,
    observed_at: datetime,
) -> CalendarDetailObservation:
    del observed_at
    reject_challenge(html)
    tree = HTMLParser(html)
    active = tree.css_first(f"tr.calendar__row[data-event-id='{source_id}']")
    detail = tree.css_first("tr.calendar__details--detail")
    if detail is None:
        raise SourcePageError("calendar detail row is missing")

    title = _text(active, ".calendar__event") if active else None
    if not title:
        overlay_title = _node_text(detail.css_first(".overlay__title"))
        title = re.sub(r"^[A-Z]{3}\s+", "", overlay_title).strip()
    if not title:
        raise SourcePageError("calendar detail missing title")

    source_value, source_links = _specs(detail).get("Source", (None, []))
    measures = _specs(detail).get("Measures", (None, []))[0]
    usual_effect = _specs(detail).get("Usual Effect", (None, []))[0]
    frequency = _specs(detail).get("Frequency", (None, []))[0]
    next_release_text, next_release_links = _specs(detail).get("Next Release", (None, []))
    ff_notes = _specs(detail).get("FF Notes", (None, []))[0]
    why_traders_care = _specs(detail).get("Why Traders Care", (None, []))[0]
    full_detail = detail.css_first(".calendardetails__solo a[href^='/calendar/']")
    previous_value = active.css_first(".calendar__previous span") if active else None
    currency_node = active.css_first(".calendar__currency abbr") if active else None

    return CalendarDetailObservation(
        source_id=source_id,
        title_en=title,
        currency=_text(active, ".calendar__currency") if active else None,
        currency_name=currency_node.attributes.get("title") if currency_node else None,
        impact=_impact(active) if active else None,
        actual=_text(active, ".calendar__actual") if active else None,
        forecast=_text(active, ".calendar__forecast") if active else None,
        previous=_text(active, ".calendar__previous") if active else None,
        actual_state=_value_state(active.css_first(".calendar__actual") if active else None),
        previous_state=_value_state(active.css_first(".calendar__previous") if active else None),
        previous_revised_from=_revised_from(previous_value),
        ff_url=_absolute_url(full_detail.attributes.get("href")) if full_detail else None,
        source_name=_source_name(source_value),
        source_url=_spec_url(source_links, 0),
        latest_release_url=_spec_url(source_links, 1),
        measures=measures,
        usual_effect=usual_effect,
        frequency=frequency,
        next_release_text=next_release_text,
        next_release_url=_spec_url(next_release_links, 0),
        ff_notes=ff_notes,
        why_traders_care=why_traders_care,
        history=_history(detail),
        related_stories=_related_stories(detail),
    )
