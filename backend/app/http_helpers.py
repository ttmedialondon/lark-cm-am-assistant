"""Small request/response helpers; domain permissions stay in their services."""

import hashlib
import hmac
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response


async def form_body(request: Request) -> bytes:
    """Read at most one byte over the form limit, including chunked requests."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk[:4097 - len(body)])
        if len(body) > 4096:
            break
    return bytes(body)


async def form_fields(request: Request) -> dict[str, str]:
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
        return {}
    body = await form_body(request)
    if len(body) > 4096:
        return {}
    parsed = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items() if values}


def csrf_matches(form: dict[str, str], expected: str) -> bool:
    supplied = form.get("csrf_token")
    return bool(supplied) and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def secure_response(response):
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def json_response(value, status_code=200):
    return secure_response(JSONResponse(jsonable_encoder(value), status_code=status_code))


def error_response(exc, *, request=None, render_template=None, return_url="/dashboard", return_label="返回工作台"):
    code = 403 if isinstance(exc, PermissionError) else 404 if isinstance(exc, LookupError) else 400
    if render_template is not None:
        title = {403: "无法访问", 404: "未找到内容", 400: "操作未完成"}[code]
        return secure_response(render_template(request, "error.html", {
            "title": title, "detail": str(exc), "return_url": return_url, "return_label": return_label,
        }, code))
    return json_response({"error": str(exc)}, code)


def conditional_response(request, response):
    """Call only after current access and material checks, never before them."""
    response.headers["ETag"] = '"' + hashlib.sha256(response.body).hexdigest() + '"'
    if request.headers.get("if-none-match") == response.headers["ETag"]:
        return Response(status_code=304, headers={key: value for key, value in response.headers.items()
                                                 if key != "content-length"})
    return response
