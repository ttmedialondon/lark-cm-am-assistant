"""Inline mention resolution reaches each active reader without new body access."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

from app import admin_reporting, message_material
from app.crypto import message_body_cipher

MENTIONS = [{"key": "@_user_1", "id_type": "open_id", "id": "ou_remi", "name": "Quanzhen Liang"}]
NAMES = {"ou_remi": "Remi（Quanzhen Liang）"}
TEXT = "Hi @_user_1; unknown @_user_2"
EXPECTED = "Hi @Remi（Quanzhen Liang）; unknown @_user_2"


def test_rich_at_nodes_are_not_dropped_and_unknown_target_remains_explicit():
    raw = json.dumps({"content": [[{"tag": "text", "text": "Hi"}, {"tag": "at", "user_id": "ou_remi"},
                                    {"tag": "at", "user_id": "ou_unmapped"}]]})
    content = admin_reporting._readable_body(raw, "post", MENTIONS, NAMES)
    assert content == "Hi\n@Remi（Quanzhen Liang）\n[未解析的@用户]"


def test_admin_reader_only_looks_up_mentions_from_the_authorized_page(monkeypatch):
    row = {"message_id": "allowed", "message_type": "text", "body_ciphertext": "cipher", "mentions_json": MENTIONS}
    metrics = {"message_count": 1, "content_count": 1, "own_message_count": 0, "p2p_message_count": 1}
    calls = []

    def execute(sql, params):
        calls.append((sql, params))
        return SimpleNamespace(fetchone=lambda: metrics, fetchall=lambda: [row])

    conn = SimpleNamespace(execute=execute)
    monkeypatch.setattr(admin_reporting, "connection", lambda: nullcontext(conn))
    monkeypatch.setattr(admin_reporting, "get_settings", lambda: SimpleNamespace(token_encryption_key=b"test"))
    monkeypatch.setattr(
        admin_reporting, "message_body_cipher", lambda _: SimpleNamespace(decrypt=lambda _: json.dumps({"text": TEXT}))
    )

    def names(current, rows, *, lock):
        assert lock is False
        assert current is conn and rows == [row]
        assert len(calls) == 2
        assert "ac.is_visible AND ac.sync_enabled" in calls[-1][0]
        assert "NOT m.is_deleted" in calls[-1][0]
        return NAMES

    monkeypatch.setattr(admin_reporting, "mention_names", names)
    assert admin_reporting.message_page(uuid4(), "allowed-chat", 1)["items"][0]["content"] == EXPECTED


def test_rich_body_keeps_stored_text_but_never_follows_links_or_forwarded_ids():
    examples = [
        ("post", {"zh_cn": {"title": "合作安排", "content": [[
            {"tag": "text", "text": "请查看"},
            {"tag": "a", "text": "活动说明", "href": "https://private.example/secret"},
        ]]}}, "合作安排\n请查看\n活动说明"),
        ("interactive", {"header": {"title": {"tag": "plain_text", "content": "卡片标题"}},
                         "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "**已安排**"}}]},
         "卡片标题\n**已安排**"),
        ("merge_forward", {"messages": [{"body": {"content": {"text": "已同步的转发文字"}}}],
                           "message_id": "unloaded-message", "chat_id": "unloaded-chat"}, "已同步的转发文字"),
    ]
    for kind, payload, expected in examples:
        assert admin_reporting._readable_body(json.dumps(payload), kind) == expected


def test_assistant_material_preserves_card_facts_links_and_forwarded_provenance():
    post = {locale: {"title": "Samples", "content": [[
        {"tag": "text", "text": "Arrived"},
        {"tag": "a", "text": "Details", "href": "https://private.example/info"},
        {"tag": "img", "image_key": "img_allowed"},
        {"tag": "media", "file_key": "video_not_supported"},
    ]]} for locale in ("zh_cn", "en_us")}
    post["files"] = [{"file_key": "file_allowed", "file_name": "samples.xlsx"}]
    material = message_material.text_material(json.dumps(post), "post")
    assert material["text"] == "Samples\nArrived\nDetails (https://private.example/info)"
    assert material["notes"] == ["富文本中的音视频或表情包暂未解析"]
    assert message_material.resource_refs(post, "post") == [
        {"key": "img_allowed", "type": "image", "name": ""},
        {"key": "file_allowed", "type": "file", "name": "samples.xlsx"},
    ]
    card = {"header": {"title": {"tag": "plain_text", "content": "Paid"}},
            "elements": [{"tag": "lark_md", "content": "**Payment received**"}],
            "i18n_elements": {"en_us": [{"tag": "lark_md", "content": "**Payment received**"}]},
            "value": {"text": "callback payload is not displayed content"}}
    assert message_material.text_material(card, "interactive")["text"] == "Paid\n**Payment received**"
    assert message_material.resource_refs(card, "interactive") == []
    forwarded = message_material.render_forward([
        {"msg_type": "text", "sender": {"name": "Actual author"}, "create_time": "1728000000000",
         "body": {"content": json.dumps({"text": "My TikTok account is @sample"})}},
        {"msg_type": "text", "body": {"content": json.dumps({"text": "Unknown author"})}},
    ])
    assert "不是转发者本人陈述" in forwarded and "引用发送者：Actual author" in forwarded
    assert "引用发送者：未知发送者" in forwarded
    assert message_material.text_material({"create_message_ids": ["unread-id"]}, "merge_forward") == {
        "text": "", "notes": ["合并转发内容尚未展开"]}
    partial = message_material.text_material({"items": [
        {"msg_type": "text", "body": {"content": json.dumps({"text": "Readable quote"})}},
        {"msg_type": "image", "body": {"content": json.dumps({"image_key": "img_unread"})}},
        {"msg_type": "file", "body": {"content": json.dumps({"file_key": "file_unread"})}},
        {"msg_type": "audio", "body": {"content": json.dumps({"file_key": "file_voice"})}},
        {"msg_type": "merge_forward", "message_id": "om_nested", "upper_message_id": "om_outer",
         "body": {"content": "Merged forward"}},
    ]}, "merge_forward")
    assert "Readable quote" in partial["text"] and "不是转发者本人陈述" in partial["text"]
    assert partial["notes"] == ["该引用含未解析附件", "音频暂未转写", "合并转发内容尚未展开"]
    assert message_material.text_material({"items": [
        {"msg_type": "text", "body": {"content": json.dumps({"text": "Unknown author is still quoted"})}},
    ]}, "merge_forward")["notes"] == []
    assert message_material.text_material({"message_id": "om_outer", "items": [
        {"msg_type": "merge_forward", "message_id": "om_outer", "body": {"content": "Merged forward"}},
        {"msg_type": "text", "body": {"content": json.dumps({"text": "Expanded child"})}},
    ]}, "merge_forward")["notes"] == []


def test_assistant_attachments_use_memory_only_and_keep_partial_reading_explicit():
    import base64
    import io
    from zipfile import ZipFile

    from openpyxl import Workbook
    from PIL import Image

    from app import offhours_summary

    image = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(image, format="PNG")
    result = message_material.file_inputs(image.getvalue(), "../../sample.png", "image/png")
    assert result["supported"] and result["inputs"][0]["type"] == "input_image"
    assert base64.b64decode(result["inputs"][0]["image_url"].split(",", 1)[1]) == image.getvalue()
    pdf = message_material.file_inputs(b"%PDF-1.7\nexample", "../sample.pdf", "application/pdf")
    assert pdf["inputs"][0]["filename"] == "sample.pdf" and pdf["inputs"][0]["type"] == "input_file"
    cipher = message_body_cipher(b'r' * 32)
    raw_file = json.dumps({'file_name': 'distinct-file-name.pdf', 'file_key': 'file_test'})
    body, unreadable = offhours_summary._body(
        {'message_type': 'file', 'body_ciphertext': cipher.encrypt(raw_file),
         '_material': {'text': '[文件解析，非发言人原话]\nBusiness facts', 'complete': True}}, cipher)
    assert not unreadable and 'distinct-file-name.pdf' in body and 'Business facts' in body
    doc = io.BytesIO()
    with ZipFile(doc, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                          '<w:body><w:p><w:r><w:t>Sample arrived</w:t></w:r></w:p></w:body></w:document>')
        archive.writestr("word/media/image.png", image.getvalue())
    result = message_material.file_inputs(doc.getvalue(), "sample.docx", "application/octet-stream")
    assert result["text"] == "Sample arrived" and result["inputs"] == []
    assert result["notes"] == ["文档内嵌图片暂未解析"]
    workbook, sheet_file = Workbook(), io.BytesIO()
    workbook.active.append(["Handle", "Status"])
    workbook.active.append(["@sample", "=1+1"])
    workbook.save(sheet_file)
    workbook.close()
    result = message_material.file_inputs(sheet_file.getvalue(), "sample.xlsx", "application/octet-stream")
    assert "@sample\t=1+1" in result["text"] and result["inputs"] == []
    assert len(result["text"].splitlines()) == 3
    assert "工作簿公式保留原式，未执行计算" in result["notes"]
    text = message_material.file_inputs(b"line\n" * 1001, "sample.txt", "text/plain")
    assert len(text["text"].splitlines()) == 1000 and text["notes"]
    for data, name, mime in [(b"invalid", "fake.png", "image/png"),
                             (b"unknown zip", "sample.zip", "application/zip")]:
        failed = message_material.file_inputs(data, name, mime)
        assert not failed["supported"] and failed["inputs"] == [] and failed["notes"]
    entity_doc = io.BytesIO()
    with ZipFile(entity_doc, "w") as archive:
        archive.writestr("word/document.xml", ('<!DOCTYPE document [<!ENTITY external SYSTEM "file:///secret">]>'
                          '<document>&external;</document>').encode("utf-16"))
    failed = message_material.file_inputs(entity_doc.getvalue(), "sample.docx", "application/octet-stream")
    assert not failed["supported"] and failed["notes"] == ["文档含不支持的 XML 实体，未解析"]
