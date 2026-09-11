from __future__ import annotations

from app import ai_dispatch


def test_shared_gate_rejects_when_three_valid_leases_are_running() -> None:
    calls: list[str] = []

    class Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def execute(self, query, _params=None):
            calls.append(query)
            if "ai_admission_state" in query:
                return Result({"paused_at": None, "throttled": False})
            if "COUNT(*)" in query:
                return Result({"count": 3})
            return Result(None)

    assert not ai_dispatch.can_claim(Connection())
    combined_query = next(query for query in calls if "COUNT(*)" in query)
    assert "contact_assessments" in combined_query
    assert "contact_assignment_assessments" not in combined_query
    assert "creator_workflow_ai_tasks" not in combined_query
    assert "offhours_summary_tasks" in combined_query
    assert "creator_ownership_tasks" in combined_query
    assert "creator_handle_tasks" in combined_query
    assert "lease_until > NOW()" in combined_query
