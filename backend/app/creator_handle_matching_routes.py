"""AI page readers can view tasks; queue controls retain the owner/CSRF boundary."""

from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import admin_permissions
from . import creator_handle_matching as matching
from .config import get_settings
from .db import connection
from .http_helpers import csrf_matches, error_response, form_fields, secure_response


def create_router(*, current_authorization, render_template, csrf_token):
    router = APIRouter(prefix="/admin/ai/creator-handle-matching", include_in_schema=False)

    def actor(request):
        auth = current_authorization(request)
        if not auth or auth["status"] != "active":
            raise PermissionError("请重新登录。")
        return auth

    @router.get("")
    def page(request: Request, page: int = 1):
        try:
            auth = actor(request)
            with connection() as conn:
                data = matching.page_data(conn, auth["id"], get_settings(), max(1, page))
            access = data.pop("admin_access")
            return secure_response(
                render_template(
                    request,
                    "admin/creator_handle_matching.html",
                    {
                        "authorization": auth,
                        "matching": data,
                        "csrf_token": csrf_token(auth),
                        "admin_role": "owner" if access["is_owner"] else "viewer",
                        "active_page": "ai",
                        "admin_landing_path": access["landing_path"],
                        "admin_permissions": access["permissions"],
                        "permission_pages": admin_permissions.PAGES,
                    },
                )
            )
        except (ValueError, LookupError, PermissionError) as exc:
            return error_response(exc, request=request, render_template=render_template,
                                  return_url="/admin/ai", return_label="返回 AI 功能")

    @router.post("/enqueue")
    @router.post("/jobs/{job_id}/{operation}")
    async def mutate(request: Request, job_id: UUID | None = None, operation: str = "enqueue"):
        try:
            auth = await run_in_threadpool(actor, request)
            form = await form_fields(request)
            if not csrf_matches(form, csrf_token(auth)):
                raise ValueError("请求校验失败，请刷新页面。")
            try:
                page_number = max(1, int(form.get("page", "1")))
            except ValueError as exc:
                raise ValueError("页码无效。") from exc
            if job_id is not None and operation not in {"recheck", "cancel"}:
                raise ValueError("操作无效。")

            def save():
                with connection() as conn, conn.transaction():
                    if job_id is None:
                        matching.enqueue_history(conn, auth["id"])
                    else:
                        matching.action(conn, auth["id"], job_id, operation)

            await run_in_threadpool(save)
            target = "/admin/ai/creator-handle-matching"
            if job_id is not None and page_number > 1:
                target += f"?page={page_number}"
            return secure_response(RedirectResponse(target, status_code=303))
        except (ValueError, LookupError, PermissionError) as exc:
            return error_response(exc, request=request, render_template=render_template,
                                  return_url="/admin/ai", return_label="返回 AI 功能")

    return router
