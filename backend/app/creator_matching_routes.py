"""Shared AI task pages; domain modules retain their own live access and write checks."""

from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import admin_permissions, creator_group_matching, creator_handle_matching, creator_responsibility_matching
from .config import get_settings
from .db import connection
from .http_helpers import csrf_matches, error_response, form_fields, secure_response


def create_router(feature, *, current_authorization, render_template, csrf_token):
    path, template, matching = {
        "handle": ("creator-handle-matching", "creator_handle_matching", creator_handle_matching),
        "responsibility": ("creator-responsibility", "creator_responsibility", creator_responsibility_matching),
        "groups": ("creator-groups", "creator_groups", creator_group_matching),
    }[feature]
    base = "/admin/ai/" + path
    router = APIRouter(prefix=base, include_in_schema=False)
    operations = {"recheck", "cancel"} if feature == "handle" else {"retry", "recheck", "cancel"}
    enqueue = matching.enqueue_history if feature == "handle" else matching.enqueue

    def actor(request):
        auth = current_authorization(request)
        if not auth or auth["status"] != "active":
            raise PermissionError("请重新登录。")
        return auth

    def render(request, auth, data=None, evidence=None, access=None):
        access = access if access is not None else data.pop("admin_access")
        return secure_response(render_template(request, f"admin/{template}.html", {
            "authorization": auth, "matching": data, "evidence": evidence,
            **({"purposes": matching.PURPOSES} if feature == "groups" else {}),
            "csrf_token": csrf_token(auth), "admin_role": "owner" if access["is_owner"] else "viewer",
            "active_page": "ai", "admin_landing_path": access["landing_path"],
            "admin_permissions": access["permissions"], "permission_pages": admin_permissions.PAGES,
        }))

    def error(request, exc):
        return error_response(exc, request=request, render_template=render_template,
                              return_url="/admin/ai", return_label="返回 AI 功能")

    @router.get("")
    def page(request: Request, page: int = 1):
        try:
            auth = actor(request)
            with connection() as conn, conn.transaction():
                data = matching.page_data(conn, auth["id"], get_settings(), max(1, page))
            return render(request, auth, data)
        except (ValueError, LookupError, PermissionError) as exc:
            return error(request, exc)

    if feature != "handle":
        @router.get("/jobs/{job_id}/evidence")
        def evidence(request: Request, job_id: UUID):
            try:
                auth = actor(request)
                with connection() as conn, conn.transaction():
                    data = matching.task_evidence(conn, auth["id"], get_settings(), job_id)
                    access = admin_permissions.require_page(conn, auth["id"], "ai")
                return render(request, auth, evidence=data, access=access)
            except (ValueError, LookupError, PermissionError) as exc:
                return error(request, exc)

    @router.post("/enqueue")
    @router.post("/jobs/{job_id}/{operation}")
    async def mutate(request: Request, job_id: UUID | None = None,
                     operation: str = "enqueue", chat_id: str | None = None):
        try:
            auth = await run_in_threadpool(actor, request)
            form = await form_fields(request)
            if not csrf_matches(form, csrf_token(auth)):
                raise ValueError("请求校验失败，请刷新页面。")
            try:
                page_number = max(1, int(form.get("page", "1")))
                source_job = UUID(form["source_job_id"]) if feature == "groups" and form.get("source_job_id") else None
            except ValueError as exc:
                raise ValueError("请求标识无效。" if feature == "groups" else "页码无效。") from exc
            if feature == "responsibility" and any(name in form for name in ("mode", "limit", "contact_ids")):
                raise ValueError("小样本入口已停用，请使用正式匹配。")
            if job_id is not None and operation not in operations:
                raise ValueError("操作无效。")

            def save():
                with connection() as conn, conn.transaction():
                    if feature == "groups" and chat_id is not None:
                        tags = [tag.strip() for tag in form.get("tags", "").replace("，", ",").split(",")
                                if tag.strip()]
                        matching.confirm(conn, auth["id"], chat_id, form.get("purpose", ""), tags,
                                         locked=form.get("locked") == "1", job_id=source_job)
                    elif job_id is not None:
                        matching.action(conn, auth["id"], job_id, operation)
                    else:
                        enqueue(conn, auth["id"])

            await run_in_threadpool(save)
            target = base + (f"?page={page_number}" if page_number > 1 and (
                feature == "groups" or job_id is not None) else "")
            return secure_response(RedirectResponse(target, status_code=303))
        except (ValueError, LookupError, PermissionError) as exc:
            return error(request, exc)

    if feature == "groups":
        router.add_api_route("/chats/{chat_id}/classification", mutate, methods=["POST"])
    return router
