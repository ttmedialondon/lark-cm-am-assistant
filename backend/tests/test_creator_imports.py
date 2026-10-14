"""Scoped roster evidence, automatic admission and manual correction."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from io import BytesIO
from pathlib import Path
from threading import Barrier
from uuid import uuid4
from xml.sax.saxutils import escape
from zipfile import ZipFile

import psycopg
import pytest
from cap_fixtures import _snapshot
from cap_fixtures import pilot as parent_pilot
from psycopg.rows import dict_row

from app import (
    admin_permissions,
    admin_reporting,
    creator_data,
    creator_roster,
    openai_client,
)
from app import creator_imports as imports
from app.crypto import message_body_cipher

pilot = parent_pilot


def test_reviewed_curva_admission_and_unassigned_visibility(pg, pilot, monkeypatch):
    from scripts.import_reviewed_creator_handles import admit

    prepare(pg, pilot, monkeypatch)
    owner, cm = pilot.ids['owner'], pilot.ids['cm']
    contact = uuid4()
    pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,'Reviewed person')", (contact,))
    rows = [{'contact_id': str(contact), 'handle': 'curva_reviewed', 'message_ids': ['reviewed-message']}]
    protected = _snapshot(pg, ('contact_assignments', 'creator_account_assignments'))
    with pytest.raises(ValueError, match='Curva'):
        admit(pg, owner, rows, set())
    with pytest.raises(PermissionError):
        admit(pg, cm, rows, {'curva_reviewed'})
    assert admit(pg, owner, rows, {'curva_reviewed'})['new_links'] == 1
    after = _snapshot(pg, ('contact_roles', 'contact_social_accounts', 'audit_events'))
    assert admit(pg, owner, rows, {'curva_reviewed'})['new_links'] == 0
    assert _snapshot(pg, ('contact_roles', 'contact_social_accounts', 'audit_events')) == after
    assert _snapshot(pg, ('contact_assignments', 'creator_account_assignments')) == protected
    assert pg.execute(
        'SELECT creator_type FROM contacts WHERE id=%s', (contact,),
    ).fetchone()['creator_type'] == 'pending'
    data = creator_roster.page_data(owner, tab='unassigned', query='curva_reviewed')
    assert data['total_count'] == 1 and data['read_only']
    assert data['items'][0]['handles'] == ['curva_reviewed']
    assert not creator_roster.page_data(cm)['items']
    with pytest.raises(PermissionError):
        creator_roster.page_data(cm, tab='unassigned')
    monkeypatch.setattr(admin_reporting, 'connection', lambda: nullcontext(pg))
    directory = admin_reporting.contacts(query='curva_reviewed')
    assert directory['total_count'] == 1
    assert directory['items'][0]['handles'] == ['curva_reviewed']
    other = uuid4()
    pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,'Other person')", (other,))
    with pytest.raises(ValueError, match='relationship conflict'):
        admit(pg, owner, [dict(rows[0], contact_id=str(other))], {'curva_reviewed'})


def test_retirement_preserves_cap_and_stops_every_old_queue(pg, pilot, monkeypatch):
    prepare(pg, pilot, monkeypatch)
    owner, cm, creator = (pilot.ids[k] for k in ("owner", "cm", "creator"))
    batch = imports.preview_import(owner, cm, "cap.xlsx", workbook("creator_one"))
    imports.start_import(owner, batch["id"])
    imports.confirm_row(cm, row_for(pg, batch, "creator_one")["id"], "creator_one", creator)
    protected = ("contacts", "contact_roles", "contact_assignments", "creator_account_assignments",
                 "social_accounts", "contact_social_accounts", "creator_import_batches", "creator_import_rows",
                 "authorizations", "chat_messages", "authorization_message_observations")
    before = _snapshot(pg, protected)
    config = creator_data.config(pg)
    pg.execute(
        "INSERT INTO ai_feature_configs(feature,provider,project_id,model,api_key_ciphertext,key_suffix,enabled) "
        "SELECT 'contact_assessment',provider,project_id,model,api_key_ciphertext,key_suffix,TRUE "
        "FROM ai_feature_configs WHERE feature='creator_import' ON CONFLICT(feature) DO UPDATE SET "
        "provider=EXCLUDED.provider,project_id=EXCLUDED.project_id,model=EXCLUDED.model,"
        "api_key_ciphertext=EXCLUDED.api_key_ciphertext,key_suffix=EXCLUDED.key_suffix,enabled=TRUE"
    )
    pg.execute("DELETE FROM ai_feature_configs WHERE feature='creator_import'")
    pg.execute("UPDATE contact_assessments SET status='running',lease_until=NOW()+INTERVAL '1 hour'")
    pg.execute("INSERT INTO contact_assignment_assessments(id,contact_id,status) VALUES(%s,%s,'queued')",
               (uuid4(), creator))
    run, task, handle_run, workflow_run = (uuid4() for _ in range(4))
    pg.execute("INSERT INTO creator_ownership_runs(id,cm_authorization_id,requested_by,policy_version,status) "
               "VALUES(%s,%s,%s,'legacy','running')", (run, cm, owner))
    pg.execute("INSERT INTO creator_ownership_tasks(id,run_id,contact_id,cm_authorization_id,policy_version,status) "
               "VALUES(%s,%s,%s,%s,'legacy','ready')", (task, run, creator, cm))
    pg.execute("INSERT INTO creator_handle_runs(id,ownership_run_id,cm_authorization_id,requested_by,"
               "policy_version,status,auto_admit) VALUES(%s,%s,%s,%s,'legacy','running',TRUE)",
               (handle_run, run, cm, owner))
    pg.execute("INSERT INTO creator_handle_tasks(id,run_id,ownership_task_id,contact_id,cm_authorization_id,"
               "policy_version,status) VALUES(%s,%s,%s,%s,%s,'legacy','queued')",
               (uuid4(), handle_run, task, creator, cm))
    pg.execute("INSERT INTO creator_workflow_ai_runs(id,responsible_authorization_id,requested_by_authorization_id,"
               "status,history_days,chat_limit,continuous,window_start,window_end) "
               "VALUES(%s,%s,%s,'running',30,10,TRUE,NOW()-INTERVAL '30 days',NOW())", (workflow_run,cm,owner))
    pg.execute("INSERT INTO creator_workflow_ai_tasks(id,run_id,chat_id,input_fingerprint,initial_fingerprint,status) "
               "VALUES(%s,%s,'chat_creator','legacy','legacy','queued')", (uuid4(),workflow_run))
    pg.execute("INSERT INTO creator_workflow_onboarding(authorization_id,status) VALUES(%s,'waiting_sync')", (cm,))
    migration = (Path(__file__).parents[1]/"migrations/070_retire_non_cap_recognition.sql").read_text()
    pg.execute(migration)
    assert _snapshot(pg, protected) == before
    assert creator_data.config(pg) == config
    for table in ("contact_assessments", "contact_assignment_assessments", "creator_ownership_tasks",
                  "creator_handle_tasks", "creator_workflow_ai_tasks"):
        assert not pg.execute(f"SELECT 1 FROM {table} WHERE status IN "
                              "('pending','preparing','ready','queued','running')").fetchone()
    assert not pg.execute("SELECT 1 FROM creator_workflow_onboarding WHERE status IN "
                          "('waiting_sync','ready','scanning')").fetchone()
    assert not pg.execute("SELECT 1 FROM creator_handle_runs WHERE auto_admit").fetchone()
    assert not pg.execute("SELECT 1 FROM creator_workflow_ai_runs WHERE continuous").fetchone()
    after = _snapshot(pg, (*protected, "creator_handle_runs", "creator_ownership_tasks", "audit_events"))
    pg.execute(migration)
    assert _snapshot(pg, (*protected, "creator_handle_runs", "creator_ownership_tasks", "audit_events")) == after
    assert creator_roster.page_data(cm, tab="mine")["total_count"] == 1


def workbook(*values, formula=False):
    output = BytesIO()
    cells = "".join(
        f'<row r="{i}"><c r="A{i}" t="inlineStr">'
        f"{'<f>HYPERLINK(&quot;https://invalid.example&quot;)</f>' if formula else ''}"
        f"<is><t>{escape(value)}</t></is></c></row>"
        for i, value in enumerate(values, 1)
    )
    with ZipFile(output, "w") as archive:
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/'
            f'spreadsheetml/2006/main"><sheetData>{cells}</sheetData></worksheet>',
        )
    return output.getvalue()


def group(pg, pilot, chat, handle, *members):
    pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES(%s,%s,'group') "
               "ON CONFLICT(chat_id) DO UPDATE SET name=EXCLUDED.name,chat_mode='group'", (chat, handle + " X TT"))
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
               (pilot.ids["cm"], chat))
    for name in ("cm", *members):
        pg.execute("INSERT INTO chat_members(chat_id,open_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                   (chat, "ou_" + name))
    pg.execute("UPDATE authorization_chats SET member_snapshot_status='succeeded',member_count=%s,"
               "member_count_status='succeeded' WHERE authorization_id=%s AND chat_id=%s",
               (len(set(("cm", *members))), pilot.ids["cm"], chat))


def prepare(pg, pilot, monkeypatch):
    monkeypatch.setattr(imports, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(imports, "get_settings", lambda: pilot.settings)
    monkeypatch.setattr(creator_roster, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(admin_permissions, "connection", lambda: nullcontext(pg))
    calls, callbacks = [], []

    def respond(_config, _key, pack, _row_id, **kwargs):
        stage = pack["stage"]
        assert kwargs["output_format"]["strict"]
        version = 6 if pack["policy_version"] == imports.creator_import_evidence.POLICY_VERSION else 5
        assert kwargs["output_format"]["name"] == f"creator_import_{stage}_v{version}"
        assert "_rules" not in pack
        calls.append(pack)
        if stage == "group":
            groups = [g["ref"] for g in pack["groups"] if pack["imported_handle"] in g["name"].casefold()]
            result = {"outcome": "matched" if groups else "not_found", "group_refs": groups,
                      "confidence": .99 if groups else 0, "reason": "根据群资料核对当前账号。",
                      "evidence_refs": groups}
        else:
            people = [u for u in pack["users"] if u["eligible"]]
            own = {u["ref"]: [m["ref"] for m in pack["messages"]
                              if m["speaker"] == u["ref"] and pack["imported_handle"] in m["text"]] for u in people}
            target = next((u for u in people if own[u["ref"]] or u.get("existing_handle_owner")), None)
            target = target or (people[0] if len(people) == 1 else None)
            refs = (own[target["ref"]] or [*(g["ref"] for g in pack["groups"]), target["ref"]]) if target else []
            result = {"outcome": "matched" if target else "review", "user_ref": target["ref"] if target else None,
                      "confidence": .99 if target else .5, "reason": "结合 CAP 名单、成员和账号发言核对用户。",
                      "evidence_refs": refs, "conflicts": [], "candidates": [
                          {"user_ref": u["ref"], "assessment": "target" if u == target else "uncertain",
                           "reason": "有对应账号的定位依据。" if u == target else "尚未确定与目标账号的关系。",
                           "evidence_refs": refs if u == target else []} for u in people]}
            if version == 6:
                known = bool(target and target.get("creator_known"))
                identity_refs = own[target["ref"]][:6] if target else []
                result.update(creator={"state": "known" if known else "supported" if identity_refs else "review",
                                      "code": "known" if known else "own_activity" if identity_refs else "insufficient",
                                      "confidence": 1 if known else .99 if identity_refs else .5,
                                      "refs": [] if known else identity_refs},
                              link_code="self_claim" if target and own[target["ref"]] else "group_context")
        for callback in callbacks:
            callback(stage, pack, result)
        return {"text": json.dumps(result), "response_id": f"synthetic-{stage}-{len(calls)}",
                "usage": {"input_tokens": 12, "output_tokens": 8},
                "telemetry": {"openai_request_id": f"synthetic-request-{len(calls)}"}}

    monkeypatch.setattr(openai_client, "call_openai", respond)
    return calls, callbacks


def drain(pilot):
    for _ in range(100):
        prepared = imports.prepare_next_import(pilot.settings)
        job = imports.claim_next_import()
        if job:
            imports.run_claimed_import(job, pilot.settings)
        if not prepared and not job:
            return
    pytest.fail("two-stage import queue did not settle")


def row_for(pg, batch, handle):
    return pg.execute(
        "SELECT * FROM creator_import_rows WHERE batch_id=%s AND handle=%s", (batch["id"], handle)
    ).fetchone()


def test_import_batch_shares_history_scan_and_validates_only_relevant_material(pg, pilot, monkeypatch):
    from collections import Counter

    from app import creator_import_evidence as evidence

    calls, callbacks = prepare(pg, pilot, monkeypatch)
    cm = pilot.ids['cm']
    group(pg, pilot, 'fallback', 'campaign', 'creator')
    group(pg, pilot, 'canonical', 'canonical_one', 'creator')
    add_message(pg, pilot, 'fallback', 'creator', 'My accounts are alpha.creator and .dot_creator.')
    own = add_message(pg, pilot, 'canonical', 'creator', 'My account is canonical_one. I film my own TikTok videos.')
    pg.execute("UPDATE chat_messages SET message_create_time=NOW()-INTERVAL '1 minute' WHERE message_id=%s", (own,))
    uncited = add_message(pg, pilot, 'canonical', 'cm', 'Please arrange campaign samples')
    batch = imports.preview_import(cm, cm, 'batch.xlsx',
                                   workbook('alpha.creator', '.dot_creator', 'canonical_one', 'alpha',
                                            *(f'fallback_{i}' for i in range(96)), 'next_cohort'))
    imports.start_import(cm, batch['id'])
    decrypt = evidence.people._decrypt_body
    seen = Counter()

    def counted(cipher, encrypted):
        seen[encrypted] += 1
        return decrypt(cipher, encrypted)

    monkeypatch.setattr(evidence.people, '_decrypt_body', counted)
    assert imports.prepare_next_import(pilot.settings)
    assert seen and max(seen.values()) == 1
    assert not calls
    assert not row_for(pg, batch, 'next_cohort')['pack_ciphertext']
    for handle in ('alpha.creator', '.dot_creator'):
        prepared = imports._decode(row_for(pg, batch, handle), pilot.settings)['group']
        assert prepared['stats']['history_matching_groups'] == 1
        assert prepared['pack']['groups'][0]['match_kind'] == 'history'
        assert '_decrypted_body' not in json.dumps(prepared)
    assert not imports._decode(row_for(pg, batch, 'alpha'), pilot.settings)['group']['pack']['groups']
    local = row_for(pg, batch, 'canonical_one')
    assert local['stats']['analysis_stage'] == 'person' and local['api_requests'] == 0
    assert local['review_status'] == 'pending' and not local['contact_id']
    # The next worker turn handles the bounded overflow before the ready person stage.
    assert imports.prepare_next_import(pilot.settings)
    assert row_for(pg, batch, 'next_cohort')['pack_ciphertext']
    assert imports.prepare_next_import(pilot.settings)
    local = row_for(pg, batch, 'canonical_one')
    material = imports._decode(local, pilot.settings)['person']
    assert evidence.validate_references(pg, pilot.settings, cm, material, check_candidates=True)
    legacy = {**material, 'stats': {k: v for k, v in material['stats'].items() if k != 'candidate_fingerprint'}}
    assert evidence.validate_references(pg, pilot.settings, cm, legacy, check_candidates=True)
    with pg.transaction(force_rollback=True):
        other = pilot.person('new_candidate')
        pg.execute("INSERT INTO chat_members(chat_id,open_id) VALUES('canonical','ou_new_candidate')")
        assert other and not evidence.validate_references(
            pg, pilot.settings, cm, material, check_candidates=True)
        assert not evidence.validate_references(pg, pilot.settings, cm, legacy, check_candidates=True)
    # An unrelated new message and an unrelated account do not invalidate paid evidence.
    def unrelated_change(stage, pack, result):
        if stage == 'person' and pack['imported_handle'] == 'canonical_one':
            add_message(pg, pilot, 'canonical', 'cm', 'Unrelated campaign update')
            pg.execute('UPDATE chat_messages SET body_ciphertext=NULL WHERE message_id=%s', (uncited,))
            pg.execute("UPDATE authorizations SET display_name='Updated colleague' WHERE id=%s",
                       (pilot.ids['owner'],))

    callbacks.append(unrelated_change)
    monkeypatch.setattr(imports, '_build_material', lambda *_a, **_kw: pytest.fail('paid material was rebuilt'))
    while job := imports.claim_next_import():
        imports.run_claimed_import(job, pilot.settings)
    admitted = row_for(pg, batch, 'canonical_one')
    assert admitted['review_status'] == 'confirmed' and admitted['api_requests'] == 1, (
        admitted['reason_code'], imports._decode(admitted, pilot.settings, 'result_ciphertext'))
    assert [p['stage'] for p in calls if p['imported_handle'] == 'canonical_one'] == ['person']
    failed = imports.preview_import(cm, cm, 'failed.xlsx', workbook('unread_first', 'unread_second'))
    imports.start_import(cm, failed['id'])

    def fail_context(*_args):
        raise RuntimeError('history search unavailable')

    monkeypatch.setattr(evidence, 'group_batch_context', fail_context)
    assert imports.prepare_next_import(pilot.settings)
    assert all(row_for(pg, failed, handle)['reason_code'] == 'preparation_failed'
               for handle in ('unread_first', 'unread_second'))
    stale = {**row_for(pg, failed, 'unread_first'), 'uploaded_by': cm}
    pg.execute("UPDATE creator_import_rows SET status='preparing',lease_until=NOW()+INTERVAL '10 minutes' WHERE id=%s",
               (stale['id'],))
    imports._prepare_import_row(pilot.settings, stale, preparation_error='preparation_failed')
    assert row_for(pg, failed, 'unread_first')['status'] == 'preparing'


def test_cap_excel_two_stage_matching_manual_confirmation_and_replays_preserve_people(pg, pilot, monkeypatch):
    calls, _ = prepare(pg, pilot, monkeypatch)
    cm, owner, creator = pilot.ids["cm"], pilot.ids["owner"], pilot.ids["creator"]
    group(pg, pilot, "chat_creator", "beauty.one_12", "creator")
    add_message(pg, pilot, "chat_creator", "creator", "My account is beauty.one_12. I make my own videos.")
    before_people = pg.execute("SELECT id,display_name FROM contacts ORDER BY id").fetchall()
    data = workbook("handle", " @Beauty.One_12 ", "beauty.one_12", "second_02", "missing", "bad handle")
    batch = imports.preview_import(owner, cm, "roster.xlsx", data)
    assert batch["counts"] == {"valid": 3, "duplicates": 1, "invalid": 1}
    saved = pg.execute("SELECT * FROM creator_import_batches WHERE id=%s", (batch["id"],)).fetchone()
    assert saved["uploaded_by"] == owner and saved["cm_authorization_id"] == cm and saved["creator_type"] == "pending"
    assert not pg.execute("SELECT 1 FROM creator_import_rows").fetchone()
    assert not calls
    assert imports.preview_import(owner, cm, "roster.xlsx", data)["id"] == batch["id"]
    pg.execute("UPDATE creator_import_batches SET creator_type='non_cap' WHERE id=%s", (batch["id"],))
    assert imports.preview_import(owner, cm, "roster.xlsx", data)["id"] == batch["id"]
    imports.start_import(owner, batch["id"])
    imports.start_import(cm, batch["id"])
    rows = pg.execute("SELECT * FROM creator_import_rows ORDER BY handle").fetchall()
    assert len(rows) == 3 and all(r["contact_id"] is None and r["review_status"] == "pending" for r in rows)
    assert all(r["api_requests"] == 0 and not r["pack_ciphertext"] and r["refs"] == [] for r in rows)
    assert all(r["status"] == "pending" for r in rows)
    assert pg.execute("SELECT status FROM creator_import_batches WHERE id=%s", (batch["id"],)).fetchone()[
        "status"] == "running"
    candidates = imports.page_data(cm, cm)["candidates"]
    assert len(candidates) == 3
    assert all(r["display_name"] == "—" and "creator_type" not in r and r["sources"] == ["Excel"]
               for r in candidates)
    first, second, missing = (row_for(pg, batch, h) for h in ("beauty.one_12", "second_02", "missing"))
    drain(pilot)
    matched = row_for(pg, batch, "beauty.one_12")
    assert matched["contact_id"] == creator and matched["review_status"] == "confirmed"
    assert matched["reason_code"] == "ai_confirmed"
    assert pg.execute("SELECT count(*) n FROM contact_assignments").fetchone()["n"] == 1
    assert matched["api_requests"] == 1 and imports.source_labels(matched) == ["Excel", "AI"]
    assert [p["stage"] for p in calls if p["imported_handle"] == "beauty.one_12"] == ["person"]
    assert all(p['roster'] == {'responsibility_confirmed': False, 'purpose': 'candidate_handles'} for p in calls)
    imports.confirm_row(cm, first["id"], "beauty.one_12", creator)
    imports.confirm_row(cm, first["id"], "@Beauty.One_12", creator)
    imports.confirm_row(cm, second["id"], "corrected_02", creator)
    imports.defer_row(cm, missing["id"])
    assert pg.execute("SELECT count(*) n FROM contact_assignments").fetchone()["n"] == 1
    assert pg.execute(
        "SELECT creator_type FROM contacts WHERE id=%s", (creator,),
    ).fetchone()["creator_type"] == "pending"
    assert pg.execute("SELECT status FROM contact_roles WHERE contact_id=%s AND role='creator'",
                      (creator,)).fetchone()["status"] == "confirmed"
    formal = creator_roster.page_data(cm, tab="mine")["items"]
    assert len(formal) == 1 and set(formal[0]["handles"]) == {"beauty.one_12", "corrected_02"}
    assert "sources" not in formal[0] and "evidence_rows" not in formal[0]
    assert formal[0]["profile_url"] is None
    assert creator_roster.page_data(owner, cm, tab="mine")["items"][0]["profile_url"] == "/admin/contacts/ou_creator"
    imports.preview_import(owner, cm, "roster.xlsx", data)
    imports.start_import(owner, batch["id"])
    assert row_for(pg, batch, "second_02") is None
    assert row_for(pg, batch, "missing")["review_status"] == "deferred"
    assert pg.execute("SELECT count(*) n FROM creator_import_rows").fetchone()["n"] == 3
    facts = _snapshot(pg, ("contacts", "contact_roles", "contact_social_accounts", "contact_assignments",
                           "creator_account_assignments"))
    again = imports.preview_import(owner, cm, "again.xlsx", workbook("beauty.one_12", "legacy_account"))
    imports.start_import(owner, again["id"])
    assert row_for(pg, again, "beauty.one_12")["reason_code"] == "existing"
    assert _snapshot(pg, facts) == facts
    conflict = row_for(pg, again, "legacy_account")
    assert conflict["status"] == "skipped" and conflict["reason_code"] == "authorization_changed"
    assert set(creator_roster.page_data(cm, tab="mine")["items"][0]["handles"]) == {"beauty.one_12", "corrected_02"}
    assert pg.execute("SELECT id,display_name FROM contacts ORDER BY id").fetchall() == before_people
    assert row_for(pg, batch, "corrected_02")["api_requests"] == 1
    assert imports.source_labels(row_for(pg, batch, "corrected_02")) == ["Excel"]
    for creator_type in ("pending", "non_cap"):
        with pg.transaction(force_rollback=True):
            pg.execute("UPDATE contacts SET creator_type=%s WHERE id=%s", (creator_type, creator))
            unchanged = _snapshot(pg, facts)
            review = imports.preview_import(owner, cm, "review.xlsx", workbook("beauty.one_12"))
            imports.start_import(owner, review["id"])
            row = row_for(pg, review, "beauty.one_12")
            assert row["review_status"] == "confirmed" and _snapshot(pg, facts) == unchanged
            assert any(p["contact_id"] == str(creator) for p in creator_roster.page_data(cm)["items"])


def test_manual_import_scope_identity_account_and_type_conflicts_are_atomic(pg, pilot, monkeypatch):
    prepare(pg, pilot, monkeypatch)
    cm, owner, creator = pilot.ids["cm"], pilot.ids["owner"], pilot.ids["creator"]
    batch = imports.preview_import(owner, cm, "roster.xlsx", workbook("creator_one", "occupied"))
    for action in (
        lambda: imports.page_data(cm, owner),
        lambda: imports.search_users(cm, owner, "creator"),
        lambda: imports.preview_import(cm, owner, "x.xlsx", workbook("x")),
    ):
        with pytest.raises(PermissionError):
            action()
    assert imports.search_users(cm, cm, "silent") == []
    imports.start_import(owner, batch["id"])
    row = row_for(pg, batch, "creator_one")
    safe = pilot.person("batch_safe")
    class Meter:
        calls = 0

        def execute(self, *args, **kwargs):
            self.calls += 1
            return pg.execute(*args, **kwargs)

    meter = Meter()
    safe_item = ({"batch_creator_type": "cap"}, safe, "unlinked_safe")
    assert imports._conflicts(meter, []) == [] and meter.calls == 0
    assert imports._conflicts(meter, [safe_item] * 50) == [False] * 50
    assert meter.calls == 1
    with pytest.raises(PermissionError):
        imports.confirm_row(cm, row["id"], "creator_one", pilot.ids["silent"])
    with pytest.raises(ValueError):
        imports.confirm_row(cm, row["id"], "creator_one", uuid4())
    with pytest.raises(ValueError):
        imports.confirm_row(cm, row["id"], "occupied", creator)
    for conflict in ("account", "merchant", "rejected"):
        with pg.transaction(force_rollback=True):
            if conflict == "merchant":
                pg.execute("INSERT INTO contact_roles(contact_id,role,status,source) "
                           "VALUES(%s,'merchant_contact','confirmed','manual')", (creator,))
            elif conflict == "rejected":
                pg.execute("UPDATE contact_roles SET status='rejected',source='manual' "
                           "WHERE contact_id=%s AND role='creator'", (creator,))
            handle = "legacy_account" if conflict == "account" else "creator_one"
            assert imports._conflicts(pg, [safe_item, ({"batch_creator_type": "cap"}, creator, handle),
                                          safe_item]) == [False, True, False]
            with pytest.raises(ValueError, match="冲突"):
                imports.confirm_row(
                    cm, row["id"], "legacy_account" if conflict == "account" else "creator_one", creator,
                )
            assert not pg.execute("SELECT 1 FROM contact_social_accounts WHERE import_row_id=%s",
                                  (row["id"],)).fetchone()
    pg.execute("UPDATE creator_import_rows SET contact_id=%s,pack_ciphertext=%s WHERE id=%s",
               (creator, creator_data.cipher(pilot.settings).encrypt("{}"), row["id"]))
    pg.execute("UPDATE contacts SET creator_type='non_cap' WHERE id=%s", (creator,))
    imports.confirm_row(cm, row["id"], "corrected_handle", creator)
    assert row_for(pg, batch, "corrected_handle")["review_status"] == "confirmed"
    assert pg.execute(
        "SELECT creator_type FROM contacts WHERE id=%s", (creator,),
    ).fetchone()['creator_type'] == 'non_cap'
    assert creator_roster.page_data(cm)['items'][0]['contact_id'] == str(creator)
    prior = pg.execute("SELECT metadata->'previous' previous FROM audit_events "
                       "WHERE action='creator_import_evidence_invalidated' AND metadata->>'row_id'=%s",
                       (str(row["id"]),)).fetchone()["previous"]
    assert prior["contact_id"] == str(creator) and prior["handle"] == "creator_one"
    with pytest.raises(ValueError):
        imports.confirm_row(cm, row["id"], "other_handle", creator)
    with pg.transaction(force_rollback=True):
        pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (cm,))
        with pytest.raises(PermissionError):
            imports.start_import(owner, batch["id"])


def test_shared_creator_reuses_identity_without_sharing_message_permissions(pg, pilot, monkeypatch):
    from scripts.reconcile_shared_creators import reconcile_row
    calls, _ = prepare(pg, pilot, monkeypatch)
    cm, other, person = pilot.ids["cm"], pilot.ids["owner"], pilot.ids["creator"]
    original = imports.preview_import(cm, cm, "first.xlsx", workbook("shared_handle"))
    imports.start_import(cm, original["id"])
    imports.confirm_row(cm, row_for(pg, original, "shared_handle")["id"], "shared_handle", person)
    link_before = pg.execute("SELECT * FROM contact_social_accounts WHERE contact_id=%s", (person,)).fetchone()
    role_before = pg.execute("SELECT * FROM contact_roles WHERE contact_id=%s AND role='creator'",
                             (person,)).fetchone()
    denied = imports.preview_import(other, other, "denied.xlsx", workbook("shared_handle"))
    imports.start_import(other, denied["id"])
    assert row_for(pg, denied, "shared_handle")["reason_code"] == "authorization_changed"
    assert pg.execute("SELECT count(*) n FROM contact_assignments WHERE contact_id=%s", (person,)).fetchone()["n"] == 1
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_open_id) "
               "SELECT %s,chat_id,p2p_counterpart_open_id FROM authorization_chats WHERE authorization_id=%s",
               (other, cm))
    denied_row = row_for(pg, denied, "shared_handle")
    assert reconcile_row(pg, other, denied_row["id"]) == "admitted"
    assert reconcile_row(pg, other, denied_row["id"]) == "unchanged"
    shared = imports.preview_import(other, other, "shared.xlsx", workbook("@shared_handle"))
    imports.start_import(other, shared["id"])
    imports.start_import(other, shared["id"])
    saved = row_for(pg, shared, "shared_handle")
    assert saved["review_status"] == "confirmed" and saved["reason_code"] == "existing"
    assert saved["api_requests"] == 0 and not calls
    assert pg.execute("SELECT count(*) n FROM contact_assignments WHERE contact_id=%s", (person,)).fetchone()["n"] == 2
    assert (
        pg.execute("SELECT count(*) n FROM social_accounts WHERE normalized_handle='shared_handle'").fetchone()["n"]
        == 1
    )
    link = pg.execute("SELECT * FROM contact_social_accounts WHERE contact_id=%s", (person,)).fetchone()
    assert link == link_before and pg.execute("SELECT * FROM contact_roles WHERE contact_id=%s AND role='creator'",
                                             (person,)).fetchone() == role_before
    for target in (cm, other):
        view = creator_roster.page_data(other, cm_id=target, tab="mine")
        assert any(item["id"] == str(person) for item in view["items"])
    with pytest.raises(PermissionError):
        imports.row_evidence(cm, saved["id"])
    # Ending one CM's relationship leaves the other CM's library intact.
    pg.execute("UPDATE contact_assignments SET ended_at=NOW() WHERE contact_id=%s AND responsible_authorization_id=%s",
               (person, cm))
    assert not creator_roster.page_data(cm, cm_id=cm, tab="mine")["items"]
    assert any(item["id"] == str(person) for item in creator_roster.page_data(other, cm_id=other, tab="mine")["items"])
    with pg.transaction(force_rollback=True):
        pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (other,))
        another = imports.preview_import(other, other, "revoked.xlsx", workbook("handle", "shared_handle"))
        imports.start_import(other, another["id"])
        assert row_for(pg, another, "shared_handle")["review_status"] == "pending"


def test_manual_import_concurrent_confirmations_create_one_person_and_account_link(pg, pilot, monkeypatch):
    prepare(pg, pilot, monkeypatch)
    cm, creator = pilot.ids["cm"], pilot.ids["creator"]
    batches = [imports.preview_import(actor, cm, name, workbook("same_account"))
               for actor, name in ((cm, "one.xlsx"), (pilot.ids["owner"], "two.xlsx"))]
    for batch in batches:
        imports.start_import(cm, batch["id"])
    row_ids = [row_for(pg, batch, "same_account")["id"] for batch in batches]
    schema = pg.execute("SELECT current_schema() name").fetchone()["name"]
    dsn = os.environ.get("TEST_DATABASE_URL") or os.environ["CREATOR_WORKFLOW_AI_TEST_DATABASE_URL"]
    pg.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(schema)))
    pg.commit()

    @contextmanager
    def connected():
        with psycopg.connect(dsn, row_factory=dict_row,
                             options=f"-csearch_path={schema} -clock_timeout=2000 -cstatement_timeout=5000") as conn:
            yield conn

    try:
        monkeypatch.setattr(imports, "connection", connected)
        barrier = Barrier(2)

        def confirm(row_id):
            barrier.wait(timeout=5)
            return imports.confirm_row(cm, row_id, "same_account", creator)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(confirm, row_id) for row_id in row_ids]
            assert all(f.result(timeout=10)["status"] == "confirmed" for f in futures)
        assert pg.execute("SELECT count(*) n FROM contact_assignments").fetchone()["n"] == 1
        assert pg.execute("SELECT count(*) n FROM social_accounts "
                          "WHERE normalized_handle='same_account'").fetchone()["n"] == 1
        assert pg.execute("SELECT count(*) n FROM contact_social_accounts WHERE contact_id=%s",
                          (creator,)).fetchone()["n"] == 1
        extra = imports.preview_import(cm, cm, "extra.xlsx", workbook("second_account"))
        imports.start_import(cm, extra["id"])
        imports.confirm_row(cm, row_for(pg, extra, "second_account")["id"], "second_account", creator)
        reverse = [imports.preview_import(cm, cm, name, workbook(*values)) for name, values in (
            ("forward.xlsx", ("same_account", "second_account")),
            ("reverse.xlsx", ("second_account", "same_account")),
        )]
        facts = _snapshot(pg, ("contacts", "contact_roles", "contact_social_accounts", "contact_assignments"))
        second_lookup, original_existing = Barrier(2), imports._existing

        def interleaved_existing(conn, row):
            if row["row_number"] == 2:
                second_lookup.wait(timeout=5)
            return original_existing(conn, row)

        monkeypatch.setattr(imports, "_existing", interleaved_existing)
        with ThreadPoolExecutor(max_workers=2) as pool:
            starts = [pool.submit(imports.start_import, cm, batch["id"]) for batch in reverse]
            assert all(f.result(timeout=10)["status"] == "completed" for f in starts)
        assert _snapshot(pg, facts) == facts
        for batch in reverse:
            for handle in ("same_account", "second_account"):
                row = row_for(pg, batch, handle)
                assert row["review_status"] == "confirmed" and row["reason_code"] == "existing"
        other = pilot.ids["owner"]
        with connected() as conn:
            conn.execute("INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_open_id) "
                         "SELECT %s,chat_id,p2p_counterpart_open_id FROM authorization_chats WHERE authorization_id=%s",
                         (other, cm))
        shared_batches = [imports.preview_import(other, target, "shared.xlsx", workbook("concurrent_shared"))
                          for target in (cm, other)]
        for batch in shared_batches:
            imports.start_import(other, batch["id"])
        with connected() as conn:
            shared_ids = [row_for(conn, batch, "concurrent_shared")["id"] for batch in shared_batches]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(imports.confirm_row, other, rid, "concurrent_shared", creator) for rid in shared_ids]
            assert all(f.result(timeout=10)["status"] == "confirmed" for f in results)
        with connected() as conn:
            assert conn.execute("SELECT count(*) n FROM contact_assignments WHERE contact_id=%s AND ended_at IS NULL",
                                (creator,)).fetchone()["n"] == 2
            assert (
                conn.execute(
                    "SELECT count(*) n FROM social_accounts WHERE normalized_handle='concurrent_shared'"
                ).fetchone()["n"]
                == 1
            )
    finally:
        pg.rollback()
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute(psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(psycopg.sql.Identifier(schema)))



def add_message(pg, pilot, chat, sender, text):
    message_id = uuid4().hex
    pg.execute("INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,"
               "body_ciphertext,message_create_time) VALUES(%s,%s,%s,'user','text',%s,NOW())", (
                   chat, message_id, "ou_" + sender,
                   message_body_cipher(pilot.settings.token_encryption_key).encrypt(json.dumps({"text": text})),
               ))
    pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
               "VALUES(%s,%s,%s)", (pilot.ids["cm"], chat, message_id))
    return message_id


def test_manuals_unify_private_import_identity_account_and_existing_facts(pg, pilot, monkeypatch):
    from app import assistant_rules

    calls, callbacks = prepare(pg, pilot, monkeypatch)
    cm = pilot.ids['cm']
    person = pilot.person('private_new')
    pg.execute("UPDATE contacts SET entity_type='person' WHERE id=%s", (person,))
    pg.execute('DELETE FROM contact_roles WHERE contact_id=%s', (person,))
    pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES('private_only','New conversation','p2p')")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_open_id) "
               "VALUES(%s,'private_only','ou_private_new')", (cm,))
    question = add_message(pg, pilot, 'private_only', 'cm', 'Please send your TikTok handle')
    answer = add_message(pg, pilot, 'private_only', 'private_new', 'private_handle')
    activity = add_message(pg, pilot, 'private_only', 'private_new', 'I filmed my TikTok videos this morning.')
    for minute, message_id in ((2, question), (1, answer), (0, activity)):
        pg.execute("UPDATE chat_messages SET message_create_time=NOW()-(%s*INTERVAL '1 minute') WHERE message_id=%s",
                   (minute, message_id))

    def private_response(stage, pack, result):
        if stage == 'group':
            assert pack['groups'][0]['mode'] == 'p2p'
            result.update(outcome='matched', group_refs=[pack['groups'][0]['ref']], confidence=.99,
                          evidence_refs=[pack['groups'][0]['ref'], next(m['ref'] for m in pack['messages']
                                                                     if m['text'] == 'private_handle')])
            return
        target = next(u['ref'] for u in pack['users'] if u['eligible'])
        by_text = {m['text']: m for m in pack['messages']}
        answer_ref = by_text['private_handle']['ref']
        question_ref = by_text['Please send your TikTok handle']['ref']
        assert by_text['private_handle']['previous_ref'] == question_ref
        identity_ref = by_text['I filmed my TikTok videos this morning.']['ref']
        result.update(user_ref=target, outcome='matched', link_code='qa_confirm',
                      evidence_refs=[question_ref, answer_ref],
                      creator={'state': 'supported', 'code': 'own_activity', 'confidence': .99,
                               'refs': [identity_ref]},
                      candidates=[{'user_ref': target, 'assessment': 'target', 'reason': '本人完整私聊问答。',
                                   'evidence_refs': [question_ref, answer_ref]}])

    callbacks.append(private_response)
    batch = imports.preview_import(cm, cm, 'private.xlsx', workbook('private_handle'))
    imports.start_import(cm, batch['id'])
    drain(pilot)
    row = row_for(pg, batch, 'private_handle')
    assert row['review_status'] == 'confirmed' and row['contact_id'] == person and row['api_requests'] == 2, (
        row['reason_code'], imports._decode(row, pilot.settings, 'result_ciphertext'))
    assert [p['stage'] for p in calls] == ['group', 'person']
    chain = imports._decode(row, pilot.settings)
    assert chain['group']['pack']['_rules']['digest'] == chain['person']['pack']['_rules']['digest']
    assert set(chain['person']['pack']['_rules']['documents']) == assistant_rules.CREATOR_KEYS
    assert imports.creator_import_evidence.validate_references(pg, pilot.settings, cm, chain['person'])
    assignment = pg.execute('SELECT source,created_by_authorization_id FROM contact_assignments '
                            'WHERE contact_id=%s', (person,)).fetchone()
    assert assignment == {'source': 'manual', 'created_by_authorization_id': cm}
    facts = _snapshot(pg, ('contact_roles', 'contact_social_accounts', 'contact_assignments'))
    duplicate = imports.preview_import(cm, cm, 'private-copy.xlsx', workbook('handle', 'private_handle'))
    imports.start_import(cm, duplicate['id'])
    assert row_for(pg, duplicate, 'private_handle')['reason_code'] == 'existing'
    assert _snapshot(pg, facts) == facts and len(calls) == 2
    with pg.transaction(force_rollback=True):
        pg.execute("UPDATE authorizations SET scopes=ARRAY['im:chat:read']::text[] WHERE id=%s", (cm,))
        assert not imports.creator_import_evidence.validate_references(pg, pilot.settings, cm, chain['person'])
    callbacks.clear()
    identity_only = pilot.person('identity_wait')
    pg.execute("UPDATE contacts SET entity_type='person' WHERE id=%s", (identity_only,))
    pg.execute('DELETE FROM contact_roles WHERE contact_id=%s', (identity_only,))
    group(pg, pilot, 'identity_wait', 'waiting_handle', 'identity_wait')
    add_message(pg, pilot, 'identity_wait', 'identity_wait', 'I filmed my TikTok videos today.')

    def identity_without_account(stage, pack, result):
        if stage != 'person':
            return
        target = next(u['ref'] for u in pack['users'] if u['eligible'])
        proof = next(m['ref'] for m in pack['messages'] if m['speaker'] == target)
        result.update(outcome='review', user_ref=target, link_code='no_link_evidence', evidence_refs=[],
                      creator={'state': 'supported', 'code': 'own_activity', 'confidence': .99, 'refs': [proof]},
                      candidates=[{'user_ref': target, 'assessment': 'uncertain',
                                   'reason': '本人在创作，但当前账号归属待核实。', 'evidence_refs': []}])

    callbacks.append(identity_without_account)
    pending = imports.preview_import(cm, cm, 'identity.xlsx', workbook('waiting_handle'))
    imports.start_import(cm, pending['id'])
    drain(pilot)
    row = row_for(pg, pending, 'waiting_handle')
    assert row['review_status'] == 'pending' and row['contact_id'] == identity_only
    assert pg.execute("SELECT 1 FROM contact_roles WHERE contact_id=%s AND role='creator' AND status='confirmed'",
                      (identity_only,)).fetchone()
    assert not pg.execute('SELECT 1 FROM contact_social_accounts WHERE contact_id=%s', (identity_only,)).fetchone()
    assert not pg.execute('SELECT 1 FROM contact_assignments WHERE contact_id=%s', (identity_only,)).fetchone()


def test_shared_group_identifies_each_handle_person_and_preserves_the_evidence_chain(pg, pilot, monkeypatch):
    calls, _ = prepare(pg, pilot, monkeypatch)
    cm, first = pilot.ids["cm"], pilot.ids["creator"]
    second = pilot.person("other")
    group(pg, pilot, "chat_creator", "first_handle + second_handle", "creator", "other")
    add_message(pg, pilot, "chat_creator", "creator", "My account is @first_handle. I publish my own videos.")
    add_message(pg, pilot, "chat_creator", "other", "My account is @second_handle. I livestream my own content.")
    batch = imports.preview_import(cm, cm, "shared.xlsx", workbook("first_handle", "second_handle"))
    assert not calls
    imports.start_import(cm, batch["id"])
    drain(pilot)
    for handle, contact in (("first_handle", first), ("second_handle", second)):
        row = row_for(pg, batch, handle)
        assert row["review_status"] == "confirmed" and row["contact_id"] == contact
        assert row["reason_code"] == "ai_confirmed"
        imports.confirm_row(cm, row["id"], handle, contact)
        row = row_for(pg, batch, handle)
        assert row["review_status"] == "confirmed"
        assert (row["api_requests"], row["input_tokens"], row["output_tokens"]) == (2, 24, 16)
        assert row["stats"]["analysis_stage"] == "completed"
        chain = json.loads(creator_data.cipher(pilot.settings).decrypt(row["pack_ciphertext"]))
        judgments = json.loads(creator_data.cipher(pilot.settings).decrypt(row["result_ciphertext"]))
        assert set(chain) >= {"group", "person"} and set(judgments) >= {"group", "person"}
        assert all(chain[phase]["pack"]["imported_handle"] == handle and chain[phase]["refs"]
                   and chain[phase]["fingerprint"] for phase in ("group", "person"))
        assert len([u for u in chain["person"]["pack"]["users"] if u["eligible"]]) == 2
        assert judgments["group"]["group_refs"] and judgments["person"]["user_ref"]
        assert imports.source_labels(row) == ["Excel", "AI"]
        formal = next(item for item in creator_roster.page_data(cm, tab="mine")["items"]
                      if item["contact_id"] == str(contact))
        assert handle in formal["handles"] and "sources" not in formal and "evidence_rows" not in formal
        displayed = imports.row_evidence(cm, row["id"])
        assert displayed["groups"] and displayed["person"]["confidence"] == .99
        assert any(g["selected"] and len(g["members"]) == 3 for g in displayed["groups"])
        assert any(handle in m["text"] for g in displayed["groups"] for m in g["messages"])
        with pytest.raises(PermissionError):
            imports.row_evidence(pilot.ids["creator"], row["id"])
        with pg.transaction(force_rollback=True):
            pg.execute("UPDATE chat_messages SET body_ciphertext=NULL WHERE chat_id='chat_creator'")
            stale = imports.row_evidence(cm, row["id"])
            assert not stale["groups"] and not stale["person"] and stale["warnings"]
        with pg.transaction(force_rollback=True):
            pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (cm,))
            hidden = imports.row_evidence(cm, row["id"])
            assert not hidden["groups"] and not hidden["person"] and hidden["warnings"]
    assert len(calls) == 4 and pg.execute("SELECT count(*) n FROM contact_assignments").fetchone()["n"] == 2


def test_import_admission_respects_configured_threshold_without_curva_and_scopes_batch_review(pg, pilot, monkeypatch):
    calls, callbacks = prepare(pg, pilot, monkeypatch)
    cm, creator = pilot.ids["cm"], pilot.ids["creator"]
    group(pg, pilot, "chat_creator", "threshold_handle", "creator")
    add_message(pg, pilot, "chat_creator", "creator", "My account is threshold_handle. I create videos.")
    callbacks.append(lambda _stage, _pack, result: result.update(confidence=.93))
    pg.execute("UPDATE ai_feature_configs SET auto_confirm_threshold=0.920 WHERE feature='creator_import'")
    batch = imports.preview_import(cm, cm, "threshold.xlsx", workbook("threshold_handle", "manual_only"))
    imports.start_import(cm, batch["id"])
    drain(pilot)
    admitted = row_for(pg, batch, "threshold_handle")
    assert admitted["review_status"] == "confirmed" and admitted["reason_code"] == "ai_confirmed"
    assert admitted["contact_id"] == creator
    assert all(pack["catalog_check"]["status"] == "not_provided" for pack in calls)
    evidence = imports.row_evidence(cm, admitted["id"])
    assert evidence["group_decision"]["status_label"] == evidence["person"]["status_label"] == "对应关系通过"
    chain = imports._decode(admitted, pilot.settings, "result_ciphertext")
    assert chain["group"]["localized_locally"] and chain["person"]["auto_confirm_threshold"] == .92
    other = imports.preview_import(cm, cm, "other.xlsx", workbook("another_manual"))
    imports.start_import(cm, other["id"])
    scoped = imports.page_data(cm, cm, batch_id=batch["id"])
    assert scoped["total"] == 1 and [row["handle"] for row in scoped["candidates"]] == ["manual_only"]
    assert imports.page_data(cm, cm)["total"] == 2
    with pytest.raises(PermissionError):
        imports.page_data(pilot.ids["owner"], pilot.ids["owner"], batch_id=batch["id"])
    pg.execute("UPDATE ai_feature_configs SET auto_confirm_threshold=0.960 WHERE feature='creator_import'")
    group(pg, pilot, "higher_threshold", "high_handle", "creator")
    add_message(pg, pilot, "higher_threshold", "creator", "My account is high_handle. I create videos.")
    high = imports.preview_import(cm, cm, "high.xlsx", workbook("high_handle"))
    imports.start_import(cm, high["id"])
    drain(pilot)
    reviewed = row_for(pg, high, "high_handle")
    assert reviewed["review_status"] == "pending" and reviewed["contact_id"] is None
    assert [pack["stage"] for pack in calls if pack["imported_handle"] == "high_handle"] == ["person"]
    assert not pg.execute("SELECT 1 FROM social_accounts WHERE normalized_handle='high_handle'").fetchone()


def test_silent_unique_person_needs_person_evidence_and_recheck_keeps_usage(pg, pilot, monkeypatch):
    calls, callbacks = prepare(pg, pilot, monkeypatch)
    pg.execute("UPDATE ai_feature_configs SET auto_confirm_threshold=0.950 WHERE feature='creator_import'")
    cm = pilot.ids["cm"]
    group(pg, pilot, "chat_creator", "silent_handle", "creator")
    pg.execute("UPDATE chat_messages SET body_ciphertext=NULL")
    def low_confidence(stage, _pack, result):
        if stage == "person":
            result["confidence"] = .9

    callbacks.append(low_confidence)
    batch = imports.preview_import(cm, cm, "silent.xlsx", workbook("silent_handle"))
    imports.start_import(cm, batch["id"])
    with pytest.raises(ValueError):
        imports.recheck_batch(cm, batch["id"])
    imports.pause_import(cm, batch["id"])
    assert not imports.prepare_next_import(pilot.settings) and not imports.claim_next_import()
    assert not calls
    imports.resume_import(cm, batch["id"])
    drain(pilot)
    first = row_for(pg, batch, "silent_handle")
    assert first["review_status"] == "pending" and first["contact_id"] is None and first["api_requests"] == 1
    assert [p["stage"] for p in calls] == ["person"]
    assert not calls[-1]["messages"] and any(u["eligible"] for u in calls[-1]["users"])
    callbacks.clear()
    imports.recheck_batch(cm, batch["id"])
    drain(pilot)
    second = row_for(pg, batch, "silent_handle")
    assert second["id"] == first["id"] and second["api_requests"] == 2
    assert second["review_status"] == "pending" and second["contact_id"] is None
    callbacks.clear()
    imports.recheck_batch(cm, batch["id"])
    drain(pilot)
    final = row_for(pg, batch, "silent_handle")
    assert final["review_status"] == "pending" and final["contact_id"] is None
    assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()
    assert (final["api_requests"], final["input_tokens"], final["output_tokens"]) == (3, 36, 24)
    assert pg.execute("SELECT count(*) n FROM creator_import_rows WHERE batch_id=%s",
                      (batch["id"],)).fetchone()["n"] == 1
    archived = pg.execute("SELECT metadata FROM audit_events WHERE action='creator_import_evidence_invalidated' "
                          "ORDER BY created_at").fetchall()
    assert len(archived) == 2
    for audit, prior in zip(archived, (first, second), strict=True):
        previous = audit["metadata"]["previous"]
        assert previous["pack_ciphertext"] == prior["pack_ciphertext"]
        assert previous["result_ciphertext"] == prior["result_ciphertext"]
        assert previous["refs"] == prior["refs"]
    mixed = imports.preview_import(cm, cm, "selective.xlsx", workbook("retry", "no_group", "deferred", "confirmed"))
    imports.start_import(cm, mixed["id"])
    imports.confirm_row(cm, row_for(pg, mixed, "confirmed")["id"], "confirmed", pilot.ids["creator"])
    imports.defer_row(cm, row_for(pg, mixed, "deferred")["id"])
    drain(pilot)
    retry = row_for(pg, mixed, "retry")
    preserved = pg.execute("SELECT * FROM creator_import_rows WHERE batch_id=%s AND id<>%s ORDER BY id",
                           (mixed["id"], retry["id"])).fetchall()
    before = _snapshot(pg, ("creator_import_batches", "creator_import_rows"))
    assert imports.recheck_batch(cm, mixed["id"], row_ids=[])["queued"] == 0
    assert _snapshot(pg, before) == before
    selected = [retry["id"], *(r["id"] for r in preserved if r["review_status"] != "pending")]
    assert imports.recheck_batch(cm, mixed["id"], row_ids=selected)["queued"] == 1
    queued = row_for(pg, mixed, "retry")
    assert queued["status"] == "pending" and queued["api_requests"] == retry["api_requests"]
    assert queued["stats"]["analysis_generation"] == retry["stats"].get("analysis_generation", 0) + 1
    call_count = len(calls)
    drain(pilot)
    assert len(calls) == call_count + 1 and row_for(pg, mixed, "retry")["api_requests"] == retry["api_requests"] + 1
    assert pg.execute("SELECT * FROM creator_import_rows WHERE batch_id=%s AND id<>%s ORDER BY id",
                      (mixed["id"], retry["id"])).fetchall() == preserved


def test_multiple_groups_merge_the_same_person_without_hiding_other_people_or_partial_rosters(pg, pilot, monkeypatch):
    calls, _ = prepare(pg, pilot, monkeypatch)
    cm, creator = pilot.ids["cm"], pilot.ids["creator"]
    pilot.person("other")
    pg.execute("INSERT INTO lark_identities(open_id,contact_id,display_name) VALUES('ou_creator_alias',%s,'creator')",
               (creator,))
    for chat, handle, member in (
        ("jodine_first", "jodine_xx", "creator"),
        ("jodine_second", "jodine_xx", "creator_alias"),
        ("different_first", "different_people", "creator"),
        ("different_second", "different_people", "other"),
        ("partial_first", "partial_roster", "silent"),
        ("partial_second", "partial_roster", "silent"),
    ):
        group(pg, pilot, chat, handle, member)
    pg.execute("UPDATE chats SET name='TikTok x TT Media | @jodine_xx' WHERE chat_id='jodine_first'")
    pg.execute("UPDATE chats SET name='❤️ jodine_xx X TT Media' WHERE chat_id='jodine_second'")
    pg.execute("UPDATE authorization_chats SET member_snapshot_status='partial' WHERE chat_id='partial_second'")
    batch = imports.preview_import(cm, cm, "multi-group.xlsx",
                                   workbook("jodine_xx", "different_people", "partial_roster"))
    imports.start_import(cm, batch["id"])
    drain(pilot)

    matched = row_for(pg, batch, "jodine_xx")
    assert matched["review_status"] == "pending" and matched["contact_id"] is None
    material = imports._decode(matched, pilot.settings)["person"]
    primary = [g for g in material["pack"]["groups"] if g['match_kind'] != 'person_context']
    private = [g for g in material["pack"]["groups"] if g['match_kind'] == 'person_context']
    assert len(primary) == 2 and len(private) == 1 and private[0]['mode'] == 'p2p'
    assert all(m['group_ref'] == private[0]['ref'] for m in material["pack"]["messages"])
    users = [u for u in material["pack"]["users"] if u["eligible"]]
    assert len(users) == 1
    assert all(g["member_refs"] == [users[0]["ref"]] for g in material["pack"]["groups"])
    assert set(next(r["open_ids"] for r in material["refs"] if r["label"] == users[0]["ref"])) == {
        "ou_creator", "ou_creator_alias"}
    displayed = imports.row_evidence(cm, matched["id"])
    assert len(displayed["groups"]) == 3 and sum(g["selected"] for g in displayed["groups"]) == 2
    assert all(len(g["members"]) == 2 for g in displayed["groups"])
    for handle, count in (("different_people", 2), ("partial_roster", 1)):
        row = row_for(pg, batch, handle)
        assert row["review_status"] == "pending" and row["contact_id"] is None
        assert row["reason_code"] == "person_uncertain"
        assert row["stats"]["person_candidate_count"] == count and row["api_requests"] == 1
    assert len(calls) == 4 and pg.execute("SELECT count(*) n FROM contact_assignments").fetchone()["n"] == 0


def test_two_person_stages_for_one_creator_do_not_drop_paid_results_after_classification(pg, pilot, monkeypatch):
    calls, callbacks = prepare(pg, pilot, monkeypatch)
    cm = pilot.ids["cm"]
    pg.execute("DELETE FROM contact_roles WHERE contact_id=%s", (pilot.ids["creator"],))
    group(pg, pilot, "chat_creator", "first_handle + second_handle", "creator")
    add_message(pg, pilot, "chat_creator", "creator",
                "My accounts are first_handle and second_handle. I make videos on both.")
    batch = imports.preview_import(cm, cm, "parallel.xlsx", workbook("first_handle", "second_handle"))
    imports.start_import(cm, batch["id"])
    people = []
    for _ in range(20):
        imports.prepare_next_import(pilot.settings)
        job = imports.claim_next_import()
        if job:
            if job["stats"]["analysis_stage"] == "person":
                people.append(job)
                if len(people) == 2:
                    break
            else:
                imports.run_claimed_import(job, pilot.settings)
    assert len(people) == 2

    def finish_other(stage, _pack, _result):
        if stage == "person":
            callbacks.clear()
            imports.run_claimed_import(people[1], pilot.settings)
            imports.confirm_row(cm, people[1]["id"], people[1]["handle"], pilot.ids["creator"])

    callbacks.append(finish_other)
    imports.run_claimed_import(people[0], pilot.settings)
    drain(pilot)
    for handle in ("first_handle", "second_handle"):
        row = row_for(pg, batch, handle)
        assert row["contact_id"] == pilot.ids["creator"]
        assert row["review_status"] == "confirmed"
        assert row_for(pg, batch, handle)["review_status"] == "confirmed"
        assert (row["api_requests"], row["input_tokens"], row["output_tokens"]) == (2, 24, 16)
    assert len(calls) == 4 and pg.execute("SELECT count(*) n FROM contact_assignments").fetchone()["n"] == 1
    settled = _snapshot(pg, ("creator_import_rows", "contact_assignments", "contact_social_accounts"))
    results = imports._decode(row_for(pg, batch, people[0]["handle"]), pilot.settings, "result_ciphertext")
    imports._finish(people[0], pilot.settings, response=results["person"]["response"], result=results["person"])
    imports.run_claimed_import(people[0], pilot.settings)
    assert _snapshot(pg, settled) == settled and len(calls) == 4
    group(pg, pilot, "paid_incomplete_group", "paid.incomplete", "creator")
    failed_batch = imports.preview_import(cm, cm, "incomplete.xlsx", workbook("paid.incomplete"))
    imports.start_import(cm, failed_batch["id"])
    assert imports.prepare_next_import(pilot.settings)
    assert imports.prepare_next_import(pilot.settings)
    failed_job = imports.claim_next_import()
    assert failed_job is not None
    incomplete_calls = []

    def incomplete(*args, **kwargs):
        incomplete_calls.append(True)
        raise openai_client.ModelOutputError("unfinished response", telemetry={
            "http_status": 200, "openai_request_id": "req-incomplete", "response_id": "resp-incomplete",
            "usage": {"input_tokens": 31, "output_tokens": 17}, "response_status": "incomplete",
            "incomplete_reason": "max_output_tokens",
        })

    monkeypatch.setattr(openai_client, "call_openai", incomplete)
    imports.run_claimed_import(failed_job, pilot.settings)
    failed = row_for(pg, failed_batch, "paid.incomplete")
    assert (failed["status"], failed["reason_code"]) == ("invalid", "invalid_result")
    assert (failed["api_requests"], failed["input_tokens"], failed["output_tokens"]) == (1, 31, 17)
    assert (failed["request_id"], failed["response_id"]) == ("req-incomplete", "resp-incomplete")
    receipt = imports._decode(failed, pilot.settings, "result_ciphertext")["person"]["response"]
    assert receipt["usage"] == {"input_tokens": 31, "output_tokens": 17}
    assert receipt["telemetry"]["http_status"] == 200
    imports.run_claimed_import(failed_job, pilot.settings)
    assert incomplete_calls == [True] and row_for(pg, failed_batch, "paid.incomplete")["input_tokens"] == 31
    assert imports.claim_next_import() is None


@pytest.mark.parametrize("change", ["body", "scope", "manual", "threshold"])
def test_inflight_person_result_cannot_overwrite_changed_evidence_or_manual_selection(pg, pilot, monkeypatch, change):
    calls, callbacks = prepare(pg, pilot, monkeypatch)
    cm = pilot.ids["cm"]
    group(pg, pilot, "chat_creator", "creator_one", "creator")
    message = add_message(pg, pilot, "chat_creator", "creator", "My account is @creator_one; I make my own videos.")
    batch = imports.preview_import(cm, cm, "inflight.xlsx", workbook("creator_one"))
    imports.start_import(cm, batch["id"])
    row_id = row_for(pg, batch, "creator_one")["id"]

    def change_during_person(stage, _pack, _result):
        if stage != "person":
            return
        if change == "body":
            pg.execute("UPDATE chat_messages SET body_ciphertext=NULL WHERE message_id=%s", (message,))
        elif change == "scope":
            pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (cm,))
        elif change == "threshold":
            pg.execute("UPDATE ai_feature_configs SET auto_confirm_threshold=1 WHERE feature='creator_import'")
        else:
            imports.confirm_row(cm, row_id, "corrected_handle", pilot.ids["creator"])

    callbacks.append(change_during_person)
    drain(pilot)
    row = pg.execute("SELECT * FROM creator_import_rows WHERE id=%s", (row_id,)).fetchone()
    assert (row["api_requests"], row["input_tokens"], row["output_tokens"]) == (1, 12, 8)
    assert len(calls) == 1
    if change == "manual":
        assert row["review_status"] == "confirmed" and row["handle"] == "corrected_handle"
        assert imports.source_labels(row) == ["Excel"]
        assert not pg.execute("SELECT 1 FROM social_accounts WHERE normalized_handle='creator_one'").fetchone()
    else:
        assert row["review_status"] == "pending" and row["contact_id"] is None
        assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()


def test_import_pages_and_user_search_only_expose_currently_accessible_identities(pg, pilot, monkeypatch):
    prepare(pg, pilot, monkeypatch)
    cm, contact = pilot.ids["cm"], pilot.ids["creator"]
    batch = imports.preview_import(cm, cm, "roster.xlsx", workbook("roster_entry"))
    imports.start_import(cm, batch["id"])
    row = row_for(pg, batch, "roster_entry")
    pg.execute("UPDATE creator_import_rows SET contact_id=%s WHERE id=%s", (contact, row["id"]))
    pg.execute("UPDATE contacts SET creator_type='cap' WHERE id=%s", (contact,))
    # The result limit applies after access checks, even with earlier inaccessible name matches.
    pg.execute("INSERT INTO contacts(id,display_name) "
               "SELECT gen_random_uuid(),'aaa creator ' || n FROM generate_series(1,21) n")

    # Each original access path remains independently sufficient.
    for access_path in ("private", "member", "observation"):
        with pg.transaction(force_rollback=True):
            if access_path != "private":
                pg.execute("UPDATE authorization_chats SET p2p_counterpart_open_id=NULL")
            if access_path != "observation":
                pg.execute("UPDATE authorization_message_observations SET observed_at=NOW()-INTERVAL '2 days'")
            if access_path == "member":
                pg.execute("INSERT INTO chat_members(chat_id,open_id) VALUES('chat_creator','ou_creator')")
            assert imports._accessible(pg, cm, contact)
            assert imports.search_users(cm, cm, "creator")[0]["id"] == str(contact)
            page = imports.page_data(cm, cm, q="creator")
            assert page["total"] == 1 and "creator_type" not in page["candidates"][0]

    # A visible sender requires a readable observation after the CM's consent.
    with pg.transaction(force_rollback=True):
        pg.execute("UPDATE authorization_chats SET p2p_counterpart_open_id=NULL")
        pg.execute("UPDATE authorization_message_observations SET observed_at=NOW()-INTERVAL '2 days'")
        assert not imports._accessible(pg, cm, contact)
    pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (cm,))
    assert not imports._accessible(pg, cm, contact)
    assert imports.search_users(cm, cm, "creator") == []
    assert imports.page_data(cm, cm, q="creator")["total"] == 0
    candidate = imports.page_data(cm, cm)["candidates"][0]
    assert candidate["handle"] == "roster_entry" and candidate["contact_id"] is None
    assert candidate["display_name"] == "—" and "creator_type" not in candidate
    with pytest.raises(PermissionError):
        imports.confirm_row(cm, row["id"], "roster_entry", contact)


def test_import_workbook_rejects_unsafe_or_ambiguous_input():
    for name, content in (
        ("roster.xls", workbook("valid")),
        ("roster.xlsx", b"not a workbook"),
        ("roster.xlsx", workbook("valid", formula=True)),
        ("roster.xlsx", b"x" * (imports.MAX_FILE_BYTES + 1)),
    ):
        with pytest.raises(ValueError):
            imports.parse_xlsx(name, content)
    parsed = imports.parse_xlsx("roster.xlsx", workbook("handle", "https://www.tiktok.com/@Account.One", "account.one"))
    assert parsed["counts"] == {"valid": 1, "duplicates": 1, "invalid": 0}
    parsed = imports.parse_xlsx(
        "roster.xlsx", workbook("handle", ".horsegirlatheart", "@.horsegirlatheart", "horsegirlatheart")
    )
    assert parsed["counts"] == {"valid": 2, "duplicates": 1, "invalid": 0}
    assert [row["handle"] for row in parsed["rows"]] == [".horsegirlatheart", ".horsegirlatheart", "horsegirlatheart"]
    groups, _ = imports.creator_import_evidence._candidate_groups([
        {"chat_id": "dot", "name": ".horsegirlatheart X TT Media"},
        {"chat_id": "other", "name": "horsegirlatheart X TT Media"},
    ], ".horsegirlatheart")
    assert [(g["chat_id"], g["match_kind"]) for g in groups] == [("dot", "exact"), ("other", "similar")]
    assert imports.creator_import_evidence._TOKEN.findall(".horsegirlatheart") == [".horsegirlatheart"]


def test_leading_dot_repair_preserves_preview_and_existing_decisions(pg, pilot, monkeypatch):
    prepare(pg, pilot, monkeypatch)
    cm = pilot.ids["cm"]
    migration = (Path(__file__).parents[1] / "migrations/066_leading_dot_import_handles.sql").read_text()
    for status in ("preview", "running", "paused", "completed"):
        batch = imports.preview_import(cm, cm, "dots.xlsx", workbook("handle", "." + status, "regular_" + status))
        entries = batch["rows"]
        entries[0]["error"] = "handle 格式不正确"
        pg.execute("UPDATE creator_import_batches SET creator_type='cap',status=%s,preview_rows=%s::jsonb WHERE id=%s",
                   (status, json.dumps(entries), batch["id"]))
    pg.execute(migration)
    pg.execute(migration)  # Replays must not duplicate rows or audits.
    batches = pg.execute("SELECT * FROM creator_import_batches ORDER BY filename").fetchall()
    for batch in batches:
        original = batch["preview_rows"][0]["handle"][1:]
        assert batch["counts"] == {"valid": 2, "duplicates": 0, "invalid": 0}
        assert batch["status"] == ("running" if original == "completed" else original)
        rows = pg.execute(
            "SELECT handle,status,contact_id FROM creator_import_rows WHERE batch_id=%s", (batch["id"],)
        ).fetchall()
        assert len(rows) == (0 if original == "preview" else 1)
        if rows:
            assert rows[0] == {"handle": "." + original, "status": "pending", "contact_id": None}
    assert (
        pg.execute("SELECT count(*) n FROM audit_events WHERE action='creator_import_leading_dot_repaired'").fetchone()[
            "n"
        ]
        == 4
    )


def test_creator_library_reset_preserves_sources_history_and_new_data_on_replay(pg, pilot, monkeypatch):
    prepare(pg, pilot, monkeypatch)
    cm, owner, creator = pilot.ids["cm"], pilot.ids["owner"], pilot.ids["creator"]
    data = workbook("creator_one", "not_found")
    batch = imports.preview_import(owner, cm, "original.xlsx", data)
    imports.start_import(owner, batch["id"])
    imports.confirm_row(cm, row_for(pg, batch, "creator_one")["id"], "creator_one", creator)
    # Seed paid legacy history directly: the retired AI import worker no longer exists.
    pg.execute("UPDATE creator_import_rows SET pack_ciphertext='saved-pack',result_ciphertext='saved-result',"
               "fingerprint='saved-fingerprint',api_requests=1,input_tokens=12,output_tokens=8,"
               "response_id='saved-response',request_id='saved-request' WHERE handle='creator_one'")
    pg.execute("UPDATE chats SET name='new_handle X TT' WHERE chat_id='chat_creator'")
    queued = imports.preview_import(cm, cm, "inflight.xlsx", workbook("new_handle"))
    imports.start_import(cm, queued["id"])
    pg.execute("UPDATE creator_import_rows SET status='running',attempts=1,lease_until=NOW()+INTERVAL '1 hour' "
               "WHERE batch_id=%s", (queued["id"],))
    pg.execute("INSERT INTO contact_roles(contact_id,role,status,source,is_primary) "
               "VALUES(%s,'merchant_contact','confirmed','manual',TRUE)", (pilot.ids["cm"],))
    merchant, rejected, merchant_assessment = pilot.person("merchant"), pilot.person("rejected"), uuid4()
    pg.execute("UPDATE contact_roles SET is_primary=FALSE WHERE contact_id=ANY(%s::uuid[])", ([merchant, rejected],))
    pg.execute("UPDATE contact_roles SET status='rejected',source='manual' WHERE contact_id=%s", (rejected,))
    pg.execute("INSERT INTO contact_roles(contact_id,role,status,source,is_primary) "
               "VALUES(%s,'merchant_contact','confirmed','ai_auto_confirmed',TRUE)", (merchant,))
    pg.execute("INSERT INTO contact_assessments(id,contact_id,status,policy_version,result,created_at) "
               "VALUES(%s,%s,'succeeded',%s,%s::jsonb,NOW()+INTERVAL '1 second')", (
                   merchant_assessment, merchant, "people-identity-v1",
                   json.dumps({"reason_code": "classified", "roles": [
                       {"role": "merchant_contact", "confidence": .99}]}),
               ))
    merchant_query = admin_reporting.CONTACT_DIRECTORY_SQL + (
        "SELECT category,classification_source FROM directory WHERE contact_id=%s"
    )
    merchant_category = pg.execute(merchant_query, (merchant,)).fetchone()
    assert merchant_category == {"category": "merchant_contact", "classification_source": "ai_auto_confirmed"}
    pg.execute("INSERT INTO creator_workflow_candidates(id,normalized_handle,responsible_authorization_id,"
               "contact_id,relationship,status,uncertainties) "
               "VALUES(%s,'legacy_account',%s,%s,'owner','pending',ARRAY['historical_review'])",
               (uuid4(), owner, pilot.ids["legacy"]))
    pg.execute("UPDATE contacts SET creator_type='non_cap' WHERE id=%s", (pilot.ids["legacy"],))
    pg.execute("UPDATE ai_feature_configs SET enabled=TRUE "
               "WHERE feature IN ('contact_assessment','contact_assignment','creator_workflow')")
    pg.execute("UPDATE contact_assessments SET input_tokens=37,output_tokens=11")
    pg.execute("UPDATE contact_assessments SET status='running',lease_until=NOW()+INTERVAL '1 hour' "
               "WHERE contact_id=%s", (pilot.ids["silent"],))
    ownership_run, ownership_task, handle_run, handle_task, workflow_run = (uuid4() for _ in range(5))
    pg.execute("INSERT INTO creator_ownership_runs(id,cm_authorization_id,requested_by,policy_version,status) "
               "VALUES(%s,%s,%s,%s,'running')", (ownership_run, cm, owner, "ownership-v1"))
    pg.execute("INSERT INTO creator_ownership_tasks(id,run_id,contact_id,cm_authorization_id,policy_version,"
               "status,api_requests,input_tokens,output_tokens,result_ciphertext,lease_until) "
               "VALUES(%s,%s,%s,%s,%s,'running',1,21,9,'historical-ownership',NOW()+INTERVAL '1 hour')",
               (ownership_task, ownership_run, creator, cm, "ownership-v1"))
    pg.execute("INSERT INTO creator_handle_runs(id,ownership_run_id,cm_authorization_id,requested_by,"
               "policy_version,status,auto_admit) VALUES(%s,%s,%s,%s,%s,'running',TRUE)",
               (handle_run, ownership_run, cm, owner, "handles-v1"))
    pg.execute("INSERT INTO creator_handle_tasks(id,run_id,ownership_task_id,contact_id,cm_authorization_id,"
               "policy_version,status,api_requests,input_tokens,output_tokens,result_ciphertext,lease_until) "
               "VALUES(%s,%s,%s,%s,%s,%s,'running',1,22,8,'historical-handle',NOW()+INTERVAL '1 hour')",
               (handle_task, handle_run, ownership_task, creator, cm, "handles-v1"))
    pg.execute("INSERT INTO creator_workflow_ai_runs(id,responsible_authorization_id,requested_by_authorization_id,"
               "status,history_days,chat_limit,continuous,window_start,window_end) "
               "VALUES(%s,%s,%s,'ready',30,10,TRUE,NOW()-INTERVAL '30 days',NOW())", (workflow_run, cm, owner))
    pg.execute("INSERT INTO creator_workflow_ai_tasks(id,run_id,chat_id,input_fingerprint,initial_fingerprint,"
               "status,usage,lease_until) VALUES(%s,%s,'chat_creator','old','old','running',"
               "'{\"input_tokens\":17,\"output_tokens\":4}',NOW()+INTERVAL '1 hour')", (uuid4(), workflow_run))
    pg.execute("INSERT INTO contact_assignment_assessments(id,contact_id,status,input_tokens,output_tokens) "
               "VALUES(%s,%s,'running',18,3)", (uuid4(), creator))
    action = "creator_library_reset_for_new_rules_20260914"
    pg.execute("DELETE FROM audit_events WHERE action=%s", (action,))
    preserved = {table: pg.execute(f"SELECT to_jsonb(t) data FROM {table} t ORDER BY to_jsonb(t)::text").fetchall()
                 for table in ("lark_identities", "chats", "authorizations", "authorization_chats", "chat_messages",
                               "authorization_message_observations", "chat_members",
                               "social_accounts", "creator_workflow_sources", "creator_workflow_candidates",
                               "audit_events")}
    people = pg.execute("SELECT id,display_name FROM contacts ORDER BY id").fetchall()
    roles = pg.execute("SELECT * FROM contact_roles WHERE role<>'creator' OR status='rejected' "
                       "ORDER BY contact_id,role").fetchall()
    removed = {key: [r["data"] for r in pg.execute(query).fetchall()] for key, query in (
        ("removed_creator_roles", "SELECT to_jsonb(r) data FROM contact_roles r "
         "WHERE role='creator' AND status='confirmed'"),
        ("removed_account_links", "SELECT to_jsonb(l) data FROM contact_social_accounts l"),
    )}
    history = pg.execute("SELECT id,contact_id,result,input_tokens,output_tokens FROM contact_assessments "
                         "ORDER BY id").fetchall()
    excel = pg.execute("SELECT id,filename,file_fingerprint,preview_rows,counts,creator_type "
                       "FROM creator_import_batches ORDER BY id").fetchall()
    rows = pg.execute("SELECT id,handle,original_handle,pack_ciphertext,result_ciphertext,refs,"
                      "api_requests,input_tokens,output_tokens,response_id,request_id FROM creator_import_rows "
                      "ORDER BY id").fetchall()
    old_stats = pg.execute("SELECT id,stats,fingerprint FROM creator_import_rows ORDER BY id").fetchall()
    assert creator_roster.page_data(cm, tab="mine")["total_count"] == 1
    assert imports.page_data(cm, cm)["total"] == 2
    migration = (Path(__file__).parents[1] / "migrations" / "052_reset_creator_library_for_rule_review.sql").read_text()
    pg.execute(migration)
    for table, original in preserved.items():
        current = pg.execute(f"SELECT to_jsonb(t) data FROM {table} t ORDER BY to_jsonb(t)::text").fetchall()
        assert all(row in current for row in original) if table == "audit_events" else current == original
    assert pg.execute("SELECT id,display_name FROM contacts ORDER BY id").fetchall() == people
    assert pg.execute("SELECT * FROM contact_roles WHERE role<>'creator' OR status='rejected' "
                      "ORDER BY contact_id,role").fetchall() == roles
    assert not pg.execute("SELECT 1 FROM contact_roles WHERE role='creator' AND status='confirmed'").fetchone()
    assert imports._identity_conflict(pg, rejected)
    assert pg.execute(merchant_query, (merchant,)).fetchone() == merchant_category
    assert not pg.execute("SELECT 1 FROM contacts WHERE creator_type<>'pending'").fetchone()
    assert not pg.execute("SELECT 1 FROM contact_social_accounts WHERE status='confirmed'").fetchone()
    for table in ("contact_assignments", "creator_account_assignments"):
        assert pg.execute(f"SELECT count(*) n FROM {table}").fetchone()["n"] == 1
        assert not pg.execute(f"SELECT 1 FROM {table} WHERE ended_at IS NULL").fetchone()
    assert pg.execute("SELECT id,contact_id,result,input_tokens,output_tokens FROM contact_assessments "
                      "ORDER BY id").fetchall() == history
    assert pg.execute("SELECT id,filename,file_fingerprint,preview_rows,counts,creator_type "
                      "FROM creator_import_batches ORDER BY id").fetchall() == excel
    assert pg.execute("SELECT id,handle,original_handle,pack_ciphertext,result_ciphertext,refs,"
                      "api_requests,input_tokens,output_tokens,response_id,request_id FROM creator_import_rows "
                      "ORDER BY id").fetchall() == rows
    for old in old_stats:
        archived = pg.execute("SELECT stats FROM creator_import_rows WHERE id=%s", (old["id"],)).fetchone()["stats"]
        assert archived == {**old["stats"], "archived_fingerprint": old["fingerprint"], "archived_for_rule_reset": True}
    audit = pg.execute("SELECT metadata FROM audit_events WHERE action=%s", (action,)).fetchone()["metadata"]
    for key, original in removed.items():
        assert len(audit[key]) == len(original) and all(row in audit[key] for row in original)
    assert audit["source_data_and_analysis_history_preserved"] and not audit["new_scan_started"]
    assert not pg.execute("SELECT 1 FROM creator_import_rows WHERE fingerprint IS NOT NULL").fetchone()
    for actor in (cm, owner):
        assert creator_roster.page_data(actor, tab="mine")["total_count"] == 0
        assert creator_roster.page_data(actor, tab="candidates")["total_count"] == 0
        assert imports.page_data(actor, actor)["total"] == 0
    for table in ("creator_ownership_runs", "creator_ownership_tasks", "creator_handle_runs", "creator_handle_tasks",
                  "creator_import_batches"):
        assert not pg.execute(f"SELECT 1 FROM {table} WHERE policy_version NOT LIKE %s",
                              ("%:reset-20260914",)).fetchone()
    for assessment in pg.execute("SELECT id,policy_version FROM contact_assessments").fetchall():
        assert assessment["policy_version"] == "people-identity-v1" + (
            "" if assessment["id"] == merchant_assessment else ":reset-20260914"
        )
    assert not pg.execute("SELECT 1 FROM creator_handle_runs WHERE auto_admit").fetchone()
    for table in ("creator_workflow_ai_runs", "creator_workflow_ai_tasks", "contact_assignment_assessments"):
        assert not pg.execute(f"SELECT 1 FROM {table} WHERE status IN ('ready','queued','running')").fetchone()
    assert pg.execute("SELECT usage FROM creator_workflow_ai_tasks").fetchone()["usage"] == {
        "input_tokens": 17, "output_tokens": 4}
    for table, tokens, output in (("creator_ownership_tasks", 21, 9), ("creator_handle_tasks", 22, 8)):
        assert pg.execute(f"SELECT api_requests,input_tokens,output_tokens FROM {table}").fetchone() == {
            "api_requests": 1, "input_tokens": tokens, "output_tokens": output}
    assert not pg.execute("SELECT 1 FROM ai_feature_configs WHERE enabled "
                          "AND feature IN ('contact_assessment','contact_assignment','creator_workflow')").fetchone()
    assert row_for(pg, queued, "new_handle")["status"] == "invalid"
    with pytest.raises(ValueError):
        imports.start_import(cm, batch["id"])
    new_batch = imports.preview_import(owner, cm, "original.xlsx", data)
    assert new_batch["id"] != batch["id"]
    imports.start_import(owner, new_batch["id"])
    imports.confirm_row(cm, row_for(pg, new_batch, "creator_one")["id"], "creator_one", creator)
    pg.execute(migration)
    assert creator_roster.page_data(cm, tab="mine")["total_count"] == 1
    assert row_for(pg, new_batch, "creator_one")["review_status"] == "confirmed"
    assert pg.execute(
        "SELECT creator_type FROM contacts WHERE id=%s", (creator,),
    ).fetchone()["creator_type"] == "pending"
    assert pg.execute("SELECT count(*) n FROM audit_events WHERE action=%s", (action,)).fetchone()["n"] == 1


def test_evidence_upgrade_queues_current_rows_in_place_and_preserves_historical_work(pg, pilot, monkeypatch):
    prepare(pg, pilot, monkeypatch)
    cm = pilot.ids["cm"]
    batch = imports.preview_import(cm, cm, "existing.xlsx", workbook("confirmed", "deferred", "queued"))
    imports.start_import(cm, batch["id"])
    imports.confirm_row(cm, row_for(pg, batch, "confirmed")["id"], "confirmed", pilot.ids["creator"])
    imports.defer_row(cm, row_for(pg, batch, "deferred")["id"])
    pg.execute("UPDATE creator_import_batches SET policy_version='creator-import-cap-manual-v1',status='completed' "
               "WHERE id=%s", (batch["id"],))
    pending = row_for(pg, batch, "queued")
    pg.execute("UPDATE creator_import_rows SET status='skipped',api_requests=3,input_tokens=70,output_tokens=40,"
               "pack_ciphertext='historical-pack',result_ciphertext='historical-result',"
               "stats=%s::jsonb WHERE id=%s", (json.dumps({"prior_analysis": True}), pending["id"]))
    preview = imports.preview_import(cm, cm, "preview.xlsx", workbook("preview_only"))
    pg.execute("UPDATE creator_import_batches SET policy_version='creator-import-cap-manual-v1' WHERE id=%s",
               (preview["id"],))
    archived = imports.preview_import(cm, cm, "archived.xlsx", workbook("archived_entry"))
    imports.start_import(cm, archived["id"])
    pg.execute("UPDATE creator_import_batches SET policy_version='creator-import-v2:reset-20260914',"
               "status='completed' WHERE id=%s", (archived["id"],))
    unchanged = pg.execute("SELECT * FROM creator_import_rows WHERE id<>%s ORDER BY id", (pending["id"],)).fetchall()
    migration = (Path(__file__).parents[1] / "migrations" / "053_enable_cap_import_evidence_chain.sql").read_text()
    pg.execute(migration)
    current = row_for(pg, batch, "queued")
    assert current["id"] == pending["id"] and current["status"] == "pending"
    assert current["contact_id"] is None and current["review_status"] == "pending"
    assert (current["api_requests"], current["input_tokens"], current["output_tokens"]) == (3, 70, 40)
    assert (current["pack_ciphertext"], current["result_ciphertext"]) == ("historical-pack", "historical-result")
    assert current["stats"] == {"prior_analysis": True, "analysis_stage": "group", "analysis_generation": 1}
    assert pg.execute("SELECT * FROM creator_import_rows WHERE id<>%s ORDER BY id",
                      (pending["id"],)).fetchall() == unchanged
    assert pg.execute("SELECT status,policy_version FROM creator_import_batches WHERE id=%s",
                      (batch["id"],)).fetchone() == {"status": "running", "policy_version": imports.POLICY_VERSION}
    assert pg.execute("SELECT status,policy_version FROM creator_import_batches WHERE id=%s",
                      (preview["id"],)).fetchone() == {"status": "preview", "policy_version": imports.POLICY_VERSION}
    assert not pg.execute("SELECT 1 FROM creator_import_rows WHERE batch_id=%s", (preview["id"],)).fetchone()
    snapshot = _snapshot(pg, ("creator_import_batches", "creator_import_rows"))
    pg.execute(migration)
    assert _snapshot(pg, snapshot) == snapshot


def test_group_person_refs_do_not_cross_people_groups_or_current_access(pg, pilot, monkeypatch):
    from copy import deepcopy

    from app import creator_import_evidence as evidence

    prepare(pg, pilot, monkeypatch)
    cm = pilot.ids["cm"]
    other = pilot.person("other")
    group(pg, pilot, "first_group", "target_account", "creator", "other")
    group(pg, pilot, "second_group", "target_account", "other")
    add_message(pg, pilot, "first_group", "creator", "My account is @target_account, I create my own videos.")
    add_message(pg, pilot, "first_group", "other", "My own account is @different_account.")
    add_message(pg, pilot, "second_group", "other", "We discussed @target_account in a different group.")
    found = evidence.build_group_evidence(pg, pilot.settings, cm, "target_account")
    assert found["eligible"] and len(found["pack"]["groups"]) == 2
    first_group = next(r["label"] for r in found["refs"] if r.get("chat_id") == "first_group" and r["kind"] == "group")
    second_group = next(r["label"] for r in found["refs"]
                        if r.get("chat_id") == "second_group" and r["kind"] == "group")
    response = {"outcome": "matched", "group_refs": [first_group], "confidence": .99,
                "reason": "群名和原文包含目标账号。", "evidence_refs": [first_group]}
    assert evidence.group_validate(json.dumps(response), found["pack"])["outcome"] == "matched"
    selected_both = {**response, "group_refs": [first_group, second_group]}
    assert evidence.group_validate(json.dumps(selected_both), found["pack"])["outcome"] == "review"
    assert evidence.group_validate(json.dumps({**selected_both, "evidence_refs": [first_group, second_group]}),
                                   found["pack"])["outcome"] == "matched"
    forged = {**response, "group_refs": ["G999"]}
    with pytest.raises(ValueError):
        evidence.group_validate(json.dumps(forged), found["pack"])
    wrong_group = next(m["ref"] for m in found["pack"]["messages"] if m["group_ref"] == second_group)
    with pytest.raises(ValueError):
        evidence.group_validate(json.dumps({**response, "evidence_refs": [wrong_group]}), found["pack"])
    material = evidence.build_person_evidence(pg, pilot.settings, cm, "target_account", ["first_group"])
    pack = material["pack"]
    target = next(r["label"] for r in material["refs"] if r.get("contact_id") == str(pilot.ids["creator"]))
    peer = next(r["label"] for r in material["refs"] if r.get("contact_id") == str(other))
    own = next(m["ref"] for m in pack["messages"] if m["speaker"] == target)
    unrelated = next(m["ref"] for m in pack["messages"] if m["speaker"] == peer)
    response = {"outcome": "matched", "user_ref": target, "confidence": .99,
                "reason": "本人明确提供目标账号。", "evidence_refs": [own], "conflicts": [], "candidates": [
                    {"user_ref": target, "assessment": "target", "reason": "本人提供账号。", "evidence_refs": [own]},
                    {"user_ref": peer, "assessment": "uncertain", "reason": "不能仅凭同群判断。", "evidence_refs": []},
                ], "creator": {"state": "known", "code": "known", "confidence": 1, "refs": []},
                "link_code": "self_claim"}
    assert evidence.person_validate(json.dumps(response), pack)["outcome"] == "matched"
    wrong_person = deepcopy(response)
    wrong_person["evidence_refs"] = [unrelated]
    assert evidence.person_validate(json.dumps(wrong_person), pack)["outcome"] == "review"
    for invalid_ref in ("M999", "M29-M33"):
        with pytest.raises(ValueError):
            evidence.person_validate(json.dumps({**response, "evidence_refs": [invalid_ref]}), pack)
    with pg.transaction(force_rollback=True):
        # Preserve short answers and their explicit reply link; staff addressing alone cannot bind an account.
        question = add_message(pg, pilot, 'first_group', 'cm', 'Is target_account your TikTok account?')
        answer = add_message(pg, pilot, 'first_group', 'creator', 'Yes')
        pg.execute("UPDATE chat_messages SET message_create_time=NOW()-INTERVAL '1 minute' WHERE message_id=%s",
                   (question,))
        pg.execute('UPDATE chat_messages SET parent_id=%s WHERE message_id=%s', (question, answer))
        qa = evidence.build_person_evidence(pg, pilot.settings, cm, 'target_account', ['first_group'])
        labels = {r['message_id']: r['label'] for r in qa['refs'] if r['kind'] == 'message'}
        yes = next(m for m in qa['pack']['messages'] if m['ref'] == labels[answer])
        assert yes['text'] == 'Yes' and yes['reply_to_ref'] == labels[question] and not yes['reply_context_missing']
        assert evidence.person_validate(
            {**response, 'evidence_refs': [labels[question]]}, qa['pack'],
        )['outcome'] == 'review'
        assert evidence.person_validate(
            {**response, 'evidence_refs': [labels[question], labels[answer]], 'link_code': 'qa_confirm'}, qa['pack'],
        )['outcome'] == 'matched'
    with pg.transaction(force_rollback=True):
        sole = evidence.build_person_evidence(pg, pilot.settings, cm, "target_account", ["second_group"])
        user = next(u["ref"] for u in sole["pack"]["users"] if u["eligible"])
        group_ref = sole["pack"]["groups"][0]["ref"]
        inferred = {"outcome": "matched", "user_ref": user, "confidence": .99,
                    "reason": "已选 CAP 群的完整成员资料仅一位可用用户。", "evidence_refs": [group_ref, user],
                    "conflicts": [], "candidates": [{"user_ref": user, "assessment": "target",
                        "reason": "基于 CAP 名单和完整群成员。", "evidence_refs": [group_ref, user]}],
                    "creator": {"state": "known", "code": "known", "confidence": 1, "refs": []},
                    "link_code": "group_context"}
        assert evidence.person_validate(json.dumps(inferred), sole["pack"])["outcome"] == "review"
        pg.execute("UPDATE authorization_chats SET member_snapshot_status='partial' WHERE chat_id='second_group'")
        partial = evidence.build_person_evidence(pg, pilot.settings, cm, "target_account", ["second_group"])
        assert evidence.person_validate(json.dumps(inferred), partial["pack"])["outcome"] == "review"
    with pg.transaction(force_rollback=True):
        pg.execute("DELETE FROM contact_roles WHERE contact_id=%s", (pilot.ids["creator"],))
        unknown = evidence.build_person_evidence(pg, pilot.settings, cm, "target_account", ["first_group"])
        assert next(u for u in unknown["pack"]["users"] if u["ref"] == target)["known_category"] == "unclassified"
        pg.execute("INSERT INTO contact_roles(contact_id,role,status,source) "
                   "VALUES(%s,'creator','confirmed','manual')", (pilot.ids["creator"],))
        classified = evidence.build_person_evidence(pg, pilot.settings, cm, "target_account", ["first_group"])
        assert unknown["fingerprint"] == classified["fingerprint"]
        assert evidence.validate_references(pg, pilot.settings, cm, unknown, check_candidates=True)
        pg.execute("UPDATE contact_roles SET role='merchant_contact' WHERE contact_id=%s", (pilot.ids["creator"],))
        assert not evidence.validate_references(pg, pilot.settings, cm, unknown, check_candidates=True)
    with pg.transaction(force_rollback=True):
        referenced = next(r["message_id"] for r in material["refs"] if r["label"] == own)
        sole = evidence.build_person_evidence(pg, pilot.settings, cm, "target_account", ["second_group"])
        mixed_scope = deepcopy(material)
        mixed_scope["refs"] = [
            *[r for r in sole["refs"] if r.get("kind") == "group"],
            *[r for r in material["refs"] if r.get("kind") == "person" and r["label"] == target],
        ]

        class Meter:
            calls = 0

            def execute(self, *args, **kwargs):
                self.calls += 1
                return pg.execute(*args, **kwargs)

        meter = Meter()
        assert evidence.validate_reference_batch(
            meter, pilot.settings, cm, [material] * 50 + [sole, mixed_scope]
        ) == [True] * 51 + [False]
        assert meter.calls == 7
        pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id=%s",
                   (message_body_cipher(pilot.settings.token_encryption_key).encrypt("Changed account evidence"),
                    referenced))
        assert not evidence.validate_references(pg, pilot.settings, cm, material)
        assert evidence.validate_reference_batch(pg, pilot.settings, cm, [material, sole]) == [False, True]
    with pg.transaction(force_rollback=True):
        pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE chat_id='first_group'")
        assert evidence.validate_reference_batch(pg, pilot.settings, cm, [material, sole]) == [False, True]
        pg.execute("UPDATE authorizations SET body_consent_at=NULL WHERE id=%s", (cm,))
        assert evidence.validate_reference_batch(pg, pilot.settings, cm, [material, sole]) == [False, False]
    with pg.transaction(force_rollback=True):
        pg.execute("UPDATE chats SET name='Campaign lounge' WHERE chat_id IN ('first_group','second_group')")
        recalled = evidence.build_group_evidence(pg, pilot.settings, cm, "target_account")
        assert recalled["eligible"] and len(recalled["pack"]["groups"]) == 2
        assert all(g["match_kind"] == "history" for g in recalled["pack"]["groups"])
        assert any("target_account" in m["text"] for m in recalled["pack"]["messages"])
        assert not evidence.build_group_evidence(pg, pilot.settings, pilot.ids["owner"], "target_account")[
            "pack"]["groups"]
        chosen = recalled["pack"]["groups"][0]["ref"]
        clue = next(m["ref"] for m in recalled["pack"]["messages"]
                    if m["group_ref"] == chosen and "target_account" in m["text"])
        decision = {"outcome": "matched", "group_refs": [chosen], "confidence": .99,
                    "reason": "改名后的群仍有可访问的账号发言。", "evidence_refs": [chosen, clue]}
        assert evidence.group_validate(json.dumps(decision), recalled["pack"])["outcome"] == "matched"
        assert evidence.group_validate(json.dumps({**decision, "evidence_refs": [chosen]}),
                                       recalled["pack"])["outcome"] == "review"
        pg.execute("UPDATE chat_messages SET message_create_time=NOW()+INTERVAL '1 day' "
                   "WHERE chat_id='first_group' AND sender_open_id='ou_creator'")
        with monkeypatch.context() as scoped_patch:
            scoped_patch.setattr(evidence, "HISTORY_SEARCH_MESSAGES", 1)
            truncated = evidence.build_group_evidence(pg, pilot.settings, cm, "target_account")
            assert truncated["stats"]["history_search_truncated"]
            chosen = truncated["pack"]["groups"][0]["ref"]
            clue = next(m["ref"] for m in truncated["pack"]["messages"] if "target_account" in m["text"])
            limited = {**decision, "group_refs": [chosen], "evidence_refs": [chosen, clue]}
            assert evidence.group_validate(json.dumps(limited), truncated["pack"])["outcome"] == "review"
        pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (cm,))
        assert not evidence.build_group_evidence(pg, pilot.settings, cm, "target_account")["pack"]["groups"]
        pg.execute("UPDATE authorization_chats SET is_visible=TRUE WHERE authorization_id=%s", (cm,))
        pg.execute("UPDATE authorizations SET body_consent_at=NULL WHERE id=%s", (cm,))
        revoked = evidence.build_group_evidence(pg, pilot.settings, cm, "target_account")
        assert not revoked["eligible"] and not revoked["pack"]["groups"]
    with pg.transaction(force_rollback=True):
        pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (cm,))
        assert not evidence.validate_references(pg, pilot.settings, cm, material, check_candidates=True)
        hidden = evidence.build_group_evidence(pg, pilot.settings, cm, "target_account")["pack"]
        assert not hidden["groups"] and not hidden["users"] and not hidden["messages"]
        pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (cm,))
        assert not evidence.build_group_evidence(pg, pilot.settings, cm, "target_account")["eligible"]


def test_delegated_creator_manager_can_review_evidence_and_import_across_cms(pg, pilot, monkeypatch):
    prepare(pg, pilot, monkeypatch)
    cm, creator = pilot.ids['cm'], pilot.ids['creator']
    manager = uuid4()
    pg.execute("INSERT INTO authorizations(id,open_id,access_token_ciphertext,body_consent_at) "
               "VALUES(%s,'ou_manager','test',NOW())", (manager,))
    pg.execute("INSERT INTO admin_page_permissions(authorization_id,permission) VALUES(%s,'creator-manage')",
               (manager,))
    group(pg, pilot, 'delegated_group', 'delegated_handle', 'creator')
    add_message(pg, pilot, 'delegated_group', 'creator', 'My account is delegated_handle. I create videos.')
    batch = imports.preview_import(manager, cm, 'delegated.xlsx',
                                   workbook('delegated_handle', 'manual_handle', 'defer_handle'))
    imports.start_import(manager, batch['id'])
    imports.pause_import(manager, batch['id'])
    imports.resume_import(manager, batch['id'])
    drain(pilot)
    matched = row_for(pg, batch, 'delegated_handle')
    assert matched['review_status'] == 'confirmed' and matched['reason_code'] == 'ai_confirmed'
    imports.confirm_row(manager, matched['id'], 'delegated_handle', creator)
    assert imports.row_evidence(manager, matched['id'])['groups']
    manual = row_for(pg, batch, 'manual_handle')
    imports.confirm_row(manager, manual['id'], 'manual_handle', creator)
    imports.defer_row(manager, row_for(pg, batch, 'defer_handle')['id'])
    assert imports.search_users(manager, cm, 'creator')
    view = creator_roster.page_data(manager, cm)
    assert view['can_manage'] and view['can_select_cm'] and not view['read_only']
    assert 'evidence_rows' not in view['items'][0]
    assert view['items'][0]['profile_url'] is None
    lark_url = 'https://applink.larksuite.com/client/chat/open?openId=ou_creator'
    assert view['items'][0]['private_chat_url'] == lark_url
    admin_permissions.set_permissions(pilot.ids['owner'], manager, ['creator-manage', 'contacts'])
    assert creator_roster.page_data(manager, cm)['items'][0]['profile_url'] == '/admin/contacts/ou_creator'
    with pg.transaction(force_rollback=True):
        pg.execute("INSERT INTO admin_roles(authorization_id,role,is_active) VALUES(%s,'owner',FALSE)", (manager,))
        assert creator_roster.page_data(manager, cm)['items'][0]['profile_url'] is None
    with pg.transaction(force_rollback=True):
        pg.execute("INSERT INTO lark_identities(open_id,contact_id,display_name,last_seen_at) "
                   "VALUES('ou_creator_alias',%s,'creator',NOW()+INTERVAL '1 hour')", (creator,))
        alias = creator_roster.page_data(manager, cm)['items'][0]
        assert alias['open_id'] == 'ou_creator_alias'
        assert alias['profile_url'] == '/admin/contacts/ou_creator_alias'
        pg.execute("UPDATE lark_identities SET open_id='javascript:alert(1)' WHERE open_id='ou_creator_alias'")
        invalid = creator_roster.page_data(manager, cm)['items'][0]
        assert invalid['profile_url'] is None and invalid['private_chat_url'] is None
        pg.execute('UPDATE lark_identities SET contact_id=NULL WHERE contact_id=%s', (creator,))
        unidentified = creator_roster.page_data(manager, cm)['items'][0]
        assert unidentified['open_id'] is None and unidentified['profile_url'] is None
    admin_permissions.set_permissions(pilot.ids['owner'], manager, ['creator-manage'])
    revoked = creator_roster.page_data(manager, cm)['items'][0]
    assert revoked['profile_url'] is None and revoked['private_chat_url'] == lark_url
    old_link = creator_roster.page_data(manager, cm, tab='candidates')
    assert old_link['tab'] == 'mine' and old_link['import_batches']
    saved = pg.execute('SELECT uploaded_by,cm_authorization_id FROM creator_import_batches WHERE id=%s',
                       (batch['id'],)).fetchone()
    assert saved['uploaded_by'] == manager and saved['cm_authorization_id'] == cm
    assert not pg.execute('SELECT 1 FROM admin_roles WHERE authorization_id=%s', (manager,)).fetchone()
    pg.execute('DELETE FROM admin_page_permissions WHERE authorization_id=%s', (manager,))
    for action in (
        lambda: creator_roster.page_data(manager, cm, tab='candidates'),
        lambda: imports.row_evidence(manager, matched['id']),
        lambda: imports.confirm_row(manager, manual['id'], 'manual_handle', creator),
        lambda: imports.recheck_batch(manager, batch['id']),
    ):
        with pytest.raises(PermissionError):
            action()


def test_person_analysis_waits_for_members_and_resumes_without_repeating_group_ai(pg, pilot, monkeypatch):
    calls, callbacks = prepare(pg, pilot, monkeypatch)
    group(pg, pilot, "chat_creator", "waiting_creator", "creator")
    add_message(pg, pilot, "chat_creator", "creator", "My account is waiting_creator. I create videos.")
    pg.execute("UPDATE authorization_chats SET member_snapshot_status='pending' WHERE chat_id='chat_creator'")
    batch = imports.preview_import(pilot.ids["owner"], pilot.ids["cm"], "wait.xlsx", workbook("waiting_creator"))
    imports.start_import(pilot.ids["owner"], batch["id"])
    drain(pilot)
    row = row_for(pg, batch, "waiting_creator")
    assert row['reason_code'] == 'members_pending' and row['status'] == 'pending'
    assert not calls
    assert not imports.prepare_next_import(pilot.settings)
    pg.execute("UPDATE authorization_chats SET member_snapshot_status='succeeded' WHERE chat_id='chat_creator'")
    pg.execute("UPDATE creator_import_rows SET scheduled_at=NOW() WHERE id=%s", (row['id'],))
    drain(pilot)
    row = row_for(pg, batch, "waiting_creator")
    assert row['review_status'] == 'confirmed' and row['reason_code'] == 'ai_confirmed'
    assert [p['stage'] for p in calls] == ['person']
