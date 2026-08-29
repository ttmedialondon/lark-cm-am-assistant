from app import admin_reporting


def test_reporting_limit_is_bounded_for_unpaginated_admin_views():
    assert admin_reporting._list_limit(0) == 1
    assert admin_reporting._list_limit(200) == 200
    assert admin_reporting._list_limit(999) == admin_reporting.MAX_LIST_LIMIT


def test_member_count_labels_preserve_pending_and_unavailable_states():
    assert admin_reporting._member_count(7, "succeeded") == "7"
    assert admin_reporting._member_count(None, "pending") == "正在获取"
    assert admin_reporting._member_count(None, "unavailable") == "Lark 未返回"


def test_seconds_label_prefers_human_readable_units():
    assert admin_reporting._seconds_label(300) == "5 分钟"
    assert admin_reporting._seconds_label(3600) == "1 小时"
    assert admin_reporting._seconds_label(45) == "45 秒"
