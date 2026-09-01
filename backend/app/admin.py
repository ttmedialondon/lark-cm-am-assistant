"""Small, read-only internal admin router.

The router receives the session and rendering functions from ``main``.  That
keeps it independent from the web application's module-level globals and
avoids importing ``main`` back into an admin module.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from . import admin_reporting


class TemplateRenderer(Protocol):
    def __call__(
        self,
        request: Request,
        name: str,
        context: dict[str, Any],
        status_code: int = 200,
    ) -> Response: ...


CurrentAuthorization = Callable[[Request], dict[str, Any] | None]


def create_admin_router(
    *,
    current_authorization: CurrentAuthorization,
    render_template: TemplateRenderer,
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
                {"title": "消息内容仅限所有者", "detail": "当前管理角色不能查看 CM / AM 的具体消息内容。"},
                403,
            )
        return access_context, None

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
        chat_mode: str | None = None,
        page: int = 1,
    ) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None

        if chat_mode not in {"group", "p2p"}:
            chat_mode = None
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
        if selected_user is None and users:
            selected_user = users[0]

        conversations: list[dict[str, Any]] = []
        content_page = {"metrics": {}, "items": [], "page": page, "has_next": False}
        if selected_user:
            conversations = admin_reporting.message_conversations(selected_user["id"], chat_mode)
            if chat_id not in {item["chat_id"] for item in conversations}:
                chat_id = None
            content_page = admin_reporting.message_page(selected_user["id"], chat_mode, chat_id, page)
            admin_reporting.record_message_view(access_context["authorization"]["id"], selected_user["id"], chat_id)

        response = render_template(
            request,
            "admin/messages.html",
            {
                **page_context(access_context, "messages"),
                "users": users,
                "selected_user": selected_user,
                "selected_chat_id": chat_id,
                "selected_chat_mode": chat_mode,
                "conversations": conversations,
                "content_page": content_page,
            },
        )
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

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


def _role_label(role: str) -> str:
    return {"owner": "所有者", "operator": "运营管理员", "viewer": "只读管理员"}.get(role, "管理员")
