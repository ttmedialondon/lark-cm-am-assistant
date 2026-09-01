from __future__ import annotations

from contextlib import nullcontext

from app import contact_assessments


def test_automatic_queue_uses_immutable_new_message_watermarks(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...] | None]] = []

    class Result:
        def __init__(self, *, row=None, rows=None):
            self.row = row
            self.rows = rows or []

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, query, params=None):
            calls.append((query, params))
            if "FROM ai_feature_configs" in query:
                return Result(row={"enabled": True, "provider": "openai", "api_key_ciphertext": "sealed"})
            return Result(rows=[{"id": "one"}, {"id": "two"}])

    monkeypatch.setattr(contact_assessments, "connection", lambda: nullcontext(Connection()))

    assert contact_assessments.queue_eligible_contact_assessments(limit=99) == 2

    query, params = calls[-1]
    assert params == (5, 3, 15)
    assert "MAX(first_observed_at)" in query
    assert "WHERE status = 'succeeded'" in query
    assert "watermark.source_message_seen_at < eligible.source_message_seen_at" in query
    assert "previous.outcome, '') <> 'review_required'" in query
    assert "previous.review_status, '') <> 'ignored'" in query
    assert "previous.source_message_seen_at IS NULL" in query
    assert "COUNT(DISTINCT (fresh.chat_id, fresh.message_id))" in query
    assert ") >= 5" in query
    assert "retry_attempts AS" in query
    assert "COALESCE(retry.attempt_count, 0) < %s" in query
    assert "active.status IN ('queued', 'running')" in query
    assert "contact_roles role" in query
    assert "contact_social_accounts account_relation" in query
    assert "LIMIT (SELECT queue_limit FROM queue_slots)" in query


def test_runner_fills_an_automatic_batch_only_after_the_current_queue_drains(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(contact_assessments, "_expire_leases", lambda: calls.append("expire"))
    monkeypatch.setattr(contact_assessments, "_automatic_execution_allowed", lambda: True)
    monkeypatch.setattr(contact_assessments, "_claim_next", lambda: calls.append("claim") or None)
    monkeypatch.setattr(contact_assessments, "queue_eligible_contact_assessments", lambda: calls.append("queue") or 5)

    assert contact_assessments.run_next_contact_assessment(object()) is False
    assert calls == ["expire", "claim", "queue", "claim"]


def test_runner_pauses_the_automatic_queue_after_a_feature_failure(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(contact_assessments, "_expire_leases", lambda: calls.append("expire"))
    monkeypatch.setattr(contact_assessments, "_automatic_execution_allowed", lambda: False)
    monkeypatch.setattr(contact_assessments, "_claim_next", lambda: calls.append("claim") or None)

    assert contact_assessments.run_next_contact_assessment(object()) is False
    assert calls == ["expire"]
