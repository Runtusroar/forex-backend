"""Source timezone metadata and unambiguous wall-clock conversion."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from selectolax.parser import HTMLParser

from app.parsers.errors import SourcePageError


def _declared_timezone(tree: HTMLParser) -> ZoneInfo | None:
    names: set[str] = set()
    for script in tree.css("script"):
        for assignment in re.finditer(
            r"\bwindow\.FF\s*=\s*\{(.*?)\}\s*;", script.text(), re.S
        ):
            match = re.search(
                r"(?:^|[,\s])['\"]?timezone_name['\"]?\s*:\s*(['\"])([^'\"]*)\1",
                assignment.group(1),
            )
            if match:
                names.add(match.group(2))
    if not names:
        return None
    if len(names) != 1:
        raise SourcePageError("source timezone metadata is inconsistent")
    try:
        return ZoneInfo(names.pop())
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise SourcePageError("source timezone metadata is invalid") from error


@dataclass(frozen=True)
class SourceTime:
    zone: tzinfo
    configured_zone: tzinfo | None = None

    @classmethod
    def from_tree(
        cls, tree: HTMLParser, configured_zone: tzinfo | None, reference: datetime,
        *, validate: bool = True,
    ) -> "SourceTime":
        declared = _declared_timezone(tree)
        source = cls(
            declared or configured_zone or UTC,
            configured_zone if declared is not None and validate else None,
        )
        source.validate(reference)
        return source

    def validate(self, instant: datetime | None) -> None:
        if instant is None or self.configured_zone is None:
            return
        if instant.astimezone(self.zone).utcoffset() != instant.astimezone(
            self.configured_zone
        ).utcoffset():
            raise SourcePageError(
                f"source timezone {self.zone} differs from configured timezone "
                f"{self.configured_zone} at {instant.isoformat()}"
            )


def wall_time_utc(local: datetime, zone: tzinfo) -> datetime | None:
    """Return None for a DST gap or repeated time; never silently choose a fold."""
    candidates: set[datetime] = set()
    for fold in (0, 1):
        instant = local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == local:
            candidates.add(instant)
    return candidates.pop() if len(candidates) == 1 else None
