"""One real PostgreSQL workflow: tools, reusable reports, drafts and revoked sources."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_offhours_summary import finish_merges, pg, seeded  # noqa: F401

from app import assistant_queries, assistant_search, summary_dialogues
from app import assistant_tools as tools
from app import offhours_summary as summaries
from app.ai_feature_config import api_key_cipher, save_feature_config


def test_owner_person_queries_preserve_selected_scope_and_recheck_saved_sources(pg, seeded):  # noqa: F811
    s = seeded
    pg.execute("UPDATE authorizations SET display_name='Jenette' WHERE id=%s", (s.ids["cm"],))
    pg.execute("UPDATE authorizations SET display_name='Janette' WHERE id=%s", (s.ids["reviewer"],))
    pg.execute("""INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_open_id,
        p2p_counterpart_type,backfill_complete,backfill_window_start_at,last_incremental_at,sync_status)
        VALUES(%s,'chat','ou_person','user',TRUE,%s,%s,'succeeded')""",
        (s.ids["reviewer"], s.start - timedelta(days=9), s.end))
    pg.execute("""INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id)
        VALUES(%s,'chat','m1')""", (s.ids["reviewer"],))
    session, turn_id, now = uuid4(), uuid4(), datetime.now(UTC)
    pg.execute("INSERT INTO assistant_sessions(id,actor_id) VALUES(%s,%s)", (session, s.ids["owner"]))
    pg.execute("INSERT INTO assistant_turns(id,session_id,client_request_id,input_ciphertext,created_at) "
               "VALUES(%s,%s,%s,'fixture',%s)", (turn_id, session, uuid4(), now))
    turn = pg.execute("SELECT * FROM assistant_turns WHERE id=%s", (turn_id,)).fetchone()
    context = tools.current_context(pg, s.ids["owner"], {"cm_id": str(s.ids["owner"])}, s.settings)
    assert context["can_query_other_cm"]
    assert {person["cm_name"] for person in context["available_scopes"]} >= {"Janette", "Jenette"}
    owner_schemas = {tool["name"]: tool["parameters"]["properties"] for tool in tools.tools_for(context)}
    assert owner_schemas["summarize_messages"]["cm_id"]["type"] == ["string", "null"]
    own = tools.current_context(pg, s.ids["cm"], {}, s.settings)
    assert tools.tools_for(own) is tools.TOOLS and not own["can_query_other_cm"]
    results = []

    def run(name, args):
        value = tools.execute(pg, s.ids["owner"], turn, name, args, s.settings, context)
        results.append(value)
        turn["checkpoint_ciphertext"] = summary_dialogues.encode({"tool_results": results}, s.settings)
        pg.execute("UPDATE assistant_turns SET checkpoint_ciphertext=%s WHERE id=%s",
                   (turn["checkpoint_ciphertext"], turn_id))
        return value

    janette = run("resolve_staff", {"query": "Janette"})
    assert janette["total"] == 1 and janette["staff"][0]["cm_id"] == str(s.ids["reviewer"])
    assert run("resolve_staff", {"query": "Jenette"})["staff"][0]["cm_id"] == str(s.ids["cm"])
    assert run("resolve_staff", {"query": "ette"})["requires_selection"]
    assert run("resolve_staff", {"query": "Jannette"})["total"] == 0
    args = {"audience": "all", "start": s.start.isoformat(), "end": now.isoformat(),
            "creator_ids": None, "chat_ids": None, "cursor": None}
    pages = [run("summarize_messages", {**args, "cm_id": str(person)})
             for person in (s.ids["cm"], s.ids["reviewer"])]
    assert [page["scope"]["cm_name"] for page in pages] == ["Jenette", "Janette"]
    assert "unrelated outsider secret" in json.dumps(pages[0])
    assert "unrelated outsider secret" not in json.dumps(pages[1])
    assert pages[0]["_request"]["args"]["cm_id"] == str(s.ids["cm"])
    refs = [next(message["message_ref"] for message in page["messages"] if message["message_id"] == "m1")
            for page in pages]
    source = run("read_message_context", {"message_ref": refs[1]})
    assert source["scope"]["cm_id"] == str(s.ids["reviewer"])
    prepared = run("draft_reply", {"event_ref": None, "message_ref": refs[1], "draft_ref": None,
        "edited_draft": None, "action": "generate", "instruction": None, "language": "en"})
    draft = run("save_reply_draft", {"draft_ref": prepared["draft_ref"], "outcome": "ready", "language": "en",
        "draft": "I will check the sample request.", "chinese_translation": "我会核查样品申请。",
        "explanation": "Uses the actual request.", "checks": [], "evidence_refs": [prepared["source_ref"]]})
    assert draft["scope"]["cm_id"] == str(s.ids["reviewer"])
    selected = tools.current_context(pg, s.ids["owner"], {
        "cm_id": str(s.ids["owner"]), "query_ref": pages[1]["query_ref"]}, s.settings)
    assert selected["scope"]["cm_id"] == str(s.ids["owner"])
    assert selected["scope"]["query_request"]["args"]["cm_id"] == str(s.ids["reviewer"])
    message_selected = tools.current_context(pg, s.ids["owner"], {
        "cm_id": str(s.ids["owner"]), "message_ref": refs[1]}, s.settings)
    assert message_selected["scope"]["cm_id"] == str(s.ids["owner"])
    final = {"answer": "Both people saw this sample request.", "event_refs": [], "result_refs": [],
             "data_refs": [page["query_ref"] for page in pages], "draft_ref": draft["draft_ref"],
             "sections": [{"title": "Samples", "summary": "A request shared by both people.", "message_refs": refs}]}
    view = tools.build_result(pg, s.ids["owner"], turn, final, s.settings, context, results)
    assert view["scope"]["cm_id"] == str(s.ids["owner"])
    assert view["scope"]["cm_names"] == ["Jenette", "Janette"]
    assert len(view["sections"][0]["sources"]) == 1
    assert view["sections"][0]["sources"][0]["cm_name"] == "Jenette"
    assert context["scope"]["cm_id"] == str(s.ids["owner"])
    with pytest.raises(PermissionError):
        tools.execute(pg, s.ids["cm"], turn, "summarize_messages", {
            **args, "cm_id": str(s.ids["reviewer"])}, s.settings, own)
    with pytest.raises(PermissionError):
        tools.execute(pg, s.ids["cm"], turn, "resolve_staff", {"query": "Janette"}, s.settings, own)
    with pytest.raises(PermissionError):
        tools.context_for_scope(pg, s.ids["cm"], own, pages[1]["scope"])
    for sql, values in [
        ("UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s", (s.ids["owner"],)),
        ("UPDATE authorizations SET body_consent_at=NULL WHERE id=%s", (s.ids["reviewer"],)),
        ("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s", (s.ids["reviewer"],)),
    ]:
        with pg.transaction(force_rollback=True):
            pg.execute(sql, values)
            for read in (
                lambda: tools.validate_tool_results(pg, s.ids["owner"], turn, results, s.settings, context),
                lambda: tools.evidence(pg, s.ids["owner"], refs[1], s.settings),
                lambda: tools.reply_status(pg, s.ids["owner"], draft["draft_ref"], s.settings),
            ):
                with pytest.raises((PermissionError, ValueError, summaries.SummaryInputError)):
                    read()


def test_creator_snapshot_keeps_all_pages_stable_and_live_sources_protected(pg, seeded):  # noqa: F811
    from app import creator_importance_facts

    s = seeded
    ids = [s.ids["person"]]
    for index in range(61):
        person_id = uuid4()
        ids.append(person_id)
        pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,%s)",
                   (person_id, f"Creator {index:02}"))
        pg.execute("INSERT INTO contact_roles(contact_id,role,status,source) "
                   "VALUES(%s,'creator','confirmed','manual')", (person_id,))
        pg.execute("INSERT INTO lark_identities(open_id,contact_id,display_name) VALUES(%s,%s,%s)",
                   (f"ou_snapshot_{index}", person_id, f"Creator {index:02}"))
        pg.execute("INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) "
                   "VALUES(%s,%s,%s,'manual')", (uuid4(), person_id, s.ids["cm"]))
    pg.execute("UPDATE chat_messages SET sender_type='user',body_fingerprint=%s WHERE message_id='m1'",
               ("1" * 64,))
    source = pg.execute("""SELECT jsonb_build_object('chat_id',m.chat_id,'message_id',m.message_id,
        'consent_at',a.body_consent_at,'body_fingerprint',m.body_fingerprint,
        'sender_open_id',m.sender_open_id,'message_type',m.message_type,'created_at',m.message_create_time,
        'parent_id',m.parent_id,'root_id',m.root_id,'thread_id',m.thread_id,
        'mentions_fingerprint',md5(m.mentions_json::text)) source
        FROM chat_messages m JOIN authorizations a ON a.id=%s WHERE m.message_id='m1'""",
        (s.ids["cm"],)).fetchone()["source"]
    for person in ids:
        pg.execute("""INSERT INTO creator_importance_profiles(contact_id,responsible_authorization_id,
            manual_tier,auto_valid,prompt_version,sources) VALUES(%s,%s,'normal',TRUE,%s,%s::jsonb)""",
            (person, s.ids["cm"], creator_importance_facts.PROMPT_VERSION,
             json.dumps([source] if person == s.ids["person"] else [])))
    session, turn_id = uuid4(), uuid4()
    pg.execute("INSERT INTO assistant_sessions(id,actor_id) VALUES(%s,%s)", (session, s.ids["cm"]))
    pg.execute("INSERT INTO assistant_turns(id,session_id,client_request_id,input_ciphertext) "
               "VALUES(%s,%s,%s,'fixture')", (turn_id, session, uuid4()))
    context = tools.current_context(pg, s.ids["cm"], {}, s.settings)
    for current, yesterday_start, yesterday_end, week_start, previous_start, same_time in [
        ("2026-10-13T01:23:00+01:00", "2026-10-12T00:00:00+01:00", "2026-10-13T00:00:00+01:00",
         "2026-10-12T00:00:00+01:00", "2026-10-05T00:00:00+01:00", "2026-10-06T01:23:00+01:00"),
        ("2026-03-30T01:23:00+01:00", "2026-03-29T00:00:00+00:00", "2026-03-30T00:00:00+01:00",
         "2026-03-30T00:00:00+01:00", "2026-03-23T00:00:00+00:00", "2026-03-23T01:23:00+00:00"),
        ("2026-10-26T01:23:00+00:00", "2026-10-25T00:00:00+01:00", "2026-10-26T00:00:00+00:00",
         "2026-10-26T00:00:00+00:00", "2026-10-19T00:00:00+01:00", "2026-10-19T01:23:00+01:00"),
    ]:
        timed = tools.current_context(pg, s.ids["cm"], {}, s.settings, now=datetime.fromisoformat(current))
        assert timed["defaults"] == {
            "analysis_start": (datetime.fromisoformat(current).astimezone(summaries.LONDON)
                               - timedelta(days=90)).isoformat(),
            "morning_start": yesterday_end, "morning_end": current,
            "yesterday_start": yesterday_start, "yesterday_end": yesterday_end,
            "week_start": week_start, "previous_week_start": previous_start, "previous_week_end": week_start,
            "previous_week_same_time_end": same_time,
        }
        elapsed = (datetime.fromisoformat(yesterday_end).astimezone(UTC)
                   - datetime.fromisoformat(yesterday_start).astimezone(UTC))
        assert elapsed.total_seconds() / 3600 == (23 if current.startswith("2026-03")
                                                 else 25 if current.startswith("2026-10-26") else 24)
    selected = tools.current_context(pg, s.ids["cm"], {
        "audience": "creators", "creator_ids": [str(ids[0])],
        "start": s.start.isoformat(), "end": datetime.now(UTC).isoformat()}, s.settings)
    current_turn = pg.execute("SELECT * FROM assistant_turns WHERE id=%s", (turn_id,)).fetchone()
    with pytest.raises(ValueError, match="已选达人"):
        tools.execute(pg, s.ids["cm"], current_turn, "activity_stats", {
            "audience": "creators", "start": s.start.isoformat(), "end": datetime.now(UTC).isoformat(),
            "creator_ids": [str(value) for value in ids], "chat_ids": None,
            "group_by": "day", "comparison": None, "cursor": None}, s.settings, selected)
    with pytest.raises(ValueError, match="已选达人"):
        tools.execute(pg, s.ids["cm"], current_turn, "query_creators", {
            "query": "Creator 00", "importance": None, "cursor": None}, s.settings, selected)
    args = {"query": None, "importance": None, "cursor": None}
    first = assistant_queries.creators(pg, s.ids["cm"], context, args, s.settings)
    expected = json.loads(json.dumps(first["_snapshot"]["creators"]))
    assert first["total"] == 62 and len(first["creators"]) == 25
    assert first["_snapshot"]["importance_sources"] and "_snapshot" not in tools.public_query(first, for_model=True)
    pg.execute("UPDATE creator_importance_profiles SET manual_tier='focus' WHERE contact_id=%s", (ids[-1],))
    pg.execute("UPDATE creator_importance_profiles SET revision=revision+1,cutoff=NOW(),evaluated_at=NOW()")
    s.add("new-contact", at=s.start + timedelta(hours=5))
    pages = [first]
    while pages[-1]["next_cursor"] is not None:
        pages.append(assistant_queries.creators(pg, s.ids["cm"], context,
            {**args, "cursor": pages[-1]["next_cursor"]}, s.settings, snapshot=first["_snapshot"]))
    assert len(pages) == 3 and [person for page in pages for person in page["creators"]] == expected
    assert len({person["id"] for page in pages for person in page["creators"]}) == 62
    assert assistant_queries.creators(pg, s.ids["cm"], context, args, s.settings)["creators"][0]["id"] == str(ids[-1])
    for index, page in enumerate(pages):
        page["_request"] = {"name": "query_creators", "args": {**args, "cursor": str(index * 25)}}
        assistant_queries.validate_result(pg, s.ids["cm"], context, page, s.settings)
    pg.execute("UPDATE assistant_turns SET checkpoint_ciphertext=%s WHERE id=%s",
               (summary_dialogues.encode({"tool_results": pages}, s.settings), turn_id))
    saved = summary_dialogues.decode(pg.execute("SELECT checkpoint_ciphertext FROM assistant_turns WHERE id=%s",
                                                (turn_id,)).fetchone()["checkpoint_ciphertext"], s.settings)
    for page in saved["tool_results"]:
        assistant_queries.validate_result(pg, s.ids["cm"], context, page, s.settings)
    # Legacy rows cannot use a new profile version to prove evidence they never saved.
    legacy = {key: value for key, value in pages[0].items() if key != "_snapshot"}
    with pytest.raises(ValueError, match="重要度"):
        assistant_queries.validate_result(pg, s.ids["cm"], context, legacy, s.settings)
    for sql, params in [
        ("UPDATE contact_assignments SET ended_at=NOW() WHERE contact_id=%s", (ids[-1],)),
        ("UPDATE contact_roles SET status='rejected' WHERE contact_id=%s", (ids[-1],)),
        ("UPDATE lark_identities SET contact_id=NULL WHERE contact_id=%s", (ids[-1],)),
        ("UPDATE social_accounts SET normalized_handle='alice_changed',display_handle='@alice_changed' "
         "WHERE normalized_handle='alice'", ()),
        ("UPDATE authorizations SET body_consent_at=NULL WHERE id=%s", (s.ids["cm"],)),
        ("UPDATE chat_messages SET is_deleted=TRUE WHERE message_id='m1'", ()),
    ]:
        with pytest.raises((PermissionError, ValueError, summaries.SummaryInputError)), pg.transaction():
            pg.execute(sql, params)
            assistant_queries.validate_result(pg, s.ids["cm"], context, saved["tool_results"][0], s.settings)


def test_business_queries_pagination_context_and_live_grants(pg, seeded, monkeypatch):  # noqa: F811
    s = seeded
    pg.execute("INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) "
               "VALUES(%s,%s,%s,'manual')", (uuid4(), s.ids["person"], s.ids["owner"]))
    account = uuid4()
    pg.execute("INSERT INTO social_accounts(id,platform,normalized_handle,display_handle) "
               "VALUES(%s,'tiktok','alice_shop','@alice_shop')", (account,))
    pg.execute("INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source) "
               "VALUES(%s,%s,'owner','confirmed','cm_confirmation')", (s.ids["person"], account))
    s.add("original", who="cm", text="Would you like a sample?", at=s.start - timedelta(minutes=5))
    pg.execute("UPDATE chat_messages SET parent_id='original' WHERE message_id='m1'")
    s.add("reply", who="cm", text="I will check the sample.", at=s.start + timedelta(hours=2))
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id) VALUES(%s,'chat')", (s.ids["owner"],))
    pg.execute("INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id) "
               "VALUES(%s,'chat','m1')", (s.ids["owner"],))
    s.add("owner-only", text="Private owner-only sample", at=s.start + timedelta(hours=3))
    pg.execute("UPDATE authorization_message_observations SET authorization_id=%s WHERE message_id='owner-only'",
               (s.ids["owner"],))
    pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES('category','Beauty Category','group')")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id,sync_enabled) VALUES(%s,'category',FALSE)",
               (s.ids["cm"],))
    pg.execute("INSERT INTO chat_group_classifications(chat_id,purpose,source,confirmed_by_authorization_id) "
               "VALUES('category','category','manual',%s)", (s.ids["owner"],))
    session_id, turn_id = uuid4(), uuid4()
    pg.execute("INSERT INTO assistant_sessions(id,actor_id) VALUES(%s,%s)", (session_id, s.ids["cm"]))
    now = datetime.now(UTC)
    pg.execute("INSERT INTO assistant_turns(id,session_id,client_request_id,input_ciphertext,created_at) "
               "VALUES(%s,%s,%s,'fixture',%s)", (turn_id, session_id, uuid4(), now))
    turn = pg.execute("SELECT * FROM assistant_turns WHERE id=%s", (turn_id,)).fetchone()
    context = tools.current_context(pg, s.ids["cm"], {}, s.settings, now=now)
    results = []

    def run(name, args):
        value = tools.execute(pg, s.ids["cm"], turn, name, args, s.settings, context)
        results.append(value)
        turn["checkpoint_ciphertext"] = summary_dialogues.encode({"tool_results": results}, s.settings)
        pg.execute("UPDATE assistant_turns SET checkpoint_ciphertext=%s WHERE id=%s",
                   (turn["checkpoint_ciphertext"], turn_id))
        return value

    person = run("query_creators", {"query": "alice", "importance": None, "cursor": None})
    assert person["total"] == 1 and set(person["creators"][0]["handles"]) == {"@alice", "@alice_shop"}
    assert {p["id"] for p in person["creators"][0]["responsible"]} == {
        str(s.ids["cm"]), str(s.ids["owner"]),
    }
    groups = run("list_groups", {"query": None, "creator_ids": None, "purpose": "category", "cursor": None})
    assert groups["total"] == 1 and not groups["groups"][0]["sync_enabled"]
    sync = run("get_sync_status", {})
    assert sync["authorization_status"] == "active"
    model_sync = tools.public_query(sync, for_model=True)
    assert model_sync["summary"]["label"] in {"消息可查看", "部分聊天暂时无法查看"}
    assert not set(model_sync) & {"watermarks", "job", "conversations", "definition", "gaps"}
    assert "conversations" in tools.public_query(sync)
    args = {"audience": "creators", "start": s.start.isoformat(), "end": now.isoformat(),
            "creator_ids": None, "chat_ids": None,
            "group_by": "day", "comparison": "previous", "cursor": None}
    stats = run("activity_stats", args)
    assert stats["metrics"]["messages"] == 2 and stats["metrics"]["contacted_creators"] == 1
    assert stats["comparison"]["metrics"]["messages"] == 1
    assert run("activity_stats", {**args, "chat_ids": ["category"]})["metrics"]["messages"] == 0
    for start, end, minutes in [
        ("2026-10-25T00:30:00+00:00", "2026-10-25T01:30:00+00:00", 60),
        ("2026-10-25T01:20:00+00:00", "2026-10-25T01:40:00+00:00", 20),
        ("2026-03-29T01:10:00+00:00", "2026-03-29T01:30:00+00:00", 20),
    ]:
        transition = assistant_queries.activity(pg, s.ids["cm"], context, {
            **args, "start": start, "end": end,
        }, s.settings, now=datetime(2026, 10, 26, tzinfo=UTC))
        assert datetime.fromisoformat(transition["comparison"]["window"]["end"]) - datetime.fromisoformat(
            transition["comparison"]["window"]["start"]) == timedelta(minutes=minutes)
        assert transition["definition"]["day_counts"]["previous"] == minutes / 1440
    search_args = {"audience": "creators", "query": ["sample"],
                   "start": s.start.isoformat(), "end": now.isoformat(),
                   "creator_ids": None, "chat_ids": None, "cursor": None}
    monkeypatch.setattr(assistant_search, "SCAN_LIMIT", 1)
    monkeypatch.setattr(assistant_search, "PAGE_SIZE", 1)
    found, cursor = [], None
    for _ in range(5):
        page = run("search_chat_history", {**search_args, "cursor": cursor})
        found.extend(page["messages"])
        assert page["coverage"]["counts_scope"] == "this_page"
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None and {m["message_id"] for m in found} == {"m1", "reply"}
    assert all(len(message["message_ref"]) < 130 for message in found)
    ref = next(m["message_ref"] for m in found if m["message_id"] == "m1")
    original = run("read_message_context", {"message_ref": ref})
    assert any(m["text"] == "Would you like a sample?" for m in original["messages"])
    tools.validate_tool_results(pg, s.ids["cm"], turn, results, s.settings, context)
    final = {"answer": "这是当前可见资料。", "event_refs": [], "result_refs": [],
             "data_refs": [result["query_ref"] for result in results], "draft_ref": None}
    view = tools.build_result(pg, s.ids["cm"], turn, final, s.settings, context, results)
    assert {value["kind"] for value in view["data"]} == {
        "creators", "groups", "sync", "activity", "messages", "message_context",
    }
    assert not any("snapshot" in value or "_message_refs" in value for value in view["data"])
    assert "Private owner-only sample" not in json.dumps(view)
    assert tools.evidence(pg, s.ids["cm"], ref, s.settings)["message"]["text"] == "Sample please"
    with pytest.raises(LookupError):
        tools.evidence(pg, s.ids["reviewer"], ref, s.settings)
    with pytest.raises(PermissionError):
        run("search_chat_history", {**search_args, "creator_ids": [str(s.ids["other"])]})
    with pytest.raises(ValueError):
        tools.build_result(pg, s.ids["cm"], turn, {**final, "data_refs": [f"q.{uuid4()}.invented"]},
                           s.settings, context, results)

    # Full-CM material does not depend on a formal creator, contact or user sender.
    visitor = uuid4()
    pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,'Unconfirmed visitor')", (visitor,))
    pg.execute("INSERT INTO lark_identities(open_id,contact_id,display_name) "
               "VALUES('ou_unconfirmed',%s,'Unconfirmed visitor'),('ou_unlinked',NULL,'Unlinked visitor')",
               (visitor,))
    for chat, visible, enabled in [("business", True, True), ("hidden", False, True),
                                   ("disabled", True, False)]:
        pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES(%s,%s,'group')", (chat, chat))
        pg.execute("""INSERT INTO authorization_chats(authorization_id,chat_id,is_visible,sync_enabled,
            backfill_complete,backfill_window_start_at,last_incremental_at,sync_status)
            VALUES(%s,%s,%s,%s,TRUE,%s,%s,'succeeded')""",
            (s.ids["cm"], chat, visible, enabled, s.start - timedelta(days=9), s.end))

    def all_message(message_id, chat, sender, sender_type="user", *, before_consent=False):
        pg.execute("""INSERT INTO chat_messages(chat_id,message_id,sender_open_id,sender_id,sender_id_type,
            sender_type,message_type,body_ciphertext,message_create_time)
            VALUES(%s,%s,%s,%s,%s,%s,'text',%s,%s)""",
            (chat, message_id, sender, sender or "cli_test_bot", "open_id" if sender else "app_id",
             sender_type, s.cipher.encrypt(json.dumps({"text": "All-scope " + message_id})),
             s.start + timedelta(hours=4)))
        observed = pg.execute("SELECT body_consent_at FROM authorizations WHERE id=%s",
                              (s.ids["cm"],)).fetchone()["body_consent_at"] - timedelta(seconds=1) \
            if before_consent else now
        pg.execute("""INSERT INTO authorization_message_observations(authorization_id,chat_id,message_id,observed_at)
            VALUES(%s,%s,%s,%s)""", (s.ids["cm"], chat, message_id, observed))

    all_message("all-unconfirmed", "business", "ou_unconfirmed")
    all_message("all-no-contact", "business", "ou_unlinked")
    all_message("all-app", "business", None, "app")
    all_message("all-before-consent", "business", "ou_unconfirmed", before_consent=True)
    all_message("all-hidden", "hidden", "ou_unconfirmed")
    all_message("all-disabled", "disabled", "ou_unconfirmed")
    monkeypatch.setattr(assistant_search, "WINDOW_PAGE_SIZE", 2)
    tables = ("contacts", "lark_identities", "contact_roles", "contact_assignments", "contact_social_accounts",
              "creator_account_assignments", "offhours_summary_runs")
    before = {table: pg.execute(f"SELECT count(*) n FROM {table}").fetchone()["n"] for table in tables}
    all_args = {"audience": "all", "start": s.start.isoformat(), "end": now.isoformat(),
                "creator_ids": None, "chat_ids": None, "cursor": None}
    with pytest.raises(ValueError):
        run("summarize_messages", {key: value for key, value in all_args.items() if key != "audience"})
    all_found, all_pages, cursor = [], [], None
    for _ in range(12):
        page = run("summarize_messages", {**all_args, "cursor": cursor})
        all_pages.append(page)
        all_found.extend(page["messages"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    expected = {"m1", "outside", "reply", "all-unconfirmed", "all-no-contact", "all-app"}
    assert cursor is None and len(all_pages) > 1 and {message["message_id"] for message in all_found} == expected
    assert len(all_found) == len(expected) and all(len(message["message_ref"]) < 130 for message in all_found)
    app_ref = next(message["message_ref"] for message in all_found if message["message_id"] == "all-app")
    assert tools.evidence(pg, s.ids["cm"], app_ref, s.settings)["message"]["text"] == "All-scope all-app"
    run("read_message_context", {"message_ref": app_ref})
    all_search_args = {**all_args, "query": ["All-scope"]}
    search_found, cursor = [], None
    for _ in range(12):
        page = run("search_chat_history", {**all_search_args, "cursor": cursor})
        search_found.extend(page["messages"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None and {message["message_id"] for message in search_found} == {
        "all-unconfirmed", "all-no-contact", "all-app",
    }
    all_stats_args = {**all_args, "group_by": "day", "comparison": None}
    assert run("activity_stats", all_stats_args)["metrics"]["messages"] == 6
    assert run("activity_stats", args)["metrics"]["messages"] == 2
    # An unconfirmed contact can receive a draft from owned text, including complete latest reply ancestors.
    visitor_ref = next(message["message_ref"] for message in all_found if message["message_id"] == "all-unconfirmed")
    page = next(page for page in all_pages if visitor_ref in page["_message_refs"])
    continuation_ref = f"u.{page['query_ref'][2:]}.{page['_message_refs'][visitor_ref]}"
    assert len(continuation_ref) <= 2048
    assert tools.evidence(pg, s.ids["cm"], continuation_ref, s.settings)["message"]["text"] == (
        "All-scope all-unconfirmed")
    all_message("latest-parent", "business", "ou_cm")
    pg.execute("UPDATE chat_messages SET message_create_time=%s WHERE message_id='latest-parent'",
               (s.start - timedelta(minutes=5),))
    all_message("latest-answer", "business", "ou_unconfirmed")
    pg.execute("UPDATE chat_messages SET message_create_time=%s,parent_id='latest-parent' "
               "WHERE message_id='latest-answer'", (datetime.now(UTC) - timedelta(microseconds=1),))
    # Latest context has its own fixed cutoff and real references for every quoted follow-up, not the old target.
    with pg.transaction(force_rollback=True):
        latest_session, latest_turn_id, latest_cutoff = uuid4(), uuid4(), datetime.now(UTC)
        pg.execute("INSERT INTO assistant_sessions(id,actor_id) VALUES(%s,%s)", (latest_session, s.ids["cm"]))
        pg.execute("INSERT INTO assistant_turns(id,session_id,client_request_id,input_ciphertext,created_at) "
                   "VALUES(%s,%s,%s,'fixture',%s)", (latest_turn_id, latest_session, uuid4(), latest_cutoff))
        latest_turn = pg.execute("SELECT * FROM assistant_turns WHERE id=%s", (latest_turn_id,)).fetchone()
        latest = tools.execute(pg, s.ids["cm"], latest_turn, "read_message_context", {
            "message_ref": continuation_ref, "mode": "latest"}, s.settings, context)
        latest_turn["checkpoint_ciphertext"] = summary_dialogues.encode({"tool_results": [latest]}, s.settings)
        pg.execute("UPDATE assistant_turns SET checkpoint_ciphertext=%s WHERE id=%s",
                   (latest_turn["checkpoint_ciphertext"], latest_turn_id))
        assert latest["message_ref"] == latest["message"]["message_ref"] == continuation_ref
        assert latest["mode"] == "latest" and latest["checked_at"] == latest_cutoff.isoformat()
        assert latest["latest_window"]["start"] == now.isoformat() and not latest["is_full_conversation"]
        new_ref = next(message["message_ref"] for message in latest["messages"]
                       if message["message_id"] == "latest-answer")
        assert new_ref != continuation_ref and new_ref in latest["_message_refs"]
        exact = tools.evidence(pg, s.ids["cm"], new_ref, s.settings)
        assert exact["message"]["message_id"] == "latest-answer"
        assert exact["message"]["text"] == "All-scope latest-answer"
        assert "latest-answer" not in {message["message_id"] for message in
                                        tools.evidence(pg, s.ids["cm"], continuation_ref, s.settings)["messages"]}
        all_message("after-latest-cutoff", "business", "ou_unconfirmed")
        pg.execute("UPDATE chat_messages SET message_create_time=%s WHERE message_id='after-latest-cutoff'",
                   (latest_cutoff + timedelta(microseconds=1),))
        tools.validate_tool_results(pg, s.ids["cm"], latest_turn, [latest], s.settings, context)
        assert "after-latest-cutoff" not in json.dumps(latest)
        with pytest.raises(LookupError):
            tools.evidence(pg, s.ids["reviewer"], new_ref, s.settings)
        pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE authorization_id=%s AND chat_id='business'",
                   (s.ids["cm"],))
        with pytest.raises(PermissionError):
            tools.validate_tool_results(pg, s.ids["cm"], latest_turn, [latest], s.settings, context)
        with pytest.raises(PermissionError):
            tools.evidence(pg, s.ids["cm"], new_ref, s.settings)
        pg.execute("UPDATE authorization_chats SET is_visible=TRUE WHERE authorization_id=%s AND chat_id='business'",
                   (s.ids["cm"],))
        pg.execute("UPDATE chat_messages SET body_ciphertext=%s WHERE message_id='latest-answer'",
                   (s.cipher.encrypt(json.dumps({"text": "Changed follow-up"})),))
        with pytest.raises(ValueError, match="已变化"):
            tools.validate_tool_results(pg, s.ids["cm"], latest_turn, [latest], s.settings, context)
        with pytest.raises(ValueError, match="已更新"):
            tools.evidence(pg, s.ids["cm"], new_ref, s.settings)
    calls_before = len(s.calls)
    prepared = run("draft_reply", {"event_ref": None, "message_ref": continuation_ref, "draft_ref": None,
        "edited_draft": None, "action": "generate", "instruction": None, "language": "en"})
    assert {message["message_id"] for message in prepared["context"]["messages"]} >= {
        "all-unconfirmed", "latest-answer", "latest-parent"}
    assert prepared["context"]["chat_url"].endswith("openChatId=business")
    assert "unrelated outsider secret" not in json.dumps(prepared)
    model_context = tools.public_query(prepared, for_model=True)["context"]
    assert all("message_ref" not in item and "evidence_ref" not in item
               for item in [model_context["message"], *model_context["messages"]])
    assert prepared["context"]["message"]["message_ref"] == continuation_ref
    draft_args = {"draft_ref": prepared["draft_ref"], "outcome": "ready", "language": "en",
        "draft": "I'll check and get back to you.", "chinese_translation": "我确认后回复你。",
        "explanation": "不编造尚未确认的进度。", "checks": ["确认能执行后续核查。"],
        "evidence_refs": [prepared["context"]["messages"][0]["ref"]]}
    draft = run("save_reply_draft", draft_args)
    source_draft = run("save_reply_draft", {**draft_args, "evidence_refs": [continuation_ref]})
    assert source_draft["draft"]["evidence"] and all(
        (item["chat_id"], item["message_id"]) == ("business", "all-unconfirmed")
        for item in source_draft["draft"]["evidence"])
    with pytest.raises(ValueError, match="依据"):
        run("save_reply_draft", {**draft_args, "evidence_refs": [app_ref]})
    assert not tools.reply_status(pg, s.ids["cm"], draft["draft_ref"], s.settings)["new_messages"]
    rendered = tools.build_result(pg, s.ids["cm"], turn, {
        "answer": "回复草稿已准备。", "event_refs": [], "result_refs": [], "data_refs": [],
        "draft_ref": draft["draft_ref"]}, s.settings, context, [prepared, draft])
    assert rendered["scope"]["audience"] == "all" and rendered["draft"]["checks"]
    assert rendered["draft"]["draft_ref"] == rendered["draft_ref"]
    with pytest.raises(PermissionError):
        tools.reply_status(pg, s.ids["reviewer"], draft["draft_ref"], s.settings)
    with pytest.raises(ValueError):
        run("save_reply_draft", {**draft_args, "evidence_refs": ["R-not-owned"]})
    edited = "I'll check the dispatch status."
    translation = run("draft_reply", {"event_ref": None, "message_ref": None, "draft_ref": draft["draft_ref"],
        "edited_draft": edited, "action": "translate", "instruction": None, "language": "en"})
    translated = run("save_reply_draft", {**draft_args, "draft_ref": translation["draft_ref"],
        "draft": "MODEL MUST NOT REPLACE EDIT", "chinese_translation": "我会查询发货状态。",
        "evidence_refs": [translation["context"]["messages"][0]["ref"]]})
    assert translated["draft"]["draft"] == edited and translated["draft"]["chinese_translation"] == "我会查询发货状态。"
    all_message("after-draft", "business", "ou_unconfirmed")
    pg.execute("UPDATE chat_messages SET message_create_time=%s WHERE message_id='after-draft'",
               (datetime.now(UTC) - timedelta(microseconds=1),))
    assert tools.reply_status(pg, s.ids["cm"], draft["draft_ref"], s.settings)["new_messages"]
    assert len(s.calls) == calls_before
    assert pg.execute("SELECT count(*) n FROM summary_reply_tasks").fetchone()["n"] == 0
    selected_scope = tools.current_context(pg, s.ids["cm"], {
        "start": s.start.isoformat(), "end": now.isoformat(), "message_ref": visitor_ref}, s.settings)
    assert selected_scope["scope"]["chat_ids"] == ["business"]
    with pytest.raises(PermissionError, match="已选会话"):
        tools.current_context(pg, s.ids["cm"], {
            "message_ref": visitor_ref, "chat_ids": []}, s.settings)
    bounded = tools.execute(pg, s.ids["cm"], turn, "activity_stats", all_stats_args, s.settings, selected_scope)
    assert bounded["scope"]["chat_ids"] == ["business"]
    with pytest.raises(ValueError, match="已选会话"):
        tools.execute(pg, s.ids["cm"], turn, "activity_stats", {**all_stats_args, "chat_ids": ["chat", "business"]},
                      s.settings, selected_scope)
    assert tools.current_context(pg, s.ids["cm"], selected_scope["scope"], s.settings)["scope"]["window"] \
        == selected_scope["scope"]["window"]
    pg.execute("UPDATE chat_messages SET is_deleted=TRUE WHERE message_id='latest-parent'")
    with pytest.raises(ValueError):
        tools.reply_status(pg, s.ids["cm"], draft["draft_ref"], s.settings)
    pg.execute("UPDATE chat_messages SET is_deleted=FALSE WHERE message_id='latest-parent'")
    pg.execute("UPDATE authorization_chats SET sync_status='not_readable' WHERE authorization_id=%s AND chat_id='chat'",
               (s.ids["cm"],))
    assert assistant_queries.activity(pg, s.ids["cm"], context, args, s.settings)["metrics"]["messages"] == 0
    pg.execute("UPDATE authorization_chats SET sync_status='succeeded' WHERE authorization_id=%s AND chat_id='chat'",
               (s.ids["cm"],))
    # A completed unbounded backfill has no start; a new sync round does not erase that coverage.
    for complete, history_start, status, watermark, expected_gap in [
        (True, None, "syncing", s.end, None),
        (False, None, "pending", s.end, "历史"),
        (True, s.start + timedelta(minutes=1), "succeeded", s.end, "历史"),
        (True, None, "not_readable", s.end, "Lark未允许读取"),
        (True, None, "syncing", s.start + timedelta(hours=1), "截止"),
    ]:
        pg.execute("""UPDATE authorization_chats SET backfill_complete=%s,backfill_window_start_at=%s,
            sync_status=%s,last_incremental_at=%s,last_backfill_at=NULL
            WHERE authorization_id=%s AND chat_id='business'""",
            (complete, history_start, status, watermark, s.ids["cm"]))
        scoped = {**all_args, "chat_ids": ["business"]}
        material = assistant_search.window_messages(pg, s.ids["cm"], context, scoped, s.settings)
        activity = assistant_queries.activity(pg, s.ids["cm"], context, {
            **scoped, "group_by": "day", "comparison": None,
        }, s.settings)
        for value in (material, activity):
            if expected_gap is None:
                assert value["gaps"] == []
            else:
                expected_reasons = ("截止", "最新消息仍在同步") if expected_gap == "截止" else (expected_gap,)
                assert any(reason in gap["reason"] for reason in expected_reasons for gap in value["gaps"])
            assert value.get("total_messages", value.get("metrics", {}).get("messages")) == (
                0 if status == "not_readable" else 3)
        assert material["sources"][0]["source_complete"] == (expected_gap is None)
        assert material["coverage"]["source_complete"] == (expected_gap is None)
    gaps = [{"chat_id": f"gap-{index}", "reason": "历史回填尚未完成"} for index in range(30)]
    gaps += [{"chat_id": "gap-0", "reason": "历史回填尚未完成"},
             {"chat_id": "gap-0", "reason": "同步水位未覆盖请求截止"},
             {"reason": "本页存在不可读正文或附件", "count": 6}]
    for for_model, sample_size in [(True, 8), (False, 25)]:
        public = tools.public_query({**material, "gaps": gaps}, for_model=for_model)
        assert public["gap_count"] == 33 and public["gap_conversation_count"] == 30
        assert len(public["gaps"]) == sample_size
        assert public["gap_summary"] == [
            {"reason": "历史回填尚未完成", "conversations": 30, "items": 0, "names": []},
            {"reason": "同步水位未覆盖请求截止", "conversations": 1, "items": 0, "names": ["未命名会话"]},
            {"reason": "本页存在不可读正文或附件", "conversations": 0, "items": 6, "names": []},
        ]
    pg.execute("""UPDATE authorization_chats SET sync_status='not_readable',
        last_error='群开启受限模式，Lark禁止读取消息（231203）' WHERE authorization_id=%s AND chat_id='business'""",
        (s.ids["cm"],))
    blocked = run("summarize_messages", {**all_args, "chat_ids": ["business"]})
    restricted = tools.public_query(blocked)
    assert restricted["gap_summary"] == [{"reason": "群开启受限模式，Lark禁止读取消息（231203）",
                                           "conversations": 1, "items": 0, "names": ["business"]}]
    assert blocked["messages"] == []
    assert assistant_search.unreadable_reason({"last_error": "Lark暂不允许读取此会话消息（错误码 230001）"}) == (
        "Lark暂不允许读取此会话消息（错误码 230001）")
    assert assistant_search.unreadable_reason({"last_error": "private provider response"}) == "Lark未允许读取"
    results.remove(blocked)
    pg.execute("""UPDATE authorization_chats SET backfill_complete=TRUE,backfill_window_start_at=%s,
        sync_status='succeeded',last_incremental_at=%s,last_error=NULL
        WHERE authorization_id=%s AND chat_id='business'""",
        (s.start - timedelta(days=9), s.end, s.ids["cm"]))
    # Refresh saved coverage only; source text, counts and the paid answer stay unchanged.
    saved = all_pages[0]
    saved["gaps"] = [{"chat_id": "business", "reason": "历史同步未覆盖检索起点"},
                     {"reason": "本页存在不可读正文或附件", "count": 1}]
    saved["coverage"]["source_complete"] = False
    original_fields = json.dumps({key: saved[key] for key in (
        "messages", "total_messages", "total_conversations", "query_ref", "snapshot", "checked_at")})
    saved_final = {**final, "answer": "历史回答保持原样。", "data_refs": [saved["query_ref"]]}
    repaired = tools.build_result(pg, s.ids["cm"], turn, saved_final, s.settings, context, [saved])
    assert repaired["answer"] == saved_final["answer"] and saved["_coverage_refreshed"]
    assert repaired["data"][0]["gaps"] == [{"reason": "本页存在不可读正文或附件", "count": 1}]
    assert repaired["data"][0]["coverage"]["source_complete"] is False
    assert json.dumps({key: saved[key] for key in (
        "messages", "total_messages", "total_conversations", "query_ref", "snapshot", "checked_at")}) == original_fields
    assert "_coverage_refreshed" not in repaired["data"][0]
    saved_stats = run("activity_stats", all_stats_args)
    saved_stats["gaps"].append({"chat_id": "business", "reason": "历史同步未覆盖检索起点"})
    metrics = dict(saved_stats["metrics"])
    tools.validate_tool_results(pg, s.ids["cm"], turn, [saved_stats], s.settings, context)
    assert saved_stats["metrics"] == metrics and saved_stats["_coverage_refreshed"]
    assert not any(gap.get("chat_id") == "business" for gap in saved_stats["gaps"])
    pinned_context = tools.current_context(pg, s.ids["cm"], {
        "creator_ids": [str(s.ids["person"])], "chat_ids": ["category"], "audience": "creators",
    }, s.settings)
    unpinned = tools.execute(pg, s.ids["cm"], turn, "summarize_messages", all_args, s.settings, pinned_context)
    assert unpinned["total_messages"] == 6 and unpinned["scope"]["chat_ids"] is None
    message_context = tools.current_context(pg, s.ids["cm"], {"message_ref": visitor_ref}, s.settings)
    unpinned_message = tools.execute(pg, s.ids["cm"], turn, "summarize_messages", all_args,
                                    s.settings, message_context)
    assert unpinned_message["scope"]["chat_ids"] is None and "message_ref" not in unpinned_message["scope"]
    changed_time = tools.current_context(pg, s.ids["cm"], {
        **unpinned_message["scope"], "start": all_args["start"], "end": all_args["end"]}, s.settings)
    assert changed_time["scope"].get("chat_ids") is None and "message_ref" not in changed_time["scope"]
    assert assistant_queries.activity(pg, s.ids["cm"], pinned_context, all_stats_args, s.settings)[
        "metrics"]["messages"] == 6
    default_args = {key: value for key, value in all_args.items() if key != "audience"}
    assert assistant_search.window_messages(pg, s.ids["cm"], context, default_args, s.settings)[
        "total_messages"] == 6
    assert assistant_queries.activity(pg, s.ids["cm"], context, {
        **default_args, "group_by": "day", "comparison": None,
    }, s.settings)["metrics"]["messages"] == 6
    default_search, cursor = [], None
    for _ in range(12):
        page = assistant_search.query(pg, s.ids["cm"], context, {
            **default_args, "query": ["All-scope"], "cursor": cursor,
        }, s.settings)
        default_search.extend(page["messages"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None and {message["message_id"] for message in default_search} == {
        "all-unconfirmed", "all-no-contact", "all-app",
    }
    empty = run("summarize_messages", {**all_args, "start": (s.start + timedelta(hours=6)).isoformat(),
                                       "end": (s.start + timedelta(hours=7)).isoformat()})
    assert empty["messages"] == [] and empty["next_cursor"] is None and empty["gaps"] == []
    pg.execute("INSERT INTO admin_page_permissions(authorization_id,permission) VALUES(%s,'creator-manage')",
               (s.ids["reviewer"],))
    with pytest.raises(PermissionError):
        tools.current_context(pg, s.ids["reviewer"], {"cm_id": str(s.ids["cm"])}, s.settings)
    for name in ("reviewer", "tester"):
        for audience in ("creators", "all"):
            with pytest.raises(PermissionError):
                tools.current_context(pg, s.ids[name], {
                    "cm_id": str(s.ids["cm"]), "audience": audience,
                }, s.settings)
            forged = {**context, "scope": {**context["scope"], "audience": audience}}
            with pytest.raises(PermissionError):
                tools.execute(pg, s.ids[name], turn, "activity_stats", {
                    **all_stats_args, "audience": audience}, s.settings, forged)
            with pytest.raises(PermissionError):
                assistant_search.window_messages(pg, s.ids[name], forged, {
                    **all_args, "audience": audience}, s.settings)
            with pytest.raises(PermissionError):
                assistant_queries.creators(pg, s.ids[name], forged, {
                    "query": None, "importance": None, "cursor": None}, s.settings)

    # Old references remain actor-owned after an Owner downgrade, but no longer allow another CM's text.
    owner_session, owner_turn_id = uuid4(), uuid4()
    pg.execute("INSERT INTO assistant_sessions(id,actor_id) VALUES(%s,%s)", (owner_session, s.ids["owner"]))
    pg.execute("INSERT INTO assistant_turns(id,session_id,client_request_id,input_ciphertext,created_at) "
               "VALUES(%s,%s,%s,'fixture',%s)", (owner_turn_id, owner_session, uuid4(), now))
    owner_turn = pg.execute("SELECT * FROM assistant_turns WHERE id=%s", (owner_turn_id,)).fetchone()
    owner_page = tools.execute(pg, s.ids["owner"], owner_turn, "search_chat_history", search_args,
                               s.settings, context)
    owner_ref = owner_page["messages"][0]["message_ref"]
    owner_turn["checkpoint_ciphertext"] = summary_dialogues.encode({"tool_results": [owner_page]}, s.settings)
    pg.execute("UPDATE assistant_turns SET checkpoint_ciphertext=%s WHERE id=%s",
               (owner_turn["checkpoint_ciphertext"], owner_turn_id))
    owner_draft = tools.execute(pg, s.ids["owner"], owner_turn, "draft_reply", {
        "event_ref": None, "message_ref": owner_ref, "draft_ref": None, "edited_draft": None,
        "action": "generate", "instruction": None, "language": "en"}, s.settings, context)
    owner_turn["checkpoint_ciphertext"] = summary_dialogues.encode({
        "tool_results": [owner_page, owner_draft]}, s.settings)
    pg.execute("UPDATE assistant_turns SET checkpoint_ciphertext=%s WHERE id=%s",
               (owner_turn["checkpoint_ciphertext"], owner_turn_id))
    assert tools.evidence(pg, s.ids["owner"], owner_ref, s.settings)["message"]["text"]
    pg.execute("INSERT INTO admin_page_permissions(authorization_id,permission) VALUES(%s,'creator-manage')",
               (s.ids["owner"],))
    pg.execute("UPDATE admin_roles SET is_active=FALSE WHERE authorization_id=%s", (s.ids["owner"],))
    with pytest.raises(PermissionError):
        tools.evidence(pg, s.ids["owner"], owner_ref, s.settings)
    with pytest.raises(PermissionError):
        tools.validate_tool_results(pg, s.ids["owner"], owner_turn, [owner_page], s.settings, context)
    with pytest.raises(PermissionError):
        tools._validate_reply_material(pg, s.ids["owner"], owner_draft, s.settings, context, turn=owner_turn)
    pg.execute("UPDATE admin_roles SET is_active=TRUE WHERE authorization_id=%s", (s.ids["owner"],))
    all_final = {"answer": "当前授权全量消息包含未入库人员和应用通知。", "event_refs": [], "result_refs": [],
                 "data_refs": [result["query_ref"] for result in results if result.get("query_ref")], "draft_ref": None}
    all_view = tools.build_result(pg, s.ids["cm"], turn, all_final, s.settings, context, results)
    for blocked in ("Private owner-only sample", "all-before-consent", "all-hidden", "all-disabled"):
        assert blocked not in json.dumps(all_view)
    assert before == {table: pg.execute(f"SELECT count(*) n FROM {table}").fetchone()["n"] for table in tables}
    assert before["offhours_summary_runs"] == 0
    # Locate a merchant or unconfirmed person by contact/Lark names and aliases, only within this CM's directory.
    with pg.transaction(force_rollback=True):
        for index in range(3):
            person_id, open_id = uuid4(), f"ou_supplier_{index}"
            pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,%s)", (person_id, f"Supplier {index}"))
            if index == 0:
                pg.execute("INSERT INTO contact_roles(contact_id,role,status,source) "
                           "VALUES(%s,'merchant_contact','confirmed','manual')", (person_id,))
            pg.execute("INSERT INTO lark_identities(open_id,contact_id,display_name,name,en_name) "
                       "VALUES(%s,%s,%s,%s,%s)", (open_id, person_id, f"Store {index}", "店主", f"Sam {index}"))
            pg.execute("INSERT INTO contact_identifiers(id,contact_id,kind,normalized_value,display_value) "
                       "VALUES(%s,%s,'alias','supplier sam','Supplier Sam')", (uuid4(), person_id))
            pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES(%s,%s,'p2p')",
                       (f"supplier-{index}", f"Direct {index}"))
            pg.execute("""INSERT INTO authorization_chats(authorization_id,chat_id,p2p_counterpart_type,
                p2p_counterpart_open_id) VALUES(%s,%s,'user',%s)""",
                (s.ids["cm"] if index < 2 else s.ids["owner"], f"supplier-{index}", open_id))
        pg.execute("INSERT INTO chats(chat_id,name,chat_mode) VALUES('supplier-group','Warehouse operations','group')")
        pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id,sync_enabled) "
                   "VALUES(%s,'supplier-group',FALSE)", (s.ids["cm"],))
        pg.execute("INSERT INTO chat_members(chat_id,open_id) VALUES('supplier-group','ou_supplier_0')")
        directory_args = {"query": "Supplier Sam", "kind": "all", "cursor": None}
        directory = tools.execute(pg, s.ids["cm"], turn, "find_conversations", directory_args, s.settings, context)
        assert directory["kind"] == "conversations" and directory["total"] == 3
        assert {item["id"] for item in directory["conversations"]} == {"supplier-0", "supplier-1", "supplier-group"}
        assert directory["scope"]["audience"] == "all" and "chat_ids" not in directory["scope"]
        assert all(item["matched_people"] for item in directory["conversations"])
        assert not next(item for item in directory["conversations"] if item["id"] == "supplier-group")["readable"]
        tools.validate_tool_results(pg, s.ids["cm"], turn, [directory], s.settings, context)
        owner_directory = tools.execute(pg, s.ids["owner"], turn, "find_conversations", directory_args,
                                        s.settings, context)
        assert owner_directory["conversations"] == directory["conversations"]
        with monkeypatch.context() as patch:
            patch.setattr(assistant_queries, "PAGE_SIZE", 1)
            first = tools.execute(pg, s.ids["cm"], turn, "find_conversations", {
                **directory_args, "kind": "p2p"}, s.settings, context)
            second = tools.execute(pg, s.ids["cm"], turn, "find_conversations", {
                **directory_args, "kind": "p2p", "cursor": first["next_cursor"]}, s.settings, context)
            assert first["total"] == second["total"] == 2 and second["next_cursor"] is None
            assert first["conversations"][0]["id"] != second["conversations"][0]["id"]
        for query, kind, expected in [("Sam", "p2p", 2), ("Supplier 0", "all", 2),
                                      ("Store 1", "p2p", 1), ("Warehouse", "group", 1)]:
            found = tools.execute(pg, s.ids["cm"], turn, "find_conversations", {
                **directory_args, "query": query, "kind": kind}, s.settings, context)
            assert found["total"] == expected
        with pytest.raises(PermissionError):
            tools.execute(pg, s.ids["reviewer"], turn, "find_conversations", directory_args, s.settings, context)
        pg.execute("UPDATE chats SET chat_status='dissolved' WHERE chat_id='supplier-0'")
        pg.execute("UPDATE authorization_chats SET is_visible=FALSE WHERE chat_id='supplier-1'")
        assert tools.execute(pg, s.ids["cm"], turn, "find_conversations", directory_args,
                             s.settings, context)["total"] == 1
        with pytest.raises(ValueError):
            tools.validate_tool_results(pg, s.ids["cm"], turn, [directory], s.settings, context)
    pg.execute("UPDATE chat_messages SET is_deleted=TRUE WHERE message_id='m1'")
    with pytest.raises((PermissionError, ValueError)):
        tools.build_result(pg, s.ids["cm"], turn, final, s.settings, context, results)
    with pytest.raises((PermissionError, ValueError)):
        tools.evidence(pg, s.ids["cm"], ref, s.settings)
    for index in range(26):
        person_id = uuid4()
        pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,%s)", (person_id, f"Paged Creator {index:02}"))
        pg.execute("INSERT INTO contact_roles(contact_id,role,status,source) VALUES(%s,'creator','confirmed','manual')",
                   (person_id,))
        pg.execute("INSERT INTO lark_identities(open_id,contact_id,display_name) VALUES(%s,%s,%s)",
                   (f"ou_paged_{index}", person_id, f"Paged Creator {index:02}"))
        pg.execute("INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source) "
                   "VALUES(%s,%s,%s,'manual')", (uuid4(), person_id, s.ids["cm"]))
    named_args = {"query": "Paged Creator", "importance": None, "cursor": None}
    named = run("query_creators", named_args)
    assert named["total"] == 26 and named["scope"]["creator_ids"] is None
    new_person = tools.execute(pg, s.ids["cm"], turn, "query_creators", {
        "query": "Paged Creator 00", "importance": None, "cursor": None}, s.settings, message_context)
    assert new_person["total"] == 1 and not any(key in new_person["scope"] for key in (
        "event_ref", "message_ref", "chat_ids", "chat_names"))
    assert tools.current_context(pg, s.ids["cm"], new_person["scope"], s.settings)["scope"]["creator_ids"] \
        == new_person["scope"]["creator_ids"]
    continuation = tools.current_context(pg, s.ids["cm"], {
        **named["scope"], "query_ref": named["query_ref"], "cursor": named["next_cursor"],
    }, s.settings)
    last_page = tools.execute(pg, s.ids["cm"], turn, "query_creators", {
        **named_args, "cursor": named["next_cursor"],
    }, s.settings, continuation)
    assert last_page["total"] == 26 and len(last_page["creators"]) == 1 and last_page["next_cursor"] is None


def test_five_tools_keep_reports_private_resume_and_recheck_sources(pg, seeded, monkeypatch):  # noqa: F811
    s = seeded
    s.save(False)
    s.add("question", who="cm", text="Would you like a sample?", at=s.start - timedelta(minutes=2))
    pg.execute("UPDATE chat_messages SET parent_id='question' WHERE message_id='m1'")
    save_feature_config(pg, api_key_cipher(s.settings.token_encryption_key), "operations_assistant",
        provider="openai", project_id="test-project", model="gpt-6-luna", enabled=True,
        api_key="test-key-not-real", updated_by_authorization_id=s.ids["owner"])
    session_id, turn_id = uuid4(), uuid4()
    pg.execute("INSERT INTO assistant_sessions(id,actor_id) VALUES(%s,%s)", (session_id, s.ids["cm"]))
    now = datetime.now(UTC)
    pg.execute("INSERT INTO assistant_turns(id,session_id,client_request_id,input_ciphertext,created_at) "
               "VALUES(%s,%s,%s,'encrypted-test',%s)", (turn_id, session_id, uuid4(), now))
    turn = pg.execute("SELECT * FROM assistant_turns WHERE id=%s", (turn_id,)).fetchone()
    context = tools.current_context(pg, s.ids["cm"], {}, s.settings, now=now)
    assert context["scope"]["cm_id"] == str(s.ids["cm"])
    assert {tool["name"] for tool in tools.TOOLS} == {
        "find_creators", "summarize_messages", "filter_events", "read_event_context", "draft_reply",
        "query_creators", "list_groups", "get_sync_status", "search_chat_history", "read_message_context",
        "activity_stats", "query_unreplied_creator_events", "save_reply_draft", "find_conversations",
    }
    assert all(tool["strict"] and not tool["parameters"]["additionalProperties"] for tool in tools.TOOLS)
    found = tools.execute(pg, s.ids["cm"], turn, "find_creators", {"query": "@alice"}, s.settings, context)
    assert found["total"] == 1 and found["creators"][0]["id"] == str(s.ids["person"])
    assert found["creators"][0]["exact_match"]
    with pytest.raises(ValueError):
        tools.execute(pg, s.ids["cm"], turn, "find_creators", {"query": "@alice", "actor_id": str(s.ids["owner"])},
                      s.settings, context)
    with pytest.raises(PermissionError):
        tools.current_context(pg, s.ids["reviewer"], {"cm_id": str(s.ids["cm"])}, s.settings)

    args = {"audience": "creators", "start": s.start.isoformat(), "end": (now - timedelta(seconds=1)).isoformat(),
            "creator_ids": None, "chat_ids": None, "cursor": None}
    before_global = pg.execute("SELECT * FROM offhours_summary_settings").fetchone()
    waiting = tools.execute(pg, s.ids["cm"], turn, "summarize_messages", args, s.settings, context)
    assert waiting["waiting"]["kind"] == "summary"
    assert tools.resume_wait(pg, s.ids["cm"], turn, waiting["waiting"], s.settings, context) is None
    assert tools.execute(pg, s.ids["cm"], turn, "summarize_messages", args, s.settings, context) == waiting
    assert pg.execute("SELECT * FROM offhours_summary_settings").fetchone() == before_global
    summaries.prepare_next_summary(s.settings)
    background_run = uuid4()
    pg.execute("""INSERT INTO offhours_summary_runs
        (id,requested_by_authorization_id,responsible_authorization_id,window_start,window_end)
        VALUES(%s,%s,%s,%s,%s)""", (background_run, s.ids["owner"], s.ids["cm"], s.start, s.end))
    for index in range(5):
        pg.execute("INSERT INTO chats(chat_id) VALUES(%s)", (f"busy-background-{index}",))
        pg.execute("""INSERT INTO offhours_summary_tasks
            (id,run_id,chat_id,input_fingerprint,cutoff,status,lease_until)
            VALUES(%s,%s,%s,%s,NOW(),'running',NOW()+INTERVAL '10 minutes')""",
            (uuid4(), background_run, f"busy-background-{index}", str(index)))
    assert summaries.claim_next_summary() is None
    job = summaries.claim_next_summary(interactive=True)
    assert job and job["origin"] == "assistant"
    summaries.run_claimed_summary(job, s.settings)
    pg.execute("UPDATE offhours_summary_tasks SET status='cancelled',lease_until=NULL WHERE run_id=%s",
               (background_run,))
    finish_merges(pg, s)
    result = tools.resume_wait(pg, s.ids["cm"], turn, waiting["waiting"], s.settings, context)
    assert result and len(result["events"]) == 1 and result["events"][0]["handling_status"] == "unresolved"
    assert not result["pending"] and result["coverage"]["messages"] == 1
    historical_scope = {**context["scope"], "audience": "creators", "creator_ids": [str(s.ids["person"])]}
    historical_snapshot = assistant_queries.creators(pg, s.ids["cm"], {**context, "scope": historical_scope},
        {"query": None, "importance": None, "cursor": None}, s.settings)["_snapshot"]
    tools.validate_tool_results(pg, s.ids["cm"], turn, [{**result, "scope": historical_scope,
        "_creator_snapshot": historical_snapshot}], s.settings, context)
    assert tools.execute(pg, s.ids["cm"], turn, "summarize_messages", args, s.settings, context)[
        "result_id"] == result["result_id"]
    event_ref = result["events"][0]["event_ref"]
    selected_event = tools.current_context(pg, s.ids["cm"], {
        "event_ref": event_ref, "start": args["start"], "end": args["end"]}, s.settings, now=now)
    assert selected_event["scope"]["creator_ids"] == [str(s.ids["person"])]
    assert selected_event["scope"]["creator_names"]
    with pytest.raises(PermissionError, match="已选达人"):
        tools.current_context(pg, s.ids["cm"], {"event_ref": event_ref, "creator_ids": []}, s.settings)
    selected_result = tools.execute(pg, s.ids["cm"], turn, "summarize_messages", args, s.settings, selected_event)
    assert selected_result["scope"]["creator_ids"] == [str(s.ids["person"])]
    assert "Sample please" not in json.dumps(result)
    assert pg.execute("SELECT * FROM offhours_summary_settings").fetchone() == before_global

    filtered = tools.execute(pg, s.ids["cm"], turn, "filter_events", {
        "result_id": result["result_id"], "creator_ids": None, "priority": "P2", "reply_status": "unreplied",
        "handling_status": "unresolved", "topic": "样品", "cursor": None,
    }, s.settings, context)
    assert filtered["total"] == 1 and filtered["events"][0]["event_ref"] == event_ref and len(s.calls) == 1
    period = tools.execute(pg, s.ids["cm"], turn, "read_event_context", {"event_ref": event_ref, "mode": "period"},
                           s.settings, context)
    latest = tools.execute(pg, s.ids["cm"], turn, "read_event_context", {"event_ref": event_ref, "mode": "latest"},
                           s.settings, context)
    assert "Sample please" in json.dumps(period) and "unrelated outsider secret" not in json.dumps(latest)
    assert "future CM reply" not in json.dumps(latest)
    evidence = tools.evidence(pg, s.ids["cm"], event_ref, s.settings)["messages"]
    assert any(message["text"] == "Sample please" for message in evidence)
    assert any(message["text"] == "Would you like a sample?" for message in evidence)
    with pytest.raises(PermissionError):
        tools.evidence(pg, s.ids["reviewer"], event_ref, s.settings)
    with pytest.raises(PermissionError):
        tools.execute(pg, s.ids["cm"], turn, "summarize_messages", {**args, "creator_ids": [str(s.ids["other"])]},
                      s.settings, context)

    summary_row = pg.execute("SELECT * FROM offhours_summary_tasks WHERE chat_id IS NULL").fetchone()
    closed = summary_dialogues.decode(summary_row["result_ciphertext"], s.settings)
    closed["events"][0]["handling_status"] = "resolved"
    pg.execute("UPDATE offhours_summary_tasks SET result_ciphertext=%s WHERE id=%s",
               (summary_dialogues.encode(closed, s.settings), summary_row["id"]))
    courtesy = tools.execute(pg, s.ids["cm"], turn, "draft_reply", {
        "event_ref": event_ref, "instruction": "礼貌回应", "language": "en",
    }, s.settings, context)
    assert courtesy["context"]["event"]["handling_status"] == "resolved"
    pg.execute("UPDATE offhours_summary_tasks SET result_ciphertext=%s WHERE id=%s",
               (summary_row["result_ciphertext"], summary_row["id"]))
    prepared = tools.execute(pg, s.ids["cm"], turn, "draft_reply", {
        "event_ref": event_ref, "instruction": "请用简短英文回复", "language": "en",
    }, s.settings, context)
    assert prepared["status"] == "prepared" and not prepared.get("waiting")
    assert "Would you like a sample?" in json.dumps(prepared["context"])
    turn["checkpoint_ciphertext"] = summary_dialogues.encode({"tool_results": [prepared]}, s.settings)
    draft = tools.execute(pg, s.ids["cm"], turn, "save_reply_draft", {
        "draft_ref": prepared["draft_ref"], "outcome": "ready", "language": "en",
        "draft": "Which sample would you like?", "chinese_translation": "你想要哪款样品？",
        "explanation": "需要达人补充产品信息。", "checks": [],
        "evidence_refs": [prepared["context"]["messages"][0]["ref"]],
    }, s.settings, context)
    assert draft["draft"]["outcome"] == "ready" and draft["draft"]["draft"] == "Which sample would you like?"
    assert pg.execute("SELECT count(*) n FROM summary_reply_tasks").fetchone()["n"] == 0
    final = {"answer": "共有一件未解决的样品事项。", "result_refs": [result["result_id"]],
             "event_refs": [event_ref], "draft_ref": draft["draft_ref"]}
    tool_results = [found, result, filtered, period, latest, draft]
    rendered = tools.build_result(pg, s.ids["cm"], turn, final, s.settings, context, tool_results)
    assert rendered["scope"]["audience"] == "creators"
    sync = tools.execute(pg, s.ids["cm"], turn, "get_sync_status", {}, s.settings, context)
    with_metadata = tools.build_result(pg, s.ids["cm"], turn, {**final, "data_refs": [sync["query_ref"]]},
                                      s.settings, context, [*tool_results, sync])
    assert with_metadata["scope"]["audience"] == "creators"
    assert rendered["events"][0]["evidence_ref"] == event_ref and rendered["draft"]["chinese_translation"]
    with pytest.raises(ValueError):
        tools.build_result(pg, s.ids["cm"], turn, {**final, "event_refs": [str(uuid4()) + ":0:invented"]},
                           s.settings, context, tool_results)
    summary_task = pg.execute("SELECT * FROM offhours_summary_tasks WHERE chat_id IS NULL").fetchone()
    assert summary_dialogues.decode(summary_task["result_ciphertext"], s.settings)["events"][0][
        "handling_status"] == "unresolved"
    assert pg.execute("SELECT count(*) n FROM contact_assignments").fetchone()["n"] == 1

    # A longer request splits completely, and no global report settings change.
    longer = tools.execute(pg, s.ids["cm"], turn, "summarize_messages", {
        **args, "start": (s.start - timedelta(days=8)).isoformat(),
    }, s.settings, context)
    assert len(longer["waiting"]["ids"]) == 2
    long_result = None
    for _ in range(8):
        summaries.prepare_next_summary(s.settings)
        finish_merges(pg, s)
        long_result = tools.resume_wait(pg, s.ids["cm"], turn, longer["waiting"], s.settings, context)
        if long_result:
            break
    assert long_result and len(long_result["windows"]) == 2 and long_result["total"] == 1
    assert long_result["windows"][0]["end"] == long_result["windows"][1]["start"]
    assert long_result["coverage"]["messages"] == 2 and long_result["coverage"]["confirmed_creators"] == 1
    assert pg.execute("SELECT * FROM offhours_summary_settings").fetchone() == before_global
    s.add("latest-only", text="Later sample progress", at=now - timedelta(milliseconds=500))
    refreshed = tools.execute(pg, s.ids["cm"], turn, "read_event_context", {
        "event_ref": event_ref, "mode": "latest",
    }, s.settings, context)
    assert "Later sample progress" in json.dumps(refreshed)
    tools.validate_tool_results(pg, s.ids["cm"], turn, [refreshed], s.settings, context)
    pg.execute("UPDATE chat_messages SET is_deleted=TRUE WHERE message_id='latest-only'")
    with pytest.raises(ValueError):
        tools.validate_tool_results(pg, s.ids["cm"], turn, [refreshed], s.settings, context)
    audit = pg.execute("SELECT metadata FROM audit_events WHERE action='operations_assistant_context_read'").fetchall()
    assert audit and "Sample please" not in json.dumps(audit)
    pg.execute("UPDATE authorizations SET body_consent_at=%s WHERE id=%s", (datetime.now(UTC), s.ids["cm"]))
    with pytest.raises(ValueError):
        tools.build_result(pg, s.ids["cm"], turn, final, s.settings, context, tool_results)
    with pytest.raises(ValueError):
        tools.evidence(pg, s.ids["cm"], event_ref, s.settings)
