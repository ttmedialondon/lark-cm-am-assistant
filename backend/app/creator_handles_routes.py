"""Session-scoped creator candidates; CM review is a frontend permission, not an admin grant."""

import json
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import admin_permissions, creator_handles
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

    def actor(request, *, owner=False):
        auth = current_authorization(request)
        if not auth or auth["status"] != "active":
            raise PermissionError("登录授权已失效，请重新登录。")
        if owner and not admin_permissions.access_for(auth["id"])["is_owner"]:
            raise PermissionError("扫描设置仅限系统主人操作。")
        return auth

    async def form_for(request, *, owner=False):
        auth = actor(request, owner=owner)
        form = await _form_fields(request)
        if not _csrf_matches(form, csrf_token(auth)):
            raise ValueError("请求校验失败，请刷新页面后重试。")
        return auth, form

    @router.get("")
    @router.get("/data")
    def page(request: Request, cm_id: UUID | None = None, tab: str = "candidates", q: str = "", page: int = 1):
        if request.url.path == "/creators" and not current_authorization(request):
            return secure(RedirectResponse("/", status_code=303))
        try:
            auth = actor(request)
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
    async def preview(request: Request):
        try:
            auth, form = await form_for(request, owner=True)
            cm_id = UUID(form.get("cm_id", ""))
            ownership_run = UUID(form["ownership_run_id"]) if form.get("ownership_run_id") else None
            run_id = await run_in_threadpool(creator_handles.create_preview, auth["id"], cm_id, ownership_run)
            return payload({"run_id": run_id})
        except (ValueError, LookupError, PermissionError) as exc:
            return error(exc)

    @router.post("/runs/{run_id}/start")
    @router.post("/runs/{run_id}/pause")
    @router.post("/runs/{run_id}/resume")
    @router.post("/runs/{run_id}/auto-admit")
    async def manage(request: Request, run_id: UUID):
        try:
            auth, form = await form_for(request, owner=True)
            action = request.url.path.rsplit("/", 1)[-1]
            if action == "auto-admit":
                if form.get("enabled") not in {"0", "1"}:
                    raise ValueError("请选择是否开启自动入库。")
                await run_in_threadpool(creator_handles.set_auto_admit, auth["id"], run_id, form["enabled"] == "1")
            else:
                function = {"start": creator_handles.start_run, "pause": creator_handles.pause_run,
                            "resume": creator_handles.resume_run}[action]
                await run_in_threadpool(function, auth["id"], run_id)
            return payload({"run_id": run_id})
        except (ValueError, LookupError, PermissionError) as exc:
            return error(exc)

    @router.post("/tasks/{task_id}/confirm")
    @router.post("/tasks/{task_id}/defer")
    @router.post("/tasks/{task_id}/recheck")
    async def review(request: Request, task_id: UUID):
        try:
            recheck = request.url.path.endswith("/recheck")
            auth, form = await form_for(request, owner=recheck)
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
            elif recheck:
                await run_in_threadpool(creator_handles.recheck_task, auth["id"], task_id)
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

    return router
