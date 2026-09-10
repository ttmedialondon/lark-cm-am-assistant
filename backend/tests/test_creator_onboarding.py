"""OAuth readiness must never restart the retired whole-chat AI scan."""

from contextlib import nullcontext
from uuid import uuid4

import pytest
from test_creator_workflow_ai import pg as pg_fixture
from test_creator_workflow_ai import real_scan as real_scan_fixture

from app import creator_onboarding as onboarding
from app import creator_workflow_ai as ai

pg, real_scan = pg_fixture, real_scan_fixture


def test_registration_waits_for_consent_and_sync_but_never_calls_ai(pg, real_scan, monkeypatch):
    ids, _ = real_scan
    monkeypatch.setattr(onboarding, "connection", lambda: nullcontext(pg))
    onboarding.register_initial_scan(pg, ids["cm"])
    before = onboarding.get_status(pg, ids["cm"])
    onboarding.register_initial_scan(pg, ids["cm"])
    assert onboarding.get_status(pg, ids["cm"]) == before
    assert onboarding.get_status(pg, ids["owner"]) is None

    def tick():
        pg.execute("UPDATE creator_workflow_onboarding SET next_check_at=NOW()")
        onboarding.advance_initial_scans()

    pg.execute("UPDATE authorizations SET assignment_eligible=FALSE WHERE id=%s", (ids["cm"],))
    tick()
    assert "CM 资格" in onboarding.get_status(pg, ids["cm"])["error_message"]
    pg.execute("UPDATE authorizations SET assignment_eligible=TRUE WHERE id=%s", (ids["cm"],))
    tick()
    assert "同步完成" in onboarding.get_status(pg, ids["cm"])["error_message"]
    pg.execute(
        """INSERT INTO sync_jobs(id,authorization_id,mode,status,finished_at)
           VALUES(%s,%s,'backfill','succeeded',NOW())""", (uuid4(), ids["cm"]),
    )
    tick()
    assert onboarding.get_status(pg, ids["cm"])["status"] == "ready"
    onboarding.register_initial_scan(pg, ids["cm"])
    tick()
    assert not pg.execute("SELECT 1 FROM creator_workflow_ai_tasks").fetchone()
    assert ai.claim_next_creator_scan() is None


def test_revocation_and_legacy_retry_cannot_send_history(pg, real_scan, monkeypatch):
    ids, _ = real_scan
    monkeypatch.setattr(onboarding, "connection", lambda: nullcontext(pg))
    onboarding.register_initial_scan(pg, ids["cm"])
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (ids["cm"],))
    onboarding.advance_initial_scans()
    assert onboarding.get_status(pg, ids["cm"])["status"] == "cancelled"
    with pytest.raises(PermissionError):
        onboarding.retry_initial_scan(ids["owner"], ids["cm"])
    pg.execute("UPDATE authorizations SET status='active' WHERE id=%s", (ids["cm"],))
    with pytest.raises(ai.ScanInputError, match="全量聊天扫描已停用"):
        onboarding.retry_initial_scan(ids["owner"], ids["cm"])
    assert not pg.execute("SELECT 1 FROM creator_workflow_ai_tasks").fetchone()
