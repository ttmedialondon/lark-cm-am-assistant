"""Owner-only responsibility suggestions and on-demand audited evidence."""

import re
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import admin_permissions
from . import creator_responsibility_matching as matching
from .config import get_settings
from .db import connection
from .http_helpers import csrf_matches, error_response, form_fields, secure_response


def create_router(*, current_authorization, render_template, csrf_token):
    router = APIRouter(prefix="/admin/ai/creator-responsibility", include_in_schema=False)

    def actor(request):
        auth = current_authorization(request)
        if not auth or auth["status"] != "active":
            raise PermissionError("请重新登录。")
        return auth

    def render(request, auth, data, evidence=None):
        return secure_response(
            render_template(
                request,
                "admin/creator_responsibility.html",
                {
                    "authorization": auth,
                    "matching": data,
                    "evidence": evidence,
                    "csrf_token": csrf_token(auth),
                    "admin_role": "owner",
                    "active_page": "ai",
                    "admin_landing_path": "/admin",
                    "admin_permissions": admin_permissions.PAGE_KEYS,
                    "permission_pages": admin_permissions.PAGES,
                },
            )
        )

    @router.get("")
    def page(request: Request, page: int = 1):
        try:
            auth = actor(request)
            with connection() as conn, conn.transaction():
                data = matching.page_data(conn, auth["id"], get_settings(), max(1, page))
            return render(request, auth, data)
        except (ValueError, LookupError, PermissionError) as exc:
            return error_response(exc)

    @router.get("/jobs/{job_id}/evidence")
    def evidence(request: Request, job_id: UUID):
        try:
            auth = actor(request)
            with connection() as conn, conn.transaction():
                data = matching.task_evidence(conn, auth["id"], get_settings(), job_id)
            return render(request, auth, None, data)
        except (ValueError, LookupError, PermissionError) as exc:
            return error_response(exc)

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
                limit = int(form.get("limit", "5"))
                ids = list(
                    dict.fromkeys(
                        UUID(value) for value in re.split(r"[\s,]+", form.get("contact_ids", "").strip()) if value
                    )
                )
            except ValueError as exc:
                raise ValueError("请填写有效的样本数量或联系人 ID。") from exc
            if not 1 <= limit <= 10 or len(ids) > 10:
                raise ValueError("小样本每次最多处理 10 位达人。")
            mode = form.get("mode", "sample")
            if mode not in {"sample", "history"} or (mode == "history" and ids):
                raise ValueError("入队方式无效。")
            if job_id is not None and operation not in {"retry", "recheck", "cancel"}:
                raise ValueError("操作无效。")

            def save():
                with connection() as conn, conn.transaction():
                    if job_id is None:
                        matching.enqueue(
                            conn,
                            auth["id"],
                            contact_ids=ids or None,
                            limit=limit,
                            all_contacts=mode == "history",
                        )
                    else:
                        matching.action(conn, auth["id"], job_id, operation)

            await run_in_threadpool(save)
            target = "/admin/ai/creator-responsibility"
            if job_id is not None and page_number > 1:
                target += f"?page={page_number}"
            return secure_response(RedirectResponse(target, status_code=303))
        except (ValueError, LookupError, PermissionError) as exc:
            return error_response(exc)

    return router
