import json
from contextlib import nullcontext
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from app import creator_review
from app.crypto import message_body_cipher

CONTACT = UUID("00000000-0000-4000-8000-000000000001")
OWNER = UUID("00000000-0000-4000-8000-000000000002")
ASSIGNMENT = UUID("00000000-0000-4000-8000-000000000003")
CHAT = "oc_primary12345678"
OTHER_CHAT = "oc_unrelated12345678"
NOW = datetime(2026, 9, 5, tzinfo=UTC)


def documented_row():
    evidence = {
        "manual_excel_authority": {"rows": [12], "owner_rule": "account_owner_in_excel"},
        "primary_identity_evidence": {"kind": "non_p2p_group_membership", "group_chat_id": CHAT},
        "p2p_policy": "auxiliary_only_not_used",
    }
    audit = {"creator_handles": ["example.handle"], "decision_id": "reviewed-decision", "evidence": evidence}
    return {"id": CONTACT, "display_name": "Example Creator", "role_source": "manual_excel_and_identity_confirmed",
            "role_updated_at": NOW, "reviewed_at": NOW, "assignment_id": ASSIGNMENT,
            "assignment_source": "manual", "started_at": NOW, "responsible_id": OWNER,
            "responsible_name": "Owner Person", "role_audits": [deepcopy(audit)],
            "assignment_audit": deepcopy(audit)}


class Result:
    def __init__(self, result):
        self.result = result

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result


class Connection:
    def __init__(self, *results):
        self.results = iter(results)
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return Result(next(self.results))

    def transaction(self):
        return nullcontext()


def bind(monkeypatch, *results):
    conn = Connection(*results)
    monkeypatch.setattr(creator_review, "connection", lambda: nullcontext(conn))
    return conn


def test_primary_evidence_is_bound_to_current_assignment_and_never_inferred_from_p2p():
    row = documented_row()
    assert creator_review._evidence(row)["documented"] is True
    assert creator_review._evidence(row)["group_ids"] == [CHAT]
    for field, value in [("role_source", "manual_p2p_confirmed"), ("assignment_audit", {}),
                         ("role_audits", None), ("role_audits", ["malformed"])]:
        changed = {**row, field: value}
        assert creator_review._evidence(changed)["documented"] is False
        assert creator_review._evidence(changed)["group_ids"] == []
    row["assignment_audit"]["decision_id"] = "later-handover"
    assert creator_review._evidence(row)["documented"] is False


@pytest.mark.parametrize("primary", [None, [], {}, {"kind": "p2p", "group_chat_id": CHAT},
                                     {"kind": "non_p2p_group_membership", "group_chat_id": "untrusted"},
                                     {"kind": "excel_identity"}])
def test_malformed_or_unsupported_primary_evidence_fails_closed(primary):
    row = documented_row()
    row["role_audits"][0]["evidence"]["primary_identity_evidence"] = primary
    row["assignment_audit"] = deepcopy(row["role_audits"][0])
    assert creator_review._evidence(row)["documented"] is False


def test_excel_identity_can_be_primary_without_a_single_chat():
    row = documented_row()
    row["role_audits"][0]["evidence"]["primary_identity_evidence"] = {
        "kind": "excel_identity", "resolution_rule": "explicit_full_name_unique_match"}
    row["assignment_audit"] = deepcopy(row["role_audits"][0])
    facts = creator_review._evidence(row)
    assert facts["documented"] is True
    assert facts["group_ids"] == []


def test_legacy_excel_rows_and_handles_are_shown_without_inventing_a_chat_scope():
    row = documented_row()
    row.update(role_source="manual_excel_import", role_audits=[
        {"handle": "handle.one", "excel_rows": "446", "owner_label": "Importer label"},
        {"handle": "handle.two", "excel_rows": "447"}], assignment_audit={})
    facts = creator_review._evidence(row)
    assert facts["handles"] == ["handle.one", "handle.two"]
    assert facts["rows"] == [446, 447]
    assert facts["documented"] is False
    assert facts["group_ids"] == []


def test_directory_filters_handle_owner_and_evidence_without_exposing_raw_audit(monkeypatch):
    first = documented_row()
    second = {**documented_row(), "id": UUID(int=4), "role_source": "manual_p2p_confirmed",
              "display_name": "Other Creator"}
    conn = bind(monkeypatch, [first, second])
    result = creator_review.directory(99, " example.HANDLE ", OWNER, "documented")
    assert result["total_count"] == 1 and result["page"] == 1
    assert result["summary"] == {"creators": 2, "handles": 1, "documented": 1, "needs_review": 1,
                                 "archive_needed": 1, "identity_review": 0}
    assert result["items"][0]["id"] == CONTACT
    assert "role_audits" not in result["items"][0]
    assert "role.role = 'creator' AND role.status = 'confirmed' AND role.is_primary" in conn.calls[0][0]
    assert "internal.tenant_classification = 'internal'" in conn.calls[0][0]


def conversation_row(**changes):
    return {"chat_id": CHAT, "name": "Account x TT", "chat_mode": "group", "message_count": 42,
            "last_message_at": NOW, "source_names": ["Another CM", "Owner Person"], **changes}


@pytest.mark.parametrize("changes", [
    {"role_source": "manual_p2p_confirmed"},
    {"role_source": "manual_excel_import"},
    {"assignment_id": None, "responsible_id": None, "responsible_name": None, "assignment_audit": {}},
    {"assignment_id": UUID(int=99), "assignment_audit": {}},
])
def test_history_is_available_independent_of_evidence_or_current_assignment(monkeypatch, changes):
    row = {**documented_row(), "role_source": "manual_p2p_confirmed"}
    row.update(changes)
    conn = bind(monkeypatch, row, [conversation_row()])
    result = creator_review.detail(CONTACT)
    assert len(result["conversations"]) == 1
    assert result["creator"]["evidence_status"] == "archive_needed"
    assert conn.calls[1][1] == (CONTACT, NOW, None, None)


def test_detail_lists_all_identity_related_sources_with_counts_and_times(monkeypatch):
    row = documented_row()
    conn = bind(monkeypatch, row, [conversation_row(), conversation_row(
        chat_id=OTHER_CHAT, chat_mode="p2p", name=None, source_names=["Another CM"])])
    result = creator_review.detail(CONTACT)
    assert conn.calls[1][1] == (CONTACT, NOW, None, None)
    group, direct = result["conversations"]
    assert group["context_label"] == "曾观察到达人账号参与"
    assert group["source_label"] == "Another CM、Owner Person"
    assert group["message_count"] == 42
    assert group["last_message_at_label"] == "2026-09-05 01:00 BST"
    assert direct["context_label"] == "与达人单聊"
    assert direct["name"] == "相关单聊"
    assert "原 CM / TAP CM 字段未留存" in result["source_owner_label"]


def test_messages_reject_noncreator_and_unrelated_chat(monkeypatch):
    bind(monkeypatch, None)
    assert creator_review.messages(CONTACT, CHAT) is None
    conn = bind(monkeypatch, documented_row(), None)
    assert creator_review.messages(CONTACT, OTHER_CHAT) is None
    assert len(conn.calls) == 2
    assert conn.calls[1][1] == (CONTACT, NOW, None, None, OTHER_CHAT)
    assert creator_review.messages(CONTACT, "' OR 1=1 --") is None


def test_each_message_read_rechecks_creator_and_all_current_authorized_sources(monkeypatch):
    cipher = message_body_cipher(b"r" * 32)
    conn = bind(monkeypatch, documented_row(), {"chat_id": CHAT}, {"total_count": 1}, [
        {"body_ciphertext": cipher.encrypt('{"text":"Hello creator"}'),
         "message_create_time": NOW, "sent_by_author": True, "sent_by_creator": False,
         "sender_name": "Owner Person", "sender_type": "user", "message_type": "text"}])
    monkeypatch.setattr(creator_review, "get_settings", lambda: SimpleNamespace(token_encryption_key=b"r" * 32))
    result = creator_review.messages(CONTACT, CHAT)
    assert result["items"] == [{"sender_label": "Owner Person", "sender_role_label": "当前负责人",
                                "created_at_label": "2026-09-05 01:00 BST", "content": "Hello creator",
                                "sent_by_author": True, "message_type_label": "文字"}]
    for sql, params in conn.calls[2:]:
        assert params[:5] == (CONTACT, NOW, None, None, CHAT)
        for guard in ["role.role = 'creator' AND role.status = 'confirmed' AND role.is_primary",
                      "source.status = 'active' AND source.body_consent_at IS NOT NULL",
                      "access.is_visible AND access.sync_enabled", "role.updated_at = %s",
                      "observation.observed_at >= scope.body_consent_at", "NOT message.is_deleted",
                      "observation.authorization_id = scope.source_id", "counterpart.contact_id = target.id",
                      "witness.status = 'active'", "seen.observed_at >= source.body_consent_at"]:
            assert guard in sql
        for obsolete in ["role.source =", "assignment_eligible", "message.message_type = 'text'",
                         "message.body_ciphertext IS NOT NULL", "chat.chat_id = ANY"]:
            assert obsolete not in sql


@pytest.mark.parametrize("reader", ["detail", "messages"])
def test_owner_scoped_reader_stops_before_conversations_for_missing_assignment(monkeypatch, reader):
    conn = bind(monkeypatch, None)
    args = (CONTACT,) if reader == "detail" else (CONTACT, CHAT)
    assert getattr(creator_review, reader)(*args, responsible_id=OWNER) is None
    assert len(conn.calls) == 1
    sql, params = conn.calls[0]
    assert params == (CONTACT, OWNER, OWNER)
    assert "assignment.ended_at IS NULL" in sql
    assert "assignment.responsible_authorization_id = %s::uuid" in sql


def test_owner_scope_rechecks_live_assignment_in_conversation_and_every_message_query(monkeypatch):
    detail_conn = bind(monkeypatch, documented_row(), [conversation_row()])
    result = creator_review.detail(CONTACT, responsible_id=OWNER)
    assert result["conversations"][0]["source_label"] == "Another CM、Owner Person"
    assert detail_conn.calls[0][1] == (CONTACT, OWNER, OWNER)
    assert detail_conn.calls[1][1] == (CONTACT, NOW, OWNER, OWNER)

    conn = bind(monkeypatch, documented_row(), {"chat_id": CHAT}, {"total_count": 0}, [])
    monkeypatch.setattr(creator_review, "get_settings", lambda: SimpleNamespace(token_encryption_key=b"r" * 32))
    assert creator_review.messages(CONTACT, CHAT, responsible_id=OWNER)["items"] == []
    assert conn.calls[0][1] == (CONTACT, OWNER, OWNER)
    for sql, params in [detail_conn.calls[1], *conn.calls[1:]]:
        assert params[:4] == (CONTACT, NOW, OWNER, OWNER)
        assert "scoped_assignment.contact_id = target.id" in sql
        assert "scoped_assignment.ended_at IS NULL" in sql
        assert "scoped_assignment.responsible_authorization_id = %s::uuid" in sql
        assert "source.status = 'active' AND source.body_consent_at IS NOT NULL" in sql
    assert all(params[4] == CHAT for _, params in conn.calls[1:])


def test_owner_handover_before_chat_scope_check_never_reads_message_bodies(monkeypatch):
    conn = bind(monkeypatch, documented_row(), None)
    assert creator_review.messages(CONTACT, CHAT, responsible_id=OWNER) is None
    assert len(conn.calls) == 2
    assert "body_ciphertext" not in conn.calls[-1][0]
    assert conn.calls[-1][1] == (CONTACT, NOW, OWNER, OWNER, CHAT)


def test_text_renderer_extracts_stored_body_only_and_masks_common_identifiers():
    cipher = message_body_cipher(b"r" * 32)
    def render(value):
        return creator_review._text_content(cipher.encrypt(value), cipher)
    text = render(json.dumps({"text": "email a+b@example.org tel +44 7700 900123 ou_secret0123"}))
    assert "example.org" not in text and "7700" not in text and "ou_secret" not in text
    assert render('{"forward":{"text":"stored context"}}') == "stored context"
    for value in ['{"file":{"key":"secret"}}', '{"parent_id":"secret"}', '"raw secret"']:
        assert "secret" not in render(value)
    assert creator_review._text_content(cipher.encrypt("stored forwarded text"), cipher, "merge_forward") == (
        "stored forwarded text")
    assert creator_review._text_content(None, cipher) == "（正文尚未同步）"
    assert "图片" in creator_review._text_content(cipher.encrypt('{"image_key":"private"}'), cipher, "image")
    assert creator_review._text_content("invalid-ciphertext", cipher) == "（正文无法读取）"


def test_other_participants_media_and_missing_bodies_are_not_silently_removed(monkeypatch):
    cipher = message_body_cipher(b"r" * 32)
    row = {**documented_row(), "role_source": "manual_p2p_confirmed", "assignment_audit": {}}
    messages = [{"body_ciphertext": body, "message_create_time": NOW, "sent_by_author": False,
                 "sent_by_creator": False, "sender_name": "Another participant", "sender_type": "user",
                 "message_type": kind} for body, kind in [
                     (cipher.encrypt('{"text":"Other speaker context"}'), "text"),
                     (cipher.encrypt('{"image_key":"private-key"}'), "image"), (None, "post")]]
    bind(monkeypatch, row, {"chat_id": CHAT}, {"total_count": 3}, messages)
    monkeypatch.setattr(creator_review, "get_settings", lambda: SimpleNamespace(token_encryption_key=b"r" * 32))
    result = creator_review.messages(CONTACT, CHAT)
    assert result["total_count"] == 3
    assert all(item["sender_role_label"] == "其他参与者" for item in result["items"])
    assert result["items"][0]["content"] == "Other speaker context"
    assert result["items"][1]["message_type_label"] == "图片"
    assert "private-key" not in result["items"][1]["content"]
    assert result["items"][2]["content"] == "（正文尚未同步）"


def test_conversation_and_message_queries_deduplicate_by_canonical_keys():
    assert "GROUP BY chat_id, name, chat_mode" in creator_review._CONVERSATIONS_SQL
    assert "array_agg(DISTINCT source_name" in creator_review._CONVERSATIONS_SQL
    for sql in [creator_review._CONVERSATIONS_SQL, creator_review._VISIBLE_MESSAGES_SQL]:
        assert "FROM chat_messages message" in sql
        assert "EXISTS (" in sql
        assert "observation.message_id = message.message_id" in sql
    scope = creator_review._CONVERSATION_SCOPE_SQL
    assert "member.chat_id = chat.chat_id AND identity.contact_id = target.id" in scope
    assert "counterpart.open_id = access.p2p_counterpart_open_id" in scope
    assert "identity.tenant_classification = 'known_external'" in scope
    assert "counterpart.identity_type = 'user'" in scope
    assert "ILIKE" not in scope and "chat.name" not in scope


def test_view_audit_records_only_target_identifiers(monkeypatch):
    conn = bind(monkeypatch, None)
    creator_review.record_view(OWNER, CONTACT, CHAT)
    sql, params = conn.calls[0]
    assert "creator_review_messages_viewed" in sql
    assert params[1] == OWNER
    assert json.loads(params[2]) == {"contact_id": str(CONTACT), "chat_id": CHAT}
