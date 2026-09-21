"""Critical third-step workflows: real PostgreSQL/evidence, mocked OpenAI only."""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from threading import Barrier, Event
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from test_creator_ownership import _prepared, _run_all, _snapshot
from test_creator_ownership import pilot as parent_pilot

from app import admin_reporting, contact_assessments
from app import creator_handle_evidence as evidence
from app import creator_handles as handles
from app import creator_ownership as ownership
from app import creator_ownership_evidence as parent_evidence
from app.crypto import message_body_cipher

pilot = parent_pilot


def _drain_preview(settings):
    for _ in range(30):
        if not handles.prepare_next_preview(settings):
            return
    pytest.fail("handle preparation did not settle")


def _drain_paid(settings):
    for _ in range(30):
        job = handles.claim_next_handle()
        if not job:
            return
        handles.run_claimed_handle(job, settings)
    pytest.fail("handle paid queue did not settle")


def _task(pg, case, contact=None):
    return pg.execute("SELECT * FROM creator_handle_tasks WHERE run_id=%s AND contact_id=%s",
                      (case.run, contact or case.creator)).fetchone()


def _case(pg, pilot, monkeypatch, *, clues=True, uncertain=False):
    from app import creator_imports

    for module in (handles, evidence, creator_imports):
        monkeypatch.setattr(module, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(handles, "get_settings", lambda: pilot.settings)
    monkeypatch.setattr(creator_imports, "get_settings", lambda: pilot.settings)
    if clues:
        text = "My TikTok account is @creator_one. My TikTok account is @creator_two. Can you arrange my sample?"
        pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='creator_7_question'",
                   (message_body_cipher(pilot.settings.token_encryption_key).encrypt(json.dumps({"text": text})),))
        pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES('handle_group','creator_one X TT','group')")
        pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,'handle_group')",
                   (pilot.ids["cm"],))
        pg.execute("INSERT INTO chat_members(chat_id,open_id,member_type) VALUES('handle_group','ou_creator','user')")
    if uncertain:
        unsure = pilot.person("unsure", pairs=2)
        original = contact_assessments.call_openai

        def second_stage(*args, **kwargs):
            response = original(*args, **kwargs)
            if args[3] == unsure:
                result = json.loads(response["text"])
                result.update(outcome="uncertain", confidence=.8)
                response["text"] = json.dumps(result)
            return response

        monkeypatch.setattr(contact_assessments, "call_openai", second_stage)
    source = _prepared(pilot)
    ownership.start_run(pilot.ids["owner"], source)
    _run_all(pilot)
    before_preview = len(pilot.calls)
    run = handles.create_preview(pilot.ids["owner"], pilot.ids["cm"], source)
    assert pg.execute("SELECT auto_admit FROM creator_handle_runs WHERE id=%s", (run,)).fetchone()["auto_admit"]
    # Most cases below exercise manual review; an owner may explicitly pause automatic admission.
    handles.set_auto_admit(pilot.ids["owner"], run, False)
    _drain_preview(pilot.settings)
    assert len(pilot.calls) == before_preview
    case = SimpleNamespace(run=run, source=source, creator=pilot.ids["creator"], calls=[], callbacks=[], score=.99,
                           outcome="matched")

    def handle_response(config, key, pack, contact_id, **kwargs):
        assert kwargs["instructions"] == evidence.INSTRUCTIONS
        assert kwargs["output_format"]["strict"] and kwargs["output_format"]["name"] == "creator_handle_v1"
        case.calls.append(contact_id)
        candidates = [{"handle": handle, "confidence": case.score, "basis": "self_declared",
                       "evidence_refs": [m["ref"] for m in pack["messages"] if handle in m["self_handles"]],
                       "conflicts": [], "missing_info": []} for handle in pack["allowed_handles"]]
        result = {"outcome": case.outcome, "reason": "本人明确提供账号。", "candidates": candidates}
        for callback in case.callbacks:
            callback()
        return {"text": json.dumps(result), "response_id": f"handle_response_{len(case.calls)}",
                "usage": {"input_tokens": 17, "output_tokens": 9},
                "telemetry": {"openai_request_id": f"handle_request_{len(case.calls)}"}}

    monkeypatch.setattr(contact_assessments, "call_openai", handle_response)
    return case


def test_suggestions_preview_then_explicit_auto_admission_is_atomic_and_idempotent(pg, pilot, monkeypatch):
    case = _case(pg, pilot, monkeypatch, uncertain=True)
    protected = ("contact_roles", "contact_assessments", "creator_account_assignments", "creator_workflow_candidates",
                 "chat_messages", "authorization_message_observations", "authorizations")
    before = _snapshot(pg, protected)
    task = _task(pg, case)
    assert task["status"] == "ready" and not case.calls
    assert pg.execute("SELECT count(*) n FROM creator_handle_tasks WHERE run_id=%s", (case.run,)).fetchone()["n"] == 1
    run = pg.execute("SELECT auto_admit FROM creator_handle_runs WHERE id=%s", (case.run,)).fetchone()
    assert not run["auto_admit"]
    assert all("text" not in ref and "body" not in ref for ref in task["refs"])
    assert handles.create_preview(pilot.ids["owner"], pilot.ids["cm"], case.source) == case.run
    pending = _task(pg, case)
    assert pending["status"] == "pending" and pending["pack_ciphertext"] is None and pending["refs"] == []
    assert pending["fingerprint"] is None and pending["api_requests"] == 0 and not case.calls
    _drain_preview(pilot.settings)
    rebuilt = _task(pg, case)
    assert rebuilt["id"] == task["id"] and rebuilt["status"] == "ready"
    assert rebuilt["fingerprint"] == task["fingerprint"] and not case.calls
    with pytest.raises(ValueError):
        ownership.confirm_tasks(pilot.ids["owner"], case.source, [task["ownership_task_id"]])
    with pg.transaction(force_rollback=True):
        old_source, old_parent, old_run = uuid4(), uuid4(), uuid4()
        pg.execute("""INSERT INTO creator_ownership_runs(id,cm_authorization_id,requested_by,policy_version,status)
            SELECT %s,cm_authorization_id,requested_by,policy_version,'completed'
            FROM creator_ownership_runs WHERE id=%s""", (old_source, case.source))
        pg.execute("""INSERT INTO creator_ownership_tasks(id,run_id,contact_id,cm_authorization_id,policy_version,
            status,outcome,confidence,created_at) SELECT %s,%s,contact_id,cm_authorization_id,policy_version,
            'succeeded',outcome,confidence,created_at-INTERVAL '1 day' FROM creator_ownership_tasks WHERE id=%s""",
            (old_parent, old_source, task["ownership_task_id"]))
        pg.execute("""INSERT INTO creator_handle_runs(id,ownership_run_id,cm_authorization_id,requested_by,
            policy_version,status) SELECT %s,%s,cm_authorization_id,requested_by,policy_version,'running'
            FROM creator_handle_runs WHERE id=%s""", (old_run, old_source, case.run))
        pg.execute("""INSERT INTO creator_handle_tasks(id,run_id,ownership_task_id,contact_id,cm_authorization_id,
            policy_version,status) SELECT %s,%s,%s,contact_id,cm_authorization_id,policy_version,'queued'
            FROM creator_handle_tasks WHERE id=%s""", (uuid4(), old_run, old_parent, task["id"]))
        with pytest.raises(ValueError, match="旧批次"):
            handles.start_run(pilot.ids["owner"], case.run)
        assert _task(pg, case)["status"] == "ready" and not case.calls
    assert handles.start_run(pilot.ids["owner"], case.run) == 1
    assert handles.start_run(pilot.ids["owner"], case.run) == 0
    with pytest.raises(ValueError, match="运行中"):
        handles.create_preview(pilot.ids["owner"], pilot.ids["cm"], case.source)
    assert _task(pg, case)["status"] == "queued" and not case.calls
    _drain_paid(pilot.settings)
    task = _task(pg, case)
    assert task["status"] == "succeeded" and task["outcome"] == "matched" and task["api_requests"] == 1
    paid = _snapshot(pg, ("creator_handle_tasks",))
    assert handles.create_preview(pilot.ids["owner"], pilot.ids["cm"], case.source) == case.run
    _drain_preview(pilot.settings)
    assert _snapshot(pg, paid) == paid and len(case.calls) == 1
    for _ in range(2):
        assert handles.page_data(pilot.ids["cm"])["items"] == []  # Archived AI work is not an Excel candidate.
    assert len(case.calls) == 1 and handles.start_run(pilot.ids["owner"], case.run) == 0
    _drain_preview(pilot.settings)
    assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()
    handles.set_auto_admit(pilot.ids["owner"], case.run, True)
    _drain_preview(pilot.settings)
    assignment = pg.execute("SELECT * FROM contact_assignments").fetchone()
    assert assignment["source"] == "ai" and assignment["handle_task_id"] == task["id"]
    assert assignment["responsible_authorization_id"] == pilot.ids["cm"]
    assert _task(pg, case)["selected_handles"] == ["creator_one", "creator_two"]
    links = pg.execute("SELECT * FROM contact_social_accounts WHERE handle_task_id=%s", (task["id"],)).fetchall()
    assert len(links) == 2 and all(row["contact_id"] == case.creator for row in links)
    assert handles.confirm_task(pilot.ids["cm"], task["id"], ["@CREATOR_TWO", "creator_one"]) == 0
    with pytest.raises(ValueError):
        handles.confirm_task(pilot.ids["cm"], task["id"], ["different_account"])
    assert _snapshot(pg, protected) == before and len(case.calls) == 1


def test_cm_can_fill_missing_handles_but_not_cross_scope_or_bypass_input_checks(pg, pilot, monkeypatch):
    case = _case(pg, pilot, monkeypatch, clues=False)
    task = _task(pg, case)
    assert task["status"] == "skipped" and not case.calls
    outsider = uuid4()
    pg.execute("INSERT INTO authorizations(id,open_id,access_token_ciphertext,body_consent_at) "
               "VALUES(%s,'ou_outsider','test',NOW())", (outsider,))
    for operation in (
        lambda: handles.page_data(outsider, pilot.ids["cm"]),
        lambda: handles.confirm_task(outsider, task["id"], ["stolen"]),
        lambda: handles.task_evidence(outsider, task["id"]),
        lambda: handles.start_run(pilot.ids["cm"], case.run),
        lambda: handles.set_auto_admit(pilot.ids["cm"], case.run, True),
        lambda: handles.set_creator_type(outsider, pilot.ids["cm"], case.creator, "cap"),
        lambda: handles.set_creator_type(pilot.ids["cm"], pilot.ids["cm"], case.creator, "cap"),
    ):
        with pytest.raises(PermissionError):
            operation()
    pg.execute("INSERT INTO admin_page_permissions(authorization_id,permission) VALUES(%s,'creator-roster')",
               (outsider,))
    view = handles.page_data(outsider, pilot.ids["cm"])
    assert view["can_select_cm"] and view["read_only"] and not view["can_manage"]
    assert len(view["cms"]) > 1 and view["import_batches"] == []
    with pytest.raises(PermissionError):
        handles.page_data(outsider, pilot.ids["cm"], tab="candidates")
    with pytest.raises(PermissionError):
        handles.confirm_task(outsider, task["id"], ["stolen"])
    pg.execute("DELETE FROM admin_page_permissions WHERE authorization_id=%s", (outsider,))
    with pytest.raises(PermissionError):
        handles.page_data(outsider, pilot.ids["cm"])
    for values in ([], [""], ["bad handle"], ["https://evil.example/@person"], ["good", "bad/handle"]):
        with pytest.raises(ValueError):
            handles.confirm_task(pilot.ids["cm"], task["id"], values)
    assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()
    assert handles.confirm_task(pilot.ids["cm"], task["id"], ["manual.one", "@manual_two"]) == 1
    assert handles.confirm_task(pilot.ids["cm"], task["id"], ["manual_two", "manual.one"]) == 0
    assert pg.execute("SELECT count(*) n FROM contact_assignments").fetchone()["n"] == 1
    assert pg.execute("SELECT source FROM contact_assignments").fetchone()["source"] == "manual"
    formal = handles.page_data(pilot.ids["cm"], tab="confirmed")["items"]
    assert formal == []  # The current library only lists CAP people.
    with pytest.raises(PermissionError):
        handles.set_creator_type(outsider, outsider, case.creator, "cap")
    for actor_id, creator_type in ((pilot.ids["cm"], "cap"), (pilot.ids["owner"], "non_cap"),
                                   (pilot.ids["cm"], "pending")):
        handles.set_creator_type(actor_id, pilot.ids["cm"], case.creator, creator_type)
        handles.set_creator_type(actor_id, pilot.ids["cm"], case.creator, creator_type)
        assert pg.execute("SELECT creator_type FROM contacts WHERE id=%s", (case.creator,)).fetchone()[
            "creator_type"] == creator_type
        people = handles.page_data(pilot.ids["cm"], tab="confirmed")["items"]
        if creator_type == "cap":
            assert len(people) == 1 and people[0]["contact_id"] == str(case.creator)
            assert len(people[0]["handles"]) == 2
        else:
            assert people == []
    with pg.transaction(force_rollback=True):
        pg.execute("UPDATE contacts SET creator_type='cap' WHERE id=%s", (case.creator,))
        avatar = "https://s1-imfile.feishucdn.com/static-resource/v1/example.png"
        for url, status, expected in (
            (None, "succeeded", None), (avatar, "succeeded", avatar),
            (avatar, "unavailable", None), ("https://untrusted.example/avatar.png", "succeeded", None),
        ):
            with pg.transaction(force_rollback=True):
                pg.execute("UPDATE lark_identities SET avatar_url=%s,avatar_status=%s WHERE open_id='ou_creator'",
                           (url, status))
                assert handles.page_data(pilot.ids["cm"], tab="mine")["items"][0]["avatar_url"] == expected
    changes = pg.execute("SELECT count(*) n FROM audit_events WHERE action='creator_handles_type_updated'").fetchone()
    assert changes["n"] == 3
    with pytest.raises(ValueError):
        handles.set_creator_type(pilot.ids["cm"], pilot.ids["cm"], case.creator, "invalid")
    assert handles.page_data(pilot.ids["cm"], tab="confirmed")["items"] == []
    assert not case.calls


def test_low_confidence_and_changed_parent_permission_group_or_members_never_auto_admit(pg, pilot, monkeypatch):
    case = _case(pg, pilot, monkeypatch)
    case.score = .94
    handles.start_run(pilot.ids["owner"], case.run)
    _drain_paid(pilot.settings)
    task = _task(pg, case)
    assert task["outcome"] == "review"  # Real validator downgrades the mocked high-level matched verdict.
    handles.set_auto_admit(pilot.ids["owner"], case.run, True)
    _drain_preview(pilot.settings)
    assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()
    with pytest.raises(PermissionError):
        handles.recheck_task(pilot.ids["cm"], task["id"])
    with pytest.raises(ValueError):
        handles.recheck_task(pilot.ids["owner"], task["id"])
    handles.set_auto_admit(pilot.ids["owner"], case.run, False)
    before = _snapshot(pg, ("creator_handle_tasks", "audit_events"))
    with pytest.raises(ValueError):
        handles.recheck_task(pilot.ids["owner"], task["id"])  # Identical material cannot be billed again.
    assert _snapshot(pg, before) == before
    for status in ("running", "paused"):
        with pg.transaction(force_rollback=True):
            pg.execute("UPDATE creator_handle_runs SET status=%s WHERE id=%s", (status, case.run))
            with pytest.raises(ValueError):
                handles.recheck_task(pilot.ids["owner"], task["id"])
    # A group-title change only affects handle evidence, leaving the real parent material valid.
    pg.execute("UPDATE chats SET name='creator_one * Trutonic Media' WHERE chat_id='handle_group'")
    with pg.transaction(force_rollback=True):
        pg.execute("UPDATE creator_ownership_tasks SET outcome='uncertain' WHERE id=%s",
                   (task["ownership_task_id"],))
        with pytest.raises(ValueError):
            handles.recheck_task(pilot.ids["owner"], task["id"])
    assert handles.recheck_task(pilot.ids["owner"], task["id"]) == case.run
    pending = _task(pg, case)
    assert pending["status"] == "pending" and pending["result_ciphertext"] is None
    for key in ("attempts", "api_requests", "input_tokens", "output_tokens"):
        assert pending[key] == task[key]
    event = pg.execute("SELECT action,metadata FROM audit_events WHERE metadata ? 'previous_task_ciphertext'")
    event = event.fetchone()
    assert event["action"] == "creator_handles_recheck_requested"
    archived = json.loads(ownership._cipher(pilot.settings).decrypt(event["metadata"]["previous_task_ciphertext"]))
    for key in ("pack_ciphertext", "result_ciphertext", "fingerprint", "attempts", "api_requests",
                "input_tokens", "output_tokens"):
        assert archived[key] == task[key]
    assert archived["id"] == str(task["id"])
    _drain_preview(pilot.settings)
    assert len(case.calls) == 1 and _task(pg, case)["status"] == "ready"
    assert _task(pg, case)["fingerprint"] != task["fingerprint"]
    case.score, case.outcome = .99, "review"
    assert handles.start_run(pilot.ids["owner"], case.run) == 1
    assert handles.start_run(pilot.ids["owner"], case.run) == 0
    _drain_paid(pilot.settings)
    task = _task(pg, case)
    stored = json.loads(ownership._cipher(pilot.settings).decrypt(task["result_ciphertext"]))
    assert stored["model_output"]["outcome"] == stored["checked"]["outcome"] == task["outcome"] == "review"
    assert all(c["confidence"] == .99 and not c["conflicts"] and not c["missing_info"]
               for c in stored["checked"]["candidates"])
    assert handles._decode(task, "result_ciphertext", pilot.settings) == stored["checked"]
    assert (task["attempts"], task["api_requests"], task["input_tokens"], task["output_tokens"]) == (2, 2, 34, 18)
    assert len(case.calls) == 2
    pg.execute("UPDATE chats SET name='creator_one X TT' WHERE chat_id='handle_group'")
    with pytest.raises(ValueError):
        handles.recheck_task(pilot.ids["owner"], task["id"])
    pg.execute("UPDATE chats SET name='creator_one * Trutonic Media' WHERE chat_id='handle_group'")
    handles.set_auto_admit(pilot.ids["owner"], case.run, True)
    _drain_preview(pilot.settings)
    assert handles.start_run(pilot.ids["owner"], case.run) == 0 and len(case.calls) == 2
    assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()
    # Reuse the frozen real material to exercise sensitive admission after different facts change.
    payload = handles._decode(task, "result_ciphertext", pilot.settings)
    payload["outcome"] = "matched"
    for candidate in payload["candidates"]:
        candidate.update(confidence=.99, missing_info=[])
    pg.execute("UPDATE creator_handle_tasks SET outcome='matched',result_ciphertext=%s WHERE id=%s",
               (ownership._cipher(pilot.settings).encrypt(json.dumps(payload)), task["id"]))
    pg.execute("INSERT INTO lark_identities(open_id,display_name) VALUES('ou_unknown','Other')")
    changes = (
        ("UPDATE chats SET name='renamed X TT' WHERE chat_id='handle_group'", ()),
        ("INSERT INTO chat_members(chat_id,open_id,member_type) VALUES('handle_group','ou_unknown','user')", ()),
        ("UPDATE creator_ownership_tasks SET outcome='uncertain' WHERE id=%s", (task["ownership_task_id"],)),
        ("UPDATE authorizations SET body_consent_at=NULL WHERE id=%s", (pilot.ids["cm"],)),
    )
    for query, params in changes:
        with pg.transaction(force_rollback=True):
            pg.execute(query, params)
            assert handles.prepare_next_preview(pilot.settings)
            assert _task(pg, case)["outcome"] == "conflict"
            assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()
    # A paused run must not keep auto-admitting completed requests.
    pg.execute("UPDATE creator_handle_runs SET status='paused' WHERE id=%s", (case.run,))
    _drain_preview(pilot.settings)
    assert _task(pg, case)["review_status"] == "pending"
    assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()


def test_existing_person_or_account_owners_are_not_overwritten_and_no_partial_accounts(pg, pilot, monkeypatch):
    case = _case(pg, pilot, monkeypatch, clues=False)
    task = _task(pg, case)
    with pg.transaction(force_rollback=True):
        pg.execute("DELETE FROM contact_social_accounts WHERE contact_id=%s", (pilot.ids["legacy"],))
        pg.execute("UPDATE contacts SET creator_type='cap' WHERE id=%s", (pilot.ids["legacy"],))
        legacy = handles.page_data(pilot.ids["owner"], tab="mine")["items"]
        assert len(legacy) == 1 and legacy[0]["handles"] == ["legacy_account"]
        assert legacy[0]["sources"] == ["手动"]
    for cm in (pilot.ids["cm"], pilot.ids["owner"]):
        with pg.transaction(force_rollback=True):
            pg.execute("INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) "
                       "VALUES(%s,%s,%s,'manual')", (uuid4(), case.creator, cm))
            before = _snapshot(pg, ("contact_assignments", "social_accounts", "contact_social_accounts"))
            if cm == pilot.ids["cm"]:
                handles.confirm_task(pilot.ids["cm"], task["id"], ["new_handle"])
                assert _snapshot(pg, ("contact_assignments",))["contact_assignments"] == before["contact_assignments"]
                pg.execute("UPDATE contacts SET creator_type='cap' WHERE id=%s", (case.creator,))
                assert handles.page_data(cm, cm_id=cm, tab="mine")["items"][0]["handles"] == ["new_handle"]
            else:
                handles.confirm_task(pilot.ids["cm"], task["id"], ["new_handle"])
                assert pg.execute("SELECT count(*) n FROM contact_assignments WHERE contact_id=%s",
                                  (case.creator,)).fetchone()["n"] == 2
                assert pg.execute("SELECT 1 FROM contact_assignments WHERE id=%s",
                                  (before["contact_assignments"][0]["id"],)).fetchone()
    before = _snapshot(pg, ("contact_assignments", "social_accounts", "contact_social_accounts",
                            "creator_account_assignments"))
    with pytest.raises(ValueError, match="冲突"):
        handles.confirm_task(pilot.ids["cm"], task["id"], ["new_unused", "legacy_account"])
    assert _snapshot(pg, before) == before


def test_revocation_during_response_and_parent_change_before_dispatch_are_safe(pg, pilot, monkeypatch):
    case = _case(pg, pilot, monkeypatch)
    handles.start_run(pilot.ids["owner"], case.run)
    job = handles.claim_next_handle()
    with pg.transaction(force_rollback=True):
        pg.execute("UPDATE creator_ownership_tasks SET outcome='uncertain' WHERE id=%s",
                   (job["ownership_task_id"],))
        handles.run_claimed_handle(job, pilot.settings)
        assert _task(pg, case)["status"] == "invalid" and _task(pg, case)["api_requests"] == 0
    handles.pause_run(pilot.ids["owner"], case.run)
    handles.run_claimed_handle(job, pilot.settings)
    assert not case.calls and _task(pg, case)["api_requests"] == 0
    assert _task(pg, case)["attempts"] == 0
    handles.resume_run(pilot.ids["owner"], case.run)
    consent = pg.execute("SELECT body_consent_at FROM authorizations WHERE id=%s",
                         (pilot.ids["cm"],)).fetchone()["body_consent_at"]
    case.callbacks.append(lambda: pg.execute("UPDATE authorizations SET body_consent_at=NULL WHERE id=%s",
                                             (pilot.ids["cm"],)))
    _drain_paid(pilot.settings)
    task = _task(pg, case)
    assert task["status"] == "invalid" and task["api_requests"] == 1
    archived = json.loads(ownership._cipher(pilot.settings).decrypt(task["result_ciphertext"]))
    assert archived["model_output"]["outcome"] == "matched" and archived["checked"] is None
    assert handles._decode(task, "result_ciphertext", pilot.settings) == {}
    for operation in (lambda: handles.page_data(pilot.ids["cm"]),
                      lambda: handles.task_evidence(pilot.ids["owner"], task["id"])):
        with pytest.raises(PermissionError):
            operation()
    with pg.transaction(force_rollback=True):
        pg.execute("UPDATE authorizations SET body_consent_at=%s WHERE id=%s", (consent, pilot.ids["cm"]))
        assert handles.page_data(pilot.ids["cm"])["items"] == []
    assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()


def test_concurrent_confirmation_is_single_write(pg, pilot, monkeypatch):
    case = _case(pg, pilot, monkeypatch, clues=False)
    task = _task(pg, case)
    schema = pg.execute("SELECT current_schema() name").fetchone()["name"]
    assert re.fullmatch(r"test_people_[0-9a-f]{32}", schema)
    dsn = pg.info.dsn
    pg.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(schema)))
    pg.commit()

    @contextmanager
    def connected():
        with psycopg.connect(dsn, row_factory=dict_row,
                             options=f"-csearch_path={schema} -clock_timeout=2000 -cstatement_timeout=5000") as conn:
            yield conn

    try:
        for module in (handles, evidence, ownership, parent_evidence, admin_reporting):
            monkeypatch.setattr(module, "connection", connected)
        barrier = Barrier(2)

        def confirm():
            barrier.wait(timeout=5)
            return handles.confirm_task(pilot.ids["cm"], task["id"], ["one_account", "second_account"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(confirm), pool.submit(confirm)]
            assert sorted(f.result(timeout=10) for f in futures) == [0, 1]
        assert pg.execute("SELECT count(*) n FROM contact_assignments").fetchone()["n"] == 1
        assert pg.execute("SELECT count(*) n FROM contact_social_accounts WHERE handle_task_id=%s",
                          (task["id"],)).fetchone()["n"] == 2
        assert not case.calls

        # Reset this fixture's admission, then let a newer second-step result win the person lock.
        pg.execute("DELETE FROM contact_assignments WHERE handle_task_id=%s", (task["id"],))
        pg.execute("DELETE FROM contact_social_accounts WHERE handle_task_id=%s", (task["id"],))
        pg.execute("UPDATE creator_handle_tasks SET review_status='pending',selected_handles='{}' WHERE id=%s",
                   (task["id"],))
        newer_run, newer_task = uuid4(), uuid4()
        pg.execute("INSERT INTO creator_ownership_runs(id,cm_authorization_id,requested_by,policy_version,status) "
                   "VALUES(%s,%s,%s,%s,'running')",
                   (newer_run, pilot.ids["cm"], pilot.ids["owner"], ownership.POLICY_VERSION))
        pg.execute("INSERT INTO creator_ownership_tasks(id,run_id,contact_id,cm_authorization_id,policy_version,"
                   "status,attempts,fingerprint,config_fingerprint,pack_ciphertext,evidence_count,stats,created_at) "
                   "SELECT %s,%s,contact_id,cm_authorization_id,policy_version,'running',1,fingerprint,"
                   "config_fingerprint,pack_ciphertext,evidence_count,stats,clock_timestamp() "
                   "FROM creator_ownership_tasks WHERE id=%s", (newer_task, newer_run, task["ownership_task_id"]))
        pg.execute("INSERT INTO creator_ownership_refs(task_id,label,authorization_id,chat_id,message_id,metadata) "
                   "SELECT %s,label,authorization_id,chat_id,message_id,metadata FROM creator_ownership_refs "
                   "WHERE task_id=%s", (newer_task, task["ownership_task_id"]))
        pg.commit()
        person_locked, parent_checked = Event(), Event()
        waiter = {}
        original_parent, original_eligible = handles._parent, ownership._eligible_now

        def checked_parent(conn, current, settings, *, full=True):
            result = original_parent(conn, current, settings, full=full)
            if full:
                waiter["pid"] = conn.info.backend_pid
                parent_checked.set()
            return result

        def hold_person_lock(conn, current):
            person_locked.set()
            assert parent_checked.wait(timeout=5), "third step never completed its first parent check"
            for _ in range(200):
                if conn.execute("SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted", (waiter["pid"],)).fetchone():
                    break
                Event().wait(.005)
            else:
                pytest.fail("third step never waited for the second-step person lock")
            return original_eligible(conn, current)

        monkeypatch.setattr(handles, "_parent", checked_parent)
        monkeypatch.setattr(ownership, "_eligible_now", hold_person_lock)
        job = {"id": newer_task, "run_id": newer_run, "contact_id": case.creator, "attempts": 1}
        with ThreadPoolExecutor(max_workers=2) as pool:
            saving = pool.submit(ownership._finish_job, job, "succeeded", "classified", settings=pilot.settings,
                                 result={"outcome": "uncertain", "confidence": .99, "reason": "归属不清。"})
            assert person_locked.wait(timeout=5), "second step never locked the person"
            admission = pool.submit(handles.confirm_task, pilot.ids["cm"], task["id"], ["one_account"])
            saving.result(timeout=10)
            with pytest.raises(ValueError, match="第二步的归属建议已变化"):
                admission.result(timeout=10)
        assert pg.execute("SELECT outcome FROM creator_ownership_tasks WHERE id=%s", (newer_task,)).fetchone()[
            "outcome"] == "uncertain"
        assert not pg.execute("SELECT 1 FROM contact_assignments WHERE contact_id=%s", (case.creator,)).fetchone()
        assert _task(pg, case)["review_status"] == "pending" and not case.calls
    finally:
        pg.rollback()
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute(psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(psycopg.sql.Identifier(schema)))


def test_preparation_and_expiry_lock_run_before_task(pg, pilot, monkeypatch):
    case = _case(pg, pilot, monkeypatch, clues=False)
    task = _task(pg, case)
    queries = []

    class RecordedConnection:
        def __getattr__(self, name):
            return getattr(pg, name)

        def execute(self, query, *args, **kwargs):
            queries.append(" ".join(str(query).split()))
            return pg.execute(query, *args, **kwargs)

    monkeypatch.setattr(handles, "connection", lambda: nullcontext(RecordedConnection()))
    original = evidence.build_evidence

    def prepare(*args, fail=False):
        result = original(*args)
        queries.clear()  # Observe the save/cleanup transaction, after read-only material building.
        if fail:
            raise ValueError("test interrupted material preparation")
        return result

    def first_locked_table():
        query = next(q for q in queries if "creator_handle_" in q and (
            "FOR UPDATE" in q or q.startswith("UPDATE creator_handle_")
        ))
        return "creator_handle_runs" if "creator_handle_runs" in query else "creator_handle_tasks"

    for fail, expected in ((False, "skipped"), (True, "failed")):
        pg.execute("UPDATE creator_handle_tasks SET status='pending' WHERE id=%s", (task["id"],))
        pg.execute("UPDATE creator_handle_runs SET status='preparing' WHERE id=%s", (case.run,))
        monkeypatch.setattr(evidence, "build_evidence", lambda *args, fail=fail: prepare(*args, fail=fail))
        assert handles.prepare_next_preview(pilot.settings)
        assert first_locked_table() == "creator_handle_runs"
        assert _task(pg, case)["status"] == expected
    pg.execute("UPDATE creator_handle_tasks SET status='running',lease_until=NOW()-INTERVAL '1 minute' WHERE id=%s",
               (task["id"],))
    pg.execute("UPDATE creator_handle_runs SET status='running' WHERE id=%s", (case.run,))
    queries.clear()
    assert handles.claim_next_handle() is None
    assert first_locked_table() == "creator_handle_runs"
    assert _task(pg, case)["status"] == "failed"
    assert not case.calls


def test_excel_only_pages_exclude_old_ai_candidates_and_merge_manually_confirmed_handles(pg, pilot, monkeypatch):
    from test_creator_imports import workbook

    from app import creator_imports as imports

    monkeypatch.setattr(imports, "connection", lambda: nullcontext(pg))
    case = _case(pg, pilot, monkeypatch)
    case.score, case.outcome = .94, "review"
    handles.start_run(pilot.ids["owner"], case.run)
    _drain_paid(pilot.settings)
    paid = len(case.calls)
    cm = pilot.ids["cm"]
    assert handles.page_data(cm)["items"] == []
    batch = imports.preview_import(
        cm, cm, "roster.xlsx", workbook("creator_one", "creator_two", *(f"unmatched_{n}" for n in range(25)))
    )
    imports.start_import(cm, batch["id"])
    page = handles.page_data(cm, cm_id=cm, tab="candidates")
    following = handles.page_data(cm, cm_id=cm, tab="candidates", page=2)
    assert page["total_count"] == 27 and page["total_pages"] == 2
    assert len(page["items"]) == 25 and len(following["items"]) == 2
    items = page["items"] + following["items"]
    assert len({r["id"] for r in items}) == 27
    assert all(r["kind"] == "import" and r["sources"] == ["Excel"] and r["contact_id"] is None for r in items)
    assert page["run"] is None and not any(page["usage"].values())
    assert len(handles.page_data(cm, tab="candidates", query="creator_one")["items"]) == 1
    for handle in ("creator_one", "creator_two"):
        row = pg.execute("SELECT id FROM creator_import_rows WHERE batch_id=%s AND handle=%s",
                         (batch["id"], handle)).fetchone()
        imports.confirm_row(cm, row["id"], handle, case.creator)
    formal = handles.page_data(cm, cm_id=cm, tab="mine")["items"]
    assert len(formal) == 1 and set(formal[0]["handles"]) == {"creator_one", "creator_two"}
    assert formal[0]["sources"] == ["Excel"] and formal[0]["creator_type"] == "cap"
    assert handles.page_data(cm, tab="candidates")["total_count"] == 25
    assert handles.page_data(cm)["total_count"] == 1
    def unused_candidates(*args):
        raise AssertionError("The confirmed tab must not load candidate details")
    monkeypatch.setattr(imports, "candidate_items", unused_candidates)
    assert handles.page_data(cm)["items"] == formal
    assert len(case.calls) == paid
