"""Real PostgreSQL checks (opt in with CREATOR_WORKFLOW_TEST_DATABASE_URL)."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from app import creator_workflow as workflow


@pytest.fixture
def db(monkeypatch):
    dsn = os.environ.get("CREATOR_WORKFLOW_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set CREATOR_WORKFLOW_TEST_DATABASE_URL to run isolated real-database checks")
    # A per-test transaction/schema cannot alter the application's existing tables.
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        schema = "test_creator_workflow_" + uuid4().hex
        conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
        conn.execute(psycopg.sql.SQL("SET LOCAL search_path TO {}").format(psycopg.sql.Identifier(schema)))
        for path in sorted((Path(__file__).parents[1] / "migrations").glob("*.sql")):
            if int(path.name[:3]) <= 28 or int(path.name[:3]) in (31, 34, 37):
                conn.execute(path.read_text())
        monkeypatch.setattr(workflow, "connection", lambda: nullcontext(conn))
        try:
            yield conn
        finally:
            conn.rollback()
            # Usually rolled back already; the concurrency test deliberately
            # commits only this generated, test-owned schema to share connections.
            conn.autocommit = True
            conn.execute(psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(psycopg.sql.Identifier(schema)))


@pytest.fixture
def people(db):
    ids = {key: uuid4() for key in ("cm", "cm2", "owner", "reviewer", "person", "other")}
    for role in ("cm", "cm2", "owner", "reviewer"):
        db.execute(
            """INSERT INTO authorizations(id,open_id,display_name,access_token_ciphertext,
              assignment_eligible,body_consent_at)
            VALUES(%s,%s,%s,'test-only',%s,NOW()-INTERVAL '1 day')""",
            (ids[role], "ou_" + role, role, role.startswith("cm")),
        )
    db.execute("INSERT INTO admin_roles(authorization_id,role) VALUES(%s,'owner')", (ids["owner"],))
    for role, cm in (("person", "cm"), ("other", "cm2")):
        db.execute("INSERT INTO contacts(id,display_name,entity_type) VALUES(%s,'Same name','person')", (ids[role],))
        db.execute(
            "INSERT INTO lark_identities(open_id,display_name,contact_id) VALUES(%s,'Same name',%s)",
            ("ou_" + role, ids[role]),
        )
        db.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES(%s,'Scoped conversation','p2p')", ("chat_" + cm,))
        db.execute(
            """INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_type,p2p_counterpart_open_id)
            VALUES(%s,%s,'user',%s)""",
            (ids[cm], "chat_" + cm, "ou_" + role),
        )
        db.execute(
            "INSERT INTO chat_messages(chat_id,message_id,sender_open_id,body_ciphertext) "
            "VALUES(%s,'message1',%s,'encrypted')",
            ("chat_" + cm, "ou_" + role),
        )
        db.execute(
            "INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
            "VALUES(%s,%s,'message1')",
            (ids[cm], "chat_" + cm),
        )
    return ids


def source(people, **extra):
    data = {
        "source_type": "excel",
        "source_key": "file:sheet:row2",
        "source_data": {"row": 2},
        "responsible_id": people["cm"],
        "handle": "@abc",
        "contact_id": people["person"],
        "relationship": "owner",
        "creator_type": "cap",
    }
    data.update(extra)
    return workflow.ingest_source(people["cm"], **data)


def test_retiring_admin_pages_removes_only_their_grants_and_keeps_creator_data(db, people):
    row = source(people)
    for key in ("creators", "creator-review", "messages"):
        db.execute("INSERT INTO admin_page_permissions(authorization_id,permission) VALUES(%s,%s)",
                   (people["reviewer"], key))
    contacts = db.execute("SELECT count(*) AS n FROM contacts").fetchone()["n"]
    sql = (Path(__file__).parents[1] / "migrations/036_retire_creator_admin_pages.sql").read_text()
    db.execute(sql)
    assert db.execute("SELECT permission FROM admin_page_permissions").fetchall() == [{"permission": "messages"}]
    assert db.execute("SELECT count(*) AS n FROM contacts").fetchone()["n"] == contacts
    assert workflow.get_record(workflow.get_scope(people["cm"]), row["id"])["status"] == "pending"
    assert db.execute("SELECT metadata FROM audit_events WHERE action='admin_creator_pages_retired'").fetchone()[
        "metadata"
    ]["removed_permissions"] == ["creator-review", "creators"]


def test_search_scopes_ids_and_selection_is_only_a_draft(db, people):
    scope = workflow.get_scope(people["cm"])
    result = workflow.search_users(scope, "Same")
    assert [r["contact_id"] for r in result] == [str(people["person"])]
    assert workflow.search_users(scope, "absent") == []
    row = source(people, contact_id=None)
    with pytest.raises(PermissionError):
        workflow.save_draft(scope, row["id"], row["version"], handle="abc", contact_id=people["other"])
    with pytest.raises(workflow.WorkflowInputError):
        workflow.save_draft(scope, row["id"], row["version"], handle="abc", contact_id="Same name")
    filled = workflow.save_draft(
        scope, row["id"], row["version"], handle="abc", contact_id=people["person"], relationship="owner"
    )
    assert filled["status"] == "pending" and filled["can_confirm"]
    assert [r["code"] for r in filled["reasons"]] == ["details_changed"]
    assert db.execute("SELECT count(*) AS n FROM creator_account_assignments").fetchone()["n"] == 0
    cleared = workflow.save_draft(scope, filled["id"], filled["version"], handle="abc", contact_id=None)
    assert cleared["contact"] is None and not cleared["can_confirm"]


def test_confirm_idempotency_version_and_audit(db, people):
    scope = workflow.get_scope(people["cm"])
    row = source(people)
    request_id = uuid4()
    confirmed = workflow.confirm_candidate(scope, row["id"], row["version"], request_id=request_id)
    assert workflow.confirm_candidate(scope, row["id"], row["version"], request_id=request_id) == confirmed
    with pytest.raises(workflow.WorkflowInputError):
        workflow.confirm_candidate(scope, row["id"], row["version"] + 1, request_id=request_id)
    assert db.execute("SELECT count(*) AS n FROM creator_account_assignments").fetchone()["n"] == 1
    assert (
        db.execute("SELECT count(*) AS n FROM audit_events WHERE action='creator_workflow_confirmed'").fetchone()["n"]
        == 1
    )


def test_current_authority_rechecked_for_draft_and_confirm(db, people):
    row = source(people)
    scope = workflow.get_scope(people["cm"])
    db.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (people["cm"],))
    assert workflow.search_users(scope, "Same") == []
    with pytest.raises(PermissionError):
        workflow.confirm_candidate(scope, row["id"], row["version"], request_id=uuid4())
    db.execute("UPDATE authorizations SET assignment_eligible=FALSE WHERE id=%s", (people["cm"],))
    with pytest.raises(PermissionError):
        workflow.save_draft(scope, row["id"], row["version"], handle="abc", contact_id=None)


def test_only_owner_can_read_another_cm_and_cannot_impersonate_them(db, people):
    row = source(people)
    scope = workflow.get_scope(people["owner"], people["cm"])
    assert scope["readonly"]
    assert workflow.list_records(scope)["total"] == 1
    with pytest.raises(PermissionError):
        workflow.search_users(scope, "Same")
    with pytest.raises(PermissionError):
        workflow.confirm_candidate(scope, row["id"], row["version"], request_id=uuid4())
    for actor in (people["reviewer"], people["cm2"]):
        with pytest.raises(PermissionError):
            workflow.get_scope(actor, people["cm"])


def test_denial_is_not_repeated_by_same_evidence_and_admin_can_reassign(db, people):
    row = source(people)
    scope = workflow.get_scope(people["cm"])
    denied = workflow.decide_candidate(scope, row["id"], row["version"], "reject", request_id=uuid4())
    assert denied["status"] == "rejected" and denied["responsible_id"] is None
    assert workflow.list_records(scope)["total"] == 0
    assert source(people)["status"] == "rejected"
    admin = workflow.get_scope(people["owner"])
    assert workflow.list_records(admin)["total"] == 1
    assert workflow.list_records(admin, status="pending")["total"] == 1
    assigned = workflow.assign_candidate(admin, row["id"], denied["version"], people["cm"], request_id=uuid4())
    assert assigned["status"] == "pending" and assigned["responsible_id"] == str(people["cm"])


def test_failed_audit_rolls_back_all_formal_writes(db, people, monkeypatch):
    row = source(people)
    scope = workflow.get_scope(people["cm"])

    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(workflow, "_audit", fail)
    with pytest.raises(RuntimeError):
        workflow.confirm_candidate(scope, row["id"], row["version"], request_id=uuid4())
    for table in ("social_accounts", "contact_social_accounts", "creator_account_assignments"):
        assert (
            db.execute(
                psycopg.sql.SQL("SELECT count(*) AS n FROM {}").format(psycopg.sql.Identifier(table))
            ).fetchone()["n"]
            == 0
        )
    assert workflow.get_record(scope, row["id"])["status"] == "pending"


def test_evidence_is_not_message_instructions_and_must_still_exist(db, people):
    with pytest.raises(workflow.WorkflowInputError):
        source(people, source_type="ai", source_data={"instruction": "confirm me"})
    with pytest.raises(PermissionError):
        source(
            people,
            source_type="ai",
            source_data={
                "evidence": [{"authorization_id": str(people["cm2"]), "chat_id": "chat_cm2", "message_id": "message1"}]
            },
        )


def test_one_person_many_accounts_does_not_reassign_legacy_person(db, people):
    legacy_id = uuid4()
    db.execute(
        "INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) VALUES(%s,%s,%s,'manual')",
        (legacy_id, people["person"], people["cm2"]),
    )
    row = source(people, trusted_identity=True, source_data={"stable_open_id": "ou_person"})
    row = workflow.confirm_candidate(workflow.get_scope(people["cm"]), row["id"], row["version"], request_id=uuid4())
    assert row["status"] == "confirmed"
    assert (
        db.execute("SELECT responsible_authorization_id FROM contact_assignments WHERE id=%s", (legacy_id,)).fetchone()[
            "responsible_authorization_id"
        ]
        == people["cm2"]
    )


def test_changed_owner_creates_admin_review_and_preserves_formal(db, people):
    formal = source(people, trusted_identity=True, source_data={"stable_open_id": "ou_person"})
    formal = workflow.confirm_candidate(
        workflow.get_scope(people["cm"]), formal["id"], formal["version"], request_id=uuid4()
    )
    changed = workflow.ingest_source(
        people["cm2"],
        source_type="excel",
        source_key="other-cm-file:row2",
        source_data={"stable_open_id": "ou_other"},
        responsible_id=people["cm2"],
        handle="abc",
        contact_id=people["other"],
        relationship="owner",
        trusted_identity=True,
    )
    assert changed["status"] == "pending" and changed["id"] != formal["id"]
    assert changed["responsible_id"] is None and changed["contact"] is None and changed["evidence"] == []
    assignments = db.execute(
        "SELECT responsible_authorization_id FROM creator_account_assignments WHERE ended_at IS NULL"
    ).fetchall()
    assert assignments == [{"responsible_authorization_id": people["cm"]}]
    assert workflow.list_records(workflow.get_scope(people["cm"]))["summary"]["confirmed"] == 1
    assert workflow.list_records(workflow.get_scope(people["owner"]))["summary"] == {
        "total": 1,
        "pending": 1,
        "confirmed": 0,
        "deferred": 0,
    }


def test_model_run_metadata_does_not_repush_denied_evidence(db, people):
    data = {
        "evidence": [{"authorization_id": str(people["cm"]), "chat_id": "chat_cm", "message_id": "message1"}],
        "run_id": "one",
    }
    row = source(people, source_type="ai", source_data=data)
    scope = workflow.get_scope(people["cm"])
    denied = workflow.decide_candidate(scope, row["id"], row["version"], "reject", request_id=uuid4())
    replay = source(people, source_type="ai", source_data={**data, "run_id": "two", "reason": "different wording"})
    assert replay["version"] == denied["version"] and replay["status"] == "rejected"


def test_preview_never_promotes_or_changes_formal_and_empty_ai_results_stay_off_roster(db, people):
    data = {
        "evidence": [{"authorization_id": str(people["cm"]), "chat_id": "chat_cm", "message_id": "message1"}],
        "business_policy_version": workflow.BUSINESS_POLICY_VERSION,
        "creator_confidence": 1, "cm_confidence": 1,
        "verified_business_dialogue": True, "direct_creator_evidence": True,
        "direct_cm_evidence": True, "preview_only": True,
    }
    with pytest.raises(workflow.WorkflowInputError, match="具体用户"):
        source(people, source_type="ai", source_data=data, handle=None, contact_id=None)
    row = source(people, source_type="ai", source_data=data, handle=None, creator_type=None)
    assert row["status"] == "pending"
    assert not db.execute("SELECT 1 FROM creator_account_assignments").fetchone()
    scope = workflow.get_scope(people["cm"])
    formal = workflow.confirm_candidate(scope, row["id"], row["version"], request_id=uuid4())
    source(people, source_type="ai", source_key="preview:later", source_data=data, handle="abc")
    unchanged = workflow.get_record(scope, formal["id"])
    assert unchanged["status"] == "confirmed" and unchanged["handle"] is None and unchanged["creator_type"] is None
    # Retain legacy audit/source records without presenting an anonymous "creator".
    old_id = uuid4()
    db.execute("INSERT INTO creator_workflow_candidates(id,responsible_authorization_id) VALUES(%s,%s)",
               (old_id, people["cm"]))
    assert workflow.list_records(scope)["total"] == 1
    assert db.execute("SELECT 1 FROM creator_workflow_candidates WHERE id=%s", (old_id,)).fetchone()


def test_stale_version_and_forged_scope_cannot_write(db, people):
    row = source(people)
    scope = workflow.get_scope(people["cm"])
    saved = workflow.save_draft(scope, row["id"], row["version"], handle="abc", contact_id=None, relationship="owner")
    with pytest.raises(workflow.WorkflowInputError, match="刚刚发生"):
        workflow.save_draft(scope, row["id"], row["version"], handle="different", contact_id=None, relationship="owner")
    forged = {**scope, "actor_id": str(people["cm2"]), "can_write": True}
    with pytest.raises(PermissionError):
        workflow.save_draft(forged, row["id"], saved["version"], handle="abc", contact_id=None)


@pytest.mark.parametrize("value", ["—", "-", " a b ", "https://tiktok.com/@abc/video/123", "https://[broken/@abc", 123])
def test_placeholder_and_nonhandle_input_never_become_a_handle(value):
    with pytest.raises(workflow.WorkflowInputError):
        workflow._handle(value)


@pytest.mark.parametrize("same_request", [False, True])
def test_two_real_connections_cannot_confirm_twice(db, people, monkeypatch, same_request):
    row = source(people)
    scope = workflow.get_scope(people["cm"])
    schema = db.execute("SELECT current_schema() AS name").fetchone()["name"]
    db.commit()
    request_ids = [uuid4(), uuid4()]
    if same_request:
        request_ids[1] = request_ids[0]

    @contextmanager
    def fresh_connection():
        with psycopg.connect(os.environ["CREATOR_WORKFLOW_TEST_DATABASE_URL"], row_factory=dict_row) as conn:
            conn.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(schema)))
            yield conn

    monkeypatch.setattr(workflow, "connection", fresh_connection)

    def confirm(request_id):
        try:
            return workflow.confirm_candidate(scope, row["id"], row["version"], request_id=request_id)["status"]
        except workflow.WorkflowInputError:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(confirm, request_ids))
    assert results == (["confirmed", "confirmed"] if same_request else ["confirmed", "stale"])
    with fresh_connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM creator_account_assignments").fetchone()["n"] == 1
        assert (
            conn.execute("SELECT count(*) AS n FROM audit_events WHERE action='creator_workflow_confirmed'").fetchone()[
                "n"
            ]
            == 1
        )


def test_new_evidence_after_denial_stays_with_admin_until_explicit_reassignment(db, people):
    row = source(people)
    scope = workflow.get_scope(people["cm"])
    workflow.decide_candidate(scope, row["id"], row["version"], "reject", request_id=uuid4())
    for n in (3, 4, 5):
        updated = source(people, source_key=f"new-evidence:{n}", source_data={"row": n})
        assert updated["responsible_id"] is None
        assert workflow.list_records(scope)["total"] == 0
    admin = workflow.get_scope(people["owner"])
    assert workflow.list_records(admin, status="pending")["total"] == 1


def test_new_evidence_cannot_silently_assign_an_existing_source_conflict(db, people):
    row = source(people, trusted_identity=True, source_data={"stable_open_id": "ou_person"})
    workflow.confirm_candidate(workflow.get_scope(people["cm"]), row["id"], row["version"], request_id=uuid4())
    for n in (1, 2, 3):
        row = workflow.ingest_source(
            people["cm2"],
            source_type="excel",
            source_key=f"conflicting:{n}",
            source_data={"row": n},
            responsible_id=people["cm2"],
            handle="abc",
            contact_id=people["other"],
            relationship="owner",
        )
        assert row["responsible_id"] is None
    assert workflow.list_records(workflow.get_scope(people["cm2"]))["total"] == 0
    assert db.execute("SELECT count(*) AS n FROM creator_account_assignments").fetchone()["n"] == 1


def test_inactive_owner_does_not_retain_cross_cm_visibility(db, people):
    row = source(people)
    scope = workflow.get_scope(people["owner"], people["cm"])
    db.execute("UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s", (people["owner"],))
    with pytest.raises(PermissionError):
        workflow.get_record(scope, row["id"])
    with pytest.raises(PermissionError):
        workflow.get_scope(people["owner"], people["cm"])


def test_last_selectable_message_fact_is_locked_during_confirmation(db, people):
    db.execute("UPDATE authorization_chats SET p2p_counterpart_open_id=NULL WHERE authorization_id=%s", (people["cm"],))
    schema = db.execute("SELECT current_schema() AS name").fetchone()["name"]
    db.commit()
    db.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(schema)))
    workflow._lock_contact_scope(db, people["cm"], people["person"])
    with psycopg.connect(os.environ["CREATOR_WORKFLOW_TEST_DATABASE_URL"], autocommit=True) as other:
        other.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(schema)))
        other.execute("SET lock_timeout='50ms'")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            other.execute("DELETE FROM authorization_message_observations WHERE authorization_id=%s", (people["cm"],))


def test_batch_contacts_are_not_truncated_at_search_limit_and_load_only_once(db, people, monkeypatch):
    db.execute("""INSERT INTO contacts(id,display_name,entity_type)
        SELECT gen_random_uuid(),'Bulk person ' || n,'person' FROM generate_series(1,300) n""")
    db.execute("""INSERT INTO lark_identities(open_id,contact_id,display_name)
        SELECT 'bulk_' || id::text,id,display_name FROM contacts WHERE display_name LIKE 'Bulk person %%'""")
    db.execute(
        """INSERT INTO chat_members(chat_id,open_id,last_observed_by_authorization_id)
        SELECT 'chat_cm',open_id,%s FROM lark_identities WHERE open_id LIKE 'bulk_%%'""",
        (people["cm"],),
    )
    db.execute(
        """INSERT INTO creator_workflow_candidates(id,normalized_handle,responsible_authorization_id,
        contact_id,relationship)
        SELECT gen_random_uuid(),'bulk' || row_number() OVER(ORDER BY id),%s,id,'owner'
        FROM contacts WHERE display_name LIKE 'Bulk person %%'""",
        (people["cm"],),
    )
    calls = []

    class Counted:
        def execute(self, query, params=None):
            calls.append(str(query))
            return db.execute(query, params)

    monkeypatch.setattr(workflow, "connection", lambda: nullcontext(Counted()))
    scope = workflow.get_scope(people["cm"])
    result = workflow.list_records(scope, page=3, page_size=100)
    assert result["total"] == 300 and len(result["items"]) == 100
    assert all(
        item["contact"] and item["contact"]["display_name"].startswith("Bulk person") for item in result["items"]
    )
    assert sum("WITH scoped_authorization AS MATERIALIZED" in query for query in calls) == 1
    assert sum("FROM creator_workflow_sources" in query for query in calls) == 1


def test_union_search_preserves_every_scope_guard(db, people):
    scope = workflow.get_scope(people["cm"])
    db.execute("UPDATE authorization_chats SET p2p_counterpart_open_id=NULL WHERE authorization_id=%s", (people["cm"],))
    db.execute("DELETE FROM authorization_message_observations WHERE authorization_id=%s", (people["cm"],))
    # A globally known sender without this CM's own observation is not selectable.
    assert workflow.search_users(scope, "Same") == []
    db.execute(
        """INSERT INTO chat_members(chat_id,open_id,last_observed_by_authorization_id)
        VALUES('chat_cm','ou_person',%s)""",
        (people["cm2"],),
    )
    assert workflow.search_users(scope, "Same") == []
    db.execute("UPDATE chat_members SET last_observed_by_authorization_id=%s", (people["cm"],))
    assert [r["contact_id"] for r in workflow.search_users(scope, "Same")] == [str(people["person"])]
    db.execute("DELETE FROM chat_members")
    db.execute(
        """INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id)
        VALUES(%s,'chat_cm','message1')""",
        (people["cm"],),
    )
    assert len(workflow.search_users(scope, "Same")) == 1
    db.execute("UPDATE chat_messages SET is_deleted=TRUE WHERE chat_id='chat_cm'")
    assert workflow.search_users(scope, "Same") == []
    db.execute("UPDATE chat_messages SET is_deleted=FALSE WHERE chat_id='chat_cm'")
    db.execute("UPDATE authorizations SET body_consent_at=NOW()+INTERVAL '1 minute' WHERE id=%s", (people["cm"],))
    assert workflow.search_users(scope, "Same") == []
