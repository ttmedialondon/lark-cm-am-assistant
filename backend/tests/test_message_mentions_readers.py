"""Inline mention resolution reaches each active reader without new body access."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_creator_workflow_http_import as import_fixtures
from jinja2 import Environment

from app import admin_reporting, contact_assessments
from app import creator_workflow as workflow
from app import creator_workflow_evidence as evidence
from app.crypto import message_body_cipher

db = import_fixtures.db
people = import_fixtures.people
real_import = import_fixtures.real_import

MENTIONS = [{"key": "@_user_1", "id_type": "open_id", "id": "ou_remi", "name": "Quanzhen Liang"}]
NAMES = {"ou_remi": "Remi（Quanzhen Liang）"}
TEXT = "Hi @_user_1; unknown @_user_2"
EXPECTED = "Hi @Remi（Quanzhen Liang）; unknown @_user_2"


@pytest.mark.parametrize("reader", ["admin", "evidence"])
@pytest.mark.parametrize("kind", ["text", "post"])
def test_all_body_readers_resolve_current_bilingual_names_without_changing_unknown_mentions(reader, kind):
    raw = json.dumps({"text": TEXT} if kind == "text" else {"content": [[{"text": TEXT}]]})
    cipher = message_body_cipher(b"r" * 32)
    encrypted = cipher.encrypt(raw)
    if reader == "admin":
        result = admin_reporting._decorate_message(
            {"body_ciphertext": encrypted, "message_type": kind, "mentions_json": MENTIONS}, cipher, NAMES
        )["content"]
    else:
        result = evidence._text_content(encrypted, cipher, kind, MENTIONS, NAMES)
    assert result == EXPECTED
    assert cipher.decrypt(encrypted) == raw


def test_missing_mapping_is_literal_and_resolved_name_is_still_escaped_by_template():
    assert admin_reporting._readable_body(json.dumps({"text": TEXT}), "text") == TEXT
    dangerous = admin_reporting._readable_body(
        json.dumps({"text": "Hi @_user_1"}), "text", MENTIONS, {"ou_remi": "<script>bad()</script>"}
    )
    rendered = Environment(autoescape=True).from_string("{{ content }}").render(content=dangerous)
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


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

    def names(current, rows):
        assert current is conn and rows == [row]
        assert len(calls) == 2
        assert "ac.is_visible AND ac.sync_enabled" in calls[-1][0]
        assert "NOT m.is_deleted" in calls[-1][0]
        return NAMES

    monkeypatch.setattr(admin_reporting, "mention_names", names)
    assert admin_reporting.message_page(uuid4(), "allowed-chat", 1)["items"][0]["content"] == EXPECTED


def test_evidence_body_masks_identifiers_and_does_not_expand_private_metadata():
    cipher = message_body_cipher(b"r" * 32)
    raw = json.dumps({"text": "email a+b@example.org tel +44 7700 900123 ou_secret0123"})
    assert evidence._text_content(cipher.encrypt(raw), cipher) == (
        "email [邮箱已隐藏] tel [电话号码已隐藏] [标识已隐藏]"
    )
    for kind in ("image", "file", "interactive"):
        private = json.dumps({"key": "secret", "token": "secret", "url": "https://secret.example"})
        assert "secret" not in evidence._text_content(cipher.encrypt(private), cipher, kind)
    assert evidence._text_content(None, cipher) == "（正文尚未同步）"
    assert evidence._text_content("invalid-ciphertext", cipher) == "（正文无法读取）"


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


def test_candidate_evidence_selects_mentions_only_inside_existing_message_scope(db, people, real_import, monkeypatch):
    cipher = message_body_cipher(b"r" * 32)
    monkeypatch.setattr(evidence, "get_settings", lambda: SimpleNamespace(token_encryption_key=b"r" * 32))
    db.execute("INSERT INTO lark_identities(open_id,display_name) VALUES('ou_remi',%s)", (NAMES["ou_remi"],))
    db.execute(
        """UPDATE chat_messages SET body_ciphertext=%s,message_type='text',mentions_json=%s::jsonb
           WHERE chat_id='chat_cm'""", (cipher.encrypt(json.dumps({"text": TEXT})), json.dumps(MENTIONS)),
    )
    row = workflow.ingest_source(
        people["cm"], source_type="excel", source_key="mention-row", source_data={},
        responsible_id=people["cm"], handle="abc", contact_id=people["person"],
    )
    assert evidence.evidence(real_import, row["id"], "chat_cm")["messages"][0]["text"] == EXPECTED
    db.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (people["cm"],))
    with pytest.raises(PermissionError):
        evidence.evidence(real_import, row["id"], "chat_cm")


def test_historical_assessment_display_resolves_only_currently_readable_refs(monkeypatch):
    cipher = message_body_cipher(b"r" * 32)
    readable = {"label": "M1", "relation": "anchor", "message_type": "text", "mentions_json": MENTIONS,
                "body_ciphertext": cipher.encrypt(json.dumps({"text": TEXT}))}
    denied = {**readable, "label": "M2", "body_ciphertext": None}
    results = iter([
        {"id": uuid4(), "contact_id": uuid4(), "display_name": "Creator", "open_id": "ou_creator",
         "status": "succeeded", "model": "existing-model", "result": {"roles": []}, "evidence_count": 2},
        [readable, denied], [],
    ])
    calls = []

    def execute(sql, params=None):
        calls.append((sql, params))
        result = next(results)
        return SimpleNamespace(fetchone=lambda: result, fetchall=lambda: result)

    conn = SimpleNamespace(execute=execute)
    monkeypatch.setattr(contact_assessments, "connection", lambda: nullcontext(conn))
    monkeypatch.setattr(contact_assessments, "get_settings", lambda: SimpleNamespace(token_encryption_key=b"r" * 32))

    def names(current, rows):
        assert current is conn and rows == [readable]
        query = calls[1][0]
        assert "THEN message.mentions_json" in query
        assert query.count("observation.observed_at >= auth.body_consent_at") == 2
        assert query.count("access.is_visible AND access.sync_enabled") == 2
        return NAMES

    monkeypatch.setattr(contact_assessments, "mention_names", names)
    result = contact_assessments.assessment_detail(uuid4())
    assert result["evidence"][0]["content"] == EXPECTED
    assert not result["evidence"][1]["available"]
    assert "Remi" not in result["evidence"][1]["content"]
