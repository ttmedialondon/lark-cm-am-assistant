"""CAP Excel candidates and scoped manual confirmation, with no matching or paid work."""

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


def prepare(pg, pilot, monkeypatch):
    monkeypatch.setattr(imports, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(creator_handles, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(creator_handles, "get_settings", lambda: pilot.settings)
    monkeypatch.setattr(contact_assessments, "call_openai",
                        lambda *_a, **_kw: pytest.fail("Excel import and manual confirmation must not call AI"))


def row_for(pg, batch, handle):
    return pg.execute(
        "SELECT * FROM creator_import_rows WHERE batch_id=%s AND handle=%s", (batch["id"], handle)
    ).fetchone()


def test_cap_excel_preview_start_manual_confirmation_and_replays_preserve_people(pg, pilot, monkeypatch):
    prepare(pg, pilot, monkeypatch)
    cm, owner, creator = pilot.ids["cm"], pilot.ids["owner"], pilot.ids["creator"]
    group(pg, pilot, "chat_creator", "beauty.one_12", "creator")
    before_people = pg.execute("SELECT id,display_name FROM contacts ORDER BY id").fetchall()
    data = workbook("handle", " @Beauty.One_12 ", "beauty.one_12", "second_02", "missing", "bad handle")
    batch = imports.preview_import(owner, cm, "roster.xlsx", data)
    assert batch["counts"] == {"valid": 3, "duplicates": 1, "invalid": 1}
    saved = pg.execute("SELECT * FROM creator_import_batches WHERE id=%s", (batch["id"],)).fetchone()
    assert saved["uploaded_by"] == owner and saved["cm_authorization_id"] == cm and saved["creator_type"] == "cap"
    assert not pg.execute("SELECT 1 FROM creator_import_rows").fetchone()
    assert imports.preview_import(owner, cm, "roster.xlsx", data)["id"] == batch["id"]
    for invalid_type in ("non_cap", "pending"):
        with pytest.raises(ValueError):
            imports.preview_import(owner, cm, "wrong.xlsx", data, creator_type=invalid_type)
    imports.start_import(owner, batch["id"])
    imports.start_import(cm, batch["id"])
    rows = pg.execute("SELECT * FROM creator_import_rows ORDER BY handle").fetchall()
    assert len(rows) == 3 and all(r["contact_id"] is None and r["review_status"] == "pending" for r in rows)
    assert all(r["api_requests"] == 0 and not r["pack_ciphertext"] and r["refs"] == [] for r in rows)
    assert all(r["status"] == "skipped" and r["reason_code"] == "manual_review" for r in rows)
    assert pg.execute("SELECT status FROM creator_import_batches WHERE id=%s", (batch["id"],)).fetchone()[
        "status"] == "completed"
    candidates = imports.page_data(cm, cm)["candidates"]
    assert len(candidates) == 3
    assert all(r["display_name"] == "—" and r["creator_type"] == "cap" and r["sources"] == ["Excel"]
               for r in candidates)
    first, second, missing = (row_for(pg, batch, h) for h in ("beauty.one_12", "second_02", "missing"))
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
    assert formal[0]["sources"] == ["Excel"]
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
    assert not pg.execute("SELECT 1 FROM creator_import_rows WHERE api_requests<>0").fetchone()
    for creator_type in ("pending", "non_cap"):
        with pg.transaction(force_rollback=True):
            pg.execute("UPDATE contacts SET creator_type=%s WHERE id=%s", (creator_type, creator))
            unchanged = _snapshot(pg, facts)
            review = imports.preview_import(owner, cm, "review.xlsx", workbook("beauty.one_12"))
            imports.start_import(owner, review["id"])
            row = row_for(pg, review, "beauty.one_12")
            assert row["review_status"] == "pending" and _snapshot(pg, facts) == unchanged
            if creator_type == "pending":
                assert row["contact_id"] == creator and row["reason_code"] == "manual_review"
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
    imports.confirm_row(cm, row["id"], "corrected_handle", creator)
    assert row_for(pg, batch, "corrected_handle")["review_status"] == "confirmed"
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
