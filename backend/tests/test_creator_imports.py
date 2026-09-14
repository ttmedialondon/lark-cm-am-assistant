"""Single-CM CAP rosters bind the unique identified creator across all matching groups."""

import json
from contextlib import nullcontext
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from xml.sax.saxutils import escape
from zipfile import ZipFile

import pytest
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


def prepare(pg, pilot, monkeypatch, *, unknown=True, matching_group=True, peers=False):
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
    if peers:
        peer = pilot.person("peer")
        pg.execute("DELETE FROM contact_roles WHERE contact_id=%s", (peer,))
        pg.execute("INSERT INTO chat_members(chat_id,open_id) VALUES('chat_creator','ou_peer')")
        pg.execute("INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,"
                   "body_ciphertext,message_create_time) VALUES('chat_creator','peer_role','ou_peer','user','text',"
                   "%s,NOW())", (message_body_cipher(pilot.settings.token_encryption_key).encrypt(json.dumps({
                       "text": "I represent the seller brand and handle product inventory for our store."
                   })),))
        pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
                   "VALUES(%s,'chat_creator','peer_role')", (pilot.ids["cm"],))
    calls, callbacks = [], []

    def response(_config, _key, pack, _row_id, **kwargs):
        assert kwargs["output_format"]["strict"]
        assert kwargs["output_format"]["name"] == "creator_import_groups_people_v3"
        assert "ou_creator" not in json.dumps(pack)
        calls.append(pack)
        result = {"groups": [{
            "group_ref": group["ref"], "match": "matched", "confidence": .99,
            "basis": "explicit_handle", "evidence_refs": [group["ref"], *[
                m["ref"] for m in pack["messages"] if m["group_ref"] == group["ref"]
                and pack["imported_handle"] in m["text"]
            ]],
        } for group in pack["groups"]], "people": []}
        for user in pack["users"]:
            own = [m["ref"] for m in pack["messages"] if m["speaker"] == user["ref"]]
            known = user["known_category"] == "creator"
            merchant = any(m["ref"] in own and "represent the seller brand" in m["text"] for m in pack["messages"])
            result["people"].append({
                "user_ref": user["ref"],
                "role": "merchant_contact" if merchant else "creator" if own or known else "insufficient",
                "confidence": .99 if own or known else 0, "basis": "self_declaration" if own else "weak_context",
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
    calls, _ = prepare(pg, pilot, monkeypatch, peers=True)
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
    calls, callbacks = prepare(pg, pilot, monkeypatch, peers=True)
    group(pg, pilot, "second_handle_group", "creator_second", "creator", "peer")
    pg.execute("INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_type,message_type,"
               "body_ciphertext,message_create_time) SELECT 'second_handle_group','second_'||message_id,sender_open_id,"
               "sender_type,message_type,body_ciphertext,message_create_time FROM chat_messages "
               "WHERE message_id IN ('creator_7_question','peer_role')")
    pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
               "SELECT %s,chat_id,message_id FROM chat_messages WHERE chat_id='second_handle_group'",
               (pilot.ids["cm"],))
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
                                   workbook("creator_one", "single_handle", "unrelated_zebra"))
    imports.start_import(pilot.ids["cm"], batch["id"])
    drain(pilot)
    ambiguous = row_for(pg, batch, "creator_one")
    assert ambiguous["contact_id"] is None and ambiguous["review_status"] == "pending"
    assert ambiguous["stats"]["creator_count"] == 2
    assert row_for(pg, batch, "single_handle")["review_status"] == "confirmed"
    missing = row_for(pg, batch, "unrelated_zebra")
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
    assert row["reason_code"] == ("person_ambiguous" if not identified else "multiple_creators")
    assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()
    monkeypatch.setattr(imports.creator_import_evidence, "build_evidence",
                        lambda *args, **kwargs: pytest.fail("Candidate GET rescanned group history"))
    item = imports.page_data(pilot.ids["cm"], pilot.ids["cm"])["candidates"][0]
    assert item["contact_id"] is None and item["display_name"] == "—"
    assert item["creator_type"] == "cap" and item["sources"] == ["Excel", "AI"]


def test_import_scope_conflict_and_manual_edit_block_stale_ai_results(pg, pilot, monkeypatch):
    calls, callbacks = prepare(pg, pilot, monkeypatch, peers=True)
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
    calls, _ = prepare(pg, pilot, monkeypatch, peers=True)
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


def test_parentheses_and_plus_group_names_import_all_five_handles_without_ai(pg, pilot, monkeypatch):
    calls, _ = prepare(pg, pilot, monkeypatch, unknown=False, matching_group=False)
    named = {
        "aquanails": "❤️ aquanailsbypatrycja (patrycjanails) X TT Media",
        "bex": "❤️bex_mumof3 + bexmays X TT media",
        "katie": "❤️katievictoriaftm (katiesshopfinds) & TT Media",
        "charlotte": "❤️(shopwithcharlotte) X TT Media (WA contact",
    }
    for chat, name in named.items():
        group(pg, pilot, chat, "placeholder", "creator")
        pg.execute("UPDATE chats SET name=%s WHERE chat_id=%s", (name, chat))
    # Dissolved historical copies contain a different creator and must not affect today's count.
    pilot.person("another")
    for chat, name in {
        "old_bex": "bex_mumof3 X TT Media",
        "old_charlotte": "Charlottescrunchymama (shopwithcharlotte) X TT Media",
    }.items():
        group(pg, pilot, chat, "placeholder", "another")
        pg.execute("UPDATE chats SET name=%s,chat_status='dissolved' WHERE chat_id=%s", (name, chat))
    wanted = ["aquanailsbypatrycja", "patrycjanails", "bex_mumof3", "katievictoriaftm", "shopwithcharlotte"]
    batch = imports.preview_import(pilot.ids["cm"], pilot.ids["cm"], "five.xlsx", workbook(*wanted))
    imports.start_import(pilot.ids["cm"], batch["id"])
    drain(pilot)
    for handle in wanted:
        row = row_for(pg, batch, handle)
        assert row["review_status"] == "confirmed" and row["contact_id"] == pilot.ids["creator"]
        assert row["api_requests"] == 0
    assert not calls


def test_similar_group_and_unknown_creator_are_resolved_in_one_ai_call(pg, pilot, monkeypatch):
    calls, _ = prepare(pg, pilot, monkeypatch)
    pg.execute("UPDATE chats SET name='creatr_one X TT Media' WHERE chat_id='chat_creator'")
    # The CM establishes the group correspondence; the unknown person's separate activity identifies them.
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='creator_7_answer'", (
        message_body_cipher(pilot.settings.token_encryption_key).encrypt(json.dumps({
            "text": "This group is for @creator_one. Welcome to your creator group."
        })),
    ))
    pilot.person("another")
    group(pg, pilot, "unrelated_candidate", "creator_onee", "another")
    original = contact_assessments.call_openai

    def jointly_match(*args, **kwargs):
        response = original(*args, **kwargs)
        result, pack = json.loads(response["text"]), args[2]
        selected = next(g["ref"] for g in pack["groups"] if "creatr_one" in g["name"])
        assert len(result["groups"]) == 2
        for found in result["groups"]:
            if found["group_ref"] != selected:
                found.update(match="unrelated", basis="context_only", evidence_refs=[found["group_ref"]])
        response["text"] = json.dumps(result)
        return response

    monkeypatch.setattr(contact_assessments, "call_openai", jointly_match)
    batch = imports.preview_import(pilot.ids["cm"], pilot.ids["cm"], "similar.xlsx", workbook("creator_one"))
    imports.start_import(pilot.ids["cm"], batch["id"])
    drain(pilot)
    row = row_for(pg, batch, "creator_one")
    assert len(calls) == 1 and row["api_requests"] == 1
    assert row["review_status"] == "confirmed" and row["contact_id"] == pilot.ids["creator"]
    checked = imports._pack(row, pilot.settings, "result_ciphertext")["checked"]
    assert checked["creator_count"] == 1 and checked["matched_group_count"] == 1
    assert imports.source_labels(row) == ["Excel", "AI"]


@pytest.mark.parametrize("match", ["uncertain", "unrelated"])
def test_similar_group_known_creator_requires_ai_group_confirmation(pg, pilot, monkeypatch, match):
    calls, _ = prepare(pg, pilot, monkeypatch, unknown=False)
    pg.execute("UPDATE chats SET name='creator_onee X TT Media' WHERE chat_id='chat_creator'")
    pg.execute("UPDATE chat_messages SET body_ciphertext=NULL")
    original = contact_assessments.call_openai

    def unresolved(*args, **kwargs):
        response = original(*args, **kwargs)
        result = json.loads(response["text"])
        for found in result["groups"]:
            found.update(match=match, basis="context_only", evidence_refs=[found["group_ref"]])
        response["text"] = json.dumps(result)
        return response

    monkeypatch.setattr(contact_assessments, "call_openai", unresolved)
    batch = imports.preview_import(pilot.ids["cm"], pilot.ids["cm"], "uncertain.xlsx", workbook("creator_one"))
    imports.start_import(pilot.ids["cm"], batch["id"])
    drain(pilot)
    row = row_for(pg, batch, "creator_one")
    assert len(calls) == 1 and row["api_requests"] == 1
    assert row["review_status"] == "pending" and row["contact_id"] is None
    assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()


@pytest.mark.parametrize("creator_type", ["cap", "non_cap", "pending"])
def test_only_cap_roster_resolves_silent_single_person_despite_old_ninety_percent_identity(
    pg, pilot, monkeypatch, creator_type,
):
    calls, _ = prepare(pg, pilot, monkeypatch)
    pg.execute("UPDATE contacts SET display_name='Kirsty' WHERE id=%s", (pilot.ids["creator"],))
    pg.execute("UPDATE contact_assessments SET result=%s::jsonb WHERE contact_id=%s", (
        json.dumps({"reason_code": "low_confidence", "roles": [{"role": "creator", "confidence": .9}]}),
        pilot.ids["creator"],
    ))
    merchant = pilot.person("brand_member")
    pg.execute("DELETE FROM contact_roles WHERE contact_id=%s", (merchant,))
    pg.execute("INSERT INTO contact_roles(contact_id,role,status,source,is_primary) "
               "VALUES(%s,'merchant_contact','confirmed','manual',TRUE)", (merchant,))
    group(pg, pilot, "chat_creator", "creator_one", "creator", "brand_member")
    group(pg, pilot, "duplicate_group", "creator_one", "creator", "brand_member")
    pg.execute("UPDATE chat_messages SET body_ciphertext=NULL")
    batch = imports.preview_import(pilot.ids["cm"], pilot.ids["cm"], "silent.xlsx", workbook("creator_one"),
                                   creator_type=creator_type)
    imports.start_import(pilot.ids["cm"], batch["id"])
    row = row_for(pg, batch, "creator_one")
    with pytest.raises(ValueError, match="冲突"):
        imports.confirm_row(pilot.ids["cm"], row["id"], "creator_one", merchant)
    if creator_type == "cap":
        for incomplete in ("partial", "unresolved_member"):
            with pg.transaction(force_rollback=True):
                if incomplete == "partial":
                    pg.execute("UPDATE authorization_chats SET member_snapshot_status='partial' "
                               "WHERE chat_id='chat_creator'")
                else:
                    pg.execute("INSERT INTO lark_identities(open_id) VALUES('ou_unresolved_member')")
                    pg.execute("INSERT INTO chat_members(chat_id,open_id) "
                               "VALUES('chat_creator','ou_unresolved_member')")
                material = imports.creator_import_evidence.build_evidence(
                    pg, pilot.settings, pilot.ids["cm"], "creator_one", creator_type="cap",
                )
                assert material["checked"]["user_ref"] is None
                assert material["checked"]["reason_code"] == "person_unresolved"
    drain(pilot)
    row = row_for(pg, batch, "creator_one")
    if creator_type == "cap":
        assert row["review_status"] == "confirmed" and row["contact_id"] == pilot.ids["creator"]
        assert row["confidence"] is None and imports.source_labels(row) == ["Excel"]
        assert pg.execute("SELECT creator_type FROM contacts WHERE id=%s", (pilot.ids["creator"],)).fetchone()[
            "creator_type"] == "cap"
    else:
        assert row["review_status"] == "pending" and row["contact_id"] is None
    assert not calls and row["api_requests"] == 0
    assert pg.execute("SELECT source FROM contact_roles WHERE contact_id=%s",
                      (merchant,)).fetchone()["source"] == "manual"


def test_cap_multiple_people_accepts_cm_addressed_context_in_the_same_ai_call(pg, pilot, monkeypatch):
    calls, _ = prepare(pg, pilot, monkeypatch, peers=True)
    pg.execute("UPDATE chat_messages SET body_ciphertext=NULL WHERE sender_open_id='ou_creator'")
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s,mentions_json=%s::jsonb "
               "WHERE message_id='creator_7_answer'", (
                   message_body_cipher(pilot.settings.token_encryption_key).encrypt(json.dumps({
                       "text": "@_user_1 You are the creator in this CAP account group. Please send your product video."
                   })), json.dumps([{"key": "@_user_1", "id": "ou_creator", "id_type": "open_id"}]),
               ))
    original = contact_assessments.call_openai

    def contextual_identity(*args, **kwargs):
        response = original(*args, **kwargs)
        pack, result = args[2], json.loads(response["text"])
        directed = next(m for m in pack["messages"] if m.get("speaker_is_staff") and m.get("target_refs"))
        person = next(p for p in result["people"] if p["user_ref"] in directed["target_refs"])
        person.update(role="creator", confidence=.99, basis="direct_interaction", evidence_refs=[directed["ref"]])
        response["text"] = json.dumps(result)
        return response

    monkeypatch.setattr(contact_assessments, "call_openai", contextual_identity)
    batch = imports.preview_import(pilot.ids["cm"], pilot.ids["cm"], "context.xlsx", workbook("creator_one"))
    imports.start_import(pilot.ids["cm"], batch["id"])
    drain(pilot)
    row = row_for(pg, batch, "creator_one")
    assert row["review_status"] == "confirmed" and row["contact_id"] == pilot.ids["creator"]
    assert row["api_requests"] == 1 and len(calls) == 1
    assert imports.source_labels(row) == ["Excel", "AI"]


@pytest.mark.parametrize("change", ["body", "scope"])
def test_paid_cap_context_cannot_survive_body_or_permission_changes(pg, pilot, monkeypatch, change):
    calls, callbacks = prepare(pg, pilot, monkeypatch, peers=True)
    batch = imports.preview_import(pilot.ids["cm"], pilot.ids["cm"], "changed.xlsx", workbook("creator_one"))
    imports.start_import(pilot.ids["cm"], batch["id"])

    def invalidate_evidence():
        if change == "body":
            pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='peer_role'", (
                message_body_cipher(pilot.settings.token_encryption_key).encrypt(json.dumps({
                    "text": "I create my own product videos and livestream every day."
                })),
            ))
        else:
            pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s",
                       (pilot.ids["cm"],))

    callbacks.append(invalidate_evidence)
    drain(pilot)
    row = row_for(pg, batch, "creator_one")
    assert row["review_status"] == "pending" and row["contact_id"] is None
    assert row["api_requests"] == 1 and row["input_tokens"] == 12 and row["output_tokens"] == 8
    assert len(calls) == 1 and not pg.execute("SELECT 1 FROM contact_assignments").fetchone()


def test_creator_library_reset_preserves_sources_history_and_new_data_on_replay(pg, pilot, monkeypatch):
    calls, _ = prepare(pg, pilot, monkeypatch, peers=True)
    monkeypatch.setattr(contact_assessments, "connection", lambda: nullcontext(pg))
    cm, owner, creator = pilot.ids["cm"], pilot.ids["owner"], pilot.ids["creator"]
    data = workbook("creator_one", "not_found")
    batch = imports.preview_import(owner, cm, "original.xlsx", data)
    imports.start_import(owner, batch["id"])
    drain(pilot)
    assert row_for(pg, batch, "creator_one")["review_status"] == "confirmed" and len(calls) == 1
    pg.execute("UPDATE chats SET name='new_handle X TT' WHERE chat_id='chat_creator'")
    queued = imports.preview_import(cm, cm, "inflight.xlsx", workbook("new_handle"))
    imports.start_import(cm, queued["id"])
    assert imports.prepare_next_import(pilot.settings)
    inflight = imports.claim_next_import()
    assert inflight
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
    assert not imports.prepare_next_import(pilot.settings) and not imports.claim_next_import()
    assert not creator_ownership.prepare_next_preview(pilot.settings) and not creator_ownership.claim_next_ownership()
    assert not creator_handles.prepare_next_preview(pilot.settings) and not creator_handles.claim_next_handle()
    assert not contact_assessments.queue_eligible_contact_assessments()
    assert not contact_assessments.claim_next_contact_assessment()
    imports.run_claimed_import(inflight, pilot.settings)
    assert len(calls) == 1 and creator_handles.page_data(cm, tab="mine")["total_count"] == 0
    with pytest.raises(ValueError):
        imports.recheck_batch(cm, batch["id"])
    new_batch = imports.preview_import(owner, cm, "original.xlsx", data)
    assert new_batch["id"] != batch["id"]
    imports.start_import(owner, new_batch["id"])
    imports.confirm_row(cm, row_for(pg, new_batch, "creator_one")["id"], "creator_one", creator)
    pg.execute(migration)
    assert creator_handles.page_data(cm, tab="mine")["total_count"] == 1
    assert row_for(pg, new_batch, "creator_one")["review_status"] == "confirmed"
    assert pg.execute("SELECT creator_type FROM contacts WHERE id=%s", (creator,)).fetchone()["creator_type"] == "cap"
    assert pg.execute("SELECT count(*) n FROM audit_events WHERE action=%s", (action,)).fetchone()["n"] == 1
