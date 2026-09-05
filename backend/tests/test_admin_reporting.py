from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

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
    assert admin_reporting._page_window(2, admin_reporting.MESSAGE_CONVERSATION_PAGE_SIZE) == (2, 6, 5)
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
    assert admin_reporting.normalize_creator_assignment_filter("pending") == "pending"
    assert admin_reporting.normalize_creator_assignment_filter("unexpected") == "all"
    assert admin_reporting.normalize_user_permission_filter("creator-review") == "creator-review"
    assert admin_reporting.normalize_user_permission_filter("none") == "none"
    assert admin_reporting.normalize_user_permission_filter("unexpected") == "all"


def test_creator_pending_filter_requires_the_latest_actionable_assignment_suggestion(monkeypatch):
    calls = []

    class Result:
        def __init__(self, *, row=None, rows=None):
            self.row = row
            self.rows = rows or []

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class Connection:
        def execute(self, query, params=None):
            calls.append((query, params))
            return Result(row={"total_count": 0}) if len(calls) == 1 else Result()

    monkeypatch.setattr(admin_reporting, "connection", lambda: nullcontext(Connection()))

    page = admin_reporting.creators(assignment="pending")

    assert page["items"] == []
    query, params = calls[0]
    assert params == (0.7, "direct_evidence_v2", "pending", "pending")
    assert "assessment.outcome = 'review_required'" in query
    assert "latest_assessment.decision IN ('assign', 'needs_review')" in query
    assert "latest_assessment.confidence >= %s::numeric" in query
    assert "assessment.policy_version = %s" in query
    assert "automatic_threshold" not in query


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
    assert contact["category_label"] == "合作机构对接人"
    assert contact["confirmed_role_labels"] == ["合作机构对接人"]
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
    page = admin_reporting.users(99, "  Remi_%  ", "creator-review")
    expected_filter = ("Remi_%", *(["creator-review"] * 5), sorted(admin_reporting.PAGE_KEYS), "creator-review")
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


def test_contact_detail_skips_ungranted_conversation_queries(monkeypatch):
    calls = _empty_reporting_database(monkeypatch, {"contact_id": uuid4(), "tenant_key": None})
    detail = admin_reporting.contact_detail("ou_contact", include_conversations=False)
    assert len(calls) == 3
    assert all("conversation_ids" not in query and "chat_messages" not in query for query, _ in calls)
    assert detail["conversations"]["items"] == []
