"""Small internal admin router with owner-only creator registry writes.

The router receives the session and rendering functions from ``main``.  That
keeps it independent from the web application's module-level globals and
avoids importing ``main`` back into an admin module.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from . import admin_reporting, creator_registry


class TemplateRenderer(Protocol):
    def __call__(
        self,
        request: Request,
        name: str,
        context: dict[str, Any],
        status_code: int = 200,
    ) -> Response: ...


CurrentAuthorization = Callable[[Request], dict[str, Any] | None]
CsrfToken = Callable[[dict[str, Any]], str]


def create_admin_router(
    *,
    current_authorization: CurrentAuthorization,
    render_template: TemplateRenderer,
    csrf_token: CsrfToken,
) -> APIRouter:
    """Build the internal routes with dependencies supplied by the app shell."""

    router = APIRouter(include_in_schema=False)

    def access(request: Request) -> tuple[dict[str, Any] | None, Response | None]:
        authorization = current_authorization(request)
        if not authorization:
            return None, RedirectResponse(url="/", status_code=303)
        if authorization["status"] != "active":
            return None, render_template(
                request,
                "error.html",
                {"title": "管理台访问受限", "detail": "当前 Lark 授权状态需要处理后才能访问管理台。"},
                403,
            )

        role = admin_reporting.admin_role_for(authorization["id"])
        if not role:
            return None, render_template(
                request,
                "error.html",
                {"title": "没有管理台权限", "detail": "当前 Lark 账号尚未列入管理台人员。"},
                403,
            )
        return {"authorization": authorization, "admin_role": role}, None

    def page_context(access_context: dict[str, Any], active_page: str) -> dict[str, Any]:
        return {
            **access_context,
            "active_page": active_page,
            "admin_role_label": _role_label(access_context["admin_role"]),
            "csrf_token": csrf_token(access_context["authorization"]),
        }

    def owner_access(request: Request) -> tuple[dict[str, Any] | None, Response | None]:
        access_context, denied = access(request)
        if denied is not None:
            return None, denied
        assert access_context is not None
        if access_context["admin_role"] != "owner":
            return None, render_template(
                request,
                "error.html",
                {"title": "内部数据仅限所有者", "detail": "当前管理角色不能访问此内部数据页面。"},
                403,
            )
        return access_context, None

    def render_creators(
        request: Request,
        access_context: dict[str, Any],
        *,
        error: str | None = None,
        status_code: int = 200,
    ) -> Response:
        return render_template(
            request,
            "admin/creators.html",
            {**page_context(access_context, "creators"), "creators": admin_reporting.creators(), "form_error": error},
            status_code,
        )

    @router.get("/admin")
    def admin_overview(request: Request) -> Response:
        access_context, denied = access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        return render_template(
            request,
            "admin/overview.html",
            {**page_context(access_context, "overview"), **admin_reporting.overview()},
        )

    @router.get("/admin/users")
    def admin_users(request: Request) -> Response:
        access_context, denied = access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        return render_template(
            request,
            "admin/users.html",
            {**page_context(access_context, "users"), "users": admin_reporting.users()},
        )

    @router.get("/admin/users/{authorization_id}")
    def admin_user_detail(request: Request, authorization_id: UUID) -> Response:
        access_context, denied = access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        detail = admin_reporting.user_detail(authorization_id)
        if not detail:
            return render_template(
                request,
                "error.html",
                {"title": "未找到授权用户", "detail": "该用户可能尚未完成授权，或已被删除。"},
                404,
            )
        return render_template(
            request,
            "admin/user_detail.html",
            {**page_context(access_context, "users"), **detail},
        )

    @router.get("/admin/chats")
    def admin_chats(request: Request) -> Response:
        access_context, denied = access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        return render_template(
            request,
            "admin/chats.html",
            {**page_context(access_context, "chats"), "chats": admin_reporting.chats()},
        )

    @router.get("/admin/messages")
    def admin_messages(
        request: Request,
        user_id: UUID | None = None,
        chat_id: str | None = None,
        page: int = 1,
    ) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None

        page = max(1, min(page, 10_000))
        users = admin_reporting.users()
        selected_user = next((item for item in users if item["id"] == user_id), None)
        if user_id is not None and not selected_user:
            return render_template(
                request,
                "error.html",
                {"title": "未找到授权用户", "detail": "该用户不存在，或尚未完成 Lark 授权。"},
                404,
            )
        conversations: list[dict[str, Any]] = []
        selected_chat: dict[str, Any] | None = None
        content_page: dict[str, Any] | None = None
        if selected_user:
            conversations = admin_reporting.message_conversations(selected_user["id"])
            selected_chat = next((item for item in conversations if item["chat_id"] == chat_id), None)
            if selected_chat:
                content_page = admin_reporting.message_page(selected_user["id"], selected_chat["chat_id"], page)
                admin_reporting.record_message_view(
                    access_context["authorization"]["id"], selected_user["id"], selected_chat["chat_id"]
                )

        response = render_template(
            request,
            "admin/messages.html",
            {
                **page_context(access_context, "messages"),
                "users": users,
                "selected_user": selected_user,
                "selected_chat": selected_chat,
                "conversations": conversations,
                "content_page": content_page,
            },
        )
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @router.get("/admin/creators")
    def admin_creators(request: Request) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        return render_creators(request, access_context)

    @router.post("/admin/creators")
    async def admin_create_creator(request: Request) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        form = await _form_fields(request)
        if not _csrf_matches(form, csrf_token(access_context["authorization"])):
            return render_template(
                request,
                "error.html",
                {"title": "请求校验失败", "detail": "请返回达人库页面后重试。"},
                400,
            )
        try:
            creator_id = creator_registry.create_creator(
                access_context["authorization"]["id"], form.get("display_name", "")
            )
        except creator_registry.CreatorInputError as exc:
            return render_creators(request, access_context, error=str(exc), status_code=400)
        return RedirectResponse(url=f"/admin/creators/{creator_id}", status_code=303)

    @router.get("/admin/creators/{creator_id}")
    def admin_creator_detail(request: Request, creator_id: UUID) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        detail = admin_reporting.creator_detail(creator_id)
        if not detail:
            return render_template(
                request,
                "error.html",
                {"title": "未找到达人", "detail": "该达人可能已被删除。"},
                404,
            )
        return render_template(
            request,
            "admin/creator_detail.html",
            {**page_context(access_context, "creators"), **detail},
        )

    @router.post("/admin/creators/{creator_id}/identifiers")
    async def admin_create_creator_identifier(request: Request, creator_id: UUID) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        form = await _form_fields(request)
        if not _csrf_matches(form, csrf_token(access_context["authorization"])):
            return render_template(
                request,
                "error.html",
                {"title": "请求校验失败", "detail": "请返回达人详情页面后重试。"},
                400,
            )
        try:
            creator_registry.add_identifier(
                access_context["authorization"]["id"],
                creator_id,
                kind=form.get("kind", ""),
                platform=form.get("platform", ""),
                value=form.get("value", ""),
            )
        except creator_registry.CreatorInputError as exc:
            detail = admin_reporting.creator_detail(creator_id)
            if not detail:
                return render_template(
                    request,
                    "error.html",
                    {"title": "未找到达人", "detail": "该达人可能已被删除。"},
                    404,
                )
            return render_template(
                request,
                "admin/creator_detail.html",
                {**page_context(access_context, "creators"), **detail, "form_error": str(exc)},
                400,
            )
        return RedirectResponse(url=f"/admin/creators/{creator_id}", status_code=303)

    @router.get("/admin/jobs")
    def admin_jobs(request: Request) -> Response:
        access_context, denied = access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        return render_template(
            request,
            "admin/jobs.html",
            {**page_context(access_context, "jobs"), "jobs": admin_reporting.jobs()},
        )

    return router


async def _form_fields(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if content_type != "application/x-www-form-urlencoded":
        return {}
    body = await request.body()
    if len(body) > 4096:
        return {}
    parsed = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items() if values}


def _csrf_matches(form: dict[str, str], expected: str) -> bool:
    supplied = form.get("csrf_token")
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _role_label(role: str) -> str:
    return {"owner": "所有者", "operator": "运营管理员", "viewer": "只读管理员"}.get(role, "管理员")
