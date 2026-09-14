"""Session-scoped creator candidates; CM review is a frontend permission, not an admin grant."""

import json
from urllib.parse import unquote
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import creator_handles, creator_imports
from .admin import _csrf_matches, _form_fields


def create_router(*, current_authorization, render_template, csrf_token):
    router = APIRouter(prefix="/creators", include_in_schema=False)

    def secure(response):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def payload(value, code=200):
        return secure(JSONResponse(jsonable_encoder(value), status_code=code))

    def error(exc):
        code = 403 if isinstance(exc, PermissionError) else 404 if isinstance(exc, LookupError) else 400
        return payload({"error": str(exc)}, code)

    def actor(request):
        auth = current_authorization(request)
        if not auth or auth["status"] != "active":
            raise PermissionError("登录授权已失效，请重新登录。")
        return auth

    async def form_for(request):
        auth = actor(request)
        form = await _form_fields(request)
        if not _csrf_matches(form, csrf_token(auth)):
            raise ValueError("请求校验失败，请刷新页面后重试。")
        return auth, form

    @router.get("")
    @router.get("/data")
    def page(request: Request, cm_id: UUID | None = None, tab: str = "mine", q: str = "", page: int = 1):
        if request.url.path == "/creators" and not current_authorization(request):
            return secure(RedirectResponse("/", status_code=303))
        try:
            auth = actor(request)
            tab = "candidates" if tab == "candidates" else "mine"
            data = creator_handles.page_data(auth["id"], cm_id=cm_id, tab=tab, query=q[:120], page=max(1, page))
            if request.url.path.endswith("/data"):
                return payload(data)
            return secure(render_template(request, "creator_handles.html", {
                "authorization": auth, "creator_data": jsonable_encoder(data),
                "creator_tab": tab, "creator_query": q[:120], "csrf_token": csrf_token(auth),
            }))
        except (ValueError, LookupError, PermissionError) as exc:
            return error(exc)

    @router.post("/preview")
    @router.post("/runs/{run_id}/start")
    @router.post("/runs/{run_id}/pause")
    @router.post("/runs/{run_id}/resume")
    @router.post("/runs/{run_id}/auto-admit")
    @router.post("/tasks/{task_id}/recheck")
    @router.post("/people/{contact_id}/type")
    async def retired_action(request: Request):
        try:
            await form_for(request)
            return payload({"error": "此操作已停用，请通过 Excel CAP 名单匹配和核对。"}, 410)
        except (ValueError, LookupError, PermissionError) as exc:
            return error(exc)

    @router.post("/tasks/{task_id}/confirm")
    @router.post("/tasks/{task_id}/defer")
    async def review(request: Request, task_id: UUID):
        try:
            auth, form = await form_for(request)
            if request.url.path.endswith("/confirm"):
                try:
                    handles = json.loads(form.get("handles", "[]"))
                except ValueError as exc:
                    raise ValueError("请输入或选择有效的 handle。") from exc
                if not isinstance(handles, list) or not 1 <= len(handles) <= 20 or any(
                    not isinstance(handle, str) or not handle.strip() or len(handle) > 160 for handle in handles
                ):
                    raise ValueError("请填写 1 至 20 个 handle，每个单独一行。")
                await run_in_threadpool(creator_handles.confirm_task, auth["id"], task_id, handles)
            else:
                await run_in_threadpool(creator_handles.defer_task, auth["id"], task_id)
            return payload({"ok": True})
        except (ValueError, LookupError, PermissionError) as exc:
            return error(exc)

    @router.get("/tasks/{task_id}/evidence")
    def evidence(request: Request, task_id: UUID):
        try:
            auth = actor(request)
            return payload(creator_handles.task_evidence(auth["id"], task_id))
        except (ValueError, LookupError, PermissionError) as exc:
            return error(exc)

    @router.post("/imports/preview")
    async def import_preview(request: Request, cm_id: UUID, creator_type: str = "cap"):
        try:
            auth = actor(request)
            if not _csrf_matches({"csrf_token": request.headers.get("X-CSRF-Token", "")}, csrf_token(auth)):
                raise ValueError("请求校验失败，请刷新页面后重试。")
            if creator_type != "cap":
                raise ValueError("当前导入仅支持 CAP 达人名单。")
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/octet-stream":
                raise ValueError("请上传 .xlsx 文件。")
            limit = creator_imports.MAX_FILE_BYTES
            if int(request.headers.get("content-length", "0")) > limit:
                raise ValueError("文件过大，请上传不超过 2 MB 的表格。")
            content = bytearray()
            async for chunk in request.stream():
                if len(content) + len(chunk) > limit:
                    raise ValueError("文件过大，请上传不超过 2 MB 的表格。")
                content.extend(chunk)
            filename = unquote(request.headers.get("X-Filename", "")[:512])
            result = await run_in_threadpool(
                creator_imports.preview_import, auth["id"], cm_id, filename, bytes(content),
                creator_type="cap",
            )
            return payload(result)
        except (ValueError, LookupError, PermissionError) as exc:
            return error(exc)

    @router.get("/imports/data")
    def import_data(request: Request, cm_id: UUID, q: str = "", page: int = 1):
        try:
            auth = actor(request)
            return payload(creator_imports.page_data(auth["id"], cm_id, q=q[:120], page=max(1, page)))
        except (ValueError, LookupError, PermissionError) as exc:
            return error(exc)

    @router.get("/imports/users")
    def import_users(request: Request, cm_id: UUID, q: str = ""):
        try:
            auth = actor(request)
            return payload(creator_imports.search_users(auth["id"], cm_id, q[:120]))
        except (ValueError, LookupError, PermissionError) as exc:
            return error(exc)

    @router.post("/imports/{batch_id}/start")
    @router.post("/imports/{batch_id}/pause")
    @router.post("/imports/{batch_id}/resume")
    @router.post("/imports/{batch_id}/recheck")
    async def import_action(request: Request, batch_id: UUID):
        try:
            auth, _form = await form_for(request)
            actions = {
                "start": creator_imports.start_import,
                "pause": creator_imports.pause_import,
                "resume": creator_imports.resume_import,
                "recheck": creator_imports.recheck_batch,
            }
            await run_in_threadpool(actions[request.url.path.rsplit("/", 1)[-1]], auth["id"], batch_id)
            return payload({"ok": True})
        except (ValueError, LookupError, PermissionError) as exc:
            return error(exc)

    @router.get("/imports/rows/{row_id}/evidence")
    def import_evidence(request: Request, row_id: UUID):
        try:
            auth = actor(request)
            return payload(creator_imports.row_evidence(auth["id"], row_id))
        except (ValueError, LookupError, PermissionError) as exc:
            return error(exc)

    @router.post("/imports/rows/{row_id}/confirm")
    @router.post("/imports/rows/{row_id}/defer")
    async def import_review(request: Request, row_id: UUID):
        try:
            auth, form = await form_for(request)
            if request.url.path.endswith("/confirm"):
                handle = form.get("handle", "").strip()
                if not handle or len(handle) > 160:
                    raise ValueError("请填写有效的 handle。")
                try:
                    contact_id = UUID(form.get("contact_id", ""))
                except ValueError as exc:
                    raise ValueError("请搜索并选择对应用户。") from exc
                await run_in_threadpool(
                    creator_imports.confirm_row, auth["id"], row_id, handle, contact_id,
                )
            else:
                await run_in_threadpool(creator_imports.defer_row, auth["id"], row_id)
            return payload({"ok": True})
        except (ValueError, LookupError, PermissionError) as exc:
            return error(exc)

    return router
