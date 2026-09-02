from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.news.listing import parse_news_listing_v2

FIXTURE = (Path(__file__).parents[1] / "fixtures/news_v2/listing_all_sections.html").read_text()
NOW = datetime(2026, 9, 3, tzinfo=UTC)


def test_all_sections_are_typed_and_duplicates_are_merged() -> None:
    batch = parse_news_listing_v2(FIXTURE, NOW, ZoneInfo("Asia/Shanghai"))
    article = next(item for item in batch.articles if item.source_id == "100")

    assert len([item for item in batch.articles if item.source_id == "100"]) == 1
    assert article.teaser_en == "Richer category preview."
    assert article.listing_thumbnail_url == "https://assets.example/yen.jpg"
    assert {row.category for row in batch.categories if row.article_id == "100"} == {
        "fundamental"
    }
    assert batch.observed_sections == frozenset(
        {
            "hot",
            "latest",
            "latest_comments",
            "fundamental",
            "technical",
            "industry",
            "entertainment",
            "educational",
        }
    )


def test_listing_parses_time_impact_hot_and_latest_comment() -> None:
    batch = parse_news_listing_v2(FIXTURE, NOW, ZoneInfo("Asia/Shanghai"))
    article = next(item for item in batch.articles if item.source_id == "100")

    assert article.published_at == datetime(2026, 9, 2, 14, 42, tzinfo=UTC)
    assert article.published_at_source_text == "Sep 2, 2026, 10:42pm"
    assert article.breaking_impact == "high"
    assert article.comment_count == 16
    assert [(row.article_id, row.feed_type, row.rank) for row in batch.feeds] == [
        ("200", "hot", 0),
        ("100", "latest", 0),
    ]
    assert batch.comments[0].comment_id == "90001"
    assert batch.comments[0].article_id == "100"
    assert batch.comments[0].text_en == "Useful update"
    assert batch.comments[0].feed_rank == 0


def test_listing_never_uses_interface_icons_as_content_media() -> None:
    batch = parse_news_listing_v2(FIXTURE, NOW, ZoneInfo("Asia/Shanghai"))
    article = next(item for item in batch.articles if item.source_id == "100")
    assert "story.svg" not in (article.listing_thumbnail_url or "")


def test_latest_comment_can_reference_an_article_outside_story_panels() -> None:
    html = """
    <div class="news-block"><h2>News / Latest Stories</h2>
      <div class="news-block__item"><div class="news-block__title">
        <a href="/news/1-one">One</a></div></div>
    </div>
    <div class="news-block"><h2>News / Latest Comments</h2>
      <div class="news-block__item news-block__item--comment">
        <a class="news-block__comment-author">Alice</a>
        <span class="news-block__comment-message">Useful</span>
        <a href="/news/2-two/comment/20">comment</a>
        <a class="news-block__title" href="/news/2-two">Two</a>
      </div>
    </div>
    """

    batch = parse_news_listing_v2(html, NOW, ZoneInfo("Asia/Shanghai"))

    assert {article.source_id for article in batch.articles} == {"1", "2"}
