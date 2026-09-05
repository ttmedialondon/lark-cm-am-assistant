import json

import pytest

from app.admin_reporting import _readable_body


def render(value, kind="post"):
    return _readable_body(json.dumps(value), kind)


def test_post_title_and_text_are_in_reading_order_without_link_targets():
    assert render({"zh_cn": {"title": "合作安排", "content": [[
        {"tag": "text", "text": "请查看 "},
        {"tag": "a", "text": "活动说明", "href": "https://private.example/secret"},
    ]]}}) == "合作安排\n请查看 \n活动说明"


def test_card_plain_text_and_markdown_nodes_include_nested_text_objects():
    assert render({
        "header": {"title": {"tag": "plain_text", "content": "卡片标题"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "**已安排**"}},
            {"tag": "button", "text": {"tag": "plain_text", "content": "查看详情"},
             "url": "https://private.example/secret"},
        ],
    }, "interactive") == "卡片标题\n**已安排**\n查看详情"


@pytest.mark.parametrize("kind,payload", [
    ("image", {"image_key": "private-image-key"}),
    ("file", {"file_key": "private-file-key", "name": "private-file-name"}),
    ("interactive", {"content": "private-untyped-content", "title": "private-untyped-title",
                     "key": "private-key", "token": "private-token", "url": "https://private.example"}),
    ("interactive", {"tag": "unknown", "content": "private-unknown-node"}),
    ("interactive", {"tag": ["plain_text"], "content": "private-malformed-node"}),
])
def test_nontext_metadata_stays_a_type_placeholder(kind, payload):
    assert render(payload, kind) == f"（{kind} 消息没有可展示的文本）"


def test_text_stays_a_plain_string_for_template_autoescaping():
    body = '<script>alert("x")</script>'
    result = render({"text": body}, "text")
    assert type(result) is str
    assert result == body


def test_forward_only_extracts_text_already_in_its_payload():
    assert render({
        "messages": [{"body": {"content": {"text": "已同步的转发文字"}}}],
        "message_id": "unloaded-message", "chat_id": "unloaded-chat",
        "url": "https://private.example/unloaded",
    }, "merge_forward") == "已同步的转发文字"
