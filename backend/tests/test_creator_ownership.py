"""Critical second-step lifecycle checks against isolated PostgreSQL; Responses is mocked."""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app import admin_reporting, openai_client
from app import creator_ownership as ownership
from app import creator_ownership_evidence as evidence
from app.ai_feature_config import api_key_cipher
from app.crypto import message_body_cipher


@pytest.fixture
def pilot(pg, monkeypatch):
    settings = SimpleNamespace(token_encryption_key=b"o" * 32)
    ids = {name: uuid4() for name in ("owner", "cm")}
    for name, identity in ids.items():
        pg.execute(
            "INSERT INTO authorizations(id,open_id,display_name,access_token_ciphertext,body_consent_at) "
            "VALUES(%s,%s,%s,'test-token',NOW()-INTERVAL '1 day')", (identity, "ou_" + name, name),
        )
        pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,%s)", (identity, name))
        pg.execute("INSERT INTO lark_identities(open_id,contact_id,display_name) VALUES(%s,%s,%s)",
                   ("ou_" + name, identity, name))
    pg.execute("INSERT INTO admin_roles(authorization_id,role) VALUES(%s,'owner')", (ids["owner"],))
    pg.execute(
        "INSERT INTO ai_feature_configs(feature,provider,project_id,model,enabled,api_key_ciphertext,key_suffix) "
        "VALUES('contact_assessment','openai','test-project','test-model',FALSE,%s,'only') "
        "ON CONFLICT(feature) DO UPDATE SET model=EXCLUDED.model,project_id=EXCLUDED.project_id,"
        "api_key_ciphertext=EXCLUDED.api_key_ciphertext,key_suffix=EXCLUDED.key_suffix",
        (api_key_cipher(settings.token_encryption_key).encrypt("sk-test-only"),),
    )
    cipher = message_body_cipher(settings.token_encryption_key)

    def person(name, pairs=0):
        contact_id, open_id, chat = uuid4(), "ou_" + name, "chat_" + name
        pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,%s)", (contact_id, name))
        pg.execute("INSERT INTO lark_identities(open_id,contact_id,display_name) VALUES(%s,%s,%s)",
                   (open_id, contact_id, name))
        pg.execute("INSERT INTO contact_roles(contact_id,role,status,source,is_primary) "
                   "VALUES(%s,'creator','confirmed','ai_auto_confirmed',TRUE)", (contact_id,))
        pg.execute(
            "INSERT INTO contact_assessments(id,contact_id,status,policy_version,result) "
            "VALUES(%s,%s,'succeeded','people-identity-v1',%s)",
            (uuid4(), contact_id, Jsonb({"reason_code": "classified", "roles": [{"role": "creator",
                                                                                 "confidence": 0.99}]})),
        )
        if pairs:
            pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES(%s,'Creator private conversation','p2p')",
                       (chat,))
            pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_open_id) "
                       "VALUES(%s,%s,%s)", (ids["cm"], chat, open_id))
        for index in range(pairs):
            question_id = f"{name}_{index}_question"
            for minute, sender, message_id, parent, text in (
                (0, open_id, question_id, None, f"Can you arrange the campaign sample number {index} for me?"),
                (1, "ou_cm", f"{name}_{index}_answer", question_id,
                 f"I am your manager and will arrange your campaign sample number {index}."),
            ):
                pg.execute(
                    "INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,"
                    "body_ciphertext,message_create_time,parent_id) VALUES(%s,%s,%s,'user','text',%s,%s,%s)",
                    (chat, message_id, sender, cipher.encrypt(json.dumps({"text": text})),
                     datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=index * 10, minutes=minute), parent),
                )
                pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
                           "VALUES(%s,%s,%s)", (ids["cm"], chat, message_id))
        return contact_id

    ids["creator"] = person("creator", pairs=8)
    ids["silent"] = person("silent")
    ids["legacy"] = person("legacy")
    account_id, candidate_id = uuid4(), uuid4()
    pg.execute("INSERT INTO social_accounts(id,platform,normalized_handle,display_handle) "
               "VALUES(%s,'tiktok','legacy_account','legacy_account')", (account_id,))
    pg.execute("INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source) "
               "VALUES(%s,%s,'owner','confirmed','cm_confirmation')", (ids["legacy"], account_id))
    pg.execute(
        "INSERT INTO creator_workflow_candidates(id,normalized_handle,responsible_authorization_id,contact_id,"
        "relationship,status,social_account_id) VALUES(%s,'legacy_account',%s,%s,'owner','confirmed',%s)",
        (candidate_id, ids["owner"], ids["legacy"], account_id),
    )
    pg.execute(
        "INSERT INTO creator_account_assignments(id,social_account_id,responsible_authorization_id,contact_id,"
        "relationship,source,candidate_id,created_by_authorization_id) "
        "VALUES(%s,%s,%s,%s,'owner','cm_confirmation',%s,%s)",
        (uuid4(), account_id, ids["owner"], ids["legacy"], candidate_id, ids["owner"]),
    )
    for module in (ownership, evidence, admin_reporting):
        monkeypatch.setattr(module, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(ownership, "get_settings", lambda: settings)
    calls, after_request = [], []

    def post(url, **kwargs):
        assert url == "https://api.openai.com/v1/responses"
        payload = kwargs["json"]
        assert payload["store"] is False and payload["model"] == "test-model"
        assert payload["text"]["format"]["strict"] is True
        assert payload["instructions"] == ownership._INSTRUCTIONS
        pack = json.loads(payload["input"])
        calls.append(pack)
        for callback in after_request:
            callback()
        result = {"outcome": "suggested", "confidence": 0.99, "reason": "双方有持续业务跟进并明确确认负责关系。",
                  "responsibility_basis": "explicit_responsibility",
                  "evidence_refs": [m["ref"] for m in pack["messages"]], "counter_evidence_refs": [],
                  "conflicts": [], "missing_info": []}
        return SimpleNamespace(status_code=200, headers={"x-request-id": "req_test"}, json=lambda: {
            "id": "resp_test", "status": "completed",
            "output": [{"content": [{"type": "output_text", "text": json.dumps(result)}]}],
            "usage": {"input_tokens": 123, "output_tokens": 45},
        })

    monkeypatch.setattr(openai_client.httpx, "post", post)
    return SimpleNamespace(pg=pg, ids=ids, settings=settings, person=person, calls=calls, after_request=after_request)


def _prepared(pilot):
    run = ownership.create_preview(pilot.ids["owner"], pilot.ids["cm"])
    # The fixture wraps every request in one rollback transaction; production requests have distinct NOW().
    pilot.pg.execute("UPDATE creator_ownership_runs SET created_at=clock_timestamp() WHERE id=%s", (run,))
    for _ in range(100):
        if not ownership.prepare_next_preview(pilot.settings):
            return run
    pytest.fail("local preparation did not settle")


def _task(pg, run, contact):
    return pg.execute("SELECT * FROM creator_ownership_tasks WHERE run_id=%s AND contact_id=%s",
                      (run, contact)).fetchone()


def _run_all(pilot):
    for _ in range(100):
        job = ownership.claim_next_ownership()
        if not job:
            return
        ownership.run_claimed_ownership(job, pilot.settings)
    pytest.fail("paid queue did not settle")


def _snapshot(pg, tables):
    return {table: pg.execute(f"SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]') AS rows "
                             f"FROM {table} t").fetchone()["rows"] for table in tables}


def test_preview_responses_cannot_bypass_handle_stage_preserves_existing_data(pg, pilot):
    protected = ("contact_roles", "contact_assessments", "authorizations", "chat_messages",
                 "authorization_message_observations", "creator_workflow_candidates", "creator_account_assignments",
                 "social_accounts", "contact_social_accounts", "offhours_summary_tasks", "ai_feature_configs")
    before = _snapshot(pg, protected)
    run = _prepared(pilot)
    task = _task(pg, run, pilot.ids["creator"])
    assert task["status"] == "ready" and task["evidence_count"] >= 10
    assert _task(pg, run, pilot.ids["silent"])["status"] == "skipped"
    assert _task(pg, run, pilot.ids["legacy"])["status"] == "skipped"
    assert pilot.calls == []
    assert [r["label"] for r in ownership._refs(pg, task)] == [f"M{i}" for i in range(1, task["evidence_count"] + 1)]
    assert ownership.start_run(pilot.ids["owner"], run) == 1
    assert ownership.start_run(pilot.ids["owner"], run) == 0
    _run_all(pilot)
    task = _task(pg, run, pilot.ids["creator"])
    assert task["status"] == "succeeded" and task["outcome"] == "suggested"
    assert task["api_requests"] == 1 and task["input_tokens"] == 123 and len(pilot.calls) == 1
    assert "持续业务" not in task["result_ciphertext"] and "campaign sample" not in task["pack_ciphertext"]
    assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()

    readonly_before = _snapshot(pg, ("creator_ownership_runs", "creator_ownership_tasks", "audit_events"))
    for _ in range(2):
        data = ownership.page_data(pilot.ids["cm"], run, include_private=True)
        creator = next(item for item in data["items"] if item["id"] == str(task["id"]))
        assert not creator["can_confirm"] and creator["handle"] == "—"
    assert _snapshot(pg, readonly_before) == readonly_before
    assert len(pilot.calls) == 1
    public = ownership.page_data(pilot.ids["cm"], run, include_private=False)
    assert all(not item["can_confirm"] and not item["has_evidence"] for item in public["items"])
    assert "持续业务" not in json.dumps(public, ensure_ascii=False)
    view = ownership.task_evidence(pilot.ids["owner"], task["id"])
    assert len(view["messages"]) == task["evidence_count"]
    assert view["cm_id"] == str(pilot.ids["cm"]) and view["cm_name"]
    assert [m["ref"] for m in view["messages"]] == [r["label"] for r in ownership._refs(pg, task)]
    assert all(m["chat_name"] == "Creator private conversation" for m in view["messages"])
    with pytest.raises(ValueError, match="handle"):
        ownership.confirm_tasks(pilot.ids["owner"], run, [task["id"]])
    assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()
    assert _snapshot(pg, protected) == before


def test_cross_run_cache_and_page_refresh_do_not_repeat_paid_analysis(pg, pilot, monkeypatch):
    first = _prepared(pilot)
    ownership.start_run(pilot.ids["owner"], first)
    _run_all(pilot)
    prior = _task(pg, first, pilot.ids["creator"])
    second = _prepared(pilot)
    current = _task(pg, second, pilot.ids["creator"])
    assert second != first and current["status"] == "succeeded" and current["reused_from"] == prior["id"]
    assert current["api_requests"] == 0 and current["result_ciphertext"] == prior["result_ciphertext"]
    for _ in range(2):
        data = ownership.page_data(pilot.ids["cm"], second, include_private=True)
        assert data["counts"]["reused"] == 1 and data["usage"]["requests"] == 0
    assert ownership.start_run(pilot.ids["owner"], second) == 0
    assert ownership.claim_next_ownership() is None and len(pilot.calls) == 1

    # A rules revision must preserve history but cannot reuse or confirm the earlier verdict.
    monkeypatch.setattr(ownership, "POLICY_VERSION", ownership.POLICY_VERSION + "-next")
    old = _snapshot(pg, ("creator_ownership_tasks",))
    page = ownership.page_data(pilot.ids["cm"], second, include_private=True)
    assert not page["run"]["can_start"] and all(not item["can_confirm"] for item in page["items"])
    with pytest.raises(ValueError, match="规则"):
        ownership.start_run(pilot.ids["owner"], second)
    with pytest.raises(ValueError, match="handle"):
        ownership.confirm_tasks(pilot.ids["owner"], second, [current["id"]])
    assert _snapshot(pg, old) == old
    third = _prepared(pilot)
    revised = _task(pg, third, pilot.ids["creator"])
    assert revised["status"] == "ready" and revised["reused_from"] is None and revised["result_ciphertext"] is None
    assert revised["fingerprint"] == current["fingerprint"] and revised["api_requests"] == 0
    assert ownership.start_run(pilot.ids["owner"], third) == 1
    assert ownership.start_run(pilot.ids["owner"], third) == 0
    _run_all(pilot)
    assert len(pilot.calls) == 2 and _task(pg, third, pilot.ids["creator"])["api_requests"] == 1
    assert _task(pg, second, pilot.ids["creator"]) == current
    assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()
    with pytest.raises(ValueError, match="handle"):
        ownership.confirm_tasks(pilot.ids["owner"], third, [revised["id"]])


def test_pause_before_dispatch_and_owner_loss_during_response_are_safe(pg, pilot):
    run = _prepared(pilot)
    ownership.start_run(pilot.ids["owner"], run)
    claimed = ownership.claim_next_ownership()
    ownership.pause_run(pilot.ids["owner"], run)
    ownership.run_claimed_ownership(claimed, pilot.settings)
    task = _task(pg, run, pilot.ids["creator"])
    assert task["status"] == "queued" and task["attempts"] == 0 and task["api_requests"] == 0
    assert ownership.claim_next_ownership() is None and not pilot.calls
    ownership.resume_run(pilot.ids["owner"], run)
    pilot.after_request.append(lambda: ownership.pause_run(pilot.ids["owner"], run))
    _run_all(pilot)
    assert _task(pg, run, pilot.ids["creator"])["status"] == "succeeded"
    assert pg.execute("SELECT status FROM creator_ownership_runs WHERE id=%s", (run,)).fetchone()["status"] == "paused"
    ownership.resume_run(pilot.ids["owner"], run)
    pilot.after_request.clear()

    pilot.person("another_creator", pairs=1)
    next_run = _prepared(pilot)
    ownership.start_run(pilot.ids["owner"], next_run)
    pilot.after_request.append(lambda: pg.execute("UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s",
                                                  (pilot.ids["owner"],)))
    _run_all(pilot)
    invalid = pg.execute("SELECT * FROM creator_ownership_tasks WHERE run_id=%s AND api_requests>0",
                         (next_run,)).fetchone()
    assert invalid["status"] == "invalid" and invalid["reason_code"] == "authorization_changed"
    assert invalid["result_ciphertext"] is None
    with pytest.raises(ValueError, match="handle"):
        ownership.confirm_tasks(pilot.ids["owner"], run, [task["id"]])
    with pytest.raises(PermissionError):
        ownership.task_evidence(pilot.ids["owner"], task["id"])
    assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()


def test_existing_same_cm_assignment_prevents_duplicate_dispatch_and_confirmation(pg, pilot):
    second = pilot.person("second_creator", pairs=1)
    run = _prepared(pilot)
    ownership.start_run(pilot.ids["owner"], run)

    def assign(contact):
        pg.execute("INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) "
                   "VALUES(%s,%s,%s,'manual')", (uuid4(), contact, pilot.ids["cm"]))

    assign(pilot.ids["creator"])
    _run_all(pilot)
    assert _task(pg, run, pilot.ids["creator"])["reason_code"] == "already_assigned"
    task = _task(pg, run, second)
    assert task["status"] == "succeeded" and len(pilot.calls) == 1
    assign(second)
    existing = _snapshot(pg, ("contact_assignments",))
    with pytest.raises(ValueError, match="handle"):
        ownership.confirm_tasks(pilot.ids["owner"], run, [task["id"]])
    assert _snapshot(pg, existing) == existing
    new_run = _prepared(pilot)
    data = ownership.page_data(pilot.ids["cm"], new_run, include_private=True)
    assert data["run"]["excluded_owned_count"] == 2 and data["counts"]["ready"] == 0
    assert ownership.start_run(pilot.ids["owner"], new_run) == 0 and len(pilot.calls) == 1


def test_changed_evidence_and_superseded_batches_cannot_pay_or_confirm(pg, pilot):
    old = _prepared(pilot)
    # A completed pilot can retain ready people, while a later preview owns this CM's active batch.
    pg.execute("UPDATE creator_ownership_runs SET status='completed' WHERE id=%s", (old,))
    current = _prepared(pilot)
    with pytest.raises(ValueError):
        ownership.start_run(pilot.ids["owner"], old)
    ownership.start_run(pilot.ids["owner"], current)
    _run_all(pilot)
    task = _task(pg, current, pilot.ids["creator"])
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='creator_0_question'",
               (message_body_cipher(pilot.settings.token_encryption_key).encrypt('{"text":"changed"}'),))
    page = ownership.page_data(pilot.ids["cm"], current, include_private=True)
    item = next(row for row in page["items"] if row["id"] == str(task["id"]))
    assert not item["can_confirm"] and not item["has_evidence"]
    with pytest.raises(ValueError):
        ownership.task_evidence(pilot.ids["owner"], task["id"])
    with pytest.raises(ValueError):
        ownership.confirm_tasks(pilot.ids["owner"], current, [task["id"]])
    assert len(pilot.calls) == 1 and not pg.execute("SELECT 1 FROM contact_assignments").fetchone()

    # The start button only checks frozen quotes. New, unquoted history is still
    # rejected in the worker before an API request, not inside a slow batch POST.
    fresh = _prepared(pilot)
    pg.execute(
        "INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,"
        "body_ciphertext,message_create_time) VALUES('chat_creator','new_handover','ou_cm','user','text',%s,NOW())",
        (message_body_cipher(pilot.settings.token_encryption_key).encrypt(
            '{"text":"I am no longer your manager; another CM has taken over."}'),),
    )
    pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
               "VALUES(%s,'chat_creator','new_handover')", (pilot.ids["cm"],))
    assert ownership.start_run(pilot.ids["owner"], fresh) == 1
    _run_all(pilot)
    stale = _task(pg, fresh, pilot.ids["creator"])
    assert stale["status"] == "invalid" and stale["api_requests"] == 0
    assert stale["reason_code"] == "authorization_changed" and len(pilot.calls) == 1


def test_concurrent_start_dispatch_and_pause_have_no_lock_cycle_or_duplicate_requests(pg, pilot, monkeypatch):
    run = _prepared(pilot)
    schema = pg.execute("SELECT current_schema() AS name").fetchone()["name"]
    assert re.fullmatch(r"test_people_[0-9a-f]{32}", schema)
    dsn = pg.info.dsn
    # Expose only this fixture's isolated schema to two real connections; remove it explicitly below.
    pg.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(schema)))
    pg.commit()

    @contextmanager
    def connected():
        with psycopg.connect(dsn, row_factory=dict_row,
                             options=f"-csearch_path={schema} -clock_timeout=2000 -cstatement_timeout=5000") as conn:
            yield conn

    try:
        for module in (ownership, evidence, admin_reporting):
            monkeypatch.setattr(module, "connection", connected)
        simultaneous = Barrier(2)

        def start():
            simultaneous.wait(timeout=5)
            return ownership.start_run(pilot.ids["owner"], run)

        with ThreadPoolExecutor(max_workers=2) as executor:
            requests = [executor.submit(start), executor.submit(start)]
            assert sorted(future.result(timeout=10) for future in requests) == [0, 1]
        assert not pilot.calls
        job = ownership.claim_next_ownership()
        assert job and job["api_requests"] == 0

        run_locked, control_ready, paused = Event(), Event(), Event()
        original_material, original_owner = ownership._valid_material, ownership._run_for_owner

        def material(conn, task, settings):
            if not run_locked.is_set():
                # The worker holds run→task. Old start_run held CM before asking for run: a real deadlock.
                run_locked.set()
                assert control_ready.wait(timeout=5), "concurrent start never reached the run lock"
            return original_material(conn, task, settings)

        def owner(conn, actor, run_id):
            control_ready.set()
            return original_owner(conn, actor, run_id)

        def start_and_pause():
            assert run_locked.wait(timeout=5), "dispatch never reached the protected material check"
            assert ownership.start_run(pilot.ids["owner"], run) == 0
            ownership.pause_run(pilot.ids["owner"], run)
            paused.set()

        def wait_for_pause():
            assert paused.wait(timeout=5), "pause could not finish while the HTTP request was in flight"

        monkeypatch.setattr(ownership, "_valid_material", material)
        monkeypatch.setattr(ownership, "_run_for_owner", owner)
        pilot.after_request.append(wait_for_pause)
        with ThreadPoolExecutor(max_workers=2) as executor:
            dispatch = executor.submit(ownership.run_claimed_ownership, job, pilot.settings)
            control = executor.submit(start_and_pause)
            control.result(timeout=10)
            dispatch.result(timeout=10)
        task = _task(pg, run, pilot.ids["creator"])
        assert task["status"] == "succeeded" and task["api_requests"] == 1 and len(pilot.calls) == 1
        assert ownership.claim_next_ownership() is None
        ownership.resume_run(pilot.ids["owner"], run)
        assert ownership.start_run(pilot.ids["owner"], run) == 0
        assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()
    finally:
        pg.rollback()
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute(psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(psycopg.sql.Identifier(schema)))
