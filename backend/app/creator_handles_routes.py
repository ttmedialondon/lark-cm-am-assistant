"""Session-scoped creator candidates; CM review is a frontend permission, not an admin grant."""

import json
from urllib.parse import unquote
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import creator_broadcast, creator_imports, creator_roster
from .http_helpers import (
    csrf_matches as _csrf_matches,
)
from .http_helpers import (
    error_response as error,
)
from .http_helpers import (
    form_fields as _form_fields,
)
from .http_helpers import (
    json_response as payload,
)
from .http_helpers import (
    secure_response as secure,
)


def create_router(*, current_authorization, render_template, csrf_token):
    router = APIRouter(prefix="/creators", include_in_schema=False)

    def actor(auth):
        if not auth or auth["status"] != "active":
            raise PermissionError("登录授权已失效，请重新登录。")
        return auth

    async def form_for(request):
        auth = actor(await run_in_threadpool(current_authorization, request))
        form = await _form_fields(request)
        if not _csrf_matches(form, csrf_token(auth)):
            raise ValueError("请求校验失败，请刷新页面后重试。")
        return auth, form

    @router.get("/broadcast")
    def broadcast_page(request: Request):
        auth = current_authorization(request)
        if not auth:
            return secure(RedirectResponse("/", status_code=303))
        try:
            auth = actor(auth)
            data = creator_broadcast.page_data(auth["id"])
            return secure(render_template(request, "creator_broadcast.html", {
                "authorization": auth, "broadcast_data": data, "csrf_token": csrf_token(auth),
            }))
        except PermissionError as exc:
            return error(exc)

    @router.post("/broadcast/match")
    @router.post("/broadcast/preview")
    @router.post("/broadcast/resolve")
    @router.post("/broadcast/groups")
    @router.post("/broadcast/lists/save")
    @router.post("/broadcast/lists/load")
    @router.post("/broadcast/lists/delete")
    @router.post("/broadcast/templates/save")
    @router.post("/broadcast/templates/delete")
    async def broadcast_read(request: Request):
        """Bounded, session-scoped routing and personal lists; never sends."""
        try:
            auth = actor(await run_in_threadpool(current_authorization, request))
            if not _csrf_matches({"csrf_token": request.headers.get("X-CSRF-Token", "")}, csrf_token(auth)):
                raise ValueError("请求校验失败，请刷新页面后重试。")
            if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise ValueError("请使用 JSON 提交名单。")
            content = bytearray()
            async for chunk in request.stream():
                content.extend(chunk[:300001 - len(content)])
                if len(content) > 300000:
                    raise ValueError("请求过大，请拆分名单。")
            try:
                fields = json.loads(content)
            except (ValueError, UnicodeError, RecursionError):
                raise ValueError("请求格式无效。") from None
            if not isinstance(fields, dict):
                raise ValueError("请求格式无效。")
            args = (auth["id"], fields.get("cm_id", "all"), fields.get("purpose", "support"))
            action = request.url.path.rsplit("/", 1)[-1]
            if "/templates/" in request.url.path:
                if action == "save":
                    result = await run_in_threadpool(creator_broadcast.template_save, auth["id"], fields)
                else:
                    result = await run_in_threadpool(
                        creator_broadcast.template_delete, auth["id"], fields.get("id"), fields.get("revision"),
                    )
            elif action == "groups":
                result = await run_in_threadpool(creator_broadcast.groups, *args)
            elif action == "match":
                result = await run_in_threadpool(creator_broadcast.match, args[0], args[1], fields.get("raw"), args[2])
            elif action == "resolve":
                result = await run_in_threadpool(
                    creator_broadcast.resolve, *args, fields.get("contact_ids"),
                    fields.get("selections"),
                )
            elif action == "save":
                result = await run_in_threadpool(creator_broadcast.list_save, auth["id"], fields)
            elif action == "load":
                result = await run_in_threadpool(creator_broadcast.list_load, auth["id"], fields.get("id"))
            elif action == "delete":
                result = await run_in_threadpool(
                    creator_broadcast.list_delete, auth["id"], fields.get("id"), fields.get("revision"),
                )
            else:
                result = await run_in_threadpool(
                    creator_broadcast.preview, *args,
                    fields.get("selections"), fields.get("message"), fields.get("mention"),
                )
            return payload(result)
        except (ValueError, LookupError, PermissionError) as exc:
            return error(exc)

    @router.get("")
    @router.get("/data")
    def page(request: Request, cm_id: UUID | None = None, tab: str = "mine", q: str = "", page: int = 1):
        auth = current_authorization(request)
        if request.url.path == "/creators" and not auth:
            return secure(RedirectResponse("/", status_code=303))
        try:
            auth = actor(auth)
            tab = tab if tab in ('candidates', 'unassigned') else 'mine'
            data = creator_roster.page_data(auth["id"], cm_id=cm_id, tab=tab, query=q[:120], page=max(1, page))
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
    @router.post("/tasks/{task_id}/confirm")
    @router.post("/tasks/{task_id}/defer")
    async def retired_action(request: Request):
        try:
            await form_for(request)
            return payload({"error": "此操作已停用，请通过 Excel 达人名单匹配和核对。"}, 410)
        except (ValueError, LookupError, PermissionError) as exc:
            return error(exc)

    @router.get("/tasks/{task_id}/evidence")
    def retired_evidence(request: Request):
        try:
            actor(current_authorization(request))
            return payload({"error": "旧识别证据入口已退役。"}, 410)
        except PermissionError as exc:
            return error(exc)


    @router.post("/imports/preview")
    async def import_preview(request: Request, cm_id: UUID):
        try:
            auth = actor(await run_in_threadpool(current_authorization, request))
            if not _csrf_matches({"csrf_token": request.headers.get("X-CSRF-Token", "")}, csrf_token(auth)):
                raise ValueError("请求校验失败，请刷新页面后重试。")
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
            )
            return payload(result)
        except (ValueError, LookupError, PermissionError) as exc:
            return error(exc)

    @router.get("/imports/data")
    def import_data(request: Request, cm_id: UUID, q: str = "", page: int = 1):
        try:
            auth = actor(current_authorization(request))
            return payload(creator_imports.page_data(auth["id"], cm_id, q=q[:120], page=max(1, page)))
        except (ValueError, LookupError, PermissionError) as exc:
            return error(exc)

    @router.get("/imports/users")
    def import_users(request: Request, cm_id: UUID, q: str = ""):
        try:
            auth = actor(current_authorization(request))
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
            auth = actor(current_authorization(request))
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
