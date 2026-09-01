from __future__ import annotations

from contextlib import nullcontext

from app import contact_assessments


def test_automatic_queue_materializes_all_eligible_contacts_from_immutable_watermarks(monkeypatch) -> None:
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
            if "UPDATE ai_feature_configs" in query:
                return Result(row={"feature": "contact_assessment"})
            return Result(rows=[{"id": "one"}, {"id": "two"}])

    monkeypatch.setattr(contact_assessments, "connection", lambda: nullcontext(Connection()))

    assert contact_assessments.queue_eligible_contact_assessments() == 2

    query, params = calls[-1]
    assert calls[0][1] == ("contact_assessment", 60)
    assert "automatic_queue_checked_at" in calls[0][0]
    assert params == (3, 15)
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
    assert "queue_slots" not in query
    assert "LIMIT (SELECT queue_limit FROM queue_slots)" not in query
    assert "ON CONFLICT (contact_id) WHERE status IN ('queued', 'running')" in query


def test_runner_refreshes_the_durable_queue_before_claiming(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(contact_assessments, "_expire_leases", lambda: calls.append("expire"))
    monkeypatch.setattr(contact_assessments, "assessment_available", lambda: calls.append("available") or True)
    monkeypatch.setattr(contact_assessments, "_claim_next", lambda: calls.append("claim") or None)
    monkeypatch.setattr(contact_assessments, "queue_eligible_contact_assessments", lambda: calls.append("queue") or 5)

    assert contact_assessments.run_next_contact_assessment(object()) is False
    assert calls == ["expire", "available", "queue", "claim"]


def test_runner_keeps_processing_after_an_individual_feature_failure(monkeypatch) -> None:
    calls: list[str] = []
    job = {"id": "assessment", "contact_id": "contact"}

    monkeypatch.setattr(contact_assessments, "_expire_leases", lambda: calls.append("expire"))
    monkeypatch.setattr(contact_assessments, "assessment_available", lambda: calls.append("available") or True)
    monkeypatch.setattr(contact_assessments, "queue_eligible_contact_assessments", lambda: calls.append("queue") or 0)
    monkeypatch.setattr(contact_assessments, "_claim_next", lambda: calls.append("claim") or job)
    monkeypatch.setattr(contact_assessments, "_run", lambda received, _settings: calls.append(f"run:{received['id']}"))

    assert contact_assessments.run_next_contact_assessment(object()) is True
    assert calls == ["expire", "available", "queue", "claim", "run:assessment"]


def test_claim_uses_a_database_lock_and_rate_limits_automatic_work(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...] | None]] = []

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, query, params=None):
            calls.append((query, params))
            if "AS ready" in query:
                return Result({"ready": False})
            return Result()

    monkeypatch.setattr(contact_assessments, "connection", lambda: nullcontext(Connection()))

    assert contact_assessments._claim_next() is None

    lock_query, lock_params = calls[0]
    assert "pg_advisory_xact_lock" in lock_query
    assert lock_params == (contact_assessments.AUTOMATIC_CLAIM_LOCK,)
    claim_query, claim_params = calls[-1]
    assert "trigger <> 'automatic' OR %s" in claim_query
    assert claim_params == (False,)
