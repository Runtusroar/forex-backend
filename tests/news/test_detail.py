from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.news.detail import parse_news_detail_v2

HTML = (Path(__file__).parents[1] / "fixtures/news_v2/detail_alloy.html").read_text()
TRUTH_SOCIAL_HTML = (
    Path(__file__).parents[1] / "fixtures/news_v2/detail_truth_social.html"
).read_text()
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

    assert detail.segments[2].text_en == "Full Forex Factory excerpt..."
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


def test_truth_social_show_more_preserves_ff_text_and_presentation() -> None:
    detail = parse_news_detail_v2(
        TRUTH_SOCIAL_HTML, "101", NOW, ZoneInfo("Asia/Shanghai")
    )

    assert len(detail.segments) == 2
    social = detail.segments[1]
    assert social.segment_type == "social"
    assert social.author_name == "Donald J. Trump"
    assert social.author_handle == "@realDonaldTrump"
    assert social.published_at_source_text == "Sep 3, 2026 10:56pm"
    assert social.source_url == (
        "https://truthsocial.com/@realDonaldTrump/117207687594983323"
    )
    assert social.text_en == (
        "For the treasonous SCUM that refuses to accurately report on our Military "
        "Operation in Iran, we have virtually unlimited amounts of ammunition."
    )
    assert social.display_mode == "clamped"
    assert social.max_lines == 10
    assert social.external_action_label == "Show More"


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
