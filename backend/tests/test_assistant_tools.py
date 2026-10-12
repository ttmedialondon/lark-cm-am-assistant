"""One real PostgreSQL workflow: tools, reusable reports, drafts and revoked sources."""

import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_offhours_summary import finish_merges, pg, seeded  # noqa: F401

from app import assistant_tools as tools
from app import offhours_summary as summaries
from app import openai_client, summary_dialogues, summary_replies
from app.ai_feature_config import api_key_cipher, save_feature_config


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
    assert len(tools.TOOLS) == 5
    assert all(tool["strict"] and not tool["parameters"]["additionalProperties"] for tool in tools.TOOLS)
    found = tools.execute(pg, s.ids["cm"], turn, "find_creators", {"query": "@alice"}, s.settings, context)
    assert found["total"] == 1 and found["creators"][0]["id"] == str(s.ids["person"])
    assert found["creators"][0]["exact_match"]
    with pytest.raises(ValueError):
        tools.execute(pg, s.ids["cm"], turn, "find_creators", {"query": "@alice", "actor_id": str(s.ids["owner"])},
                      s.settings, context)
    with pytest.raises(PermissionError):
        tools.current_context(pg, s.ids["reviewer"], {"cm_id": str(s.ids["cm"])}, s.settings)

    args = {"start": s.start.isoformat(), "end": (now - timedelta(seconds=1)).isoformat(), "creator_ids": None}
    before_global = pg.execute("SELECT * FROM offhours_summary_settings").fetchone()
    waiting = tools.execute(pg, s.ids["cm"], turn, "summarize_messages", args, s.settings, context)
    assert waiting["waiting"]["kind"] == "summary"
    assert tools.resume_wait(pg, s.ids["cm"], turn, waiting["waiting"], s.settings, context) is None
    assert tools.execute(pg, s.ids["cm"], turn, "summarize_messages", args, s.settings, context) == waiting
    assert pg.execute("SELECT * FROM offhours_summary_settings").fetchone() == before_global
    summaries.prepare_next_summary(s.settings)
    job = summaries.claim_next_summary()
    assert job and job["origin"] == "assistant"
    summaries.run_claimed_summary(job, s.settings)
    finish_merges(pg, s)
    result = tools.resume_wait(pg, s.ids["cm"], turn, waiting["waiting"], s.settings, context)
    assert result and len(result["events"]) == 1 and result["events"][0]["handling_status"] == "unresolved"
    assert not result["pending"] and result["coverage"]["messages"] == 1
    assert tools.execute(pg, s.ids["cm"], turn, "summarize_messages", args, s.settings, context)[
        "result_id"] == result["result_id"]
    event_ref = result["events"][0]["event_ref"]
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

    def reply_model(cfg, _key, pack, _actor, **kwargs):
        assert cfg["model"] == "gpt-6-luna"
        if kwargs["output_format"]["name"] == "reply_evidence":
            payload = {"relevant_refs": pack["core_refs"], "style_refs": [], "processed_refs": pack["core_refs"]}
        else:
            assert pack["draft_request"] == {"instruction": "请用简短英文回复", "language": "en"}
            payload = {"outcome": "ready", "language": "en", "draft": "Which sample would you like?",
                "chinese_translation": "你想要哪款样品？", "explanation": "需要达人补充产品信息。", "checks": [],
                "evidence_refs": [pack["messages"][0]["ref"]]}
        return {"text": json.dumps(payload), "usage": {"input_tokens": 1, "output_tokens": 1}}

    monkeypatch.setattr(summary_replies, "connection", lambda: nullcontext(pg))
    summary_model = openai_client.call_openai
    monkeypatch.setattr(openai_client, "call_openai", reply_model)
    draft_wait = tools.execute(pg, s.ids["cm"], turn, "draft_reply", {
        "event_ref": event_ref, "instruction": "请用简短英文回复", "language": "en",
    }, s.settings, context)
    assert tools.resume_wait(pg, s.ids["cm"], turn, draft_wait["waiting"], s.settings, context) is None
    for _ in range(4):
        job = summary_replies.claim()
        if not job:
            break
        assert job["feature"] == "operations_assistant"
        summary_replies.step(job, s.settings)
    draft = tools.resume_wait(pg, s.ids["cm"], turn, draft_wait["waiting"], s.settings, context)
    assert draft["draft"]["outcome"] == "ready" and draft["draft"]["draft"] == "Which sample would you like?"
    final = {"answer": "共有一件未解决的样品事项。", "result_refs": [result["result_id"]],
             "event_refs": [event_ref], "draft_ref": draft["draft_ref"]}
    tool_results = [found, result, filtered, period, latest, draft]
    rendered = tools.build_result(pg, s.ids["cm"], turn, final, s.settings, context, tool_results)
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
        "start": (s.start - timedelta(days=8)).isoformat(), "end": args["end"], "creator_ids": None,
    }, s.settings, context)
    assert len(longer["waiting"]["ids"]) == 2
    monkeypatch.setattr(openai_client, "call_openai", summary_model)
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
