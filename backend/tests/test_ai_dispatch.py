from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from email.utils import format_datetime
from uuid import UUID

import httpx
import pytest

from app import ai_dispatch, openai_client


def test_interactive_and_background_admission_are_independent_but_share_project_pause() -> None:
    calls: list[str] = []
    counts = {"background": 5, "interactive": 0}
    paused = False

    class Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def execute(self, query, _params=None):
            calls.append(query)
            if "ai_admission_state" in query:
                return Result({"paused_at": True if paused else None, "throttled": False})
            if "COUNT(*)" in query:
                return Result({"count": counts["interactive" if "assistant_turns" in query else "background"]})
            return Result(None)

    assert not ai_dispatch.can_claim(Connection())
    assert ai_dispatch.can_claim(Connection(), interactive=True)
    counts.update(background=0, interactive=ai_dispatch.INTERACTIVE_REQUESTS)
    assert ai_dispatch.can_claim(Connection())
    assert not ai_dispatch.can_claim(Connection(), interactive=True)
    paused = True
    assert not ai_dispatch.can_claim(Connection())
    assert not ai_dispatch.can_claim(Connection(), interactive=True)


def test_single_request_transport_preserves_retry_hints_and_safe_failure_metadata(monkeypatch) -> None:
    calls = []
    result = httpx.Response(200, json={
        "status": "completed", "output_text": "{}", "output": [], "id": "resp_test",
        "usage": {"input_tokens": 80, "output_tokens": 20},
    })

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(openai_client.httpx, "post", post)
    monkeypatch.setattr(openai_client.random, "uniform", lambda *_: 0.5)
    monkeypatch.setattr(openai_client.time, "time", lambda: 1_700_000_000)

    def request(**kwargs):
        before = len(calls)
        try:
            return openai_client.call_openai(
                {"model": "test-model"}, "test-key", {"target": "U1"},
                UUID("00000000-0000-0000-0000-000000000001"),
                instructions="test", output_format={"type": "json_object"}, **kwargs,
            )
        finally:
            assert len(calls) == before + 1  # Feature queues own retry budgets; no nested retries.

    assert request()["usage"] == {"input_tokens": 80, "output_tokens": 20}
    assert calls[-1][1]["json"]["reasoning"] == {"effort": "low"}
    request(reasoning_effort="none")
    assert calls[-1][1]["json"]["reasoning"] == {"effort": "none"}
    assert calls[-1][1]["json"]["store"] is False
    inline = [{"type": "input_image", "image_url": "data:image/png;base64,dGVzdA=="},
              {"type": "input_file", "filename": "test.pdf", "file_data": "data:application/pdf;base64,dGVzdA=="}]
    parsed = request(input_content=inline)
    payload = calls[-1][1]["json"]
    assert payload["input"][0]["content"][0] == {"type": "input_text", "text": '{"target":"U1"}'}
    assert payload["input"][0]["content"][1:] == inline and payload["store"] is False
    assert "dGVzdA" not in str(parsed)  # Receipts never retain original attachment data.

    result = httpx.Response(200, json={"status": "completed", "output": [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "PRIVATE REASONING"}]},
        {"type": "message", "content": [{"type": "output_text", "text": '{"ready":'},
            {"type": "refusal", "refusal": "PRIVATE REFUSAL"}]},
        {"type": "message", "content": [{"type": "output_text", "text": "true}"}]},
    ]})
    assert request()["text"] == '{"ready":true}'
    result = httpx.Response(200, json={"status": "completed", "output_text": "{}", "output": []})

    async def tool_post(headers, payload, total_timeout):
        return post("https://api.openai.com/v1/responses", headers=headers, json=payload, timeout=total_timeout)

    monkeypatch.setattr(openai_client, "_post_with_deadline", tool_post)
    tool_response = openai_client.call_openai_tools(
        {"model": "test-model"}, "test-key", [], UUID(int=1), instructions="test", tools=[],
        output_format={"type": "json_object"}, max_output_tokens=16000,
    )
    assert calls[-1][1]["json"]["max_output_tokens"] == 16000 and tool_response["output"] == []
    assert tool_response["telemetry"]["response_status"] == "completed"

    result = httpx.Response(200, headers={"x-request-id": "req_incomplete"}, json={
        "status": "incomplete", "id": "resp_incomplete", "output_text": "PRIVATE BODY",
        "incomplete_details": {"reason": "max_output_tokens"},
        "usage": {"input_tokens": 80, "output_tokens": 20},
    })
    with pytest.raises(openai_client.ModelOutputError) as stopped:
        request()
    assert stopped.value.telemetry["usage"] == {"input_tokens": 80, "output_tokens": 20}
    assert stopped.value.telemetry["response_id"] == "resp_incomplete"
    assert stopped.value.telemetry["openai_request_id"] == "req_incomplete"
    assert stopped.value.telemetry["response_status"] == "incomplete"
    assert stopped.value.telemetry["incomplete_reason"] == "max_output_tokens"
    assert "PRIVATE BODY" not in str(stopped.value) + str(stopped.value.telemetry)
    result = httpx.Response(200, json={
        "status": "incomplete", "output_text": "PRIVATE BODY", "incomplete_details": {"reason": "PRIVATE BODY"},
    })
    with pytest.raises(openai_client.ModelOutputError) as stopped:
        request()
    assert stopped.value.telemetry["incomplete_reason"] is None
    assert "PRIVATE BODY" not in str(stopped.value) + str(stopped.value.telemetry)

    result = httpx.Response(503, headers={"Retry-After": "7200", "x-request-id": "req_test"},
                            json={"error": {"code": "server_is_overloaded", "message": "PRIVATE BODY"}})
    with pytest.raises(openai_client.OpenAIRequestError) as stopped:
        request()
    assert stopped.value.retryable and not stopped.value.is_quota
    assert stopped.value.retry_after_seconds == 7200
    assert openai_client.retry_delay(stopped.value.retry_after_seconds, 1) == 7200.5
    assert stopped.value.telemetry["openai_request_id"] == "req_test"
    assert stopped.value.telemetry["http_status"] == 503
    assert "PRIVATE BODY" not in str(stopped.value) + str(stopped.value.telemetry)

    date = format_datetime(datetime.fromtimestamp(1_700_007_200, UTC), usegmt=True)
    result = httpx.Response(429, headers={"Retry-After": date}, json={"error": {"code": "slow_down"}})
    with pytest.raises(openai_client.OpenAIRequestError) as stopped:
        request()
    assert stopped.value.retry_after_seconds == 7200

    result = httpx.Response(429, json={"error": {"code": "project_spend_limit_exceeded"}})
    with pytest.raises(openai_client.OpenAIRequestError) as stopped:
        request()
    assert stopped.value.is_quota and not stopped.value.retryable
    assert not openai_client.OpenAIRequestError("quota", is_quota=True, retryable=True).retryable

    result = httpx.Response(401, json={"error": {"message": "PRIVATE KEY"}})
    with pytest.raises(openai_client.OpenAIRequestError) as stopped:
        request()
    assert not stopped.value.retryable
    assert "PRIVATE KEY" not in str(stopped.value) + str(stopped.value.telemetry)

    result = httpx.ReadTimeout("PRIVATE REQUEST", request=httpx.Request("POST", "https://api.openai.com"))
    with pytest.raises(openai_client.OpenAIRequestError) as stopped:
        request()
    assert stopped.value.request_unknown and not stopped.value.retryable
    assert "PRIVATE REQUEST" not in str(stopped.value) + str(stopped.value.telemetry)
    assert openai_client.retry_delay(float("nan"), 2) == 4.5
    assert openai_client.retry_delay(-1, 2) == 4.5




@pytest.mark.parametrize("interactive", [False, True])
def test_total_deadline_cancels_network_request_without_background_retry(monkeypatch, interactive) -> None:
    calls = []
    stopped = []
    closed = []
    delay = False

    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 45.0}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            closed.append(True)

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            try:
                if delay:
                    await asyncio.sleep(10)
                return httpx.Response(200, json={"status": "completed", "output_text": "{}"})
            finally:
                stopped.append(True)

    monkeypatch.setattr(openai_client.httpx, "AsyncClient", Client)

    def request():
        if interactive:
            return asyncio.run(openai_client.call_openai_async(
                {"model": "test-model"}, "test-key", {}, UUID(int=1),
                instructions="test", output_format={"type": "json_object"}, total_timeout=0.01,
            ))
        return openai_client.call_openai(
            {"model": "test-model"}, "test-key", {}, UUID(int=1),
            instructions="test", output_format={"type": "json_object"}, total_timeout=0.01,
        )

    assert request()["text"] == "{}"
    delay = True
    with pytest.raises(openai_client.OpenAIRequestError) as error:
        request()
    assert error.value.request_unknown and not error.value.retryable
    assert len(calls) == len(stopped) == len(closed) == 2
    assert error.value.telemetry["response_latency_ms"] < 1_000
