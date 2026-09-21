"""Small internal admin router for the owner-only administration console.

The router receives the session and rendering functions from ``main``.  That
keeps it independent from the web application's module-level globals and
avoids importing ``main`` back into an admin module.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import parse_qs, quote, urlencode
from uuid import UUID, uuid4

import anyio
from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import (
    admin_permissions,
    admin_reporting,
    ai_dispatch,
    ai_feature_config,
    contact_assessments,
    creator_ownership,
    message_export,
    offhours_summary,
    organization_sync,
)
from .config import get_settings
from .contact_taxonomy import ROLE_LABELS, ROLE_ORDER
from .db import connection


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
AI_FEATURES = {
    "contact_assessment": {
        "title": "全员身份识别",
        "description": "按在库人员本人的重要历史发言识别六类身份；置信度不足留待识别，不判断 CM 归属或 handle。",
        "enabled_label": "开启全员身份识别",
    },
    "offhours_summary": {
        "title": "达人消息总结",
        "description": "整理已入库达人的群聊和私聊，按上班、下班时段归纳并分级。",
        "enabled_label": "开启达人消息总结",
    },
}


def create_admin_router(
    *,
    current_authorization: CurrentAuthorization,
    render_template: TemplateRenderer,
    csrf_token: CsrfToken,
) -> APIRouter:
    """Build the internal routes with dependencies supplied by the app shell."""

    router = APIRouter(include_in_schema=False)
    original_render = render_template

    def render_template(request: Request, name: str, context: dict[str, Any], status_code: int = 200) -> Response:
        response = original_render(request, name, context, status_code)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

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

        acl = admin_permissions.access_for(authorization["id"])
        if not acl["has_admin_access"]:
            return None, render_template(
                request,
                "error.html",
                {"title": "没有后台页面权限", "detail": "你仍可使用前台。后台页面需由系统主人勾选授权。"},
                403,
            )
        route_path = request.scope["route"].path
        permission = {
            "/admin": "overview", "/admin/users": "users", "/admin/users/{authorization_id}": "users",
            "/admin/chats": "chats", "/admin/messages": "messages", "/admin/contacts": "contacts",
            "/admin/contacts/{open_id}": "contacts",
            "/admin/messages/export": "messages",
            "/admin/ai": "ai", "/admin/jobs": "jobs",
            "/admin/ai/creator-ownership": "ai", "/admin/ai/creator-ownership/data": "ai",
        }.get(route_path)
        if not acl["is_owner"]:
            if request.method == "GET" and route_path == "/admin" and "overview" not in acl["permissions"]:
                return None, RedirectResponse(acl["landing_path"], status_code=303,
                                              headers={"Cache-Control": "no-store, max-age=0"})
            if request.method != "GET" or permission not in acl["permissions"]:
                return None, render_template(request, "error.html", {
                    "title": "当前页面或操作未获授权", "detail": "仅可查看系统主人已勾选的页面；管理操作仅限主人。",
                }, 403)
        return {"authorization": authorization, "admin_role": "owner" if acl["is_owner"] else "member",
                "message_source_ids": acl.get("message_source_ids"),
                "admin_permissions": acl["permissions"], "admin_landing_path": acl["landing_path"]}, None

    def page_context(access_context: dict[str, Any], active_page: str) -> dict[str, Any]:
        return {
            **access_context,
            "active_page": active_page,
            "admin_role_label": _role_label(access_context["admin_role"]),
            "permission_pages": admin_permissions.PAGES,
            "csrf_token": csrf_token(access_context["authorization"]),
        }

    def pagination(page_data: dict[str, Any], path: str, *, page_name: str = "page",
                   fragment: str = "", **query: object) -> dict[str, Any]:
        """Build compact, numbered navigation while retaining every query value."""

        page = int(page_data["page"])
        query = {key: value for key, value in query.items() if value is not None}

        def url(target_page: int) -> str:
            return f"{path}?{urlencode({**query, page_name: target_page})}" + (f"#{fragment}" if fragment else "")

        total_count = page_data.get("total_count")
        page_size = page_data.get("page_size")
        total_pages = page_data.get("total_pages")
        if not isinstance(total_pages, int):
            total_pages = (
                (total_count + page_size - 1) // page_size
                if isinstance(total_count, int) and isinstance(page_size, int) and page_size > 0
                else None
            )
        if total_pages is not None:
            total_pages = max(0, total_pages)
            page = min(page, max(1, total_pages))

        page_numbers: list[int] = []
        if total_pages and total_pages > 1:
            if total_pages <= 7:
                page_numbers = list(range(1, total_pages + 1))
            elif page <= 3:
                page_numbers = [1, 2, 3, total_pages]
            elif page >= total_pages - 2:
                page_numbers = [1, total_pages - 2, total_pages - 1, total_pages]
            else:
                page_numbers = [1, page - 1, page, page + 1, total_pages]

        pages: list[dict[str, Any]] = []
        previous_number = 0
        for number in page_numbers:
            if number > previous_number + 1:
                pages.append({"ellipsis": True})
            pages.append({"page": number, "url": url(number), "current": number == page})
            previous_number = number

        has_next = page < total_pages if total_pages is not None else bool(page_data["has_next"])

        return {
            **page_data,
            "page": page,
            "total_count": total_count,
            "total_pages": total_pages,
            "pages": pages,
            "previous_url": url(page - 1) if page > 1 else None,
            "next_url": url(page + 1) if has_next else None,
        }

    def contact_query(
        *,
        page: int,
        category: str,
        organization_status: str,
        organization_query: str,
        q: str = "",
    ) -> str:
        query: dict[str, object] = {
            "page": page,
            "category": category,
            "organization_status": organization_status,
        }
        if organization_query:
            query["organization_query"] = organization_query
        if q:
            query["q"] = q
        return urlencode(query)

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

    def render_ai_config(
        request: Request,
        access_context: dict[str, Any],
        *,
        error: str | None = None,
        status_code: int = 200,
    ) -> Response:
        with connection() as conn:
            configs = [
                {
                    **AI_FEATURES[feature],
                    "id_suffix": feature.replace("_", "-"),
                    "config": ai_feature_config.display_config(ai_feature_config.get_feature_config(conn, feature)),
                    "feature": feature,
                }
                for feature in AI_FEATURES
            ]
            dispatch_status = ai_dispatch.status(conn)
            summary_settings = offhours_summary.get_settings_view(conn)
        response = render_template(
            request,
            "admin/ai.html",
            {
                **page_context(access_context, "ai"),
                "configs": configs,
                "ai_dispatch": dispatch_status,
                "summary_settings": summary_settings,
                "summary_saved": request.query_params.get("saved") == "offhours-summary",
                "identity_saved": request.query_params.get("saved") == "people-identity",
                # Retain this template-safe alias while external callers migrate to ``configs``.
                "config": configs[0]["config"],
                "form_error": error,
            },
            status_code,
        )
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    def render_contact_assessment(
        request: Request,
        access_context: dict[str, Any],
        *,
        open_id: str,
        assessment_id: UUID,
        source_page: int = 1,
        source_category: str = "all",
        source_organization_status: str = "all",
        source_organization_query: str = "",
        error: str | None = None,
        status_code: int = 200,
    ) -> Response:
        """Render one advisory result; only this page can include evidence text."""

        detail = contact_assessments.assessment_detail(assessment_id)
        if not detail or not contact_assessments.assessment_matches_contact(assessment_id, open_id):
            response = render_template(
                request,
                "error.html",
                {"title": "未找到 AI 判定", "detail": "该判定不存在，或不属于当前联系人。"},
                404,
            )
        else:
            if any(item.get("available") for item in detail.get("evidence", ())):
                contact_assessments.record_assessment_view(access_context["authorization"]["id"], assessment_id)
            response = render_template(
                request,
                "admin/contact_assessment.html",
                {
                    **page_context(access_context, "contacts"),
                    "assessment": detail,
                    "identity_policy": contact_assessments.POLICY_VERSION,
                    "route_open_id": open_id,
                    "source_page": source_page,
                    "source_category": source_category,
                    "source_organization_status": source_organization_status,
                    "source_organization_query": source_organization_query,
                    "source_query": contact_query(
                        page=source_page,
                        category=source_category,
                        organization_status=source_organization_status,
                        organization_query=source_organization_query,
                    ),
                    "form_error": error,
                },
                status_code,
            )
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

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
    def admin_users(request: Request, page: int = 1, q: str = "", permission_filter: str = "all",
                    saved: bool = False) -> Response:
        access_context, denied = access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        q = admin_reporting.normalize_message_conversation_query(q)
        permission_filter = admin_reporting.normalize_user_permission_filter(permission_filter)
        user_page = admin_reporting.users(page, q, permission_filter)
        access_by_user = admin_permissions.users_access_data(item["id"] for item in user_page["items"])
        return render_template(
            request,
            "admin/users.html",
            {
                **page_context(access_context, "users"),
                "users": [{**item, "access": access_by_user[item["id"]]} for item in user_page["items"]],
                "summary": user_page["summary"], "q": q, "permission_filter": permission_filter, "saved": saved,
                "user_page": pagination(user_page, "/admin/users", q=q or None,
                                        permission_filter=permission_filter if permission_filter != "all" else None),
            },
        )

    @router.post("/admin/users/{authorization_id}/permissions")
    async def admin_set_page_permissions(request: Request, authorization_id: UUID) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        try:
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
                raise ValueError("请通过人员页面提交权限设置。")
            body = await request.body()
            if len(body) > 4096:
                raise ValueError("提交内容过长。")
            parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=20)
            if set(parsed) - {"csrf_token", "permissions", "page", "q", "permission_filter"} or any(
                len(values) != 1 for key, values in parsed.items() if key != "permissions"
            ):
                raise ValueError("权限设置包含无效字段。")
            form = {key: values[0] for key, values in parsed.items() if key != "permissions"}
            if not _csrf_matches(form, csrf_token(access_context["authorization"])):
                raise ValueError("请求校验失败，请刷新人员页面后重试。")
            source_page = max(1, min(int(form.get("page", "1")), admin_reporting.MAX_PAGE))
            q = admin_reporting.normalize_message_conversation_query(form.get("q", ""))
            permission_filter = admin_reporting.normalize_user_permission_filter(form.get("permission_filter", "all"))
            admin_permissions.set_permissions(access_context["authorization"]["id"], authorization_id,
                                              parsed.get("permissions", []))
        except (ValueError, UnicodeDecodeError) as exc:
            return render_template(request, "error.html", {"title": "权限未保存", "detail": str(exc)}, 400)
        except PermissionError as exc:
            return render_template(request, "error.html", {"title": "权限未保存", "detail": str(exc)}, 403)
        except LookupError as exc:
            return render_template(request, "error.html", {"title": "未找到人员", "detail": str(exc)}, 404)
        query = urlencode({"page": source_page, "q": q, "permission_filter": permission_filter, "saved": "true"})
        return RedirectResponse(f"/admin/users?{query}", status_code=303,
                                headers={"Cache-Control": "no-store, max-age=0"})

    @router.get("/admin/users/{authorization_id}")
    def admin_user_detail(
        request: Request,
        authorization_id: UUID,
        page: int = 1,
        conversations_page: int = 1,
        jobs_page: int = 1,
        q: str = "",
        permission_filter: str = "all",
    ) -> Response:
        access_context, denied = access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        q = admin_reporting.normalize_message_conversation_query(q)
        permission_filter = admin_reporting.normalize_user_permission_filter(permission_filter)
        can_view_conversations = bool({"chats", "messages"} & access_context["admin_permissions"])
        can_view_jobs = "jobs" in access_context["admin_permissions"]
        detail = admin_reporting.user_detail(authorization_id, conversations_page, jobs_page,
                                             include_conversations=can_view_conversations, include_jobs=can_view_jobs)
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
                "user_access": admin_permissions.access_for(authorization_id),
                "can_view_conversations": can_view_conversations, "can_view_jobs": can_view_jobs,
                "source_page": page,
                "q": q, "permission_filter": permission_filter,
                "conversations": conversation_page["items"],
                "conversation_page": pagination(
                    conversation_page,
                    f"/admin/users/{authorization_id}",
                    page_name="conversations_page",
                    page=page,
                    jobs_page=job_page["page"],
                    q=q or None, permission_filter=permission_filter if permission_filter != "all" else None,
                ),
                "jobs": job_page["items"],
                "job_page": pagination(
                    job_page,
                    f"/admin/users/{authorization_id}",
                    page_name="jobs_page",
                    page=page,
                    conversations_page=conversation_page["page"],
                    q=q or None, permission_filter=permission_filter if permission_filter != "all" else None,
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

    @router.get("/admin/messages/export")
    def export_messages(request: Request, user_id: UUID) -> Response:
        context, denied = access(request)
        if denied is not None:
            return denied
        limits = context["message_source_ids"]
        if limits is not None and user_id not in limits:
            return Response("消息来源未获授权", status_code=403)
        source = admin_reporting.message_user(user_id)
        if not source:
            return Response("未找到人员", status_code=404)
        user = source["user"]
        if user.get("authorization_status") != "active" or not user.get("body_consent_at"):
            return Response("该人员当前无有效正文授权", status_code=403)

        def check_access():
            refreshed, denied = access(request)
            if denied is not None:
                raise PermissionError("导出访问权限已变化")
            limits = refreshed["message_source_ids"]
            latest = admin_reporting.message_user(user_id)
            if (limits is not None and user_id not in limits) or not latest or (
                latest["user"].get("authorization_status") != "active"
                or latest["user"].get("body_consent_at") != user["body_consent_at"]
            ):
                raise PermissionError("消息来源授权已变化")

        async def chunks():
            iterator = message_export.stream_export(user_id, context["authorization"]["id"], check_access)
            try:
                while (chunk := await run_in_threadpool(next, iterator, None)) is not None:
                    yield chunk
            finally:
                with anyio.CancelScope(shield=True):
                    await run_in_threadpool(iterator.close)

        filename = quote(f'{message_export.safe_name(user["display_name"])}_全部聊天记录.zip')
        return StreamingResponse(chunks(), media_type="application/zip", headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no",
        })

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
        access_context, denied = access(request)
        if denied is not None:
            return denied
        assert access_context is not None

        source_limit = access_context["message_source_ids"]
        if source_limit is not None and user_id is not None and user_id not in source_limit:
            return render_template(request, "error.html", {
                "title": "消息来源未获授权", "detail": "你只能查看已获授权的 CM 消息。",
            }, 403)
        if source_limit is not None:
            sources = [admin_reporting.message_user(source_id)
                       for source_id in source_limit]
            allowed_users = sorted([source["user"] for source in sources if source],
                                   key=lambda user: user.get("display_name") or "")
            user_page_data = admin_reporting._page_result(
                allowed_users, 1, total_count=len(allowed_users),
            )
        else:
            user_page_data = admin_reporting.message_users(user_page)
        users = user_page_data["items"]
        selected_user = next((item for item in users if item["id"] == user_id), None)
        selected_user_in_page = selected_user is not None
        if user_id is not None and selected_user is None:
            selected = admin_reporting.message_user(user_id)
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
        source_unavailable_reason = None
        if selected_user:
            if selected_user.get("authorization_status", "active") != "active":
                source_unavailable_reason = "该人员的 Lark 授权当前不可用，暂时无法查看其会话和记录。"
            elif not selected_user.get("body_consent_at"):
                source_unavailable_reason = "该人员尚未完成消息正文授权，暂时无法查看其会话和记录。"
        if selected_user and not source_unavailable_reason:
            conversation_page_data = admin_reporting.message_conversations(
                selected_user["id"], conversation_page, conversation_query, conversation_type
            )
            conversations = conversation_page_data["items"]
            selected_chat = admin_reporting.message_conversation(selected_user["id"], chat_id) if chat_id else None
            if selected_chat:
                content_page = admin_reporting.message_page(selected_user["id"], selected_chat["chat_id"], page)
                admin_reporting.record_message_view(
                    access_context["authorization"]["id"], selected_user["id"], selected_chat["chat_id"]
                )

        selected_chat_in_page = bool(selected_chat and any(
            item["chat_id"] == selected_chat["chat_id"] for item in conversations
        ))
        source_params = (
            {"user_id": str(selected_user["id"]), "user_page": user_page_data["page"]} if selected_user else {}
        )
        conversation_params = {
            **source_params, "conversation_page": conversation_page_data["page"] if conversation_page_data else 1,
            "conversation_query": conversation_query, "conversation_type": conversation_type,
        }
        conversations = [
            {**item, "url": "/admin/messages?" + urlencode({**conversation_params, "chat_id": item["chat_id"]})
             + "#message-panel"}
            for item in conversations
        ]
        latest_messages_url = (
            "/admin/messages?" + urlencode({**conversation_params, "chat_id": selected_chat["chat_id"], "page": 1})
            + "#message-latest"
            if selected_chat else None
        )
        response = render_template(
            request,
            "admin/messages.html",
            {
                **page_context(access_context, "messages"),
                "users": users,
                "source_unavailable_reason": source_unavailable_reason,
                "requested_chat_unavailable": bool(selected_user and chat_id and not selected_chat
                                                    and not source_unavailable_reason),
                "selected_chat_in_page": selected_chat_in_page,
                "clear_conversation_filters_url": f"/admin/messages?{urlencode(source_params)}",
                "latest_messages_url": latest_messages_url,
                "selected_user_in_page": selected_user_in_page,
                "user_page": pagination(
                    user_page_data,
                    "/admin/messages",
                    page_name="user_page",
                    user_id=user_id,
                    chat_id=selected_chat["chat_id"] if selected_chat else None,
                    conversation_page=conversation_page_data["page"] if conversation_page_data else 1,
                    page=content_page["page"] if content_page else 1,
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
                        fragment="conversation-directory",
                        user_id=selected_user["id"],
                        chat_id=selected_chat["chat_id"] if selected_chat else None,
                        user_page=user_page_data["page"],
                        page=content_page["page"] if content_page else 1,
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
                        fragment="message-panel",
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

    @router.get("/admin/ai")
    def admin_ai_config(request: Request) -> Response:
        access_context, denied = access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        return render_ai_config(request, access_context)

    def ownership_json(payload: dict[str, Any], status_code: int = 200) -> Response:
        return JSONResponse(jsonable_encoder(payload), status_code=status_code, headers={
            "Cache-Control": "no-store, max-age=0", "Referrer-Policy": "same-origin",
        })

    def ownership_error(exc: Exception) -> Response:
        code = 403 if isinstance(exc, PermissionError) else 404 if isinstance(exc, LookupError) else 400
        return ownership_json({"error": str(exc)}, code)

    @router.get("/admin/ai/creator-ownership")
    @router.get("/admin/ai/creator-ownership/data")
    def admin_creator_ownership(
        request: Request, cm_id: UUID | None = None, run_id: UUID | None = None,
        q: str = "", status: str = "all", page: int = 1,
    ) -> Response:
        access_context, denied = access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        try:
            data = creator_ownership.page_data(
                cm_id=cm_id, run_id=run_id, query=q[:120], status=status, page=max(1, page),
                include_private=access_context["admin_role"] == "owner",
            )
        except (ValueError, LookupError, PermissionError) as exc:
            return ownership_error(exc)
        if request.url.path.endswith("/data"):
            response = ownership_json(data)
            response.headers["ETag"] = '"' + hashlib.sha256(response.body).hexdigest() + '"'
            # Compare only after current permissions and evidence have been rechecked.
            if request.headers.get("if-none-match") == response.headers["ETag"]:
                return Response(status_code=304, headers={key: value for key, value in response.headers.items()
                                                         if key != "content-length"})
            return response
        return render_template(request, "admin/creator_ownership.html", {
            **page_context(access_context, "ai"), "ownership": jsonable_encoder(data),
            "ownership_query": q[:120], "ownership_status": status,
        })

    @router.post("/admin/ai/creator-ownership/preview")
    async def admin_creator_ownership_preview(request: Request) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        form = await _form_fields(request)
        if not _csrf_matches(form, csrf_token(access_context["authorization"])):
            return ownership_json({"error": "请求校验失败，请刷新页面后重试。"}, 400)
        try:
            cm_id = UUID(form.get("cm_id", ""))
            run_id = await run_in_threadpool(
                creator_ownership.create_preview, access_context["authorization"]["id"], cm_id,
            )
        except (ValueError, LookupError, PermissionError) as exc:
            return ownership_error(exc)
        return ownership_json({"run_id": run_id, "cm_id": cm_id})

    @router.post("/admin/ai/creator-ownership/{run_id}/start")
    @router.post("/admin/ai/creator-ownership/{run_id}/pause")
    @router.post("/admin/ai/creator-ownership/{run_id}/resume")
    @router.post("/admin/ai/creator-ownership/{run_id}/confirm")
    @router.post("/admin/ai/creator-ownership/{run_id}/defer")
    async def admin_creator_ownership_action(request: Request, run_id: UUID) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        form = await _form_fields(request)
        if not _csrf_matches(form, csrf_token(access_context["authorization"])):
            return ownership_json({"error": "请求校验失败，请刷新页面后重试。"}, 400)
        actor_id = access_context["authorization"]["id"]
        action = request.url.path.rsplit("/", 1)[-1]
        try:
            count = None
            if action == "confirm":
                raise ValueError("请到前台候选核对 handle；第二步不能直接确认入库。")
            if action == "start":
                if form.get("limit") not in {"20", "all"}:
                    raise ValueError("请选择先分析 20 人或分析剩余。")
                await run_in_threadpool(
                    creator_ownership.start_run, actor_id, run_id, limit=20 if form["limit"] == "20" else None,
                )
            elif action in {"pause", "resume"}:
                await run_in_threadpool(
                    creator_ownership.pause_run if action == "pause" else creator_ownership.resume_run,
                    actor_id, run_id,
                )
            else:
                values = form.get("task_ids", "").split(",")
                if not 1 <= len(values) <= 100 or len(values) != len(set(values)):
                    raise ValueError("请选择 1 至 100 位不重复的达人。")
                try:
                    task_ids = [UUID(value) for value in values]
                except ValueError as exc:
                    raise ValueError("所选记录无效，请刷新后重试。") from exc
                count = await run_in_threadpool(
                    creator_ownership.defer_tasks, actor_id, run_id, task_ids,
                )
        except (ValueError, LookupError, PermissionError) as exc:
            return ownership_error(exc)
        return ownership_json({"run_id": run_id, "count": count})

    @router.get("/admin/ai/creator-ownership/tasks/{task_id}/evidence")
    def admin_creator_ownership_evidence(request: Request, task_id: UUID) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        try:
            data = creator_ownership.task_evidence(access_context["authorization"]["id"], task_id)
        except (ValueError, LookupError, PermissionError) as exc:
            return ownership_error(exc)
        return ownership_json(data)

    @router.get("/admin/contacts")
    def admin_contacts(
        request: Request,
        page: int = 1,
        category: str = "all",
        q: str = "",
    ) -> Response:
        access_context, denied = access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        category = admin_reporting.normalize_contact_category(category)
        q = admin_reporting.normalize_organization_query(q)
        contact_page = admin_reporting.contacts(page, category, "all", "", q)
        filters = {"q": q} if q else {}
        identity_summary = contact_page.get("identity_summary", {})
        response = render_template(
            request,
            "admin/contacts.html",
            {
                **page_context(access_context, "contacts"),
                "contacts": contact_page["items"],
                "contact_page": pagination(
                    contact_page,
                    "/admin/contacts",
                    category=category,
                    q=q or None,
                ),
                "category": category,
                "q": q,
                "name_query_param": urlencode({"q": q}) if q else "",
                "identity_summary": identity_summary,
                "refresh_url": "/admin/contacts?" + urlencode({"category": category, "page": page, **filters}),
                "category_filters": [{"value": code, "label": label,
                                      "count": identity_summary.get("total", 0) if code == "all" else
                                      identity_summary.get("categories", {}).get(code, 0),
                                      "url": "/admin/contacts?" + urlencode({"category": code, **filters})}
                                     for code, label in [("all", "全部"), ("unclassified", "待识别"),
                                                         *((code, ROLE_LABELS[code]) for code in ROLE_ORDER)]],
            },
        )
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @router.get("/admin/contacts/{open_id}")
    def admin_contact_detail(
        request: Request,
        open_id: str,
        page: int = 1,
        category: str = "all",
        organization_status: str = "all",
        organization_query: str = "",
        conversations_page: int = 1,
        q: str = "",
    ) -> Response:
        access_context, denied = access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        category = admin_reporting.normalize_contact_category(category)
        organization_status = admin_reporting.normalize_organization_status(organization_status)
        organization_query = admin_reporting.normalize_organization_query(organization_query)
        q = admin_reporting.normalize_organization_query(q)
        can_view_conversations = bool({"chats", "messages"} & access_context["admin_permissions"])
        is_owner = access_context["admin_role"] == "owner"
        detail = admin_reporting.contact_detail(open_id, conversations_page,
                                                include_conversations=can_view_conversations)
        if not detail:
            return render_template(
                request,
                "error.html",
                {"title": "未找到 Lark 参与者", "detail": "该参与者尚未被同步，或不在人员目录中。"},
                404,
            )
        conversations = detail["conversations"]
        latest_assessment = (
            contact_assessments.latest_contact_assessment(detail["contact"]["contact_id"]) if is_owner else None
        )
        response = render_template(
            request,
            "admin/contact_detail.html",
            {
                **page_context(access_context, "contacts"),
                "contact": detail["contact"],
                "identities": detail["identities"],
                "organizations": detail["organizations"],
                "tenant_population": detail.get("tenant_population", 0),
                "source_page": page,
                "source_category": category,
                "source_organization_status": organization_status,
                "source_organization_query": organization_query,
                "source_query": contact_query(
                    page=page,
                    category=category,
                    organization_status=organization_status,
                    organization_query=organization_query,
                    q=q,
                ),
                "assessment": latest_assessment,
                "identity_policy": contact_assessments.POLICY_VERSION,
                "assessment_available": contact_assessments.assessment_available() if is_owner else False,
                "can_view_conversations": can_view_conversations,
                "conversations": conversations["items"],
                "conversation_page": pagination(
                    conversations,
                    f"/admin/contacts/{open_id}",
                    page_name="conversations_page",
                    page=page,
                    category=category,
                    organization_status=organization_status,
                    organization_query=organization_query or None,
                    q=q or None,
                ),
            },
        )
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @router.post("/admin/contacts/{open_id}/assessments")
    async def admin_continue_contact_assessment(request: Request, open_id: str) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        form = await _form_fields(request)
        if not _csrf_matches(form, csrf_token(access_context["authorization"])):
            response = render_template(request, "error.html", {
                "title": "请求校验失败", "detail": "请返回人员详情页后重试。",
            }, 400)
        else:
            with connection() as conn:
                contact = conn.execute(
                    "SELECT contact_id FROM lark_identities WHERE open_id=%s AND identity_type='user'",
                    (open_id,),
                ).fetchone()
            if not contact:
                response = render_template(request, "error.html", {
                    "title": "未找到人员", "detail": "此 Lark 用户尚未关联在库人员。",
                }, 404)
            else:
                try:
                    contact_assessments.requeue_contact_assessment(contact["contact_id"])
                    response = RedirectResponse(url=f"/admin/contacts/{open_id}", status_code=303)
                except contact_assessments.ContactAssessmentInputError as exc:
                    response = render_template(request, "error.html", {
                        "title": "无法继续识别", "detail": str(exc),
                    }, 400)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @router.post("/admin/contacts/{open_id}/organization")
    async def admin_confirm_contact_organization(
        request: Request,
        open_id: str,
        page: int = 1,
        category: str = "all",
        organization_status: str = "all",
        organization_query: str = "",
    ) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        category = admin_reporting.normalize_contact_category(category)
        organization_status = admin_reporting.normalize_organization_status(organization_status)
        organization_query = admin_reporting.normalize_organization_query(organization_query)
        form = await _form_fields(request)
        if not _csrf_matches(form, csrf_token(access_context["authorization"])):
            response = render_template(
                request,
                "error.html",
                {"title": "请求校验失败", "detail": "请返回联系人详情页后重试。"},
                400,
            )
            response.headers["Cache-Control"] = "no-store, max-age=0"
            return response
        with connection() as conn:
            contact = conn.execute(
                """
                SELECT contact_id FROM lark_identities
                WHERE open_id = %s AND identity_type = 'user'
                """,
                (open_id,),
            ).fetchone()
        if not contact:
            response = render_template(
                request,
                "error.html",
                {"title": "未找到联系人", "detail": "该 Lark 参与者尚未被同步。"},
                404,
            )
            response.headers["Cache-Control"] = "no-store, max-age=0"
            return response
        try:
            organization_sync.manually_confirm_contact_organization(
                access_context["authorization"]["id"],
                contact["contact_id"],
                open_id,
                form.get("canonical_name", ""),
                form.get("organization_kind", ""),
                map_current_tenant=form.get("map_current_tenant") == "1",
            )
        except organization_sync.OrganizationInputError as exc:
            response = render_template(
                request,
                "error.html",
                {"title": "无法确认组织", "detail": str(exc)},
                400,
            )
            response.headers["Cache-Control"] = "no-store, max-age=0"
            return response
        query = contact_query(
            page=page,
            category=category,
            organization_status=organization_status,
            organization_query=organization_query,
        )
        response = RedirectResponse(url=f"/admin/contacts/{open_id}?{query}", status_code=303)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @router.get("/admin/contacts/{open_id}/assessments/{assessment_id}")
    def admin_contact_assessment(
        request: Request,
        open_id: str,
        assessment_id: UUID,
        page: int = 1,
        category: str = "all",
        organization_status: str = "all",
        organization_query: str = "",
    ) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        category = admin_reporting.normalize_contact_category(category)
        organization_status = admin_reporting.normalize_organization_status(organization_status)
        organization_query = admin_reporting.normalize_organization_query(organization_query)
        return render_contact_assessment(
            request,
            access_context,
            open_id=open_id,
            assessment_id=assessment_id,
            source_page=page,
            source_category=category,
            source_organization_status=organization_status,
            source_organization_query=organization_query,
        )

    @router.post("/admin/contacts/{open_id}/assessments/{assessment_id}/roles")
    async def admin_confirm_assessment_role(
        request: Request,
        open_id: str,
        assessment_id: UUID,
        page: int = 1,
        category: str = "all",
        organization_status: str = "all",
        organization_query: str = "",
    ) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        category = admin_reporting.normalize_contact_category(category)
        organization_status = admin_reporting.normalize_organization_status(organization_status)
        organization_query = admin_reporting.normalize_organization_query(organization_query)
        if not contact_assessments.assessment_matches_contact(assessment_id, open_id):
            response = render_template(
                request,
                "error.html",
                {"title": "未找到 AI 判定", "detail": "该判定不存在，或不属于当前联系人。"},
                404,
            )
            response.headers["Cache-Control"] = "no-store, max-age=0"
            return response
        form = await _form_fields(request)
        if not _csrf_matches(form, csrf_token(access_context["authorization"])):
            return render_contact_assessment(
                request,
                access_context,
                open_id=open_id,
                assessment_id=assessment_id,
                source_page=page,
                source_category=category,
                source_organization_status=organization_status,
                source_organization_query=organization_query,
                error="请求校验失败，请返回此页面后重试。",
                status_code=400,
            )
        try:
            contact_assessments.confirm_assessment_role(
                access_context["authorization"]["id"], assessment_id, form.get("role", "")
            )
        except contact_assessments.ContactAssessmentInputError as exc:
            return render_contact_assessment(
                request,
                access_context,
                open_id=open_id,
                assessment_id=assessment_id,
                source_page=page,
                source_category=category,
                source_organization_status=organization_status,
                source_organization_query=organization_query,
                error=str(exc),
                status_code=400,
            )
        query = contact_query(
            page=page,
            category=category,
            organization_status=organization_status,
            organization_query=organization_query,
        )
        response = RedirectResponse(
            url=f"/admin/contacts/{open_id}/assessments/{assessment_id}?{query}", status_code=303
        )
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @router.post("/admin/contacts/{open_id}/assessments/{assessment_id}/dismiss")
    async def admin_dismiss_contact_assessment(
        request: Request,
        open_id: str,
        assessment_id: UUID,
        page: int = 1,
        category: str = "all",
        organization_status: str = "all",
        organization_query: str = "",
    ) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        category = admin_reporting.normalize_contact_category(category)
        organization_status = admin_reporting.normalize_organization_status(organization_status)
        organization_query = admin_reporting.normalize_organization_query(organization_query)
        if not contact_assessments.assessment_matches_contact(assessment_id, open_id):
            response = render_template(
                request,
                "error.html",
                {"title": "未找到 AI 判定", "detail": "该判定不存在，或不属于当前联系人。"},
                404,
            )
            response.headers["Cache-Control"] = "no-store, max-age=0"
            return response
        form = await _form_fields(request)
        if not _csrf_matches(form, csrf_token(access_context["authorization"])):
            return render_contact_assessment(
                request,
                access_context,
                open_id=open_id,
                assessment_id=assessment_id,
                source_page=page,
                source_category=category,
                source_organization_status=organization_status,
                source_organization_query=organization_query,
                error="请求校验失败，请返回此页面后重试。",
                status_code=400,
            )
        try:
            contact_assessments.dismiss_assessment(access_context["authorization"]["id"], assessment_id)
        except contact_assessments.ContactAssessmentInputError as exc:
            return render_contact_assessment(
                request,
                access_context,
                open_id=open_id,
                assessment_id=assessment_id,
                source_page=page,
                source_category=category,
                source_organization_status=organization_status,
                source_organization_query=organization_query,
                error=str(exc),
                status_code=400,
            )
        query = contact_query(
            page=page,
            category=category,
            organization_status=organization_status,
            organization_query=organization_query,
        )
        response = RedirectResponse(url=f"/admin/contacts/{open_id}?{query}", status_code=303)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @router.post("/admin/ai")
    async def admin_save_ai_config(request: Request) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        form = await _form_fields(request)
        if not _csrf_matches(form, csrf_token(access_context["authorization"])):
            response = render_template(
                request,
                "error.html",
                {"title": "请求校验失败", "detail": "请返回 AI 功能配置页面后重试。"},
                400,
            )
            response.headers["Cache-Control"] = "no-store, max-age=0"
            return response
        feature = form.get("feature", "")
        if feature not in AI_FEATURES:
            return render_ai_config(request, access_context, error="未知的 AI 功能配置。", status_code=400)
        try:
            threshold = form.get("auto_confirm_threshold", "0.95" if feature == "contact_assessment" else
                                 str(ai_feature_config.DEFAULT_AUTO_CONFIRM_THRESHOLD))
            if feature == "contact_assessment":
                try:
                    valid_threshold = 0.95 <= float(threshold) <= 1
                except (TypeError, ValueError):
                    valid_threshold = False
                if not valid_threshold:
                    raise ai_feature_config.FeatureConfigInputError("全员身份识别阈值必须为 0.95 到 1.00。")
            with connection() as conn, conn.transaction():
                key_rotated = bool(form.get("api_key", "").strip())
                ai_feature_config.save_feature_config(
                    conn,
                    ai_feature_config.api_key_cipher(get_settings().token_encryption_key),
                    feature,
                    provider="openai",
                    project_id=form.get("project_id", ""),
                    model=form.get("model", ""),
                    enabled=form.get("enabled") == "1",
                    api_key=form.get("api_key", ""),
                    updated_by_authorization_id=access_context["authorization"]["id"],
                    auto_confirm_threshold=threshold,
                )
                if feature == offhours_summary.FEATURE:
                    offhours_summary.save_settings_and_enqueue(
                        conn, access_context["authorization"]["id"], form,
                    )
                if feature == "contact_assessment" and form.get("enabled") != "1":
                    conn.execute("""
                        UPDATE contact_assessments SET status='cancelled',finished_at=NOW(),lease_until=NULL,
                            outcome='no_action',review_status='not_required',
                            error_message='已暂停；可在人员详情继续识别。'
                        WHERE policy_version=%s AND status='running'
                        """, (contact_assessments.POLICY_VERSION,))
                conn.execute(
                    "INSERT INTO audit_events (id, authorization_id, action, metadata) VALUES (%s, %s, %s, %s::jsonb)",
                    (
                        uuid4(),
                        access_context["authorization"]["id"],
                        "ai_feature_config_updated",
                        json.dumps(
                            {
                                "feature": feature,
                                "provider": "openai",
                                "project_id": form.get("project_id", "").strip(),
                                "model": form.get("model", "").strip(),
                                "enabled": form.get("enabled") == "1",
                                "auto_confirm_threshold": threshold.strip(),
                                "key_rotated": key_rotated,
                            }
                        ),
                    ),
                )
        except ai_feature_config.FeatureConfigInputError as exc:
            return render_ai_config(request, access_context, error=str(exc), status_code=400)
        saved_url = (
            "/admin/ai?saved=offhours-summary#offhours-summary" if feature == offhours_summary.FEATURE else
            "/admin/ai?saved=people-identity#contact-assessment" if feature == "contact_assessment" else "/admin/ai"
        )
        response = RedirectResponse(url=saved_url, status_code=303)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @router.post("/admin/ai/resume")
    async def admin_resume_ai_queue(request: Request) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        form = await _form_fields(request)
        if not _csrf_matches(form, csrf_token(access_context["authorization"])):
            response = render_template(
                request,
                "error.html",
                {"title": "请求校验失败", "detail": "请返回 AI 功能配置页面后重试。"},
                400,
            )
            response.headers["Cache-Control"] = "no-store, max-age=0"
            return response
        with connection() as conn, conn.transaction():
            ai_dispatch.resume(conn)
            conn.execute(
                "INSERT INTO audit_events (id, authorization_id, action, metadata) VALUES (%s, %s, %s, %s::jsonb)",
                (uuid4(), access_context["authorization"]["id"], "ai_queue_resumed", json.dumps({"scope": "shared"})),
            )
        response = RedirectResponse(url="/admin/ai", status_code=303)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

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
    return bool(supplied) and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _role_label(role: str) -> str:
    return {"owner": "系统主人", "member": "按页面授权 · 只读"}.get(role, "管理员")
