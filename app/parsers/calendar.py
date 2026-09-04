import re
from datetime import UTC, date, datetime, timedelta, tzinfo

from selectolax.parser import HTMLParser, Node

from app.domain import CalendarObservation
from app.parsers.errors import SourcePageError, reject_challenge


def _text(node: Node, selector: str) -> str | None:
    found = node.css_first(selector)
    value = found.text(strip=True) if found else ""
    return value or None


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
    clock = "12:00am" if re.fullmatch(r"(?:Day \d+|All Day|Tentative)", time_text) else time_text
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
        last_time = _text(row, ".calendar__time") or last_time
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
            )
        )
    if expected_date and not requested_day_seen:
        raise SourcePageError("calendar page does not contain requested day")
    if not results and expected_date is None:
        raise SourcePageError("calendar page contains no event rows")
    return results
