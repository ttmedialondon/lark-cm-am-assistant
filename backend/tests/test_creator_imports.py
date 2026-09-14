"""Single-CM CAP rosters bind the unique identified creator across all matching groups."""

import json
from contextlib import nullcontext
from io import BytesIO
from uuid import uuid4
from xml.sax.saxutils import escape
from zipfile import ZipFile

import pytest
from test_creator_ownership import pilot as parent_pilot

from app import contact_assessments, creator_handles
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


def prepare(pg, pilot, monkeypatch, *, unknown=True, matching_group=True):
    monkeypatch.setattr(imports, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(creator_handles, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(creator_handles, "get_settings", lambda: pilot.settings)
    if matching_group:
        group(pg, pilot, "chat_creator", "creator_one", "creator")
    if unknown:
        pg.execute("DELETE FROM contact_roles WHERE contact_id=%s", (pilot.ids["creator"],))
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='creator_7_question'", (
        message_body_cipher(pilot.settings.token_encryption_key).encrypt(json.dumps({
            "text": "I create my own videos and go live with the campaign products."
        })),
    ))
    calls, callbacks = [], []

    def response(_config, _key, pack, _row_id, **kwargs):
        assert kwargs["output_format"]["strict"]
        assert kwargs["output_format"]["name"] == "creator_import_people_v1"
        assert "ou_creator" not in json.dumps(pack)
        calls.append(pack)
        result = {"people": []}
        for user in pack["users"]:
            own = [m["ref"] for m in pack["messages"] if m["speaker"] == user["ref"]]
            result["people"].append({
                "user_ref": user["ref"], "role": "creator" if own else "insufficient",
                "confidence": .99 if own else 0, "basis": "self_declaration" if own else "weak_context",
                "evidence_refs": own,
            })
        for callback in callbacks:
            callback()
        return {"text": json.dumps(result), "response_id": "synthetic-response",
                "usage": {"input_tokens": 12, "output_tokens": 8},
                "telemetry": {"openai_request_id": "synthetic-request"}}

    monkeypatch.setattr(contact_assessments, "call_openai", response)
    return calls, callbacks


def drain(pilot):
    for _ in range(20):
        if not imports.prepare_next_import(pilot.settings):
            break
    else:
        pytest.fail("import preparation did not settle")
    for _ in range(20):
        job = imports.claim_next_import()
        if not job:
            return
        imports.run_claimed_import(job, pilot.settings)
    pytest.fail("import queue did not settle")


def row_for(pg, batch, handle):
    return pg.execute(
        "SELECT * FROM creator_import_rows WHERE batch_id=%s AND handle=%s", (batch["id"], handle)
    ).fetchone()


def test_excel_ai_identity_manual_and_repeated_import_preserve_people_and_creator_type(pg, pilot, monkeypatch):
    calls, _ = prepare(pg, pilot, monkeypatch)
    data = workbook("handle", "@Creator_One", "creator_one", "not_found", "legacy_account", "bad handle")
    batch = imports.preview_import(pilot.ids["owner"], pilot.ids["cm"], "roster.xlsx", data)
    assert batch["counts"] == {"valid": 3, "duplicates": 1, "invalid": 1}
    saved = pg.execute("SELECT * FROM creator_import_batches WHERE id=%s", (batch["id"],)).fetchone()
    assert saved["uploaded_by"] == pilot.ids["owner"] and saved["cm_authorization_id"] == pilot.ids["cm"]
    assert saved["creator_type"] == "cap"
    assert imports.preview_import(pilot.ids["owner"], pilot.ids["cm"], "roster.xlsx", data)["id"] == batch["id"]
    assert not imports.prepare_next_import(pilot.settings) and not calls
    before_people = pg.execute("SELECT count(*) n FROM contacts").fetchone()["n"]
    imports.start_import(pilot.ids["owner"], batch["id"])
    imports.start_import(pilot.ids["cm"], batch["id"])
    drain(pilot)
    matched, missing = row_for(pg, batch, "creator_one"), row_for(pg, batch, "not_found")
    assert matched["review_status"] == "confirmed" and matched["contact_id"] == pilot.ids["creator"]
    assert matched["api_requests"] == 1 and len(calls) == 1
    assert missing["contact_id"] is None and missing["api_requests"] == 0
    assert imports.source_labels(matched) == ["Excel", "AI"] and imports.source_labels(missing) == ["Excel"]
    assert pg.execute("SELECT count(*) n FROM contacts").fetchone()["n"] == before_people
    role = pg.execute("SELECT * FROM contact_roles WHERE contact_id=%s", (pilot.ids["creator"],)).fetchone()
    assert role["role"] == "creator" and role["status"] == "confirmed"
    assert pg.execute("SELECT creator_type FROM contacts WHERE id=%s", (pilot.ids["creator"],)).fetchone()[
        "creator_type"] == "cap"
    assert row_for(pg, batch, "legacy_account")["reason_code"] == "account_conflict"
    assert not pg.execute("SELECT 1 FROM creator_ownership_tasks").fetchone()
    candidate = next(r for r in imports.page_data(pilot.ids["cm"], pilot.ids["cm"])["candidates"]
                     if r["id"] == str(missing["id"]))
    assert candidate["display_name"] == "—" and candidate["creator_type"] == "cap"
    imports.confirm_row(pilot.ids["cm"], missing["id"], "creator_second", pilot.ids["creator"])
    imports.confirm_row(pilot.ids["cm"], missing["id"], "creator_second", pilot.ids["creator"])
    assert pg.execute("SELECT count(*) n FROM contact_assignments").fetchone()["n"] == 1
    formal = creator_handles.page_data(pilot.ids["cm"], tab="mine")["items"]
    assert len(formal) == 1 and set(formal[0]["handles"]) == {"creator_one", "creator_second"}
    again = imports.preview_import(pilot.ids["cm"], pilot.ids["cm"], "again.xlsx", workbook("creator_one"))
    pg.execute("UPDATE ai_feature_configs SET api_key_ciphertext=NULL,key_suffix=NULL,key_updated_at=NULL "
               "WHERE feature='contact_assessment'")
    pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (pilot.ids["cm"],))
    imports.start_import(pilot.ids["cm"], again["id"])
    drain(pilot)
    assert row_for(pg, again, "creator_one")["reason_code"] == "existing" and len(calls) == 1
    imports.preview_import(pilot.ids["owner"], pilot.ids["cm"], "roster.xlsx", data)
    assert pg.execute("SELECT count(*) n FROM creator_import_rows WHERE batch_id=%s",
                      (batch["id"],)).fetchone()["n"] == 3


def test_two_inflight_handles_for_one_creator_preserve_results_and_api_usage(pg, pilot, monkeypatch):
    calls, callbacks = prepare(pg, pilot, monkeypatch)
    group(pg, pilot, "second_handle_group", "creator_second", "creator")
    pg.execute("INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,"
               "body_ciphertext,message_create_time) SELECT 'second_handle_group','second_video',sender_open_id,"
               "sender_type,message_type,body_ciphertext,message_create_time FROM chat_messages "
               "WHERE message_id='creator_7_question'")
    pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
               "VALUES(%s,'second_handle_group','second_video')", (pilot.ids["cm"],))
    batch = imports.preview_import(pilot.ids["cm"], pilot.ids["cm"], "parallel.xlsx",
                                   workbook("creator_one", "creator_second"))
    imports.start_import(pilot.ids["cm"], batch["id"])
    assert imports.prepare_next_import(pilot.settings) and imports.prepare_next_import(pilot.settings)
    first, second = imports.claim_next_import(), imports.claim_next_import()
    assert first and second

    def finish_second_during_first_request():
        callbacks.clear()
        imports.run_claimed_import(second, pilot.settings)

    callbacks.append(finish_second_during_first_request)
    imports.run_claimed_import(first, pilot.settings)
    for handle in ("creator_one", "creator_second"):
        row = row_for(pg, batch, handle)
        assert row["review_status"] == "confirmed" and row["contact_id"] == pilot.ids["creator"]
        assert row["api_requests"] == 1 and row["input_tokens"] == 12 and row["output_tokens"] == 8
        assert imports._pack(row, pilot.settings, "result_ciphertext")["response"]["text"]
    assert len(calls) == 2 and pg.execute("SELECT count(*) n FROM contact_assignments").fetchone()["n"] == 1


def test_unique_known_creator_needs_no_messages_or_ai_and_groups_deduplicate_people(pg, pilot, monkeypatch):
    calls, _ = prepare(pg, pilot, monkeypatch, unknown=False)
    group(pg, pilot, "second_creator_group", "creator_one", "creator")
    group(pg, pilot, "another_handle_group", "creator_second", "creator")
    pg.execute("UPDATE chat_messages SET body_ciphertext=NULL")
    pg.execute("UPDATE ai_feature_configs SET api_key_ciphertext=NULL,key_suffix=NULL,key_updated_at=NULL "
               "WHERE feature='contact_assessment'")
    batch = imports.preview_import(pilot.ids["cm"], pilot.ids["cm"], "known.xlsx",
                                   workbook("creator_one", "creator_second"))
    imports.start_import(pilot.ids["cm"], batch["id"])
    drain(pilot)
    for handle in ("creator_one", "creator_second"):
        row = row_for(pg, batch, handle)
        assert row["review_status"] == "confirmed" and row["contact_id"] == pilot.ids["creator"]
        assert row["api_requests"] == 0
    assert not calls and pg.execute("SELECT count(*) n FROM contact_assignments").fetchone()["n"] == 1
    assert row_for(pg, batch, "creator_one")["stats"]["groups"] == 2
    assert set(creator_handles.page_data(pilot.ids["cm"], tab="mine")["items"][0]["handles"]) == {
        "creator_one", "creator_second"}


def test_all_matching_groups_count_creators_before_admission_and_ignore_nonmembers(pg, pilot, monkeypatch):
    calls, _ = prepare(pg, pilot, monkeypatch, unknown=False)
    pilot.person("another")
    group(pg, pilot, "second_creator_group", "creator_one", "another")
    # An unrelated classified creator cannot affect a different handle's single-member group.
    group(pg, pilot, "single_group", "single_handle", "creator")
    batch = imports.preview_import(pilot.ids["cm"], pilot.ids["cm"], "groups.xlsx",
                                   workbook("creator_one", "single_handle", "missing_handle"))
    imports.start_import(pilot.ids["cm"], batch["id"])
    drain(pilot)
    ambiguous = row_for(pg, batch, "creator_one")
    assert ambiguous["contact_id"] is None and ambiguous["review_status"] == "pending"
    assert ambiguous["stats"]["creator_count"] == 2
    assert row_for(pg, batch, "single_handle")["review_status"] == "confirmed"
    missing = row_for(pg, batch, "missing_handle")
    assert missing["contact_id"] is None and missing["review_status"] == "pending"
    assert not calls
    for item in imports.page_data(pilot.ids["cm"], pilot.ids["cm"])["candidates"]:
        assert item["contact_id"] is None and item["display_name"] == "—" and item["creator_type"] == "cap"


@pytest.mark.parametrize("identified", [0, 2])
def test_ai_identity_zero_or_multiple_creators_leave_blank_user(pg, pilot, monkeypatch, identified):
    calls, _ = prepare(pg, pilot, monkeypatch)
    another = pilot.person("another", pairs=2)
    pg.execute("DELETE FROM contact_roles WHERE contact_id=%s", (another,))
    group(pg, pilot, "chat_another", "creator_one", "another")
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='another_1_question'", (
        message_body_cipher(pilot.settings.token_encryption_key).encrypt(json.dumps({
            "text": "I create and post my own product videos every day."
        })),
    ))
    original = contact_assessments.call_openai
    def classify(*args, **kwargs):
        response = original(*args, **kwargs)
        result = json.loads(response["text"])
        assert len(result["people"]) == 2
        if not identified:
            for person in result["people"]:
                person.update(role="insufficient", confidence=0, basis="weak_context", evidence_refs=[])
        response["text"] = json.dumps(result)
        return response
    monkeypatch.setattr(contact_assessments, "call_openai", classify)
    batch = imports.preview_import(pilot.ids["cm"], pilot.ids["cm"], "identity.xlsx", workbook("creator_one"))
    imports.start_import(pilot.ids["cm"], batch["id"])
    drain(pilot)
    row = row_for(pg, batch, "creator_one")
    assert len(calls) == 1 and row["api_requests"] == 1
    assert row["review_status"] == "pending" and row["contact_id"] is None
    assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()
    monkeypatch.setattr(imports.creator_import_evidence, "build_evidence",
                        lambda *args, **kwargs: pytest.fail("Candidate GET rescanned group history"))
    item = imports.page_data(pilot.ids["cm"], pilot.ids["cm"])["candidates"][0]
    assert item["contact_id"] is None and item["display_name"] == "—"
    assert item["creator_type"] == "cap" and item["sources"] == ["Excel", "AI"]


def test_import_scope_conflict_and_manual_edit_block_stale_ai_results(pg, pilot, monkeypatch):
    calls, callbacks = prepare(pg, pilot, monkeypatch)
    batch = imports.preview_import(pilot.ids["owner"], pilot.ids["cm"], "roster.xlsx", workbook("creator_one"))
    for action in (
        lambda: imports.page_data(pilot.ids["cm"], pilot.ids["owner"]),
        lambda: imports.search_users(pilot.ids["cm"], pilot.ids["owner"], "creator"),
        lambda: imports.preview_import(pilot.ids["cm"], pilot.ids["owner"], "x.xlsx", workbook("x")),
    ):
        with pytest.raises(PermissionError):
            action()
    assert imports.search_users(pilot.ids["cm"], pilot.ids["cm"], "silent") == []
    imports.start_import(pilot.ids["owner"], batch["id"])
    pending = row_for(pg, batch, "creator_one")
    with pytest.raises(PermissionError):
        imports.confirm_row(pilot.ids["cm"], pending["id"], "creator_one", pilot.ids["silent"])
    with pg.transaction(force_rollback=True):
        pg.execute("INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) "
                   "VALUES(%s,%s,%s,'manual')", (uuid4(), pilot.ids["creator"], pilot.ids["owner"]))
        with pytest.raises(ValueError, match="冲突"):
            imports.confirm_row(pilot.ids["cm"], pending["id"], "creator_one", pilot.ids["creator"])
        assert not pg.execute("SELECT 1 FROM contact_social_accounts WHERE import_row_id=%s",
                              (pending["id"],)).fetchone()
    with pg.transaction(force_rollback=True):
        pg.execute("UPDATE contacts SET creator_type='non_cap' WHERE id=%s", (pilot.ids["creator"],))
        with pytest.raises(ValueError, match="冲突"):
            imports.confirm_row(pilot.ids["cm"], pending["id"], "creator_one", pilot.ids["creator"])
    callbacks.append(
        lambda: imports.confirm_row(pilot.ids["cm"], pending["id"], "corrected_handle", pilot.ids["creator"])
    )
    drain(pilot)
    current = row_for(pg, batch, "corrected_handle")
    assert current["review_status"] == "confirmed" and current["reason_code"] == "manual_confirmed"
    assert current["confidence"] is None and current["refs"] == []
    assert current["pack_ciphertext"] is None and current["result_ciphertext"] is None
    assert current["fingerprint"] is None and current["config_fingerprint"] is None
    assert imports.source_labels(current) == ["Excel"] and len(calls) == 1
    assert not pg.execute("SELECT 1 FROM social_accounts WHERE normalized_handle='creator_one'").fetchone()


def test_paused_or_revoked_import_does_not_send_and_expired_paid_lease_does_not_retry(pg, pilot, monkeypatch):
    calls, _ = prepare(pg, pilot, monkeypatch)
    batch = imports.preview_import(pilot.ids["cm"], pilot.ids["cm"], "roster.xlsx", workbook("creator_one"))
    imports.start_import(pilot.ids["cm"], batch["id"])
    imports.pause_import(pilot.ids["cm"], batch["id"])
    assert not imports.prepare_next_import(pilot.settings) and imports.claim_next_import() is None
    imports.resume_import(pilot.ids["cm"], batch["id"])
    assert imports.prepare_next_import(pilot.settings)
    with pg.transaction(force_rollback=True):
        job = imports.claim_next_import()
        pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (pilot.ids["cm"],))
        imports.run_claimed_import(job, pilot.settings)
        assert not calls and row_for(pg, batch, "creator_one")["review_status"] == "pending"
    job = imports.claim_next_import()
    pg.execute("UPDATE creator_import_rows SET lease_until=NOW()-INTERVAL '1 minute' WHERE id=%s", (job["id"],))
    assert imports.claim_next_import() is None and not calls
    assert row_for(pg, batch, "creator_one")["reason_code"] == "interrupted"


def test_pending_batch_recheck_preserves_confirmed_deferred_and_paid_history(pg, pilot, monkeypatch):
    calls, _ = prepare(pg, pilot, monkeypatch, unknown=False)
    batch = imports.preview_import(pilot.ids["cm"], pilot.ids["cm"], "recheck.xlsx",
                                   workbook("creator_one", "later_handle", "deferred_handle"))
    imports.start_import(pilot.ids["cm"], batch["id"])
    drain(pilot)
    confirmed = row_for(pg, batch, "creator_one")
    deferred = row_for(pg, batch, "deferred_handle")
    imports.defer_row(pilot.ids["cm"], deferred["id"])
    pending = row_for(pg, batch, "later_handle")
    pg.execute("UPDATE creator_import_rows SET api_requests=2,input_tokens=50,output_tokens=20 WHERE id=%s",
               (pending["id"],))
    group(pg, pilot, "later_group", "later_handle", "creator")
    assert imports.recheck_batch(pilot.ids["cm"], batch["id"])["queued"] == 1
    with pytest.raises(ValueError):
        imports.recheck_batch(pilot.ids["cm"], batch["id"])
    drain(pilot)
    current = row_for(pg, batch, "later_handle")
    assert current["review_status"] == "confirmed" and current["contact_id"] == pilot.ids["creator"]
    assert (current["api_requests"], current["input_tokens"], current["output_tokens"]) == (2, 50, 20)
    assert row_for(pg, batch, "creator_one") == confirmed
    assert row_for(pg, batch, "deferred_handle")["review_status"] == "deferred"
    assert not calls and pg.execute("SELECT count(*) n FROM contact_assignments").fetchone()["n"] == 1
    audit = pg.execute(
        "SELECT metadata FROM audit_events WHERE action='creator_import_reanalysis_requested'"
    ).fetchone()
    assert audit["metadata"]["previous"]["pack_ciphertext"] == pending["pack_ciphertext"]
    assert audit["metadata"]["previous"]["result_ciphertext"] == pending["result_ciphertext"]
    assert audit["metadata"]["previous"]["refs"] == pending["refs"]


def test_import_pages_and_user_search_only_expose_currently_accessible_identities(pg, pilot, monkeypatch):
    prepare(pg, pilot, monkeypatch, matching_group=False)
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
