from datetime import UTC, datetime

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


def _event_time(row: Node, date_text: str | None, time_text: str | None, now: datetime) -> datetime:
    timestamp = row.attributes.get("data-timestamp")
    if timestamp and timestamp.isdigit():
        return datetime.fromtimestamp(int(timestamp), tz=UTC)
    if not date_text or not time_text:
        raise SourcePageError("calendar row has no timestamp")
    parsed = datetime.strptime(f"{date_text} {now.year} {time_text}", "%a %b %d %Y %I:%M%p")
    return parsed.replace(tzinfo=UTC)


def parse_calendar(html: str, now: datetime) -> list[CalendarObservation]:
    reject_challenge(html)
    tree = HTMLParser(html)
    results: list[CalendarObservation] = []
    last_date: str | None = None
    last_time: str | None = None
    for row in tree.css("tr.calendar__row"):
        source_id = row.attributes.get("data-event-id", "").strip()
        if not source_id:
            raise SourcePageError("calendar row missing source identity")
        last_date = _text(row, ".calendar__date") or last_date
        last_time = _text(row, ".calendar__time") or last_time
        title = _text(row, ".calendar__event")
        currency = _text(row, ".calendar__currency")
        if not title or not currency:
            raise SourcePageError("calendar row missing required fields")
        results.append(
            CalendarObservation(
                source_id=source_id,
                event_at=_event_time(row, last_date, last_time, now),
                currency=currency,
                impact=_impact(row),
                title_en=title,
                actual=_text(row, ".calendar__actual"),
                forecast=_text(row, ".calendar__forecast"),
                previous=_text(row, ".calendar__previous"),
            )
        )
    if not results:
        raise SourcePageError("calendar page contains no event rows")
    return results
