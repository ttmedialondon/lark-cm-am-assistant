from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from email.utils import format_datetime
from uuid import UUID

import httpx
import pytest

from app import ai_dispatch, openai_client


def test_shared_gate_rejects_when_three_valid_leases_are_running() -> None:
    calls: list[str] = []

    class Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def execute(self, query, _params=None):
            calls.append(query)
            if "ai_admission_state" in query:
                return Result({"paused_at": None, "throttled": False})
            if "COUNT(*)" in query:
                return Result({"count": 3})
            return Result(None)

    assert not ai_dispatch.can_claim(Connection())
    combined_query = next(query for query in calls if "COUNT(*)" in query)
    for retired in ("contact_assessments", "creator_ownership_tasks", "creator_handle_tasks"):
        assert retired not in combined_query
    assert "contact_assignment_assessments" not in combined_query
    assert "creator_workflow_ai_tasks" not in combined_query
    assert "offhours_summary_tasks" in combined_query
    assert "creator_import_rows" in combined_query
    assert "lease_until > NOW()" in combined_query


def test_single_request_transport_preserves_retry_hints_and_safe_failure_metadata(monkeypatch) -> None:
    calls = []
    result = httpx.Response(200, json={
        "status": "completed", "output_text": "{}", "id": "resp_test",
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

    result = httpx.Response(200, headers={"x-request-id": "req_incomplete"}, json={
        "status": "incomplete", "id": "resp_incomplete", "output_text": "PRIVATE BODY",
        "usage": {"input_tokens": 80, "output_tokens": 20},
    })
    with pytest.raises(openai_client.ModelOutputError) as stopped:
        request()
    assert stopped.value.telemetry["usage"] == {"input_tokens": 80, "output_tokens": 20}
    assert stopped.value.telemetry["response_id"] == "resp_incomplete"
    assert stopped.value.telemetry["openai_request_id"] == "req_incomplete"
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

    result = httpx.Response(401, json={"error": {"message": "PRIVATE KEY"}})
    with pytest.raises(openai_client.OpenAIRequestError) as stopped:
        request()
    assert not stopped.value.retryable
    assert "PRIVATE KEY" not in str(stopped.value) + str(stopped.value.telemetry)

    result = httpx.ReadTimeout("PRIVATE REQUEST", request=httpx.Request("POST", "https://api.openai.com"))
    with pytest.raises(openai_client.OpenAIRequestError) as stopped:
        request()
    assert stopped.value.retryable
    assert "PRIVATE REQUEST" not in str(stopped.value) + str(stopped.value.telemetry)
    assert openai_client.retry_delay(float("nan"), 2) == 4.5
    assert openai_client.retry_delay(-1, 2) == 4.5


def test_worker_total_deadline_cancels_network_request_without_background_retry(monkeypatch) -> None:
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
        return openai_client.call_openai(
            {"model": "test-model"}, "test-key", {}, UUID(int=1),
            instructions="test", output_format={"type": "json_object"}, total_timeout=0.01,
        )

    assert request()["text"] == "{}"
    delay = True
    with pytest.raises(openai_client.OpenAIRequestError) as error:
        request()
    assert error.value.retryable
    assert len(calls) == len(stopped) == len(closed) == 2
    assert error.value.telemetry["response_latency_ms"] < 1_000
