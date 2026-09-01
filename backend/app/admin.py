"""Small internal admin router with owner-only creator registry writes.

The router receives the session and rendering functions from ``main``.  That
keeps it independent from the web application's module-level globals and
avoids importing ``main`` back into an admin module.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode
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

    def pagination(
        page_data: dict[str, Any], path: str, *, page_name: str = "page", **query: object
    ) -> dict[str, Any]:
        """Add stable previous/next links to a limit-plus-one result page."""

        page = int(page_data["page"])
        query = {key: value for key, value in query.items() if value is not None}

        def url(target_page: int) -> str:
            return f"{path}?{urlencode({**query, page_name: target_page})}"

        return {
            **page_data,
            "previous_url": url(page - 1) if page > 1 else None,
            "next_url": url(page + 1) if page_data["has_next"] else None,
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
        page: int = 1,
        error: str | None = None,
        status_code: int = 200,
    ) -> Response:
        creator_page = admin_reporting.creators(page)
        return render_template(
            request,
            "admin/creators.html",
            {
                **page_context(access_context, "creators"),
                "creators": creator_page["items"],
                "creator_page": pagination(creator_page, "/admin/creators"),
                "form_error": error,
            },
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
    def admin_users(request: Request, page: int = 1) -> Response:
        access_context, denied = access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        user_page = admin_reporting.users(page)
        return render_template(
            request,
            "admin/users.html",
            {
                **page_context(access_context, "users"),
                "users": user_page["items"],
                "user_page": pagination(user_page, "/admin/users"),
            },
        )

    @router.get("/admin/users/{authorization_id}")
    def admin_user_detail(
        request: Request,
        authorization_id: UUID,
        page: int = 1,
        conversations_page: int = 1,
        jobs_page: int = 1,
    ) -> Response:
        access_context, denied = access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        detail = admin_reporting.user_detail(authorization_id, conversations_page, jobs_page)
        if not detail:
            return render_template(
                request,
                "error.html",
                {"title": "未找到授权用户", "detail": "该用户可能尚未完成授权，或已被删除。"},
                404,
            )
        conversation_page = detail["conversations"]
        job_page = detail["jobs"]
        return render_template(
            request,
            "admin/user_detail.html",
            {
                **page_context(access_context, "users"),
                "user": detail["user"],
                "source_page": page,
                "conversations": conversation_page["items"],
                "conversation_page": pagination(
                    conversation_page,
                    f"/admin/users/{authorization_id}",
                    page_name="conversations_page",
                    page=page,
                    jobs_page=job_page["page"],
                ),
                "jobs": job_page["items"],
                "job_page": pagination(
                    job_page,
                    f"/admin/users/{authorization_id}",
                    page_name="jobs_page",
                    page=page,
                    conversations_page=conversation_page["page"],
                ),
            },
        )

    @router.get("/admin/chats")
    def admin_chats(request: Request, page: int = 1) -> Response:
        access_context, denied = access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        chat_page = admin_reporting.chats(page)
        return render_template(
            request,
            "admin/chats.html",
            {
                **page_context(access_context, "chats"),
                "chats": chat_page["items"],
                "chat_page": pagination(chat_page, "/admin/chats"),
            },
        )

    @router.get("/admin/messages")
    def admin_messages(
        request: Request,
        user_id: UUID | None = None,
        chat_id: str | None = None,
        page: int = 1,
        user_page: int = 1,
        conversation_page: int = 1,
        conversation_query: str = "",
        conversation_type: str = "all",
    ) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None

        user_page_data = admin_reporting.users(user_page)
        users = user_page_data["items"]
        selected_user = next((item for item in users if item["id"] == user_id), None)
        selected_user_in_page = selected_user is not None
        if user_id is not None and selected_user is None:
            selected = admin_reporting.user_detail(user_id)
            selected_user = selected["user"] if selected else None
        if user_id is not None and not selected_user:
            return render_template(
                request,
                "error.html",
                {"title": "未找到授权用户", "detail": "该用户不存在，或尚未完成 Lark 授权。"},
                404,
            )
        conversation_query = admin_reporting.normalize_message_conversation_query(conversation_query)
        conversation_type = admin_reporting.normalize_message_conversation_type(conversation_type)
        conversation_page_data: dict[str, Any] | None = None
        conversations: list[dict[str, Any]] = []
        selected_chat: dict[str, Any] | None = None
        content_page: dict[str, Any] | None = None
        if selected_user:
            conversation_page_data = admin_reporting.message_conversations(
                selected_user["id"], conversation_page, conversation_query, conversation_type
            )
            conversations = conversation_page_data["items"]
            selected_chat = (
                admin_reporting.message_conversation(selected_user["id"], chat_id) if chat_id else None
            )
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
                "selected_user_in_page": selected_user_in_page,
                "user_page": pagination(
                    user_page_data,
                    "/admin/messages",
                    page_name="user_page",
                    user_id=user_id,
                    chat_id=chat_id,
                    conversation_page=conversation_page,
                    page=page,
                    conversation_query=conversation_query,
                    conversation_type=conversation_type,
                ),
                "selected_user": selected_user,
                "selected_chat": selected_chat,
                "conversations": conversations,
                "conversation_query": conversation_query,
                "conversation_type": conversation_type,
                "conversation_page": (
                    pagination(
                        conversation_page_data,
                        "/admin/messages",
                        page_name="conversation_page",
                        user_id=selected_user["id"],
                        chat_id=chat_id,
                        user_page=user_page_data["page"],
                        page=page,
                        conversation_query=conversation_query,
                        conversation_type=conversation_type,
                    )
                    if conversation_page_data
                    else None
                ),
                "content_page": content_page,
                "message_page": (
                    pagination(
                        content_page,
                        "/admin/messages",
                        user_id=selected_user["id"],
                        chat_id=selected_chat["chat_id"],
                        user_page=user_page_data["page"],
                        conversation_page=conversation_page_data["page"] if conversation_page_data else None,
                        conversation_query=conversation_query,
                        conversation_type=conversation_type,
                    )
                    if content_page and selected_chat
                    else None
                ),
            },
        )
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @router.get("/admin/creators")
    def admin_creators(request: Request, page: int = 1) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        return render_creators(request, access_context, page=page)

    @router.get("/admin/candidates")
    def admin_creator_candidates(request: Request, page: int = 1) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        candidate_page = admin_reporting.creator_candidates(page)
        response = render_template(
            request,
            "admin/candidates.html",
            {
                **page_context(access_context, "candidates"),
                "candidates": candidate_page["items"],
                "candidate_page": pagination(candidate_page, "/admin/candidates"),
            },
        )
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @router.get("/admin/candidates/{candidate_id}")
    def admin_creator_candidate_detail(request: Request, candidate_id: UUID, evidence_page: int = 1) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        detail = admin_reporting.creator_candidate_detail(candidate_id, evidence_page)
        if not detail:
            return render_template(
                request,
                "error.html",
                {"title": "未找到识别候选", "detail": "该候选可能尚未生成，或已被删除。"},
                404,
            )
        admin_reporting.record_candidate_view(access_context["authorization"]["id"], candidate_id)
        evidence = detail["evidence"]
        response = render_template(
            request,
            "admin/candidate_detail.html",
            {
                **page_context(access_context, "candidates"),
                "candidate": detail["candidate"],
                "evidence": evidence["items"],
                "evidence_page": pagination(
                    evidence, f"/admin/candidates/{candidate_id}", page_name="evidence_page"
                ),
            },
        )
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

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
    def admin_creator_detail(request: Request, creator_id: UUID, identifier_page: int = 1) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        detail = admin_reporting.creator_detail(creator_id, identifier_page)
        if not detail:
            return render_template(
                request,
                "error.html",
                {"title": "未找到达人", "detail": "该达人可能已被删除。"},
                404,
            )
        identifiers = detail["identifiers"]
        return render_template(
            request,
            "admin/creator_detail.html",
            {
                **page_context(access_context, "creators"),
                "creator": detail["creator"],
                "identifiers": identifiers["items"],
                "identifier_page": pagination(
                    identifiers, f"/admin/creators/{creator_id}", page_name="identifier_page"
                ),
            },
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
            identifiers = detail["identifiers"]
            return render_template(
                request,
                "admin/creator_detail.html",
                {
                    **page_context(access_context, "creators"),
                    "creator": detail["creator"],
                    "identifiers": identifiers["items"],
                    "identifier_page": pagination(
                        identifiers, f"/admin/creators/{creator_id}", page_name="identifier_page"
                    ),
                    "form_error": str(exc),
                },
                400,
            )
        return RedirectResponse(url=f"/admin/creators/{creator_id}", status_code=303)

    @router.get("/admin/jobs")
    def admin_jobs(request: Request, page: int = 1) -> Response:
        access_context, denied = access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        job_page = admin_reporting.jobs(page)
        return render_template(
            request,
            "admin/jobs.html",
            {
                **page_context(access_context, "jobs"),
                "jobs": job_page["items"],
                "job_page": pagination(job_page, "/admin/jobs"),
            },
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
