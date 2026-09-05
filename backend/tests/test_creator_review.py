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
    assert result["summary"] == {"creators": 2, "handles": 1, "documented": 1, "needs_review": 1}
    assert result["items"][0]["id"] == CONTACT
    assert "role_audits" not in result["items"][0]
    assert "role.role = 'creator' AND role.status = 'confirmed' AND role.is_primary" in conn.calls[0][0]
    assert "internal.tenant_classification = 'internal'" in conn.calls[0][0]


def test_detail_does_not_query_any_conversation_for_insufficient_evidence(monkeypatch):
    row = {**documented_row(), "role_source": "manual_p2p_confirmed"}
    conn = bind(monkeypatch, row)
    result = creator_review.detail(CONTACT)
    assert result["conversations"] == []
    assert len(conn.calls) == 1
    assert "未留存" in result["creator"]["evidence_reason"]


def test_detail_passes_only_explicit_primary_groups_and_never_raw_identity_ids(monkeypatch):
    row = documented_row()
    conn = bind(monkeypatch, row, [{"chat_id": CHAT, "name": "Account x TT", "chat_mode": "group"}])
    result = creator_review.detail(CONTACT)
    assert conn.calls[1][1] == (CONTACT, ASSIGNMENT, OWNER, NOW, [CHAT])
    assert result["conversations"][0]["context_label"] == "主证据关联群 · 局部文字视图"
    assert "原 CM / TAP CM 字段未留存" in result["source_owner_label"]


def test_messages_reject_noncreator_insufficient_evidence_and_unrelated_chat(monkeypatch):
    bind(monkeypatch, None)
    assert creator_review.messages(CONTACT, CHAT) is None
    conn = bind(monkeypatch, {**documented_row(), "role_source": "manual_p2p_confirmed"})
    assert creator_review.messages(CONTACT, CHAT) is None
    assert len(conn.calls) == 1
    conn = bind(monkeypatch, documented_row(), None)
    assert creator_review.messages(CONTACT, OTHER_CHAT) is None
    assert len(conn.calls) == 2
    assert conn.calls[1][1][-2:] == ([CHAT], OTHER_CHAT)
    assert creator_review.messages(CONTACT, "' OR 1=1 --") is None


def test_each_message_read_rechecks_current_authorization_scope_and_limits_senders(monkeypatch):
    cipher = message_body_cipher(b"r" * 32)
    conn = bind(monkeypatch, documented_row(), {"chat_id": CHAT}, {"total_count": 1}, [
        {"body_ciphertext": cipher.encrypt('{"text":"Hello creator"}'),
         "message_create_time": NOW, "sent_by_author": True}])
    monkeypatch.setattr(creator_review, "get_settings", lambda: SimpleNamespace(token_encryption_key=b"r" * 32))
    result = creator_review.messages(CONTACT, CHAT)
    assert result["items"] == [{"sender_label": "Owner Person", "sender_role_label": "负责人",
                                "created_at_label": "2026-09-05 01:00 BST", "content": "Hello creator",
                                "sent_by_author": True}]
    for sql, params in conn.calls[2:]:
        assert params[:6] == (CONTACT, ASSIGNMENT, OWNER, NOW, [CHAT], CHAT)
        for guard in ["assignment.ended_at IS NULL", "responsible.status = 'active'",
                      "responsible.body_consent_at IS NOT NULL", "access.is_visible AND access.sync_enabled",
                      "observation.observed_at >= scope.body_consent_at AND NOT message.is_deleted",
                      "observation.authorization_id = scope.responsible_id", "message.message_type = 'text'",
                      "sender.contact_id = scope.contact_id", "counterpart.contact_id = target.id",
                      "message.sender_open_id = scope.responsible_open_id", "chat.chat_id = ANY(%s::text[])"]:
            assert guard in sql


def test_text_renderer_does_not_expand_nested_content_and_masks_common_identifiers():
    cipher = message_body_cipher(b"r" * 32)
    def render(value):
        return creator_review._text_content(cipher.encrypt(value), cipher)
    text = render(json.dumps({"text": "email a+b@example.org tel +44 7700 900123 ou_secret0123"}))
    assert "example.org" not in text and "7700" not in text and "ou_secret" not in text
    for value in ['{"forward":{"text":"secret"}}', '{"text":"safe","forward":{"text":"secret"}}',
                  '{"file":{"name":"secret"}}', '"raw secret"', 'raw secret']:
        assert "secret" not in render(value)
    assert creator_review._text_content("invalid-ciphertext", cipher) == "（正文无法读取）"


def test_view_audit_records_only_target_identifiers(monkeypatch):
    conn = bind(monkeypatch, None)
    creator_review.record_view(OWNER, CONTACT, CHAT)
    sql, params = conn.calls[0]
    assert "creator_review_messages_viewed" in sql
    assert params[1] == OWNER
    assert json.loads(params[2]) == {"contact_id": str(CONTACT), "chat_id": CHAT}
