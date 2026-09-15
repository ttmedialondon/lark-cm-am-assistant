"""Read-only, session-scoped off-hours summaries for the confirmed-creator pilot."""

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse

from . import offhours_summary, summary_periods
from .db import connection


def create_router(*, current_authorization, render_template):
    router = APIRouter(prefix="/dashboard/offhours-summary", include_in_schema=False)

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

    def read(request):
        auth = current_authorization(request)
        if not auth or auth["status"] != "active":
            raise PermissionError("登录授权已失效，请重新登录。")
        with connection() as conn, conn.transaction():
            try:
                cm_id, _start, _end, selection = summary_periods.selection(
                    conn, auth["id"], request.query_params.get("cm_id"),
                    request.query_params.get("date"), request.query_params.get("period"),
                )
            except ValueError as exc:
                raise offhours_summary.SummaryInputError("请选择有效的 CM、日期和时段。") from exc
            data = offhours_summary.get_summary_view(
                conn, auth["id"], cm_id=cm_id, day=selection["date"], period=selection["period"],
            )
            data["selection"] = selection
        return auth, jsonable_encoder(data)

    def denied(exc):
        return secure(JSONResponse({"error": str(exc)}, status_code=403))

    @router.get("")
    def page(request: Request):
        if not current_authorization(request):
            return secure(RedirectResponse("/", status_code=303))
        try:
            auth, data = read(request)
            return secure(render_template(request, "offhours_summary.html", {
                "authorization": auth, "summary_data": data,
            }))
        except (PermissionError, offhours_summary.SummaryInputError) as exc:
            return denied(exc)

    @router.get("/data")
    def data(request: Request):
        try:
            _, result = read(request)
            return secure(JSONResponse(result))
        except (PermissionError, offhours_summary.SummaryInputError) as exc:
            return denied(exc)

    return router
