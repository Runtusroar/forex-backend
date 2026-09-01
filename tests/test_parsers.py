from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.parsers import ChallengePageError, parse_calendar, parse_news_detail, parse_news_listing

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


def test_news_relative_age_does_not_change_translatable_data() -> None:
    html = fixture("news.html")
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    first = parse_news_listing(html, now)[0]
    second = parse_news_listing(html.replace("5 min ago", "6 min ago"), now)[0]

    assert first.source_id == "9001"
    assert first.title_en == second.title_en
    assert first.summary_en == second.summary_en
    assert first.first_seen_at == second.first_seen_at


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
