from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

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
    assert admin_reporting.normalize_creator_assignment_filter("pending") == "pending"
    assert admin_reporting.normalize_creator_assignment_filter("unexpected") == "all"


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
