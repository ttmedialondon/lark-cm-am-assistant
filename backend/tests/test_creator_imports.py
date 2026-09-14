"""Single-CM handle import: real DB/evidence, synthetic workbooks and mocked provider only."""

import json
from contextlib import nullcontext
from io import BytesIO
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


def prepare(pg, pilot, monkeypatch):
    monkeypatch.setattr(imports, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(creator_handles, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(creator_handles, "get_settings", lambda: pilot.settings)
    text = "My TikTok account is @creator_one. I post my own videos and go LIVE with the campaign products."
    pg.execute(
        "UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='creator_7_question'",
        (message_body_cipher(pilot.settings.token_encryption_key).encrypt(json.dumps({"text": text})),),
    )
    calls, callbacks = [], []

    def response(_config, _key, pack, _row_id, **kwargs):
        assert kwargs["output_format"]["strict"]
        assert kwargs["output_format"]["name"] == "creator_import_match_v1"
        assert "ou_creator" not in json.dumps(pack)
        calls.append(pack)
        own = next(m for m in pack["messages"] if pack["imported_handle"] in m["self_handles"])
        result = {
            "outcome": "matched",
            "reason": "本人明确提供账号，并说明本人创作。",
            "candidates": [
                {
                    "user_ref": own["speaker"],
                    "confidence": 0.99,
                    "is_creator": True,
                    "creator_confidence": 0.99,
                    "basis": "self_declared",
                    "evidence_refs": [own["ref"]],
                    "conflicts": [],
                    "missing_info": [],
                }
            ],
        }
        for callback in callbacks:
            callback()
        return {
            "text": json.dumps(result),
            "response_id": "synthetic-response",
            "usage": {"input_tokens": 12, "output_tokens": 8},
            "telemetry": {"openai_request_id": "synthetic-request"},
        }

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


def test_excel_auto_manual_and_repeated_import_preserve_people_and_creator_type(pg, pilot, monkeypatch):
    calls, _ = prepare(pg, pilot, monkeypatch)
    # Import identity evidence must not become a manual fact in the global classification.
    pg.execute("DELETE FROM contact_roles WHERE contact_id=%s", (pilot.ids["creator"],))
    data = workbook("handle", "@Creator_One", "creator_one", "not_found", "legacy_account", "bad handle")
    batch = imports.preview_import(pilot.ids["owner"], pilot.ids["cm"], "roster.xlsx", data)
    assert batch["counts"] == {"valid": 3, "duplicates": 1, "invalid": 1}
    saved_batch = pg.execute("SELECT * FROM creator_import_batches WHERE id=%s", (batch["id"],)).fetchone()
    assert saved_batch["uploaded_by"] == pilot.ids["owner"]
    assert saved_batch["cm_authorization_id"] == pilot.ids["cm"]
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
    assert imports.source_labels(matched) == ["Excel", "AI"]
    assert imports.source_labels({**matched, "stats": {}}) == ["Excel", "AI"]
    assert imports.source_labels({**matched, "stats": {"ai_matched_handle": False}}) == ["Excel"]
    assert imports.source_labels(missing) == ["Excel"]
    assert pg.execute("SELECT count(*) n FROM contacts").fetchone()["n"] == before_people
    assert (
        pg.execute("SELECT creator_type FROM contacts WHERE id=%s", (pilot.ids["creator"],)).fetchone()["creator_type"]
        == "pending"
    )
    assert imports._pack(matched, pilot.settings, "result_ciphertext")["checked"]["creator_confirmed"] is True
    assert not pg.execute("SELECT 1 FROM contact_roles WHERE contact_id=%s", (pilot.ids["creator"],)).fetchone()
    formal = creator_handles.page_data(pilot.ids["owner"], cm_id=pilot.ids["cm"], tab="mine")
    assert formal["total_count"] == 1 and formal["items"][0]["handles"] == ["creator_one"]
    conflict = row_for(pg, batch, "legacy_account")
    assert conflict["reason_code"] == "account_conflict" and conflict["api_requests"] == 0
    assignment = pg.execute("SELECT * FROM contact_assignments WHERE contact_id=%s", (pilot.ids["creator"],)).fetchone()
    assert assignment["responsible_authorization_id"] == pilot.ids["cm"]
    assert not pg.execute("SELECT 1 FROM creator_ownership_tasks").fetchone()
    assert not pg.execute("SELECT enabled FROM ai_feature_configs WHERE feature='contact_assessment'").fetchone()[
        "enabled"
    ]
    candidate = next(
        r for r in imports.page_data(pilot.ids["cm"], pilot.ids["cm"])["candidates"] if r["id"] == str(missing["id"])
    )
    assert candidate["display_name"] == "—" and candidate["creator_type"] == "pending"
    pg.execute("UPDATE contacts SET creator_type='cap' WHERE id=%s", (pilot.ids["creator"],))
    imports.confirm_row(pilot.ids["cm"], missing["id"], "creator_second", pilot.ids["creator"])
    imports.confirm_row(pilot.ids["cm"], missing["id"], "creator_second", pilot.ids["creator"])
    assert pg.execute("SELECT count(*) n FROM contact_assignments").fetchone()["n"] == 1
    assert (
        pg.execute(
            "SELECT count(*) n FROM contact_social_accounts WHERE contact_id=%s", (pilot.ids["creator"],)
        ).fetchone()["n"]
        == 2
    )
    assert (
        pg.execute("SELECT creator_type FROM contacts WHERE id=%s", (pilot.ids["creator"],)).fetchone()["creator_type"]
        == "cap"
    )
    formal = creator_handles.page_data(pilot.ids["owner"], cm_id=pilot.ids["cm"], tab="mine")
    assert formal["total_count"] == 1 and set(formal["items"][0]["handles"]) == {"creator_one", "creator_second"}
    again = imports.preview_import(pilot.ids["cm"], pilot.ids["cm"], "again.xlsx", workbook("creator_one"))
    pg.execute("UPDATE contacts SET creator_type='non_cap' WHERE id=%s", (pilot.ids["creator"],))
    pg.execute("DELETE FROM contact_roles WHERE contact_id=%s", (pilot.ids["creator"],))
    pg.execute(
        "UPDATE ai_feature_configs SET api_key_ciphertext=NULL,key_suffix=NULL,key_updated_at=NULL "
        "WHERE feature='contact_assessment'"
    )
    pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (pilot.ids["cm"],))
    imports.start_import(pilot.ids["cm"], again["id"])
    drain(pilot)
    assert row_for(pg, again, "creator_one")["reason_code"] == "existing" and len(calls) == 1
    assert not pg.execute("SELECT 1 FROM contact_roles WHERE contact_id=%s", (pilot.ids["creator"],)).fetchone()
    assert (
        pg.execute("SELECT creator_type FROM contacts WHERE id=%s", (pilot.ids["creator"],)).fetchone()["creator_type"]
        == "non_cap"
    )
    # Re-upload after manual correction must not recreate the original row.
    imports.preview_import(pilot.ids["owner"], pilot.ids["cm"], "roster.xlsx", data)
    assert (
        pg.execute("SELECT count(*) n FROM creator_import_rows WHERE batch_id=%s", (batch["id"],)).fetchone()["n"] == 3
    )


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
        pg.execute(
            "INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) "
            "VALUES(gen_random_uuid(),%s,%s,'manual')",
            (pilot.ids["creator"], pilot.ids["owner"]),
        )
        with pytest.raises(ValueError, match="冲突"):
            imports.confirm_row(pilot.ids["cm"], pending["id"], "creator_one", pilot.ids["creator"])
        assert not pg.execute(
            "SELECT 1 FROM contact_social_accounts WHERE import_row_id=%s", (pending["id"],)
        ).fetchone()
    callbacks.append(
        lambda: imports.confirm_row(pilot.ids["cm"], pending["id"], "corrected_handle", pilot.ids["creator"])
    )
    drain(pilot)
    current = row_for(pg, batch, "corrected_handle")
    assert current["review_status"] == "confirmed" and current["reason_code"] == "manual_confirmed"
    assert current["confidence"] is None and current["refs"] == []
    assert current["pack_ciphertext"] is None and current["result_ciphertext"] is None
    assert current["fingerprint"] is None and current["config_fingerprint"] is None
    assert imports.source_labels(current) == ["Excel"]
    assert (
        len(calls) == 1
        and not pg.execute("SELECT 1 FROM social_accounts WHERE normalized_handle='creator_one'").fetchone()
    )


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
    assert candidate["display_name"] == "—" and candidate["creator_type"] == "pending"
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


@pytest.mark.parametrize("score", [.94, .99])
def test_existing_ai_accounts_merge_into_excel_without_new_requests(pg, pilot, monkeypatch, score):
    from test_creator_handles import _case, _drain_paid, _task

    monkeypatch.setattr(imports, "connection", lambda: nullcontext(pg))
    case = _case(pg, pilot, monkeypatch)
    case.score = score
    creator_handles.start_run(pilot.ids["owner"], case.run)
    _drain_paid(pilot.settings)
    task = _task(pg, case)
    assert len(case.calls) == 1 and task["review_status"] == "pending"
    batch = imports.preview_import(pilot.ids["owner"], pilot.ids["cm"], "roster.xlsx", workbook("creator_one"))
    imports.start_import(pilot.ids["owner"], batch["id"])
    drain(pilot)
    row = row_for(pg, batch, "creator_one")
    assert row["contact_id"] == case.creator and imports.source_labels(row) == ["Excel", "AI"]
    assert row["stats"]["reused_handle_task_id"] == str(task["id"])
    assert row["api_requests"] == 0 and len(case.calls) == 1
    assert row["review_status"] == ("confirmed" if score >= .95 else "pending")
    if score < .95:
        assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()
        imports.confirm_row(pilot.ids["cm"], row["id"], row["handle"], case.creator)
        unchanged = row_for(pg, batch, "creator_one")
        assert unchanged["confidence"] == row["confidence"]
        assert imports.source_labels(unchanged) == ["Excel", "AI"]
    assert pg.execute("""SELECT status FROM contact_social_accounts l JOIN social_accounts s
        ON s.id=l.social_account_id WHERE s.normalized_handle='creator_one'""").fetchone()["status"] == "confirmed"
    assert pg.execute("SELECT count(*) n FROM contact_assignments").fetchone()["n"] == 1
    # Losing the source scope removes reuse eligibility even though the AI result still exists.
    pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (pilot.ids["cm"],))
    hidden = imports.preview_import(pilot.ids["cm"], pilot.ids["cm"], "hidden.xlsx", workbook("creator_two"))
    imports.start_import(pilot.ids["cm"], hidden["id"])
    drain(pilot)
    hidden_row = row_for(pg, hidden, "creator_two")
    assert hidden_row["contact_id"] is None and imports.source_labels(hidden_row) == ["Excel"]
    assert hidden_row["api_requests"] == 0 and len(case.calls) == 1


def test_existing_ai_multiple_people_for_one_handle_remains_one_conflicted_excel_candidate(pg, pilot, monkeypatch):
    from test_creator_handles import _case, _drain_paid

    monkeypatch.setattr(imports, "connection", lambda: nullcontext(pg))
    pilot.person("another", pairs=2)
    pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='another_1_question'", (
        message_body_cipher(pilot.settings.token_encryption_key).encrypt(
            json.dumps({"text": "My TikTok account is @creator_one. Can you arrange my sample?"})),
    ))
    case = _case(pg, pilot, monkeypatch)
    creator_handles.start_run(pilot.ids["owner"], case.run)
    _drain_paid(pilot.settings)
    assert len(case.calls) == 2
    batch = imports.preview_import(pilot.ids["cm"], pilot.ids["cm"], "roster.xlsx", workbook("creator_one"))
    imports.start_import(pilot.ids["cm"], batch["id"])
    drain(pilot)
    row = row_for(pg, batch, "creator_one")
    assert row["outcome"] == "conflict" and row["review_status"] == "pending" and row["contact_id"] is None
    assert imports.source_labels(row) == ["Excel", "AI"] and len(row["stats"]["reused_handle_task_ids"]) == 2
    assert row["api_requests"] == 0 and len(case.calls) == 2
    assert not pg.execute("SELECT 1 FROM contact_assignments").fetchone()


def test_excel_admits_a_strong_handle_even_when_another_ai_handle_needs_review(pg, pilot, monkeypatch):
    from test_creator_handles import _case, _drain_paid, _task

    monkeypatch.setattr(imports, "connection", lambda: nullcontext(pg))
    case = _case(pg, pilot, monkeypatch)
    original = contact_assessments.call_openai

    def mixed_scores(*args, **kwargs):
        response = original(*args, **kwargs)
        result = json.loads(response["text"])
        for candidate in result["candidates"]:
            candidate["confidence"] = .99 if candidate["handle"] == "creator_one" else .8
        response["text"] = json.dumps(result)
        return response

    monkeypatch.setattr(contact_assessments, "call_openai", mixed_scores)
    creator_handles.start_run(pilot.ids["owner"], case.run)
    _drain_paid(pilot.settings)
    assert _task(pg, case)["outcome"] == "review" and len(case.calls) == 1
    batch = imports.preview_import(
        pilot.ids["cm"], pilot.ids["cm"], "roster.xlsx", workbook("creator_one", "creator_two")
    )
    imports.start_import(pilot.ids["cm"], batch["id"])
    drain(pilot)
    strong, weak = row_for(pg, batch, "creator_one"), row_for(pg, batch, "creator_two")
    assert strong["review_status"] == "confirmed" and weak["review_status"] == "pending"
    assert strong["contact_id"] == weak["contact_id"] == case.creator
    assert imports.source_labels(strong) == imports.source_labels(weak) == ["Excel", "AI"]
    assert strong["api_requests"] == weak["api_requests"] == 0 and len(case.calls) == 1
    formal = creator_handles.page_data(pilot.ids["cm"], tab="mine")
    assert formal["total_count"] == 1 and formal["items"][0]["handles"] == ["creator_one"]
