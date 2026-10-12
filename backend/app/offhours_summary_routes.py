"""Session-scoped summaries and human-reviewed reply drafts for confirmed creators."""

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import offhours_summary, summary_periods, summary_replies
from .db import connection
from .http_helpers import conditional_response, error_response, secure_response
from .http_helpers import csrf_matches as _csrf_matches


def create_router(*, current_authorization, render_template, csrf_token=lambda auth: ""):
    router = APIRouter(prefix="/dashboard/offhours-summary", include_in_schema=False)

    def secure(response):
        secure_response(response)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https:; "
            "font-src 'self' data: https://cdn.jsdelivr.net; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    def read(request, auth):
        if not auth or auth["status"] != "active":
            raise PermissionError("登录授权已失效，请重新登录。")
        with connection() as conn, conn.transaction():
            try:
                cm_id, _start, _end, selection = summary_periods.selection(
                    conn,
                    auth["id"],
                    request.query_params.get("cm_id"),
                    request.query_params.get("date"),
                    request.query_params.get("period"),
                )
            except ValueError as exc:
                raise offhours_summary.SummaryInputError("请选择有效的 CM、日期和时段。") from exc
            data = offhours_summary.get_summary_view(
                conn,
                auth["id"],
                cm_id=cm_id,
                day=selection["date"],
                period=selection["period"],
            )
            data["selection"] = selection
        return auth, jsonable_encoder(data)

    def denied(exc):
        return secure(JSONResponse({"error": str(exc)}, status_code=403))

    @router.get("")
    def page(request: Request):
        auth = current_authorization(request)
        if not auth:
            return secure(RedirectResponse("/", status_code=303))
        try:
            auth, data = read(request, auth)
            return secure(
                render_template(
                    request,
                    "offhours_summary.html",
                    {
                        "authorization": auth,
                        "summary_data": data,
                        "csrf_token": csrf_token(auth),
                    },
                )
            )
        except (PermissionError, offhours_summary.SummaryInputError) as exc:
            return secure(error_response(PermissionError(str(exc)), request=request, render_template=render_template))

    @router.get("/data")
    def data(request: Request):
        try:
            _, result = read(request, current_authorization(request))
            response = secure(JSONResponse(result))
            # No cross-request cache: live grants/material are checked before a 304.
            return conditional_response(request, response)
        except (PermissionError, offhours_summary.SummaryInputError) as exc:
            return denied(exc)

    def reply_actor(request, *, write=False):
        auth = current_authorization(request)
        if not auth or auth["status"] != "active":
            raise PermissionError("登录已失效，请重新登录。")
        token = csrf_token(auth)
        if write and (not token or not _csrf_matches({"csrf_token": request.headers.get("X-CSRF-Token", "")}, token)):
            raise PermissionError("请求校验失败，请刷新页面。")
        return auth

    @router.post("/replies")
    async def reply_create(request: Request):
        try:
            auth = await run_in_threadpool(reply_actor, request, write=True)
            content = bytearray()
            async for piece in request.stream():
                content.extend(piece)
                if len(content) > 120_000:
                    raise ValueError("回复正文过大。")
            import json

            body = json.loads(content)
            if not isinstance(body, dict) or not isinstance(body.get("event_id"), str):
                raise ValueError("请指定事件。")

            def submit():
                with connection() as conn, conn.transaction():
                    return summary_replies.enqueue(
                        conn,
                        auth["id"],
                        body["event_id"],
                        offhours_summary.get_settings(),
                        parent_id=body.get("parent_id"),
                        text=body.get("text"),
                    )

            job_id = await run_in_threadpool(submit)
            return secure(JSONResponse({"id": job_id, "status": "queued"}, status_code=202))
        except PermissionError as exc:
            return denied(exc)
        except (ValueError, LookupError) as exc:
            return secure(
                JSONResponse(
                    {
                        "error": str(exc)
                        if isinstance(exc, offhours_summary.SummaryInputError)
                        else "请求无效，请刷新后重试。"
                    },
                    status_code=400,
                )
            )

    @router.get("/replies/{job_id}")
    def reply_read(request: Request, job_id: str, fresh: bool = False):
        try:
            auth = reply_actor(request)
            with connection() as conn, conn.transaction():
                result = summary_replies.read(conn, auth["id"], job_id, offhours_summary.get_settings(), fresh=fresh)
            return secure(JSONResponse(jsonable_encoder(result)))
        except PermissionError as exc:
            return denied(exc)
        except (ValueError, LookupError):
            return secure(JSONResponse({"error": "事件或资料已变化，请刷新报告后重新生成。"}, status_code=409))

    return router
