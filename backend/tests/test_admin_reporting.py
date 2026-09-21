import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.exceptions import InvalidTag

from app import admin_reporting


def test_reporting_page_window_is_bounded_and_fetches_one_extra_row():
    page_size = admin_reporting.LIST_PAGE_SIZE
    assert admin_reporting._page_window(0) == (1, page_size + 1, 0)
    assert admin_reporting._page_window(2) == (2, page_size + 1, page_size)
    assert admin_reporting._page_window(99_999) == (
        admin_reporting.MAX_PAGE,
        page_size + 1,
        (admin_reporting.MAX_PAGE - 1) * page_size,
    )
    assert admin_reporting._page_window(2, admin_reporting.DETAIL_PAGE_SIZE) == (2, 11, 10)


def test_reporting_pagination_uses_exact_totals_and_preserves_empty_results():
    assert admin_reporting._page_window(99, 5, total_count=17) == (4, 6, 15)

    page = admin_reporting._page_result([], 1, 5, total_count=0)

    assert page == {
        "items": [],
        "page": 1,
        "page_size": 5,
        "total_count": 0,
        "total_pages": 0,
        "has_next": False,
    }
    assert admin_reporting._message_count_label(0) == "尚无消息"


def test_message_conversation_filter_values_are_bounded_and_allowlisted():
    assert admin_reporting._page_window(2, admin_reporting.MESSAGE_CONVERSATION_PAGE_SIZE) == (2, 21, 20)
    assert admin_reporting.normalize_message_conversation_query("  Creator  ") == "Creator"
    assert admin_reporting.normalize_message_conversation_query("x" * 121) == "x" * 120
    assert admin_reporting.normalize_message_conversation_type("p2p") == "p2p"
    assert admin_reporting.normalize_message_conversation_type("unknown") == "all"
    assert admin_reporting.normalize_contact_category("creator") == "creator"
    assert admin_reporting.normalize_contact_category("unexpected") == "all"
    assert admin_reporting.normalize_organization_status("verified") == "verified"
    assert admin_reporting.normalize_organization_status("unexpected") == "all"
    assert admin_reporting.normalize_organization_query("  TT   MEDIA ITALY  ") == "TT MEDIA ITALY"
    assert admin_reporting.normalize_organization_query("x" * 121) == "x" * 120
    assert admin_reporting.normalize_user_permission_filter("messages") == "messages"
    assert admin_reporting.normalize_user_permission_filter("creator-review") == "all"
    assert admin_reporting.normalize_user_permission_filter("none") == "none"
    assert admin_reporting.normalize_user_permission_filter("unexpected") == "all"


def test_member_count_labels_preserve_pending_and_unavailable_states():
    assert admin_reporting._member_count(7, "succeeded") == "7"
    assert admin_reporting._member_count(None, "pending") == "正在获取"
    assert admin_reporting._member_count(None, "unavailable") == "Lark 未返回"


def test_contact_labels_do_not_imply_identity_or_ownership():
    contact = admin_reporting._decorate_contact(
        {
            "display_name": "Ada",
            "tenant_classification": "known_external",
            "primary_role": "partner_contact",
            "category": "partner_contact",
            "confirmed_roles": ["partner_contact"],
            "organization_name": "TT MEDIA ITALY",
            "organization_status": "verified",
        }
    )
    assert contact["display_name"] == "Ada"
    assert contact["tenant_classification_label"] == "外部成员"
    assert contact["category_label"] == "其他机构负责人"
    assert contact["confirmed_role_labels"] == ["其他机构负责人"]
    assert contact["organization_status_label"] == "已验证"
    assert contact["is_confirmed"] is True

    unnamed = admin_reporting._decorate_contact({"display_name": None, "tenant_classification": "unresolved"})
    assert unnamed["display_name"] == "Lark 参与者（姓名未返回）"
    assert unnamed["tenant_classification_label"] == "租户未返回"
    assert unnamed["confirmed_role_labels"] == []
    assert unnamed["is_confirmed"] is False
    assert unnamed["category"] == "unclassified"
    assert unnamed["category_label"] == "待识别"
    assert unnamed["organization_status_label"] == "未解析"


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
    avatar_host = "https://s1-imfile.feishucdn.com/static-resource/v1/"
    for name in ("manual", "AI"):
        pg.execute("UPDATE lark_identities SET display_name='Same Lark name',avatar_url=%s WHERE open_id=%s",
                   (avatar_host + name + ".png", "ou_" + name))
    pg.execute("INSERT INTO lark_identities(open_id,contact_id,display_name,avatar_url,last_seen_at) "
               "VALUES('ou_AI_old',%s,'Same Lark name',%s,NOW()-INTERVAL '1 day')",
               (people["AI"], avatar_host + "old.png"))
    directory = admin_reporting.contacts()
    by_name = {row["display_name"]: row for row in directory["items"]}
    assert directory["total_count"] == 8 and by_name["orphan"]["open_id"] is None
    assert by_name["orphan"]["avatar_url"] is None
    assert by_name["manual"]["avatar_url"] == avatar_host + "manual.png"
    assert by_name["AI"]["open_id"] == "ou_AI" and by_name["AI"]["avatar_url"] == avatar_host + "AI.png"
    assert by_name["AI"]["lark_identity_count"] == 2
    assert by_name["manual"]["category"] == "merchant_contact"
    assert by_name["internal"]["classification_source_label"] == "已核实同事"
    assert by_name["old"]["category"] == by_name["low"]["category"] == "unclassified"
    for name in ("manual", "internal", "old", "low", "orphan"):
        assert by_name[name]["identity_ai_confidence_label"] == "—"
    assert by_name["AI"]["identity_ai_confidence_label"] == "97.0%"
    assert by_name["queued"]["identity_ai_status_label"] == "已暂停，队列保留"
    summary = directory["identity_summary"]
    assert {key: summary[key] for key in ("total", "processed", "ai_results", "no_material", "exceptions",
                                         "queued", "not_started")} == {
        "total": 8, "processed": 5, "ai_results": 2, "no_material": 1, "exceptions": 1,
        "queued": 1, "not_started": 1,
    }
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


def test_seconds_label_prefers_human_readable_units():
    assert admin_reporting._seconds_label(300) == "5 分钟"
    assert admin_reporting._seconds_label(3600) == "1 小时"
    assert admin_reporting._seconds_label(45) == "45 秒"


def test_message_body_renderer_extracts_text_without_showing_raw_json():
    assert admin_reporting._readable_body('{"text":"hello"}', "text") == "hello"
    assert admin_reporting._readable_body('{"zh_cn":{"content":[[{"text":"hello"}]]}}', "post") == "hello"


def test_sync_summary_shows_initial_progress_instead_of_a_generic_running_job():
    label, badge, detail = admin_reporting.sync_summary(
        {
            "authorization_status": "active",
            "syncable_conversation_count": 295,
            "backfill_complete_count": 37,
            "syncing_conversation_count": 1,
            "waiting_conversation_count": 257,
            "not_readable_conversation_count": 0,
            "archived_conversation_count": 9,
            "job_mode": "backfill",
            "job_status": "running",
        }
    )

    assert (label, badge) == ("首次同步中", "primary")
    assert "37/295" in detail
    assert "1 个正在读取" in detail
    assert "9 个 Lark 已归档/解散会话不计划回填" in detail


def test_sync_summary_treats_future_incremental_work_as_a_schedule_not_a_queue():
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    label, badge, detail = admin_reporting.sync_summary(
        {
            "authorization_status": "active",
            "syncable_conversation_count": 13,
            "backfill_complete_count": 13,
            "syncing_conversation_count": 0,
            "waiting_conversation_count": 0,
            "not_readable_conversation_count": 0,
            "archived_conversation_count": 0,
            "job_mode": "incremental",
            "job_status": "queued",
            "job_scheduled_at": now + timedelta(minutes=5),
        },
        now=now,
    )

    assert (label, badge) == ("已接入 · 定时同步", "success")
    assert "下一轮检查" in detail


def test_sync_status_shows_both_current_lark_rate_limit_variants():
    assert admin_reporting._job_status("running", last_http_status=429) == ("Lark 限流暂停", "warning")
    assert admin_reporting._job_status("queued", last_error_code=99991400) == ("Lark 限流暂停", "warning")


def test_member_snapshot_retry_is_distinct_from_the_daily_refresh_state():
    label = admin_reporting._member_snapshot_activity(
        {
            "chat_mode": "group",
            "member_snapshot_status": "pending",
            "member_snapshot_retry_at": datetime(2026, 9, 3, 12, tzinfo=UTC),
        }
    )

    assert label.startswith("等待重试于")


def test_message_retry_is_shown_as_a_specific_delayed_conversation():
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    label, badge, detail = admin_reporting.sync_summary(
        {
            "authorization_status": "active",
            "syncable_conversation_count": 4,
            "backfill_complete_count": 3,
            "syncing_conversation_count": 0,
            "waiting_conversation_count": 1,
            "not_readable_conversation_count": 0,
            "archived_conversation_count": 0,
            "job_mode": "backfill",
            "job_status": "queued",
            "job_scheduled_at": now + timedelta(minutes=2),
        },
        now=now,
    )

    assert (label, badge) == ("等待会话重试", "primary")
    assert "下一次会话重试于" in detail
    assert admin_reporting._message_page_activity(
        {"sync_status": "failed_retryable", "page_retry_at": now + timedelta(minutes=2)}
    ).startswith("等待重试于")


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
    assert "strpos(LOWER" in calls[0][0]
    assert "a.status = 'active'" in calls[0][0]
    assert "admin_page_permissions" in calls[0][0]
    for query, _ in calls:
        assert "COALESCE(r.role = 'owner', FALSE) AND COALESCE(r.is_active, FALSE)" in query
        assert "NOT COALESCE(r.role = 'owner', FALSE) AND EXISTS" in query
        assert "LEFT JOIN admin_roles r ON r.authorization_id = a.id AND r.is_active" not in query
    assert "COUNT(*) FILTER (WHERE COALESCE(r.role = 'owner', FALSE)) AS owners" in calls[0][0]
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
    for user in (listing["items"][0], detail["user"]):
        assert user["message_count"] == user["own_message_count"] == 10
    assert detail["conversations"]["total_count"] == 10
    assert all(chat["message_count"] == 1 for chat in detail["conversations"]["items"])
    assert admin_reporting.users(query="missing")["items"] == []


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
    for query, _ in calls:
        assert admin_reporting.MESSAGE_CONVERSATION_DIRECTORY_SQL in query
        assert admin_reporting.MESSAGE_CONVERSATION_FILTER_SQL in query
    assert "ORDER BY LOWER(COALESCE(c.name, counterpart.display_name, '')), ac.chat_id" in calls[1][0]
    assert "ORDER BY LOWER(COALESCE(name, counterpart_display_name, '')), chat_id" in calls[1][0]


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
    for query, _ in calls:
        for condition in (
            "a.id = %s", "a.status = 'active'", "a.body_consent_at IS NOT NULL",
            "o.observed_at >= a.body_consent_at", "ac.is_visible AND ac.sync_enabled",
            "COALESCE(c.chat_status, 'normal') = 'normal'", "NOT m.is_deleted", "m.chat_id = %s",
            "o.authorization_id = ac.authorization_id AND o.chat_id = ac.chat_id",
            "m.chat_id = o.chat_id AND m.message_id = o.message_id",
        ):
            assert condition in query
    assert "COUNT(*) FILTER (WHERE body_ciphertext IS NOT NULL) AS content_count" in calls[0][0]
    assert "WHERE body_ciphertext IS NOT NULL" in calls[1][0]
    assert "ORDER BY message_create_time DESC NULLS LAST, message_id DESC" in calls[1][0]
    # Equal timestamps retain the inverse of the database's deterministic ID tie-breaker.
    assert page["items"][0]["message_create_time"] == page["items"][1]["message_create_time"]
    assert [item["message_id"] for item in page["items"][:2]] == ["m_102", "m_103"]


@pytest.mark.parametrize(
    "message_type,label",
    [("text", "文字"), ("post", "富文本"), ("interactive", "卡片"), ("image", "图片"),
     ("file", "文件"), ("audio", "语音"), ("media", "视频"), ("sticker", "表情"),
     ("merge_forward", "合并转发"), ("share_chat", "分享群聊"), ("share_user", "分享联系人"),
     (None, "其他消息"), ("unknown", "其他消息")],
)
def test_message_type_labels_match_the_rendered_body(message_type, label):
    assert admin_reporting._message_type_label(message_type) == label
    item = admin_reporting._decorate_message(
        {"message_type": message_type, "body_ciphertext": "synthetic"},
        SimpleNamespace(decrypt=lambda value: '{}'),
    )
    assert item["message_type_label"] == label
    assert item["content"] == f"（{message_type or '未知类型'} 消息没有可展示的文本）"


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
