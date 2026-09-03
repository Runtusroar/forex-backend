from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.news.detail import parse_news_detail_v2

HTML = (Path(__file__).parents[1] / "fixtures/news_v2/detail_alloy.html").read_text()
NOW = datetime(2026, 9, 3, tzinfo=UTC)


def test_all_article_nodes_become_ordered_segments() -> None:
    detail = parse_news_detail_v2(HTML, "100", NOW, ZoneInfo("Asia/Shanghai"))
    assert [item.segment_type for item in detail.segments] == ["social", "social", "article"]
    assert [item.position for item in detail.segments] == [0, 1, 2]
    assert detail.segments[0].text_en == "First alert"
    assert detail.segments[0].published_at == datetime(2026, 9, 2, 13, 27, tzinfo=UTC)
    assert detail.segments[0].published_at_source_text == "Sep 2, 2026 9:27pm"
    assert detail.segments[2].is_excerpt is True


def test_full_story_anchor_is_structured_and_not_flattened_into_prose() -> None:
    detail = parse_news_detail_v2(HTML, "100", NOW, ZoneInfo("Asia/Shanghai"))

    assert detail.segments[2].text_en == "Full Forex Factory excerpt"
    assert [
        (link.kind, link.label, link.url, link.segment_key, link.position)
        for link in detail.links
    ] == [
        (
            "full_story",
            "full story",
            "https://publisher.example/story",
            detail.segments[2].stable_key,
            0,
        )
    ]


def test_content_media_and_nested_comments_are_preserved() -> None:
    detail = parse_news_detail_v2(HTML, "100", NOW, ZoneInfo("Asia/Shanghai"))
    assert [(item.media_type, item.original_url) for item in detail.media] == [
        (
            "image",
            "https://assets.faireconomy.media/nfs/npd/2026/09/03/chart.png",
        ),
        ("chart", "https://www.forexfactory.com/attachment/image/55")
    ]
    assert [item.comment_id for item in detail.comments] == ["700", "701"]
    assert detail.comments[1].parent_comment_id == "700"
    assert all("avatar" not in item.original_url for item in detail.media)
