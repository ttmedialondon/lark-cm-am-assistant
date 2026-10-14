import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.exceptions import InvalidTag

from app import admin_reporting


def test_people_directory_all_contacts_current_policy_priority_and_unfiltered_progress(pg, monkeypatch):  # noqa: F811
    monkeypatch.setattr(admin_reporting, "connection", lambda: nullcontext(pg))
    people = {}
    cases = [
        ("manual", "merchant_contact", "manual", "succeeded", "protected_manual", None),
        ("internal", "internal_colleague", "lark_internal_tenant", "succeeded", "verified_internal", None),
        ("old", "creator", "ai_auto_confirmed", "succeeded", "classified", 0.99),
        ("low", "creator", "ai_auto_confirmed", "succeeded", "low_confidence", 0.99),
        ("AI", "creator", "ai_auto_confirmed", "succeeded", "classified", 0.97),
        ("orphan", None, None, "succeeded", "no_readable_messages", None),
        ("failed", None, None, "invalid", None, None),
        ("queued", None, None, "queued", None, None),
    ]
    for name, role, source, status, reason, confidence in cases:
        contact_id = uuid4()
        people[name] = contact_id
        pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,%s)", (contact_id, name))
        if name != "orphan":
            pg.execute("INSERT INTO lark_identities(open_id,contact_id,display_name,tenant_classification) "
                       "VALUES(%s,%s,%s,%s)", ("ou_" + name, contact_id, name,
                                              "internal" if name == "internal" else "unresolved"))
        if role:
            pg.execute("INSERT INTO contact_roles(contact_id,role,status,source,is_primary) "
                       "VALUES(%s,%s,'confirmed',%s,TRUE)", (contact_id, role, source))
        payload = {"reason_code": reason,
                   "roles": [{"role": "insufficient" if name == "low" else role}] if role else []}
        if confidence is not None:
            payload["roles"][0]["confidence"] = confidence
        pg.execute("INSERT INTO contact_assessments(id,contact_id,status,policy_version,result) "
                   "VALUES(%s,%s,%s,%s,%s::jsonb)",
                   (uuid4(), contact_id, status, "legacy" if name == "old" else admin_reporting.CONTACT_IDENTITY_POLICY,
                    json.dumps(payload)))
    # Source priority beats an AI role marked primary; untrusted tenant claims never win.
    for name in ("manual", "internal"):
        pg.execute("UPDATE contact_roles SET is_primary=FALSE WHERE contact_id=%s", (people[name],))
        pg.execute("INSERT INTO contact_roles(contact_id,role,status,source,is_primary) "
                   "VALUES(%s,'creator','confirmed','ai_auto_confirmed',TRUE)", (people[name],))
        pg.execute("UPDATE contact_assessments SET result=%s::jsonb WHERE contact_id=%s",
                   (json.dumps({"reason_code": "classified", "roles": [{"role": "creator"}]}), people[name]))
    avatar_host = "https://s1-imfile.feishucdn.com/static-resource/v1/"
    for name in ("manual", "AI"):
        pg.execute("UPDATE lark_identities SET display_name='Same Lark name',avatar_url=%s WHERE open_id=%s",
                   (avatar_host + name + ".png", "ou_" + name))
    pg.execute("INSERT INTO lark_identities(open_id,contact_id,display_name,avatar_url,last_seen_at) "
               "VALUES('ou_AI_old',%s,'Same Lark name',%s,NOW()-INTERVAL '1 day')",
               (people["AI"], avatar_host + "old.png"))
    pg.execute("INSERT INTO lark_identities(open_id,contact_id,last_seen_at) "
               "VALUES('ou_manual_old',%s,NOW()-INTERVAL '1 day')", (people["manual"],))
    owner = uuid4()
    pg.execute("INSERT INTO authorizations(id,open_id,access_token_ciphertext) VALUES(%s,'directory_owner','fixture')",
               (owner,))
    for index in range(13):
        chat = f"directory_{index:02}"
        pg.execute("INSERT INTO chats(chat_id,name,chat_status,chat_mode) VALUES(%s,%s,%s,%s)",
                   (chat, chat, 'dissolved' if index == 12 else 'normal', 'p2p' if index == 1 else 'group'))
        if index == 0:
            pg.execute("INSERT INTO chat_members(chat_id,open_id) VALUES(%s,'ou_manual')", (chat,))
        elif index == 1:
            pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_open_id) "
                       "VALUES(%s,%s,'ou_manual_old')", (owner, chat))
        else:
            pg.execute("INSERT INTO chat_messages(chat_id,message_id,sender_open_id,message_create_time) "
                       "VALUES(%s,'live',%s,NOW()-%s*INTERVAL '1 minute')",
                       (chat, 'ou_manual_old' if index % 2 == 0 else 'ou_manual', index))
            pg.execute("INSERT INTO chat_messages(chat_id,message_id,sender_open_id,is_deleted,message_create_time) "
                       "VALUES(%s,'deleted','ou_manual',TRUE,NOW()+INTERVAL '1 day')", (chat,))
    directory = admin_reporting.contacts()
    by_name = {row["display_name"]: row for row in directory["items"]}
    assert directory["total_count"] == 8 and by_name["orphan"]["open_id"] is None
    assert by_name["orphan"]["avatar_url"] is None
    assert by_name["manual"]["avatar_url"] == avatar_host + "manual.png"
    assert by_name["AI"]["open_id"] == "ou_AI" and by_name["AI"]["avatar_url"] == avatar_host + "AI.png"
    assert by_name["AI"]["lark_identity_count"] == 2
    assert by_name["manual"]["category"] == "merchant_contact"
    assert by_name["manual"]["conversation_count"] == 13 and by_name["manual"]["message_count"] == 11
    details = admin_reporting.contact_detail("ou_manual")
    assert details["conversations"]["total_count"] == 12
    assert [row["chat_id"] for row in details["conversations"]["items"]] == [
        f"directory_{index:02}" for index in range(2, 12)]
    assert all(row["message_count"] == row["contact_message_count"] == 1
               for row in details["conversations"]["items"])
    last = admin_reporting.contact_detail("ou_manual", conversation_page=99)["conversations"]
    assert last["page"] == 2 and [row["chat_id"] for row in last["items"]] == ["directory_00", "directory_01"]
    assert all(row["message_count"] == row["contact_message_count"] == 0 for row in last["items"])
    assert by_name["internal"]["classification_source_label"] == "已核实同事"
    assert by_name["old"]["category"] == by_name["low"]["category"] == "unclassified"
    summary = directory["identity_summary"]
    assert summary["total"] == 8 and summary["categories"]["creator"] == 1
    filtered = admin_reporting.contacts(category="creator", query=" AI ")
    assert [row["display_name"] for row in filtered["items"]] == ["AI"]
    assert filtered["identity_summary"] == summary and filtered["total_count"] == 1
    assert admin_reporting.contacts(page=999, category="creator", query="AI")["page"] == 1
    empty = admin_reporting.contacts(query="not-present")
    assert empty["items"] == [] and empty["total_count"] == 0 and empty["identity_summary"] == summary
    detail = admin_reporting.contact_detail("ou_AI", include_conversations=False)
    assert detail["contact"]["category"] == "creator"
    assert detail["conversations"]["items"] == []
    pg.execute("UPDATE lark_identities SET avatar_status='unavailable' WHERE open_id='ou_AI'")
    assert admin_reporting.contact_detail("ou_AI", include_conversations=False)["contact"]["avatar_url"] is None
    pg.execute("UPDATE lark_identities SET avatar_status='failed' WHERE open_id='ou_AI'")
    assert admin_reporting.contact_detail("ou_AI", include_conversations=False)["contact"]["avatar_url"] == (
        avatar_host + "AI.png")
    pg.execute("UPDATE lark_identities SET avatar_url=NULL WHERE open_id='ou_AI'")
    assert admin_reporting.contact_detail("ou_AI", include_conversations=False)["contact"]["avatar_url"] is None
    pg.execute("INSERT INTO contact_assessments(id,contact_id,status,policy_version,result,created_at) "
               "VALUES(%s,%s,'invalid','legacy','{}'::jsonb,NOW()+INTERVAL '2 hours')",
               (uuid4(), people["AI"]))
    assert admin_reporting.contacts(query="AI")["items"][0]["category"] == "creator"
    pg.execute("INSERT INTO contact_assessments(id,contact_id,status,policy_version,result,created_at) "
               "VALUES(%s,%s,'invalid',%s,'{}'::jsonb,NOW()+INTERVAL '1 hour')",
               (uuid4(), people["AI"], admin_reporting.CONTACT_IDENTITY_POLICY))
    assert admin_reporting.contacts(query="AI")["items"][0]["category"] == "unclassified"
    pg.execute("UPDATE lark_identities SET tenant_classification='known_external' WHERE open_id='ou_internal'")
    internal = admin_reporting.contact_detail("ou_internal", include_conversations=False)["contact"]
    assert internal["category"] == "creator"
    for person, handle, platform, relationship, status in (
        ("manual", "match.alpha", "tiktok", "owner", "confirmed"),
        ("manual", "match.beta", "tiktok", "owner", "confirmed"),
        ("AI", "match.gamma", "tiktok", "owner", "confirmed"),
        ("low", "match.rejected", "tiktok", "owner", "rejected"),
        ("queued", "match.representative", "tiktok", "representative", "confirmed"),
        ("old", "match.youtube", "youtube", "owner", "confirmed"),
    ):
        account = uuid4()
        pg.execute("INSERT INTO social_accounts(id,platform,normalized_handle,display_handle) VALUES(%s,%s,%s,%s)",
                   (account, platform, handle, handle))
        pg.execute("INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source) "
                   "VALUES(%s,%s,%s,%s,'manual')", (people[person], account, relationship, status))
    handles = admin_reporting.contacts(query="  MATCH.  ")
    assert handles["total_count"] == 2 and {row["contact_id"] for row in handles["items"]} == {
        people["manual"], people["AI"]}
    pg.execute("UPDATE contacts SET display_name='Duplicate Fixture' WHERE id=ANY(%s::uuid[])",
               ([people["queued"], people["failed"]],))
    duplicates = admin_reporting.contacts(query="duplicate fixture")
    assert duplicates["total_count"] == 2 and {row["contact_id"] for row in duplicates["items"]} == {
        people["queued"], people["failed"]}
    assert admin_reporting.contacts(query="")["total_count"] == 8


def _empty_reporting_database(monkeypatch, first_row):
    calls = []

    def execute(query, params=()):
        assert query.count("%s") == len(params)
        calls.append((query, params))
        return SimpleNamespace(fetchone=lambda: first_row, fetchall=lambda: [])

    monkeypatch.setattr(admin_reporting, "connection", lambda: nullcontext(SimpleNamespace(execute=execute)))
    return calls


def test_personnel_permission_filter_binds_values_and_keeps_global_summary(monkeypatch):
    calls = _empty_reporting_database(monkeypatch, {"total": 7, "backend": 2, "owners": 1, "total_count": 0})
    page = admin_reporting.users(99, "  Remi_%  ", "messages")
    expected_filter = ("Remi_%", *(["messages"] * 5), sorted(admin_reporting.PAGE_KEYS), "messages")
    assert calls[0][1] == expected_filter
    assert calls[1][1] == (*expected_filter, admin_reporting.LIST_PAGE_SIZE + 1, 0)
    assert all("chat_messages" not in sql and "authorization_message_observations" not in sql
               for sql, _params in calls)
    assert page["items"] == []
    assert page["page"] == 1
    assert page["total_count"] == 0
    assert page["summary"] == {"total": 7, "backend": 2, "owners": 1, "frontend": 5}


def test_personnel_detail_skips_ungranted_conversation_and_job_queries(monkeypatch):
    authorization_id = uuid4()
    calls = _empty_reporting_database(monkeypatch, {"id": authorization_id, "authorization_status": "active"})
    detail = admin_reporting.user_detail(authorization_id, include_conversations=False, include_jobs=False)
    assert len(calls) == 1
    assert detail["conversations"]["items"] == []
    assert detail["jobs"]["items"] == []


def test_personnel_counts_preserve_deleted_hidden_and_paginated_scope(pg, monkeypatch):
    monkeypatch.setattr(admin_reporting, "connection", lambda: nullcontext(pg))
    actor = uuid4()
    pg.execute("INSERT INTO authorizations(id,open_id,display_name,access_token_ciphertext) "
               "VALUES(%s,'reporting_actor','Reporting','fixture')", (actor,))
    for index in range(12):
        chat = f"reporting_{index:02}"
        pg.execute("INSERT INTO chats(chat_id,name,chat_status) VALUES(%s,%s,%s)",
                   (chat, chat, 'dissolved' if index == 11 else 'normal'))
        pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id,is_visible) VALUES(%s,%s,%s)",
                   (actor, chat, index != 10))
        for deleted in (False, True):
            message = str(deleted)
            pg.execute("INSERT INTO chat_messages(chat_id,message_id,is_deleted) VALUES(%s,%s,%s)",
                       (chat, message, deleted))
            pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id,"
                       "sent_by_author) VALUES(%s,%s,%s,TRUE)", (actor, chat, message))
    listing = admin_reporting.users(query="Reporting")
    detail = admin_reporting.user_detail(actor)
    assert detail["user"]["message_count"] == detail["user"]["own_message_count"] == 10
    assert not {"message_count", "own_message_count"} & listing["items"][0].keys()
    assert listing["items"][0]["group_count"] == 10 and listing["items"][0]["archived_conversation_count"] == 1
    assert detail["conversations"]["total_count"] == 10
    assert all(chat["message_count"] == 1 for chat in detail["conversations"]["items"])
    groups = admin_reporting.chats()
    assert groups["total_count"] == 11
    by_chat = {row["chat_id"]: row for row in groups["items"]}
    assert all(row["message_count"] == 1 for row in groups["items"])
    assert by_chat["reporting_00"]["visible_authorization_count"] == 1
    assert by_chat["reporting_10"]["visible_authorization_count"] == 0
    monkeypatch.setattr(admin_reporting, "runtime_settings", lambda: ())
    metrics = {metric["label"]: metric["value"] for metric in admin_reporting.overview()["metrics"]}
    assert metrics["去重后的群聊"] == 11 and metrics["人员—会话关系"] == 10
    assert metrics["去重后消息索引"] == 12 and metrics["已加密保存正文"] == 0
    assert admin_reporting.users(query="missing")["items"] == []
    other = uuid4()
    pg.execute("INSERT INTO authorizations(id,open_id,display_name,access_token_ciphertext) "
               "VALUES(%s,'reporting_other','Other','fixture')", (other,))
    selected = admin_reporting.message_users(source_ids=[actor, actor, uuid4()])
    assert [item["id"] for item in selected["items"]] == [actor]
    assert admin_reporting.message_users(source_ids=[])["items"] == []
    assert {item["id"] for item in admin_reporting.message_users(source_ids=[actor, other])["items"]} == {actor, other}


def test_contact_detail_skips_ungranted_conversation_queries(monkeypatch):
    calls = _empty_reporting_database(monkeypatch, {"contact_id": uuid4(), "tenant_key": None})
    detail = admin_reporting.contact_detail("ou_contact", include_conversations=False)
    assert len(calls) == 3
    assert all("conversation_ids" not in query and "chat_messages" not in query for query, _ in calls)
    assert detail["conversations"]["items"] == []


@pytest.mark.parametrize("requested_page,expected_page,offset", [(0, 1, 0), (2, 2, 20), (999_999, 3, 40)])
def test_message_directory_twenty_item_pages_clamp_and_preserve_scope(
    monkeypatch, requested_page, expected_page, offset
):
    authorization_id = uuid4()
    calls = _empty_reporting_database(monkeypatch, {"total_count": 41})
    page = admin_reporting.message_conversations(authorization_id, requested_page, "  synthetic  ", "p2p")
    directory_params = (authorization_id, "synthetic", "synthetic", "synthetic", "p2p", "p2p", "p2p")
    assert calls[0][1] == directory_params
    assert calls[1][1] == (*directory_params, 21, offset)
    assert page["page_size"] == 20
    assert page["page"] == expected_page
    assert page["total_count"] == 41
    assert page["total_pages"] == 3


@pytest.mark.parametrize("requested_page,expected_page,offset", [(0, 1, 0), (2, 2, 100), (999_999, 3, 200)])
def test_message_page_decrypts_only_selected_hundred_then_displays_chronologically(
    monkeypatch, requested_page, expected_page, offset
):
    calls = []
    decrypted = []
    authorization_id = uuid4()
    start = datetime(2026, 9, 5, 12, tzinfo=UTC)
    rows = [
        {
            "message_id": f"m_{index:03}", "message_create_time": start + timedelta(seconds=index // 2),
            "message_type": "text", "body_ciphertext": f"cipher-{index}",
        }
        for index in range(201, 100, -1)
    ]
    metrics = {"message_count": 205, "content_count": 201, "own_message_count": 99, "p2p_message_count": 205}

    def execute(query, params=()):
        assert query.count("%s") == len(params)
        calls.append((query, params))
        return SimpleNamespace(fetchone=lambda: metrics, fetchall=lambda: rows)

    def decrypt(ciphertext):
        decrypted.append(ciphertext)
        return '{"text":"synthetic test message"}'

    monkeypatch.setattr(admin_reporting, "connection", lambda: nullcontext(SimpleNamespace(execute=execute)))
    monkeypatch.setattr(admin_reporting, "get_settings", lambda: SimpleNamespace(token_encryption_key=b"test"))
    monkeypatch.setattr(admin_reporting, "message_body_cipher", lambda key: SimpleNamespace(decrypt=decrypt))
    page = admin_reporting.message_page(authorization_id, "oc_synthetic", requested_page)
    assert page["page"] == expected_page
    assert page["page_size"] == 100
    assert len(page["items"]) == 100
    assert page["metrics"] == metrics
    assert page["total_count"] == 201
    assert page["has_next"] is (expected_page < 3)
    assert [item["message_id"] for item in page["items"]] == [f"m_{index:03}" for index in range(102, 202)]
    assert decrypted == [f"cipher-{index}" for index in range(102, 202)]
    assert "cipher-101" not in decrypted
    assert calls[0][1] == (authorization_id, "oc_synthetic")
    assert calls[1][1] == (authorization_id, "oc_synthetic", 101, offset)
    # Equal timestamps retain the inverse of the database's deterministic ID tie-breaker.
    assert page["items"][0]["message_create_time"] == page["items"][1]["message_create_time"]
    assert [item["message_id"] for item in page["items"][:2]] == ["m_102", "m_103"]


@pytest.mark.parametrize(
    "created_at,date_label,time_label",
    [(datetime(2026, 7, 4, 23, 30, tzinfo=UTC), "2026-07-05", "2026-07-05 00:30 BST"),
     (datetime(2026, 1, 4, 23, 30, tzinfo=UTC), "2026-01-04", "2026-01-04 23:30 GMT"),
     (None, "日期未知", "时间未知")],
)
def test_message_date_heading_uses_london_and_retains_existing_timestamp(created_at, date_label, time_label):
    item = admin_reporting._decorate_message(
        {"message_create_time": created_at, "message_type": "text", "body_ciphertext": "synthetic"},
        SimpleNamespace(decrypt=lambda value: 'raw text fallback'),
    )
    assert item["created_date_label"] == date_label
    assert item["created_at_label"] == time_label
    assert item["content"] == "raw text fallback"


def test_message_decryption_failure_still_has_safe_display_labels():
    def decrypt(value):
        raise InvalidTag

    item = admin_reporting._decorate_message(
        {"message_type": "image", "body_ciphertext": "synthetic"}, SimpleNamespace(decrypt=decrypt)
    )
    assert item["content"] == "（正文无法读取）"
    assert item["message_type_label"] == "图片"
    assert item["created_date_label"] == "日期未知"
