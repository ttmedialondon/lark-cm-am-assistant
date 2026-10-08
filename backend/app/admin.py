"""Small internal admin router for the owner-only administration console.

The router receives the session and rendering functions from ``main``.  That
keeps it independent from the web application's module-level globals and
avoids importing ``main`` back into an admin module.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated, Any, Protocol
from urllib.parse import parse_qs, quote, urlencode
from uuid import UUID, uuid4

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import (
    admin_permissions,
    admin_reporting,
    ai_dispatch,
    ai_feature_config,
    creator_group_matching,
    creator_handle_matching,
    creator_importance,
    creator_responsibility_matching,
    message_export,
    offhours_summary,
    organization_sync,
)
from .config import get_settings
from .contact_taxonomy import ROLE_LABELS, ROLE_ORDER
from .db import connection
from .http_helpers import (
    csrf_matches as _csrf_matches,
)
from .http_helpers import (
    form_body,
    form_fields,
)


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
    "creator_import": {
        "title": "达人名单匹配",
        "description": "仅处理手动启动的 达人导入批次，不扫描全员或推断负责人。",
        "enabled_label": "按导入批次手动启动",
        "processing_label": "手动启动名单匹配",
    },
    "creator_handle_matching": {
        "title": "达人—handle 匹配",
        "description": (
            "结合群名和授权消息匹配账号，Curva 仅作参考；强证据自动关联，其余待核实，"
            "不修改身份、负责人或已有关系。"
        ),
        "enabled_label": "开启自动匹配",
        "processing_label": "队列匹配，强证据自动关联",
    },
    "creator_responsibility_matching": {
        "title": "达人负责人匹配",
        "description": "从已接入的 CM/AM 中匹配负责人，支持多位共同负责；高置信度结果自动关联，其余待核实。",
        "enabled_label": "启用负责人匹配队列",
        "processing_label": "正式队列，高置信度自动关联",
    },
    "creator_group_classification": {
        "title": "达人群用途分类",
        "description": "结合群名、成员结构和授权聊天判断用途；高置信度自动入库，其余待核实，保留人工分类。",
        "enabled_label": "启用群用途匹配队列",
        "processing_label": "逐群分类，高置信度自动入库，不发送消息",
    },
    "creator_importance": {
        "title": "合作重要度",
        "description": "仅从本人授权聊天提取合作事实，规则分为重点、常规、待评估；不依赖 Curva 或销量。",
        "enabled_label": "开启合作重要度评估",
        "processing_label": "首次历史分析，之后增量更新",
    },
    "offhours_summary": {
        "title": "达人消息总结",
        "description": "整理已入库达人的群聊和私聊，按上班、下班时段归纳并分级。",
        "enabled_label": "开启达人消息总结",
        "processing_label": "直接总结分级，不自动回复",
    },
    "desktop_reply": {
        "title": "桌面回复建议",
        "description": "仅按桌面主动提交的屏幕文字生成回复和中文翻译；独立调用，不进入后台匹配或总结队列。",
        "enabled_label": "开启桌面回复建议",
        "processing_label": "按需生成回复和中文对照",
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
            importance_metrics = creator_importance.metrics(conn)
        response = render_template(
            request,
            "admin/ai.html",
            {
                **page_context(access_context, "ai"),
                "configs": configs,
                "ai_dispatch": dispatch_status,
                "summary_settings": summary_settings,
                "importance_metrics": importance_metrics,
                "summary_saved": request.query_params.get("saved") == "offhours-summary",
                "identity_saved": request.query_params.get("saved") == "people-identity",
                "matching_saved": request.query_params.get("saved") == "creator-handle-matching",
                "responsibility_saved": request.query_params.get("saved") == "creator-responsibility-matching",
                "groups_saved": request.query_params.get("saved") == "creator-group-classification",
                "importance_saved": request.query_params.get("saved") == "creator-importance",
                # Retain this template-safe alias while external callers migrate to ``configs``.
                "config": configs[0]["config"],
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
    def admin_set_page_permissions(
        request: Request, authorization_id: UUID, *, body: Annotated[bytes, Depends(form_body)],
    ) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        try:
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
                raise ValueError("请通过人员页面提交权限设置。")
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
            user_page_data = admin_reporting.message_users(source_ids=list(source_limit))
        else:
            user_page_data = admin_reporting.message_users(user_page)
        users = user_page_data["items"]
        selected_user = next((item for item in users if item["id"] == user_id), None)
        selected_user_in_page = selected_user is not None
        if user_id is not None and selected_user is None and source_limit is None:
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


    @router.post("/admin/contacts/{open_id}/organization")
    def admin_confirm_contact_organization(
        request: Request,
        open_id: str,
        page: int = 1,
        category: str = "all",
        organization_status: str = "all",
        organization_query: str = "",
        *, form: Annotated[dict[str, str], Depends(form_fields)],
    ) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        category = admin_reporting.normalize_contact_category(category)
        organization_status = admin_reporting.normalize_organization_status(organization_status)
        organization_query = admin_reporting.normalize_organization_query(organization_query)
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


    @router.post("/admin/ai")
    def admin_save_ai_config(request: Request, *, form: Annotated[dict[str, str], Depends(form_fields)]) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
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
            threshold = str(ai_feature_config.DEFAULT_AUTO_CONFIRM_THRESHOLD)
            if feature in {creator_responsibility_matching.FEATURE, creator_group_matching.FEATURE}:
                threshold = form.get("auto_confirm_threshold", "0.950")
            enabled = feature != "creator_import" and form.get("enabled") == "1"
            with connection() as conn, conn.transaction():
                key_rotated = bool(form.get("api_key", "").strip())
                ai_feature_config.save_feature_config(
                    conn,
                    ai_feature_config.api_key_cipher(get_settings().token_encryption_key),
                    feature,
                    provider="openai",
                    project_id=form.get("project_id", ""),
                    model=form.get("model", ""),
                    enabled=enabled,
                    api_key=form.get("api_key", ""),
                    updated_by_authorization_id=access_context["authorization"]["id"],
                    auto_confirm_threshold=threshold,
                )
                if feature == offhours_summary.FEATURE:
                    offhours_summary.save_settings_and_enqueue(
                        conn, access_context["authorization"]["id"], form,
                    )
                elif feature == "creator_handle_matching":
                    creator_handle_matching.configuration_changed(conn)
                elif feature == creator_responsibility_matching.FEATURE:
                    creator_responsibility_matching.configuration_changed(conn)
                elif feature == creator_group_matching.FEATURE:
                    creator_group_matching.configuration_changed(conn)
                elif feature == creator_importance.FEATURE:
                    creator_importance.configuration_changed(conn)
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
                                "enabled": enabled,
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
            "/admin/ai?saved=creator-handle-matching#creator-handle-matching"
            if feature == "creator_handle_matching" else
            "/admin/ai?saved=creator-responsibility-matching#creator-responsibility-matching"
            if feature == creator_responsibility_matching.FEATURE else
            "/admin/ai?saved=creator-group-classification#creator-group-classification"
            if feature == creator_group_matching.FEATURE else
            "/admin/ai?saved=creator-importance#creator-importance"
            if feature == creator_importance.FEATURE else "/admin/ai"
        )
        response = RedirectResponse(url=saved_url, status_code=303)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @router.post("/admin/ai/resume")
    def admin_resume_ai_queue(request: Request, *, form: Annotated[dict[str, str], Depends(form_fields)]) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
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


def _role_label(role: str) -> str:
    return {"owner": "系统主人", "member": "按页面授权 · 只读"}.get(role, "管理员")
