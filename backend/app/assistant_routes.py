"""Same-origin assistant submission and read-only progress/evidence endpoints."""

import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import assistant, assistant_tools
from .config import get_settings
from .db import connection
from .http_helpers import csrf_matches, error_response, json_response, secure_response

MAX_BODY_BYTES = 32 * 1024
TERMINAL = {"completed", "cancelled", "failed", "unknown"}


async def payload(request):
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise HTTPException(415, "请使用 JSON 提交请求。")
    content = bytearray()
    async for piece in request.stream():
        content.extend(piece[:MAX_BODY_BYTES + 1 - len(content)])
        if len(content) > MAX_BODY_BYTES:
            raise HTTPException(413, "输入过长，请缩短后提交。")
    try:
        value = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("请求格式无效，请刷新后重试。") from exc
    if not isinstance(value, dict):
        raise ValueError("请求格式无效，请刷新后重试。")
    return value


def event(name, value):
    return f"event: {name}\ndata: {json.dumps(jsonable_encoder(value), ensure_ascii=False, separators=(',', ':'))}\n\n"


def failure(exc):
    if isinstance(exc, HTTPException):
        return json_response({"error": exc.detail}, exc.status_code)
    return error_response(exc)


def create_router(*, current_authorization, csrf_token):
    router = APIRouter(prefix="/assistant", include_in_schema=False)

    def actor(request, *, write=False):
        auth = current_authorization(request)
        if not auth or auth.get("status") != "active":
            raise HTTPException(401, "登录已失效，请重新登录。")
        if write and not csrf_matches(
            {"csrf_token": request.headers.get("X-CSRF-Token", "")}, csrf_token(auth)
        ):
            raise PermissionError("请求校验失败，请刷新页面。")
        return auth["id"]

    def read(request, function, reference):
        actor_id = actor(request)
        with connection() as conn, conn.transaction():
            return function(conn, actor_id, reference, get_settings())

    @router.get("/scopes")
    def scopes(request: Request):
        try:
            actor_id = actor(request)
            with connection() as conn, conn.transaction():
                return json_response(assistant_tools.scopes_for(conn, actor_id))
        except (HTTPException, PermissionError, LookupError, ValueError) as exc:
            return failure(exc)

    @router.post("/turns")
    async def submit(request: Request):
        try:
            actor_id = await run_in_threadpool(actor, request, write=True)
            value = await payload(request)

            def save():
                with connection() as conn, conn.transaction():
                    return assistant.submit(conn, actor_id, value, get_settings())

            return json_response(await run_in_threadpool(save), 202)
        except (HTTPException, PermissionError, LookupError, ValueError) as exc:
            return failure(exc)

    @router.get("/sessions/{session_id}")
    def session(request: Request, session_id: UUID):
        try:
            return json_response(read(request, assistant.session_view, session_id))
        except (HTTPException, PermissionError, LookupError, ValueError) as exc:
            return failure(exc)

    @router.get("/turns/{turn_id}/events")
    async def events(request: Request, turn_id: UUID):
        try:
            first = await run_in_threadpool(read, request, assistant.turn_view, turn_id)
        except (HTTPException, PermissionError, LookupError, ValueError) as exc:
            return failure(exc)

        async def stream():
            previous = {}
            value = first
            idle = 0
            while not await request.is_disconnected():
                if value.get("error_code") in {"permission", "source_changed"}:
                    code = value["error_code"]
                    yield event("error", {"message": assistant.ERRORS[code], "code": code, "clear": True})
                    yield event("done", {"turn_id": str(turn_id), "status": "failed"})
                    return
                scope = value.get("scope") or (value.get("result") or {}).get("scope")
                progress = value.get("progress")
                progress = {**progress, "status": value["status"]} if isinstance(progress, dict) else {
                    "status": value["status"], "message": progress or ""
                }
                changes = [("scope", scope), ("progress", progress), ("result", value.get("result")),
                           ("error", value.get("error"))]
                changed = False
                for name, item in changes:
                    if item is None:
                        continue
                    encoded = json.dumps(jsonable_encoder(item), sort_keys=True, ensure_ascii=False)
                    if previous.get(name) != encoded:
                        previous[name] = encoded
                        changed = True
                        yield event(name, item)
                if value["status"] in TERMINAL:
                    yield event("done", {"turn_id": str(turn_id), "status": value["status"]})
                    return
                idle = 0 if changed else idle + 1
                if idle >= 15:
                    yield ": keepalive\n\n"
                    idle = 0
                await asyncio.sleep(1)
                if await request.is_disconnected():
                    return
                try:
                    value = await run_in_threadpool(read, request, assistant.turn_view, turn_id)
                except (HTTPException, PermissionError, LookupError, ValueError) as exc:
                    denied = isinstance(exc, PermissionError) or (
                        isinstance(exc, HTTPException) and exc.status_code == 401
                    )
                    yield event("error", {"message": "登录或查看权限已变化，请重新打开助手。" if denied else
                                           "这轮内容已不可读取，请重新打开助手。", "clear": True})
                    yield event("done", {"turn_id": str(turn_id), "status": "failed"})
                    return

        response = StreamingResponse(stream(), media_type="text/event-stream")
        response.headers["X-Accel-Buffering"] = "no"
        return secure_response(response)

    @router.post("/turns/{turn_id}/cancel")
    def cancel(request: Request, turn_id: UUID):
        try:
            actor_id = actor(request, write=True)
            with connection() as conn, conn.transaction():
                value = assistant.cancel(conn, actor_id, turn_id, get_settings())
            return json_response(value)
        except (HTTPException, PermissionError, LookupError, ValueError) as exc:
            return failure(exc)

    @router.get("/evidence/{reference}")
    def evidence(request: Request, reference: str):
        try:
            if len(reference) > 512:
                raise ValueError("原文引用无效。")
            return json_response(read(request, assistant_tools.evidence, reference))
        except (HTTPException, PermissionError, LookupError, ValueError) as exc:
            return failure(exc)

    return router
