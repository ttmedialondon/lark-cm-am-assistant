"""Interactive desktop API. It deliberately does not use the background AI admission queue."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Mapping
from contextlib import suppress
from uuid import UUID

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import desktop_replies as replies
from . import desktop_reply_store as store
from . import openai_client
from .config import get_settings
from .db import connection
from .desktop_reply_auth import AuthError, authenticate
from .desktop_reply_input import MAX_REQUEST_BYTES, ReplyInputError, validate_request

TOTAL_TIMEOUT = 55.0
logger = logging.getLogger(__name__)


def response(value, status=200):
    return JSONResponse(value, status_code=status, headers={
        "Cache-Control": "no-store, max-age=0", "X-Content-Type-Options": "nosniff",
    })


def error(code, message, status):
    return response({"error": {"code": code, "message": message}}, status)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-finite JSON number")


async def read_payload(request):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise ReplyInputError("invalid_content_type", "请使用 application/json 提交。", 415)
    lengths = request.headers.getlist("content-length")
    if lengths:
        try:
            declared = int(lengths[0])
            if len(lengths) != 1 or declared < 0:
                raise ValueError
        except ValueError:
            raise ReplyInputError("invalid_content_length", "请求长度无效。") from None
        if declared > MAX_REQUEST_BYTES:
            raise ReplyInputError("request_too_large", "请求超过 1 MiB，未截断内容。", 413)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_REQUEST_BYTES:
            raise ReplyInputError("request_too_large", "请求超过 1 MiB，未截断内容。", 413)
    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=_object, parse_constant=_invalid_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ReplyInputError("invalid_json", "请求不是有效的 UTF-8 JSON。") from None
    return validate_request(payload)


def claim(actor, payload):
    request_id = UUID(payload["request_id"])
    canonical = dict(payload, request_id=str(request_id))
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()
    with connection() as conn, conn.transaction():
        return store.claim(conn, actor["id"], request_id, digest)


def finish(actor, payload, receipt, *, result=None, failure=None, cipher=None, usage=None, latency_ms=None):
    with connection() as conn, conn.transaction():
        args = (conn, actor["id"], payload["request_id"], receipt["fence_token"])
        if result is not None:
            return store.finish_success(*args, result, cipher, usage=usage, latency_ms=latency_ms)
        return store.finish_error(*args, code=failure.code, message=failure.message, http_status=failure.status,
                                  status="cancelled" if failure.code == "cancelled" else "failed",
                                  usage=usage, latency_ms=latency_ms)


def log_provider_call(request_id, failure, telemetry, usage, latency_ms):
    def identifier(value, pattern):
        return value if isinstance(value, str) and len(value) <= 160 and re.fullmatch(pattern, value) else None

    def counter(value, maximum=2**63 - 1):
        return value if type(value) is int and 0 <= value <= maximum else None

    telemetry = telemetry if isinstance(telemetry, Mapping) else {}
    usage = usage if isinstance(usage, Mapping) else {}
    status = telemetry.get("response_status")
    reason = telemetry.get("incomplete_reason")
    http_status = counter(telemetry.get("http_status"), 599)
    terminal_response = isinstance(status, str) and status in {"completed", "failed", "cancelled", "incomplete"}
    diagnostic = {
        "request_id": str(UUID(request_id)), "feature": replies.FEATURE,
        "outcome": "failed" if failure else "succeeded", "error_code": failure.code if failure else None,
        "request_unknown": bool(failure and not terminal_response
                                and (failure.code in {"cancelled", "provider_timeout", "abandoned"}
                                             or failure.code == "provider_error" and http_status is None)),
        "http_status": http_status if http_status is not None and http_status >= 100 else None,
        "openai_request_id": identifier(telemetry.get("openai_request_id"),
            r"(?:req_[A-Za-z0-9_-]+|[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})"),
        "response_id": identifier(telemetry.get("response_id"), r"resp_[A-Za-z0-9_-]+"),
        "response_status": status if isinstance(status, str) and status in {
            "completed", "failed", "in_progress", "cancelled", "queued", "incomplete"} else None,
        "incomplete_reason": reason if status == "incomplete" and isinstance(reason, str)
            and reason in {"max_output_tokens", "content_filter"} else None,
        "usage": {key: counter(usage.get(key)) for key in ("input_tokens", "output_tokens")},
        "latency_ms": counter(latency_ms, 2**31 - 1),
    }
    logger.log(logging.WARNING if failure else logging.INFO, "desktop_reply_provider %s",
               json.dumps(diagnostic, separators=(",", ":")))


async def disconnected(request, stopped):
    # is_disconnected uses an AnyIO cancel scope: task.cancel alone can be swallowed there.
    while not stopped.is_set():
        if await request.is_disconnected():
            return True
        await asyncio.sleep(0.1)
    return False


def create_router(*, current_authorization, csrf_token):
    router = APIRouter(prefix="/v1", include_in_schema=False)

    @router.post("/reply-suggestions")
    async def reply_suggestions(request: Request):
        started = time.monotonic()
        actor = payload = receipt = cipher = None
        model_task = watcher = None
        stopped = asyncio.Event()
        usage = {}
        telemetry = {}
        failure = None
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT):
                actor = await run_in_threadpool(authenticate, request, current_authorization, csrf_token)
                payload = await read_payload(request)
                settings = get_settings()
                cipher = store.result_cipher(settings.token_encryption_key)
                receipt = await run_in_threadpool(claim, actor, payload)
                state = receipt["claim_status"]
                if state == "conflict":
                    return error("request_conflict", "同一请求编号的内容发生变化，请重新生成。", 409)
                if state == "running":
                    return error("request_in_progress", "此请求正在处理，请稍后重试，不会重复调用 AI。", 409)
                if state == "failed":
                    return error(receipt["error_code"], receipt["error_message"], receipt["error_http_status"])
                if state == "cached":
                    value = json.loads(cipher.decrypt(receipt["result_ciphertext"]))
                    value["request_id"] = payload["request_id"]
                    await run_in_threadpool(authenticate, request, current_authorization, csrf_token)
                    return response(value)
                config, api_key = await run_in_threadpool(replies.configuration, settings)
                if await request.is_disconnected():
                    raise replies.ReplyError("cancelled", "请求已取消。", 499)
                remaining = TOTAL_TIMEOUT - (time.monotonic() - started)
                model_task = asyncio.create_task(replies.generate(
                    payload, actor, config, api_key, total_timeout=min(45.0, remaining),
                ))
                watcher = asyncio.create_task(disconnected(request, stopped))
                done, _ = await asyncio.wait((model_task, watcher), return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    raise replies.ReplyError("cancelled", "请求已取消。", 499)
                result, usage, telemetry = await model_task
                await run_in_threadpool(authenticate, request, current_authorization, csrf_token)
                saved = await run_in_threadpool(
                    finish, actor, payload, receipt, result=result, cipher=cipher, usage=usage,
                    latency_ms=round((time.monotonic() - started) * 1000),
                )
                if not saved:
                    raise replies.ReplyError("abandoned", "请求已失效，请重新生成。", 409)
                await run_in_threadpool(authenticate, request, current_authorization, csrf_token)
                return response(result)
        except (AuthError, ReplyInputError, replies.ReplyError) as exc:
            failure = exc
            usage = getattr(exc, "usage", None) or usage
            telemetry = getattr(exc, "telemetry", None) or telemetry
        except store.ActorInactiveError:
            failure = AuthError("actor_inactive", "账号授权已失效，请重新登录。", 401)
        except openai_client.OpenAIRequestError as exc:
            telemetry = exc.telemetry
            usage = exc.telemetry.get("usage", {})
            if isinstance(exc.__cause__, TimeoutError | httpx.TimeoutException):
                failure = replies.ReplyError("provider_timeout", "AI 请求超时，请重新发起生成。", 504)
            elif exc.is_quota:
                failure = replies.ReplyError("provider_quota", "AI 配额不足，请联系管理员。", 503)
            elif exc.telemetry.get("http_status") == 429:
                failure = replies.ReplyError("provider_rate_limit", "AI 服务限流，请稍后重新生成。", 429)
            else:
                failure = replies.ReplyError("provider_error", "AI 服务暂时无法完成请求。", 502)
        except openai_client.ModelOutputError as exc:
            telemetry = exc.telemetry
            usage = exc.telemetry.get("usage", {})
            failure = replies.ReplyError("invalid_ai_response", "AI 未返回有效建议，请重新生成。", 502)
        except TimeoutError:
            failure = replies.ReplyError("provider_timeout", "请求超过处理期限，请重新生成。", 504)
        except asyncio.CancelledError:
            failure = replies.ReplyError("cancelled", "请求已取消。", 499)
            raise
        except Exception as exc:
            # Never log exception text: transport/decoder exceptions may contain private inputs.
            logger.error("desktop_reply_failed error_type=%s", type(exc).__name__)
            failure = replies.ReplyError("provider_error", "服务暂时无法完成请求。", 503)
        finally:
            stopped.set()
            tasks = [task for task in (model_task, watcher) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if model_task is not None and model_task.done() and not model_task.cancelled():
                model_error = model_task.exception()
                if isinstance(model_error, replies.ReplyError | openai_client.OpenAIRequestError
                              | openai_client.ModelOutputError):
                    telemetry = getattr(model_error, "telemetry", None) or telemetry
                    usage = getattr(model_error, "usage", None) or telemetry.get("usage") or usage
                elif model_error is None:
                    completed = model_task.result()
                    usage, telemetry = completed[1] or usage, completed[2] or telemetry
            if failure is not None and receipt is not None and receipt["claim_status"] == "new":
                failure.code, failure.message = store.safe_error(failure.code, failure.message)
                context_reason = next((key for key, message in store.CONTEXT_MESSAGES.items()
                                       if failure.code == "context_unavailable" and failure.message == message), None)
                if context_reason:
                    with suppress(Exception):
                        logger.warning("desktop_reply_needs_review request_id=%s scope=%s reason=%s "
                                       "selected=%s context=%s",
                                       payload["request_id"], payload["reply_scope"], context_reason,
                                       len(payload["selected_messages"]),
                                       len(payload["conversation_context"]["messages"]))
                with suppress(Exception):
                    await run_in_threadpool(
                        finish, actor, payload, receipt, failure=failure, usage=usage,
                        latency_ms=round((time.monotonic() - started) * 1000),
                    )
            if receipt is not None and receipt["claim_status"] == "new" and model_task is not None:
                with suppress(Exception):
                    log_provider_call(payload["request_id"], failure, telemetry, usage,
                                      round((time.monotonic() - started) * 1000))
        return error(failure.code, failure.message, failure.status)

    return router
