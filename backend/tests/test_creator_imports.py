"""Scoped CAP group/person evidence, automatic admission and manual correction."""

import json
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
from psycopg.rows import dict_row
from test_creator_ownership import _snapshot
from test_creator_ownership import pilot as parent_pilot

from app import admin_reporting, contact_assessments, creator_handles, creator_ownership
from app import creator_imports as imports
from app.crypto import message_body_cipher

pilot = parent_pilot


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
    monkeypatch.setattr(creator_handles, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(creator_handles, "get_settings", lambda: pilot.settings)
    calls, callbacks = [], []

    def respond(_config, _key, pack, _row_id, **kwargs):
        stage = pack["stage"]
        assert kwargs["output_format"]["strict"]
        assert kwargs["output_format"]["name"] == f"creator_import_{stage}_v4"
        calls.append(pack)
        if stage == "group":
            group = next((g for g in pack["groups"] if pack["imported_handle"] in g["name"].casefold()), None)
            result = {"outcome": "matched" if group else "not_found", "group_ref": group["ref"] if group else None,
                      "confidence": .99 if group else 0, "reason": "根据群资料核对当前账号。",
                      "evidence_refs": [group["ref"]] if group else []}
        else:
            people = [u for u in pack["users"] if u["eligible"]]
            own = {u["ref"]: [m["ref"] for m in pack["messages"]
                              if m["speaker"] == u["ref"] and pack["imported_handle"] in m["text"]] for u in people}
            target = next((u for u in people if own[u["ref"]] or u.get("existing_handle_owner")), None)
            target = target or (people[0] if len(people) == 1 else None)
            refs = (own[target["ref"]] or [pack["groups"][0]["ref"], target["ref"]]) if target else []
            result = {"outcome": "matched" if target else "review", "user_ref": target["ref"] if target else None,
                      "confidence": .99 if target else .5, "reason": "结合 CAP 名单、成员和账号发言核对用户。",
                      "evidence_refs": refs, "conflicts": [], "candidates": [
                          {"user_ref": u["ref"], "assessment": "target" if u == target else "uncertain",
                           "reason": "有对应账号的定位依据。" if u == target else "尚未确定与目标账号的关系。",
                           "evidence_refs": refs if u == target else []} for u in people]}
        for callback in callbacks:
            callback(stage, pack, result)
        return {"text": json.dumps(result), "response_id": f"synthetic-{stage}-{len(calls)}",
                "usage": {"input_tokens": 12, "output_tokens": 8},
                "telemetry": {"openai_request_id": f"synthetic-request-{len(calls)}"}}

    monkeypatch.setattr(contact_assessments, "call_openai", respond)
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


def test_cap_excel_two_stage_matching_manual_confirmation_and_replays_preserve_people(pg, pilot, monkeypatch):
    calls, _ = prepare(pg, pilot, monkeypatch)
    cm, owner, creator = pilot.ids["cm"], pilot.ids["owner"], pilot.ids["creator"]
    group(pg, pilot, "chat_creator", "beauty.one_12", "creator")
    before_people = pg.execute("SELECT id,display_name FROM contacts ORDER BY id").fetchall()
    data = workbook("handle", " @Beauty.One_12 ", "beauty.one_12", "second_02", "missing", "bad handle")
    batch = imports.preview_import(owner, cm, "roster.xlsx", data)
    assert batch["counts"] == {"valid": 3, "duplicates": 1, "invalid": 1}
    saved = pg.execute("SELECT * FROM creator_import_batches WHERE id=%s", (batch["id"],)).fetchone()
    assert saved["uploaded_by"] == owner and saved["cm_authorization_id"] == cm and saved["creator_type"] == "cap"
    assert not pg.execute("SELECT 1 FROM creator_import_rows").fetchone()
    assert not calls
    assert imports.preview_import(owner, cm, "roster.xlsx", data)["id"] == batch["id"]
    for invalid_type in ("non_cap", "pending"):
        with pytest.raises(ValueError):
            imports.preview_import(owner, cm, "wrong.xlsx", data, creator_type=invalid_type)
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
    assert all(r["display_name"] == "—" and r["creator_type"] == "cap" and r["sources"] == ["Excel"]
               for r in candidates)
    first, second, missing = (row_for(pg, batch, h) for h in ("beauty.one_12", "second_02", "missing"))
    drain(pilot)
    matched = row_for(pg, batch, "beauty.one_12")
    assert matched["contact_id"] == creator and matched["review_status"] == "confirmed"
    assert matched["api_requests"] == 2 and imports.source_labels(matched) == ["Excel", "AI"]
    assert [p["stage"] for p in calls if p["imported_handle"] == "beauty.one_12"] == ["group", "person"]
    imports.confirm_row(cm, first["id"], "beauty.one_12", creator)
    imports.confirm_row(cm, first["id"], "@Beauty.One_12", creator)
    imports.confirm_row(cm, second["id"], "corrected_02", creator)
    imports.defer_row(cm, missing["id"])
    assert pg.execute("SELECT count(*) n FROM contact_assignments").fetchone()["n"] == 1
    assert pg.execute("SELECT creator_type FROM contacts WHERE id=%s", (creator,)).fetchone()["creator_type"] == "cap"
    assert pg.execute("SELECT status FROM contact_roles WHERE contact_id=%s AND role='creator'",
                      (creator,)).fetchone()["status"] == "confirmed"
    formal = creator_handles.page_data(cm, tab="mine")["items"]
    assert len(formal) == 1 and set(formal[0]["handles"]) == {"beauty.one_12", "corrected_02"}
    assert formal[0]["sources"] == ["Excel", "AI"]
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
    assert conflict["reason_code"] == "account_conflict" and conflict["contact_id"] is None
    assert set(creator_handles.page_data(cm, tab="mine")["items"][0]["handles"]) == {"beauty.one_12", "corrected_02"}
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
            assert row["review_status"] == "pending" and _snapshot(pg, facts) == unchanged
            if creator_type == "pending":
                assert row["contact_id"] == creator and row["status"] == "pending"
                assert row["stats"]["analysis_stage"] == "group"
                imports.confirm_row(cm, row["id"], "beauty.one_12", creator)
                assert pg.execute("SELECT creator_type FROM contacts WHERE id=%s", (creator,)).fetchone()[
                    "creator_type"] == "cap"
            else:
                assert row["contact_id"] is None and row["reason_code"] == "account_conflict"


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
    with pytest.raises(PermissionError):
        imports.confirm_row(cm, row["id"], "creator_one", pilot.ids["silent"])
    with pytest.raises(ValueError):
        imports.confirm_row(cm, row["id"], "creator_one", uuid4())
    with pytest.raises(ValueError):
        imports.confirm_row(cm, row["id"], "occupied", creator)
    for conflict in ("owner", "account", "type", "merchant", "rejected"):
        with pg.transaction(force_rollback=True):
            if conflict == "owner":
                pg.execute("INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) "
                           "VALUES(%s,%s,%s,'manual')", (uuid4(), creator, owner))
            elif conflict == "type":
                pg.execute("UPDATE contacts SET creator_type='non_cap' WHERE id=%s", (creator,))
            elif conflict == "merchant":
                pg.execute("INSERT INTO contact_roles(contact_id,role,status,source) "
                           "VALUES(%s,'merchant_contact','confirmed','manual')", (creator,))
            elif conflict == "rejected":
                pg.execute("UPDATE contact_roles SET status='rejected',source='manual' "
                           "WHERE contact_id=%s AND role='creator'", (creator,))
            with pytest.raises(ValueError, match="冲突"):
                imports.confirm_row(
                    cm, row["id"], "legacy_account" if conflict == "account" else "creator_one", creator,
                )
            assert not pg.execute("SELECT 1 FROM contact_social_accounts WHERE import_row_id=%s",
                                  (row["id"],)).fetchone()
    pg.execute("UPDATE creator_import_rows SET contact_id=%s,pack_ciphertext=%s WHERE id=%s",
               (creator, creator_ownership._cipher(pilot.settings).encrypt("{}"), row["id"]))
    imports.confirm_row(cm, row["id"], "corrected_handle", creator)
    assert row_for(pg, batch, "corrected_handle")["review_status"] == "confirmed"
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


def test_manual_import_concurrent_confirmations_create_one_person_and_account_link(pg, pilot, monkeypatch):
    prepare(pg, pilot, monkeypatch)
    cm, creator = pilot.ids["cm"], pilot.ids["creator"]
    batches = [imports.preview_import(actor, cm, name, workbook("same_account"))
               for actor, name in ((cm, "one.xlsx"), (pilot.ids["owner"], "two.xlsx"))]
    for batch in batches:
        imports.start_import(cm, batch["id"])
    row_ids = [row_for(pg, batch, "same_account")["id"] for batch in batches]
    schema, dsn = pg.execute("SELECT current_schema() name").fetchone()["name"], pg.info.dsn
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
        assert (row["api_requests"], row["input_tokens"], row["output_tokens"]) == (2, 24, 16)
        assert row["stats"]["analysis_stage"] == "completed"
        chain = json.loads(creator_ownership._cipher(pilot.settings).decrypt(row["pack_ciphertext"]))
        judgments = json.loads(creator_ownership._cipher(pilot.settings).decrypt(row["result_ciphertext"]))
        assert set(chain) >= {"group", "person"} and set(judgments) >= {"group", "person"}
        assert all(chain[phase]["pack"]["imported_handle"] == handle and chain[phase]["refs"]
                   and chain[phase]["fingerprint"] for phase in ("group", "person"))
        assert len([u for u in chain["person"]["pack"]["users"] if u["eligible"]]) == 2
        assert judgments["group"]["group_ref"] and judgments["person"]["user_ref"]
        assert imports.source_labels(row) == ["Excel", "AI"]
        formal = next(item for item in creator_handles.page_data(cm, tab="mine")["items"]
                      if item["contact_id"] == str(contact))
        assert formal["sources"] == ["Excel", "AI"]
        assert {"id": str(row["id"]), "handle": handle} in formal["evidence_rows"]
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


def test_silent_unique_person_still_needs_both_ai_thresholds_and_recheck_keeps_usage(pg, pilot, monkeypatch):
    calls, callbacks = prepare(pg, pilot, monkeypatch)
    cm = pilot.ids["cm"]
    group(pg, pilot, "chat_creator", "silent_handle", "creator")
    pg.execute("UPDATE chat_messages SET body_ciphertext=NULL")
    lower = ["person"]

    def low_confidence(stage, _pack, result):
        if stage == lower[0]:
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
    assert first["review_status"] == "pending" and first["contact_id"] is None and first["api_requests"] == 2
    assert [p["stage"] for p in calls] == ["group", "person"]
    assert not calls[-1]["messages"] and any(u["eligible"] for u in calls[-1]["users"])
    lower[0] = "group"
    imports.recheck_batch(cm, batch["id"])
    drain(pilot)
    second = row_for(pg, batch, "silent_handle")
    assert second["id"] == first["id"] and second["api_requests"] == 3
    assert second["review_status"] == "pending" and second["contact_id"] is None
    callbacks.clear()
    imports.recheck_batch(cm, batch["id"])
    drain(pilot)
    final = row_for(pg, batch, "silent_handle")
    assert final["review_status"] == "confirmed" and final["contact_id"] == pilot.ids["creator"]
    assert (final["api_requests"], final["input_tokens"], final["output_tokens"]) == (5, 60, 40)
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


def test_two_person_stages_for_one_creator_do_not_drop_paid_results_after_classification(pg, pilot, monkeypatch):
    calls, callbacks = prepare(pg, pilot, monkeypatch)
    cm = pilot.ids["cm"]
    pg.execute("DELETE FROM contact_roles WHERE contact_id=%s", (pilot.ids["creator"],))
    group(pg, pilot, "chat_creator", "first_handle + second_handle", "creator")
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

    callbacks.append(finish_other)
    imports.run_claimed_import(people[0], pilot.settings)
    drain(pilot)
    for handle in ("first_handle", "second_handle"):
        row = row_for(pg, batch, handle)
        assert row["review_status"] == "confirmed" and row["contact_id"] == pilot.ids["creator"]
        assert (row["api_requests"], row["input_tokens"], row["output_tokens"]) == (2, 24, 16)
    assert len(calls) == 4 and pg.execute("SELECT count(*) n FROM contact_assignments").fetchone()["n"] == 1
    settled = _snapshot(pg, ("creator_import_rows", "contact_assignments", "contact_social_accounts"))
    results = imports._decode(row_for(pg, batch, people[0]["handle"]), pilot.settings, "result_ciphertext")
    imports._finish(people[0], pilot.settings, response=results["person"]["response"], result=results["person"])
    imports.run_claimed_import(people[0], pilot.settings)
    assert _snapshot(pg, settled) == settled and len(calls) == 4


@pytest.mark.parametrize("change", ["body", "scope", "manual"])
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
        else:
            imports.confirm_row(cm, row_id, "corrected_handle", pilot.ids["creator"])

    callbacks.append(change_during_person)
    drain(pilot)
    row = pg.execute("SELECT * FROM creator_import_rows WHERE id=%s", (row_id,)).fetchone()
    assert (row["api_requests"], row["input_tokens"], row["output_tokens"]) == (2, 24, 16)
    assert len(calls) == 2
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
            assert page["total"] == 1 and page["candidates"][0]["creator_type"] == "cap"

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
    assert candidate["display_name"] == "—" and candidate["creator_type"] == "cap"
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


def test_creator_library_reset_preserves_sources_history_and_new_data_on_replay(pg, pilot, monkeypatch):
    prepare(pg, pilot, monkeypatch)
    monkeypatch.setattr(contact_assessments, "connection", lambda: nullcontext(pg))
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
                   merchant_assessment, merchant, contact_assessments.POLICY_VERSION,
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
               "VALUES(%s,%s,%s,%s,'running')", (ownership_run, cm, owner, creator_ownership.POLICY_VERSION))
    pg.execute("INSERT INTO creator_ownership_tasks(id,run_id,contact_id,cm_authorization_id,policy_version,"
               "status,api_requests,input_tokens,output_tokens,result_ciphertext,lease_until) "
               "VALUES(%s,%s,%s,%s,%s,'running',1,21,9,'historical-ownership',NOW()+INTERVAL '1 hour')",
               (ownership_task, ownership_run, creator, cm, creator_ownership.POLICY_VERSION))
    pg.execute("INSERT INTO creator_handle_runs(id,ownership_run_id,cm_authorization_id,requested_by,"
               "policy_version,status,auto_admit) VALUES(%s,%s,%s,%s,%s,'running',TRUE)",
               (handle_run, ownership_run, cm, owner, creator_handles.POLICY_VERSION))
    pg.execute("INSERT INTO creator_handle_tasks(id,run_id,ownership_task_id,contact_id,cm_authorization_id,"
               "policy_version,status,api_requests,input_tokens,output_tokens,result_ciphertext,lease_until) "
               "VALUES(%s,%s,%s,%s,%s,%s,'running',1,22,8,'historical-handle',NOW()+INTERVAL '1 hour')",
               (handle_task, handle_run, ownership_task, creator, cm, creator_handles.POLICY_VERSION))
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
    assert creator_handles.page_data(cm, tab="mine")["total_count"] == 1
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
        assert creator_handles.page_data(actor, tab="mine")["total_count"] == 0
        assert creator_handles.page_data(actor, tab="candidates")["total_count"] == 0
        assert imports.page_data(actor, actor)["total"] == 0
    for table in ("creator_ownership_runs", "creator_ownership_tasks", "creator_handle_runs", "creator_handle_tasks",
                  "creator_import_batches"):
        assert not pg.execute(f"SELECT 1 FROM {table} WHERE policy_version NOT LIKE %s",
                              ("%:reset-20260914",)).fetchone()
    for assessment in pg.execute("SELECT id,policy_version FROM contact_assessments").fetchall():
        assert assessment["policy_version"] == contact_assessments.POLICY_VERSION + (
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
    assert not creator_ownership.prepare_next_preview(pilot.settings) and not creator_ownership.claim_next_ownership()
    assert not creator_handles.prepare_next_preview(pilot.settings) and not creator_handles.claim_next_handle()
    assert not contact_assessments.queue_eligible_contact_assessments()
    assert not contact_assessments.claim_next_contact_assessment()
    assert row_for(pg, queued, "new_handle")["status"] == "invalid"
    with pytest.raises(ValueError):
        imports.start_import(cm, batch["id"])
    new_batch = imports.preview_import(owner, cm, "original.xlsx", data)
    assert new_batch["id"] != batch["id"]
    imports.start_import(owner, new_batch["id"])
    imports.confirm_row(cm, row_for(pg, new_batch, "creator_one")["id"], "creator_one", creator)
    pg.execute(migration)
    assert creator_handles.page_data(cm, tab="mine")["total_count"] == 1
    assert row_for(pg, new_batch, "creator_one")["review_status"] == "confirmed"
    assert pg.execute("SELECT creator_type FROM contacts WHERE id=%s", (creator,)).fetchone()["creator_type"] == "cap"
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
    response = {"outcome": "matched", "group_ref": first_group, "confidence": .99,
                "reason": "群名和原文包含目标账号。", "evidence_refs": [first_group]}
    assert evidence.group_validate(json.dumps(response), found["pack"])["outcome"] == "matched"
    forged = {**response, "group_ref": "G999"}
    with pytest.raises(ValueError):
        evidence.group_validate(json.dumps(forged), found["pack"])
    wrong_group = next(m["ref"] for m in found["pack"]["messages"] if m["group_ref"] == second_group)
    with pytest.raises(ValueError):
        evidence.group_validate(json.dumps({**response, "evidence_refs": [wrong_group]}), found["pack"])
    material = evidence.build_person_evidence(pg, pilot.settings, cm, "target_account", "first_group")
    pack = material["pack"]
    target = next(r["label"] for r in material["refs"] if r.get("contact_id") == str(pilot.ids["creator"]))
    peer = next(r["label"] for r in material["refs"] if r.get("contact_id") == str(other))
    own = next(m["ref"] for m in pack["messages"] if m["speaker"] == target)
    unrelated = next(m["ref"] for m in pack["messages"] if m["speaker"] == peer)
    response = {"outcome": "matched", "user_ref": target, "confidence": .99,
                "reason": "本人明确提供目标账号。", "evidence_refs": [own], "conflicts": [], "candidates": [
                    {"user_ref": target, "assessment": "target", "reason": "本人提供账号。", "evidence_refs": [own]},
                    {"user_ref": peer, "assessment": "uncertain", "reason": "不能仅凭同群判断。", "evidence_refs": []},
                ]}
    assert evidence.person_validate(json.dumps(response), pack)["outcome"] == "matched"
    wrong_person = deepcopy(response)
    wrong_person["evidence_refs"] = [unrelated]
    assert evidence.person_validate(json.dumps(wrong_person), pack)["outcome"] == "review"
    for invalid_ref in ("M999", "M29-M33"):
        with pytest.raises(ValueError):
            evidence.person_validate(json.dumps({**response, "evidence_refs": [invalid_ref]}), pack)
    with pg.transaction(force_rollback=True):
        sole = evidence.build_person_evidence(pg, pilot.settings, cm, "target_account", "second_group")
        user = next(u["ref"] for u in sole["pack"]["users"] if u["eligible"])
        group_ref = sole["pack"]["groups"][0]["ref"]
        inferred = {"outcome": "matched", "user_ref": user, "confidence": .99,
                    "reason": "已选 CAP 群的完整成员资料仅一位可用用户。", "evidence_refs": [group_ref, user],
                    "conflicts": [], "candidates": [{"user_ref": user, "assessment": "target",
                        "reason": "基于 CAP 名单和完整群成员。", "evidence_refs": [group_ref, user]}]}
        assert evidence.person_validate(json.dumps(inferred), sole["pack"])["outcome"] == "matched"
        pg.execute("UPDATE authorization_chats SET member_snapshot_status='partial' WHERE chat_id='second_group'")
        partial = evidence.build_person_evidence(pg, pilot.settings, cm, "target_account", "second_group")
        assert evidence.person_validate(json.dumps(inferred), partial["pack"])["outcome"] == "review"
    with pg.transaction(force_rollback=True):
        pg.execute("DELETE FROM contact_roles WHERE contact_id=%s", (pilot.ids["creator"],))
        unknown = evidence.build_person_evidence(pg, pilot.settings, cm, "target_account", "first_group")
        assert next(u for u in unknown["pack"]["users"] if u["ref"] == target)["known_category"] == "unclassified"
        pg.execute("INSERT INTO contact_roles(contact_id,role,status,source) "
                   "VALUES(%s,'creator','confirmed','manual')", (pilot.ids["creator"],))
        classified = evidence.build_person_evidence(pg, pilot.settings, cm, "target_account", "first_group")
        assert unknown["fingerprint"] == classified["fingerprint"]
        assert evidence.validate_evidence(pg, pilot.settings, cm, "target_account", unknown["pack"],
                                          unknown["refs"], unknown["fingerprint"])
        pg.execute("UPDATE contact_roles SET role='merchant_contact' WHERE contact_id=%s", (pilot.ids["creator"],))
        assert not evidence.validate_evidence(pg, pilot.settings, cm, "target_account", unknown["pack"],
                                              unknown["refs"], unknown["fingerprint"])
    with pg.transaction(force_rollback=True):
        referenced = next(r["message_id"] for r in material["refs"] if r["label"] == own)
        pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id=%s",
                   (message_body_cipher(pilot.settings.token_encryption_key).encrypt("Changed account evidence"),
                    referenced))
        assert not evidence.validate_references(pg, pilot.settings, cm, material)
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
        decision = {"outcome": "matched", "group_ref": chosen, "confidence": .99,
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
            limited = {**decision, "group_ref": chosen, "evidence_refs": [chosen, clue]}
            assert evidence.group_validate(json.dumps(limited), truncated["pack"])["outcome"] == "review"
        pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (cm,))
        assert not evidence.build_group_evidence(pg, pilot.settings, cm, "target_account")["pack"]["groups"]
        pg.execute("UPDATE authorization_chats SET is_visible=TRUE WHERE authorization_id=%s", (cm,))
        pg.execute("UPDATE authorizations SET body_consent_at=NULL WHERE id=%s", (cm,))
        revoked = evidence.build_group_evidence(pg, pilot.settings, cm, "target_account")
        assert not revoked["eligible"] and not revoked["pack"]["groups"]
    with pg.transaction(force_rollback=True):
        pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (cm,))
        assert not evidence.validate_evidence(pg, pilot.settings, cm, "target_account", material["pack"],
                                              material["refs"], material["fingerprint"])
        hidden = evidence.build_group_evidence(pg, pilot.settings, cm, "target_account")["pack"]
        assert not hidden["groups"] and not hidden["users"] and not hidden["messages"]
        pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (cm,))
        assert not evidence.build_group_evidence(pg, pilot.settings, cm, "target_account")["eligible"]
