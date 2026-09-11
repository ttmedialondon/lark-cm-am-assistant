from __future__ import annotations

from contextlib import nullcontext

import pytest

from app import contact_assessments, contact_assignments


def test_claim_refreshes_the_durable_queue_before_claiming(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(contact_assessments, "_expire_leases", lambda: calls.append("expire"))
    monkeypatch.setattr(contact_assessments, "assessment_available", lambda: calls.append("available") or True)
    monkeypatch.setattr(contact_assessments, "_claim_next", lambda: calls.append("claim") or None)
    monkeypatch.setattr(contact_assessments, "queue_eligible_contact_assessments", lambda: calls.append("queue") or 5)

    assert contact_assessments.claim_next_contact_assessment() is None
    assert calls == ["expire", "available", "queue", "claim"]


def test_claim_returns_the_durable_job_for_an_ai_slot(monkeypatch) -> None:
    calls: list[str] = []
    job = {"id": "assessment", "contact_id": "contact"}

    monkeypatch.setattr(contact_assessments, "_expire_leases", lambda: calls.append("expire"))
    monkeypatch.setattr(contact_assessments, "assessment_available", lambda: calls.append("available") or True)
    monkeypatch.setattr(contact_assessments, "queue_eligible_contact_assessments", lambda: calls.append("queue") or 0)
    monkeypatch.setattr(contact_assessments, "_claim_next", lambda: calls.append("claim") or job)
    assert contact_assessments.claim_next_contact_assessment() == job
    assert calls == ["expire", "available", "queue", "claim"]


@pytest.mark.parametrize("module", (contact_assessments, contact_assignments))
def test_both_ai_queues_stop_before_claiming_when_the_shared_gate_is_full(monkeypatch, module) -> None:
    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, *_args, **_kwargs):
            raise AssertionError("a full shared gate must prevent queue SQL")

    monkeypatch.setattr(module, "connection", lambda: nullcontext(Connection()))
    monkeypatch.setattr(module.ai_dispatch, "can_claim", lambda _conn: False)

    assert module._claim_next() is None
