"""Shared OpenAI transport. Evidence, decisions and retry budgets belong to each feature."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import time
from collections.abc import Mapping
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import UUID

import httpx


class AIInputError(ValueError):
    """An AI request could not safely complete."""


class OpenAIRequestError(AIInputError):
    """A provider transport failure with only safe retry metadata."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        is_quota: bool = False,
        retryable: bool = False,
        telemetry: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.is_quota = is_quota
        self.telemetry = dict(telemetry or {})
        # A timeout can occur after the provider has accepted a billable request.
        self.request_unknown = not is_quota and not isinstance(self.telemetry.get("http_status"), int)
        self.retryable = retryable and not self.request_unknown


class ModelOutputError(ValueError):
    """The model did not return the constrained review shape."""

    def __init__(self, message: str, *, telemetry: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.telemetry = dict(telemetry or {})


def retry_delay(retry_after_seconds: float | None, attempts: object) -> float:
    """Respect a provider retry deadline; only use a small fallback if absent."""

    try:
        provider_delay = float(retry_after_seconds) if retry_after_seconds is not None else None
    except (TypeError, ValueError):
        provider_delay = None
    if provider_delay is not None and math.isfinite(provider_delay) and provider_delay >= 0:
        base = provider_delay
    else:
        try:
            base = min(60.0, 2.0 ** min(max(int(attempts or 0), 0), 5))
        except (TypeError, ValueError):
            base = 2.0
    return float(base) + random.uniform(0.0, 1.0)


def call_openai(
    config: Mapping[str, Any],
    api_key: str,
    pack: Mapping[str, Any],
    contact_id: UUID,
    *,
    instructions: str,
    output_format: Mapping[str, Any],
    max_output_tokens: int = 500,
    reasoning_effort: str = "low",
    total_timeout: float | None = None,
) -> dict[str, Any]:
    """Call Responses once for an evidence-bound internal AI feature."""

    headers, payload = _request_parts(
        config, api_key, pack, contact_id, instructions, output_format,
        max_output_tokens, reasoning_effort, total_timeout,
    )
    started = time.perf_counter()
    try:
        if total_timeout is None:
            response = httpx.post("https://api.openai.com/v1/responses", headers=headers, json=payload, timeout=45.0)
        else:
            response = asyncio.run(_post_with_deadline(headers, payload, total_timeout))
    except (httpx.HTTPError, TimeoutError) as exc:
        raise _transport_error(started) from exc
    return _read_response(response, started)


async def call_openai_async(
    config: Mapping[str, Any],
    api_key: str,
    pack: Mapping[str, Any],
    contact_id: UUID,
    *,
    instructions: str,
    output_format: Mapping[str, Any],
    max_output_tokens: int = 500,
    reasoning_effort: str = "low",
    total_timeout: float = 45.0,
) -> dict[str, Any]:
    """The same Responses transport, cancellable by an interactive HTTP request."""
    headers, payload = _request_parts(
        config, api_key, pack, contact_id, instructions, output_format,
        max_output_tokens, reasoning_effort, total_timeout,
    )
    started = time.perf_counter()
    try:
        response = await _post_with_deadline(headers, payload, total_timeout)
    except (httpx.HTTPError, TimeoutError) as exc:
        raise _transport_error(started) from exc
    return _read_response(response, started)


def _request_parts(config, api_key, pack, contact_id, instructions, output_format,
                   max_output_tokens, reasoning_effort, total_timeout):
    if total_timeout is not None and (not math.isfinite(total_timeout) or total_timeout <= 0):
        raise AIInputError("AI 请求超时设置无效。")
    payload = {
        "model": config["model"],
        "store": False,
        "reasoning": {"effort": reasoning_effort},
        "max_output_tokens": max_output_tokens,
        "instructions": instructions,
        "input": json.dumps(pack, ensure_ascii=False, separators=(",", ":")),
        "text": {"format": output_format},
        "safety_identifier": hashlib.sha256(str(contact_id).encode("ascii")).hexdigest(),
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if config.get("project_id"):
        headers["OpenAI-Project"] = config["project_id"]
    return headers, payload


def _transport_error(started):
    return OpenAIRequestError(
        "AI 服务暂时不可用；请稍后重新发起。",
        retryable=True,
        telemetry={"response_latency_ms": max(0, round((time.perf_counter() - started) * 1_000))},
    )


def _read_response(response, started):
    telemetry = _openai_telemetry(response, started)
    if response.status_code < 200 or response.status_code >= 300:
        is_quota = _is_quota_response(response)
        raise OpenAIRequestError(
            f"AI 服务请求未完成（HTTP {response.status_code}）。",
            retry_after_seconds=_retry_after_header(response),
            is_quota=is_quota,
            retryable=not is_quota and response.status_code in {408, 429, 500, 502, 503, 504},
            telemetry=telemetry,
        )
    try:
        data = response.json()
        if not isinstance(data, Mapping):
            raise TypeError("response object")
    except (TypeError, ValueError, KeyError) as exc:
        raise ModelOutputError("invalid response", telemetry=telemetry) from exc
    usage = data.get("usage") if isinstance(data.get("usage"), Mapping) else {}
    safe_usage = {
        "input_tokens": _nonnegative_int(usage.get("input_tokens")),
        "output_tokens": _nonnegative_int(usage.get("output_tokens")),
    }
    response_id = _bounded_text(data.get("id"), 160)
    telemetry = {**telemetry, "usage": safe_usage, "response_id": response_id}
    if data.get("status") not in {None, "completed"}:
        raise ModelOutputError("unfinished response", telemetry=telemetry)
    try:
        text = _response_text(data)
    except (TypeError, ValueError, KeyError) as exc:
        raise ModelOutputError("invalid response", telemetry=telemetry) from exc
    return {
        "text": text,
        "response_id": response_id,
        "usage": safe_usage,
        "telemetry": telemetry,
    }


async def _post_with_deadline(headers: dict, payload: dict, total_timeout: float) -> httpx.Response:
    # Worker calls this synchronous facade from its existing executor, not an event loop.
    async with asyncio.timeout(total_timeout), httpx.AsyncClient(timeout=45.0) as client:
        return await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)


def _openai_telemetry(response: httpx.Response, started: float) -> dict[str, Any]:
    """Persist only stable operational headers, never a raw provider response."""

    headers = getattr(response, "headers", {}) or {}

    def header(name: str) -> str | None:
        value = headers.get(name) or headers.get(name.lower())
        return _bounded_text(value, 160) or None

    return {
        "http_status": response.status_code,
        "openai_request_id": header("x-request-id"),
        "response_latency_ms": max(0, round((time.perf_counter() - started) * 1_000)),
        "rate_limit_remaining_requests": _header_nonnegative_int(header("x-ratelimit-remaining-requests")),
        "rate_limit_remaining_tokens": _header_nonnegative_int(header("x-ratelimit-remaining-tokens")),
        "rate_limit_reset_requests": header("x-ratelimit-reset-requests"),
        "rate_limit_reset_tokens": header("x-ratelimit-reset-tokens"),
    }


def _retry_after_header(response: httpx.Response) -> float | None:
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After")
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        try:
            seconds = max(0.0, parsedate_to_datetime(str(raw)).timestamp() - time.time())
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _is_quota_response(response: httpx.Response) -> bool:
    """Recognize durable billing/quota failures without retaining raw error text."""

    try:
        payload = response.json()
    except (TypeError, ValueError):
        return False
    error = payload.get("error") if isinstance(payload, Mapping) else None
    if not isinstance(error, Mapping):
        return False
    signals = " ".join(str(error.get(field) or "").casefold() for field in ("code", "type", "message"))
    return any(
        token in signals
        for token in (
            "insufficient_quota",
            "billing",
            "credit balance",
            "payment required",
            "hard_limit",
            "quota exceeded",
            "spend_limit_exceeded",
            "usage_limit_exceeded",
        )
    )


def _response_text(data: Mapping[str, Any]) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    for output in _json_list(data.get("output")):
        for item in _json_list(output.get("content") if isinstance(output, Mapping) else None):
            if isinstance(item, Mapping) and item.get("type") == "output_text" and isinstance(item.get("text"), str):
                return item["text"]
    raise KeyError("output_text")


def _nonnegative_int(value: object) -> int | None:
    return int(value) if isinstance(value, int) and value >= 0 else None


def _header_nonnegative_int(value: str | None) -> int | None:
    try:
        parsed = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed >= 0 else None


def _bounded_text(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _json_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []
