"""Historical manual review and current roster; no retired scanner is used to seed fixtures."""

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
from psycopg.types.json import Jsonb
from test_creator_ownership import _prepared, _run_all, _snapshot
from test_creator_ownership import pilot as parent_pilot

from app import admin_reporting, creator_data, creator_roster, openai_client
from app import creator_handle_evidence as evidence
from app import creator_handles as handles
from app import creator_ownership as ownership
from app import creator_ownership_evidence as parent_evidence
from app.crypto import message_body_cipher

pilot = parent_pilot


def _task(pg, case, contact=None):
    return pg.execute("SELECT * FROM creator_handle_tasks WHERE run_id=%s AND contact_id=%s",
                      (case.run, contact or case.creator)).fetchone()


def _case(pg, pilot, monkeypatch, *, clues=True):
    from app import creator_imports

    for module in (handles, evidence, creator_imports, creator_roster):
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
    source = _prepared(pilot)
    ownership.start_run(pilot.ids["owner"], source)
    _run_all(pilot)
    parent = pg.execute("SELECT id FROM creator_ownership_tasks WHERE run_id=%s AND contact_id=%s",
                        (source, pilot.ids["creator"])).fetchone()
    material = evidence.build_evidence(pilot.ids["creator"], pilot.ids["cm"], pilot.settings)
    run, task = uuid4(), uuid4()
    pg.execute("""INSERT INTO creator_handle_runs(id,ownership_run_id,cm_authorization_id,requested_by,
                  policy_version,status,auto_admit) VALUES(%s,%s,%s,%s,%s,'completed',FALSE)""",
               (run, source, pilot.ids["cm"], pilot.ids["owner"], handles.POLICY_VERSION))
    pg.execute("""INSERT INTO creator_handle_tasks(id,run_id,ownership_task_id,contact_id,cm_authorization_id,
                  policy_version,status,pack_ciphertext,refs,stats,fingerprint)
                  VALUES(%s,%s,%s,%s,%s,%s,'skipped',%s,%s,%s,%s)""",
               (task, run, parent["id"], pilot.ids["creator"], pilot.ids["cm"], handles.POLICY_VERSION,
                creator_data.cipher(pilot.settings).encrypt(creator_data.encode(material["pack"])),
                Jsonb(json.loads(creator_data.encode(material["refs"]))), Jsonb(material["stats"]),
                material["fingerprint"]))
    def no_model(*args, **kwargs):
        raise AssertionError("Historical review must never call a model")
    monkeypatch.setattr(openai_client, "call_openai", no_model)
    return SimpleNamespace(run=run, source=source, creator=pilot.ids["creator"], calls=[])


def test_cm_can_fill_missing_handles_but_not_cross_scope_or_bypass_input_checks(pg, pilot, monkeypatch):
    case = _case(pg, pilot, monkeypatch, clues=False)
    task = _task(pg, case)
    assert task["status"] == "skipped" and not case.calls
    outsider = uuid4()
    pg.execute("INSERT INTO authorizations(id,open_id,access_token_ciphertext,body_consent_at) "
               "VALUES(%s,'ou_outsider','test',NOW())", (outsider,))
    for operation in (
        lambda: creator_roster.page_data(outsider, pilot.ids["cm"]),
        lambda: handles.confirm_task(outsider, task["id"], ["stolen"]),
        lambda: handles.task_evidence(outsider, task["id"]),
    ):
        with pytest.raises(PermissionError):
            operation()
    pg.execute("INSERT INTO admin_page_permissions(authorization_id,permission) VALUES(%s,'creator-roster')",
               (outsider,))
    view = creator_roster.page_data(outsider, pilot.ids["cm"])
    assert view["can_select_cm"] and view["read_only"] and not view["can_manage"]
    assert len(view["cms"]) > 1 and view["import_batches"] == []
    with pytest.raises(PermissionError):
        creator_roster.page_data(outsider, pilot.ids["cm"], tab="candidates")
    with pytest.raises(PermissionError):
        handles.confirm_task(outsider, task["id"], ["stolen"])
    pg.execute("DELETE FROM admin_page_permissions WHERE authorization_id=%s", (outsider,))
    with pytest.raises(PermissionError):
        creator_roster.page_data(outsider, pilot.ids["cm"])
    for values in ([], [""], ["bad handle"], ["https://evil.example/@person"], ["good", "bad/handle"]):
        with pytest.raises(ValueError):
            handles.confirm_task(pilot.ids["cm"], task["id"], values)
    assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()
    assert handles.confirm_task(pilot.ids["cm"], task["id"], ["manual.one", "@manual_two"]) == 1
    assert handles.confirm_task(pilot.ids["cm"], task["id"], ["manual_two", "manual.one"]) == 0
    assert pg.execute("SELECT count(*) n FROM contact_assignments").fetchone()["n"] == 1
    assert pg.execute("SELECT source FROM contact_assignments").fetchone()["source"] == "manual"
    formal = creator_roster.page_data(pilot.ids["cm"], tab="confirmed")["items"]
    assert formal == []  # The current library only lists CAP people.
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
                assert creator_roster.page_data(pilot.ids["cm"], tab="mine")["items"][0]["avatar_url"] == expected
    assert creator_roster.page_data(pilot.ids["cm"], tab="confirmed")["items"] == []
    assert not case.calls


def test_historical_evidence_and_manual_review_revalidate_current_scope(pg, pilot, monkeypatch):
    case = _case(pg, pilot, monkeypatch)
    task = _task(pg, case)
    actor = pilot.ids["cm"]
    assert handles.task_evidence(actor, task["id"])["messages"]
    handles.defer_task(actor, task["id"])
    assert _task(pg, case)["review_status"] == "deferred"
    for statement, params in (
        ("UPDATE authorizations SET status='revoked' WHERE id=%s", (actor,)),
        ("UPDATE creator_ownership_tasks SET outcome='uncertain' WHERE id=%s", (task["ownership_task_id"],)),
        ("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (actor,)),
    ):
        with pg.transaction(force_rollback=True):
            pg.execute(statement, params)
            for operation in (
                lambda: handles.task_evidence(actor, task["id"]),
                lambda: handles.confirm_task(actor, task["id"], ["creator_one"]),
            ):
                with pytest.raises((PermissionError, ValueError)):
                    operation()
            assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()
    with pg.transaction(force_rollback=True):
        pg.execute("UPDATE chats SET name='Changed group' WHERE chat_id='handle_group'")
        with pytest.raises(ValueError):
            handles.task_evidence(actor, task["id"])
    assert handles.confirm_task(actor, task["id"], ["creator_one"]) == 1
    with pytest.raises(ValueError):
        handles.defer_task(actor, task["id"])


def test_existing_person_or_account_owners_are_not_overwritten_and_no_partial_accounts(pg, pilot, monkeypatch):
    case = _case(pg, pilot, monkeypatch, clues=False)
    task = _task(pg, case)
    with pg.transaction(force_rollback=True):
        pg.execute("DELETE FROM contact_social_accounts WHERE contact_id=%s", (pilot.ids["legacy"],))
        pg.execute("UPDATE contacts SET creator_type='cap' WHERE id=%s", (pilot.ids["legacy"],))
        legacy = creator_roster.page_data(pilot.ids["owner"], tab="mine")["items"]
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
                assert creator_roster.page_data(cm, cm_id=cm, tab="mine")["items"][0]["handles"] == ["new_handle"]
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


def test_excel_only_pages_exclude_old_ai_candidates_and_merge_manually_confirmed_handles(pg, pilot, monkeypatch):
    from test_creator_imports import workbook

    from app import creator_imports as imports

    monkeypatch.setattr(imports, "connection", lambda: nullcontext(pg))
    case = _case(pg, pilot, monkeypatch)
    paid = len(case.calls)
    cm = pilot.ids["cm"]
    assert creator_roster.page_data(cm)["items"] == []
    batch = imports.preview_import(
        cm, cm, "roster.xlsx", workbook("creator_one", "creator_two", *(f"unmatched_{n}" for n in range(25)))
    )
    imports.start_import(cm, batch["id"])
    def no_nested_connection():
        raise AssertionError("Roster reads must reuse their authorized connection")

    with monkeypatch.context() as patch:
        patch.setattr(imports, "connection", no_nested_connection)
        page = creator_roster.page_data(cm, cm_id=cm, tab="candidates")
        following = creator_roster.page_data(cm, cm_id=cm, tab="candidates", page=2)
    assert page["total_count"] == 27 and page["total_pages"] == 2
    assert len(page["items"]) == 25 and len(following["items"]) == 2
    items = page["items"] + following["items"]
    assert len({r["id"] for r in items}) == 27
    assert all(r["kind"] == "import" and r["sources"] == ["Excel"] and r["contact_id"] is None for r in items)
    assert page["run"] is None and not any(page["usage"].values())
    assert len(creator_roster.page_data(cm, tab="candidates", query="creator_one")["items"]) == 1
    for handle in ("creator_one", "creator_two"):
        row = pg.execute("SELECT id FROM creator_import_rows WHERE batch_id=%s AND handle=%s",
                         (batch["id"], handle)).fetchone()
        imports.confirm_row(cm, row["id"], handle, case.creator)
    formal = creator_roster.page_data(cm, cm_id=cm, tab="mine")["items"]
    assert len(formal) == 1 and set(formal[0]["handles"]) == {"creator_one", "creator_two"}
    assert formal[0]["sources"] == ["Excel"] and formal[0]["creator_type"] == "cap"
    assert creator_roster.page_data(cm, tab="candidates")["total_count"] == 25
    assert creator_roster.page_data(cm)["total_count"] == 1
    def unused_candidates(*args):
        raise AssertionError("The confirmed tab must not load candidate details")
    monkeypatch.setattr(imports, "candidate_items", unused_candidates)
    assert creator_roster.page_data(cm)["items"] == formal
    assert len(case.calls) == paid
