"""Small internal admin router for the owner-only administration console.

The router receives the session and rendering functions from ``main``.  That
keeps it independent from the web application's module-level globals and
avoids importing ``main`` back into an admin module.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from . import (
    admin_reporting,
    ai_dispatch,
    ai_feature_config,
    contact_assessments,
    contact_assignments,
    organization_sync,
)
from .config import get_settings
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
        "title": "联系人身份识别",
        "description": "自动判断外部联系人的业务身份；高置信度结果才会确认身份。",
        "enabled_label": "启用联系人判定",
    },
    "contact_assignment": {
        "title": "达人负责人匹配",
        "description": "只在达人尚无负责人时，根据直接互动生成一位 CM/AM 建议；由被建议人确认后生效。",
        "enabled_label": "启用负责人匹配",
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

    def pagination(page_data: dict[str, Any], path: str, *, page_name: str = "page", **query: object) -> dict[str, Any]:
        """Build compact, numbered navigation while retaining every query value."""

        page = int(page_data["page"])
        query = {key: value for key, value in query.items() if value is not None}

        def url(target_page: int) -> str:
            return f"{path}?{urlencode({**query, page_name: target_page})}"

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
    ) -> str:
        query: dict[str, object] = {
            "page": page,
            "category": category,
            "organization_status": organization_status,
        }
        if organization_query:
            query["organization_query"] = organization_query
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

    def render_creators(
        request: Request,
        access_context: dict[str, Any],
        *,
        page: int = 1,
        assignment: str = "all",
    ) -> Response:
        assignment = admin_reporting.normalize_creator_assignment_filter(assignment)
        creator_page = admin_reporting.creators(page, assignment)
        return render_template(
            request,
            "admin/creators.html",
            {
                **page_context(access_context, "creators"),
                "creators": creator_page["items"],
                "assignment": assignment,
                "creator_page": pagination(creator_page, "/admin/creators", assignment=assignment),
            },
        )

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
        response = render_template(
            request,
            "admin/ai.html",
            {
                **page_context(access_context, "ai"),
                "configs": configs,
                "ai_dispatch": dispatch_status,
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

    @router.post("/admin/users/{authorization_id}/assignment-eligibility")
    async def admin_set_assignment_eligibility(
        request: Request, authorization_id: UUID, page: int = 1
    ) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        form = await _form_fields(request)
        if not _csrf_matches(form, csrf_token(access_context["authorization"])):
            return render_template(
                request,
                "error.html",
                {"title": "请求校验失败", "detail": "请返回人员页面后重试。"},
                400,
            )
        enabled = form.get("enabled") == "1"
        with connection() as conn, conn.transaction():
            if enabled:
                # Match candidate confirmation's config -> authorization lock order.
                conn.execute(
                    "SELECT feature FROM ai_feature_configs WHERE feature = %s FOR UPDATE",
                    (contact_assignments.FEATURE,),
                ).fetchone()
            user = conn.execute(
                """
                UPDATE authorizations
                SET assignment_eligible = %s, updated_at = NOW()
                WHERE id = %s
                RETURNING id
                """,
                (enabled, authorization_id),
            ).fetchone()
            if not user:
                return render_template(
                    request,
                    "error.html",
                    {"title": "未找到授权用户", "detail": "该用户可能已经被删除。"},
                    404,
                )
            if enabled:
                conn.execute(
                    """
                    UPDATE ai_feature_configs
                    SET automatic_queue_checked_at = '-infinity'::timestamptz, updated_at = NOW()
                    WHERE feature = %s
                    """,
                    (contact_assignments.FEATURE,),
                )
            conn.execute(
                "INSERT INTO audit_events (id, authorization_id, action, metadata) VALUES (%s, %s, %s, %s::jsonb)",
                (
                    uuid4(),
                    access_context["authorization"]["id"],
                    "contact_assignment_eligibility_updated",
                    json.dumps({"target_authorization_id": str(authorization_id), "enabled": enabled}),
                ),
            )
        return RedirectResponse(url=f"/admin/users/{authorization_id}?{urlencode({'page': page})}", status_code=303)

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
            selected_chat = admin_reporting.message_conversation(selected_user["id"], chat_id) if chat_id else None
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
    def admin_creators(request: Request, page: int = 1, assignment: str = "all") -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        return render_creators(request, access_context, page=page, assignment=assignment)

    @router.get("/admin/ai")
    def admin_ai_config(request: Request) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        return render_ai_config(request, access_context)

    @router.get("/admin/contacts")
    def admin_contacts(
        request: Request,
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
        contact_page = admin_reporting.contacts(page, category, organization_status, organization_query)
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
                    organization_status=organization_status,
                    organization_query=organization_query or None,
                ),
                "category": category,
                "organization_status": organization_status,
                "organization_query": organization_query,
                "organization_query_param": urlencode({"organization_query": organization_query})
                if organization_query
                else "",
                "organization_sync": admin_reporting.organization_sync_status(),
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
    ) -> Response:
        access_context, denied = owner_access(request)
        if denied is not None:
            return denied
        assert access_context is not None
        category = admin_reporting.normalize_contact_category(category)
        organization_status = admin_reporting.normalize_organization_status(organization_status)
        organization_query = admin_reporting.normalize_organization_query(organization_query)
        detail = admin_reporting.contact_detail(open_id, conversations_page)
        if not detail:
            return render_template(
                request,
                "error.html",
                {"title": "未找到 Lark 参与者", "detail": "该参与者尚未被同步，或不在人员目录中。"},
                404,
            )
        conversations = detail["conversations"]
        is_creator = detail["contact"].get("primary_role") == "creator"
        latest_assessment = contact_assessments.latest_contact_assessment(detail["contact"]["contact_id"])
        assignment = contact_assignments.active_assignment(detail["contact"]["contact_id"]) if is_creator else None
        assignment_assessment = (
            contact_assignments.latest_contact_assignment_assessment(detail["contact"]["contact_id"])
            if is_creator
            else None
        )
        assignees = contact_assignments.assignable_authorizations(detail["contact"]["contact_id"]) if is_creator else []
        assignment_choices = [
            item
            for item in assignees
            if not assignment or item["id"] != assignment["responsible_authorization_id"]
        ]
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
                ),
                "assessment": latest_assessment,
                "assessment_available": contact_assessments.assessment_available(),
                "is_creator": is_creator,
                "assignment": assignment,
                "assignment_assessment": assignment_assessment,
                "assignment_available": contact_assignments.assignment_available() if is_creator else False,
                "assignees": assignees,
                "assignment_choices": assignment_choices,
                "conversations": conversations["items"],
                "conversation_page": pagination(
                    conversations,
                    f"/admin/contacts/{open_id}",
                    page_name="conversations_page",
                    page=page,
                    category=category,
                    organization_status=organization_status,
                    organization_query=organization_query or None,
                ),
            },
        )
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @router.post("/admin/contacts/{open_id}/assignment")
    @router.post("/admin/contacts/{open_id}/assignment/handoff")
    async def admin_set_contact_assignment(
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
                {"title": "请求校验失败", "detail": "请返回达人详情页后重试。"},
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
                {"title": "未找到达人", "detail": "该外部联系人尚未被同步。"},
                404,
            )
            response.headers["Cache-Control"] = "no-store, max-age=0"
            return response
        try:
            contact_assignments.manually_assign(
                access_context["authorization"]["id"],
                contact["contact_id"],
                form.get("responsible_authorization_id", ""),
            )
        except contact_assignments.ContactAssignmentInputError as exc:
            response = render_template(
                request,
                "error.html",
                {"title": "无法更新负责人", "detail": str(exc)},
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
                    auto_confirm_threshold=form.get(
                        "auto_confirm_threshold", str(ai_feature_config.DEFAULT_AUTO_CONFIRM_THRESHOLD)
                    ),
                )
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
                                "auto_confirm_threshold": form.get("auto_confirm_threshold", "").strip(),
                                "key_rotated": key_rotated,
                            }
                        ),
                    ),
                )
        except ai_feature_config.FeatureConfigInputError as exc:
            return render_ai_config(request, access_context, error=str(exc), status_code=400)
        response = RedirectResponse(url="/admin/ai", status_code=303)
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
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _role_label(role: str) -> str:
    return {"owner": "所有者", "operator": "运营管理员", "viewer": "只读管理员"}.get(role, "管理员")
