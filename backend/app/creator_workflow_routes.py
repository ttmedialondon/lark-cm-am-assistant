"""Session/CSRF boundaries for the existing CM frontend, not an admin permission bypass."""

from __future__ import annotations

import hmac
import json
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse

from . import creator_workflow as workflow
from . import creator_workflow_ai as ai
from . import creator_workflow_evidence
from . import creator_workflow_import as imports

REMI_ID = "24d1ef13-3762-4359-ba11-d26c03cf1f7f"
REVIEWERS = {"9d5f4370-6463-4c3a-a2ed-67753a1cabc0", "1225b4a0-fbd8-431e-962e-25e66943a8a0"}


def create_router(*, current_authorization, render_template, csrf_token):
    router = APIRouter(prefix="/dashboard/creators", include_in_schema=False)

    def secure(response):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https:; "
            "font-src 'self' data: https://cdn.jsdelivr.net; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    def response(data, status=200):
        return secure(JSONResponse(jsonable_encoder(data), status_code=status))

    def authorize(request):
        auth = current_authorization(request)
        if not auth or auth["status"] != "active":
            raise PermissionError("登录授权已失效，请重新登录。")
        target = request.query_params.get("responsible_id")
        if target is None and str(auth["id"]) in REVIEWERS:
            target = REMI_ID
        if target == "unassigned":
            target = None
        scope = workflow.get_scope(auth["id"], UUID(target) if target else None)
        return auth, scope

    async def body(request, auth, *, binary=False):
        token = request.headers.get("x-csrf-token", "")
        if not token or not hmac.compare_digest(token, csrf_token(auth)):
            raise PermissionError("页面校验已失效，请刷新后重试。")
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.url.netloc:
            raise PermissionError("不能从其他网站提交。")
        limit = imports.MAX_FILE_BYTES if binary else 32_768
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > limit:
                raise workflow.WorkflowInputError("提交内容超过大小限制。")
        if binary:
            return bytes(raw)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise workflow.WorkflowInputError("提交格式无效。") from exc
        if not isinstance(value, dict):
            raise workflow.WorkflowInputError("提交格式无效。")
        return value

    def failure(exc):
        code = 403 if isinstance(exc, PermissionError) else 404 if isinstance(exc, LookupError) else 409
        return response({"error": str(exc)}, code)

    def listing(request, scope):
        params = request.query_params
        return workflow.list_records(
            scope,
            status=params.get("status", "all"),
            source=params.get("source", ""),
            reason=params.get("reason", ""),
            query=params.get("q", params.get("query", "")),
            page=int(params.get("page", 1)),
        )

    @router.get("")
    def page(request: Request):
        if not current_authorization(request):
            return secure(RedirectResponse("/", status_code=303))
        try:
            auth, scope = authorize(request)
            data = jsonable_encoder(listing(request, scope))
            return secure(
                render_template(
                    request,
                    "creator_workflow.html",
                    {
                        "authorization": auth,
                        "scope": scope,
                        "csrf_token": csrf_token(auth),
                        "creator_data": data,
                        "responsibles": workflow.list_responsibles(auth["id"]),
                    },
                )
            )
        except (ValueError, PermissionError, LookupError) as exc:
            return failure(exc)

    @router.get("/data")
    def data(request: Request):
        try:
            _, scope = authorize(request)
            return response(listing(request, scope))
        except (ValueError, PermissionError, LookupError) as exc:
            return failure(exc)

    @router.get("/users")
    def users(request: Request):
        try:
            _, scope = authorize(request)
            return response({"items": workflow.search_users(scope, request.query_params.get("q", ""))})
        except (ValueError, PermissionError, LookupError) as exc:
            return failure(exc)

    @router.get("/{candidate_id}/evidence")
    def evidence(request: Request, candidate_id: UUID):
        try:
            _, scope = authorize(request)
            return response(
                creator_workflow_evidence.evidence(
                    scope, candidate_id, request.query_params.get("chat_id"), int(request.query_params.get("page", 1))
                )
            )
        except (ValueError, PermissionError, LookupError) as exc:
            return failure(exc)

    @router.post("/import/preview")
    async def import_preview(request: Request):
        try:
            auth, scope = authorize(request)
            if not scope["can_import"]:
                raise PermissionError("当前页面为只读，不能导入。")
            raw = await body(request, auth, binary=True)
            return response(imports.preview(scope, raw, request.query_params.get("filename", "")))
        except (ValueError, PermissionError, LookupError) as exc:
            return failure(exc)

    @router.post("/import/{batch_id}/commit")
    async def import_commit(request: Request, batch_id: UUID):
        try:
            auth, scope = authorize(request)
            payload = await body(request, auth)
            return response(imports.commit(scope, batch_id, workflow._uuid(payload.get("request_id"))))
        except (ValueError, PermissionError, LookupError) as exc:
            return failure(exc)

    @router.get("/ai/status")
    def ai_status(request: Request):
        try:
            auth, scope = authorize(request)
            return response(
                {"run": ai.scan_status(auth["id"], scope["responsible_id"]) if scope["responsible_id"] else None}
            )
        except (ValueError, PermissionError, LookupError) as exc:
            return failure(exc)

    @router.post("/ai/scan")
    async def ai_scan(request: Request):
        try:
            auth, scope = authorize(request)
            payload = await body(request, auth)
            workflow._uuid(payload.get("request_id"))
            if not scope["responsible_id"]:
                raise workflow.WorkflowInputError("请先选择一位负责人。")
            return response(ai.queue_scan(auth["id"], scope["responsible_id"],
                                          retry_failed=payload.get("retry_failed") is True))
        except (ValueError, PermissionError, LookupError) as exc:
            return failure(exc)

    @router.post("/{candidate_id}/{action}")
    async def mutate(request: Request, candidate_id: UUID, action: str):
        try:
            auth, scope = authorize(request)
            payload = await body(request, auth)
            request_id = workflow._uuid(payload.get("request_id"))
            version = payload.get("version")
            if type(version) is not int:
                raise workflow.WorkflowInputError("缺少有效的记录版本，请刷新后重试。")
            if action == "draft":
                required = {"handle", "contact_id"}
                if not required.issubset(payload):
                    raise workflow.WorkflowInputError("草稿字段不完整，请重试。")
                record = workflow.save_draft(
                    scope,
                    candidate_id,
                    version,
                    handle=payload["handle"],
                    contact_id=payload["contact_id"],
                    relationship=payload.get("relationship"),
                    resolved_reasons=payload.get("resolved_reasons", []),
                    request_id=request_id,
                )
            elif action == "confirm":
                record = workflow.confirm_candidate(scope, candidate_id, version, request_id=request_id)
            elif action == "decision":
                record = workflow.decide_candidate(
                    scope, candidate_id, version, action=payload.get("action"), request_id=request_id
                )
            elif action == "assign":
                record = workflow.assign_candidate(
                    scope, candidate_id, version, responsible_id=payload.get("responsible_id"), request_id=request_id
                )
            else:
                raise LookupError("找不到该操作。")
            return response(record)
        except (ValueError, PermissionError, LookupError) as exc:
            return failure(exc)

    return router
