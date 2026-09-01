import json

from app.creator_rules import CreatorClaim, extract_creator_claims


def test_extracts_explicit_profile_urls_and_platform_labelled_handles_from_nested_lark_content() -> None:
    body = json.dumps(
        {
            "zh_cn": {
                "content": [
                    [{"tag": "text", "text": "TikTok: @Creator.One https://www.tiktok.com/@Creator.One/"}],
                    [{"tag": "text", "text": "Instagram：@Insta_Creator"}],
                    [{"tag": "text", "text": "https://youtube.com/@Video-Maker"}],
                    [{"tag": "a", "text": "Creator profile", "href": "https://www.twitch.tv/Live_Creator"}],
                ]
            }
        }
    )

    assert extract_creator_claims(body) == (
        CreatorClaim("tiktok", "creator.one", "@Creator.One", "profile_url"),
        CreatorClaim("instagram", "insta_creator", "@Insta_Creator", "labeled_handle"),
        CreatorClaim("youtube", "video-maker", "@Video-Maker", "profile_url"),
        CreatorClaim("twitch", "live_creator", "@Live_Creator", "profile_url"),
    )


def test_rejects_bare_mentions_and_non_profile_content_urls() -> None:
    body = json.dumps(
        {
            "text": (
                "Please ask @ordinary-person. "
                "https://www.instagram.com/p/post-id/ "
                "https://www.instagram.com/reel/reel-id/ "
                "https://www.instagram.com/stories/someone/ "
                "https://www.tiktok.com/@creator/video/123 "
                "https://www.youtube.com/@creator/videos "
                "https://www.twitch.tv/creator/videos"
            )
        }
    )

    assert extract_creator_claims(body) == ()


def test_plain_text_fallback_keeps_only_explicit_claims() -> None:
    assert extract_creator_claims("Twitch account: @Stream_Creator") == (
        CreatorClaim("twitch", "stream_creator", "@Stream_Creator", "labeled_handle"),
    )
