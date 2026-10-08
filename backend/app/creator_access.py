"""Creator-domain permissions, shared by imports, roster reads and legacy review."""

from uuid import UUID


def parse_id(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("请求标识无效。") from exc


def require_owner(conn, actor_id):
    if not conn.execute(
        """SELECT 1 FROM authorizations a JOIN admin_roles r ON r.authorization_id=a.id
           WHERE a.id=%s AND a.status='active' AND r.role='owner' AND r.is_active""",
        (actor_id,),
    ).fetchone():
        raise PermissionError("只有当前有效的系统主人可以操作。")


def authorized_cm(conn, cm_id):
    return conn.execute(
        """SELECT id,open_id,display_name FROM authorizations
           WHERE id=%s AND status='active' AND body_consent_at IS NOT NULL""",
        (cm_id,),
    ).fetchone()


def is_owner(conn, actor):
    try:
        require_owner(conn, actor)
        return True
    except PermissionError:
        return False


def require_broadcast_access(conn, actor, *, preview=False):
    """A separate frontend grant never inherits creator-management privileges."""
    row = conn.execute(
        """SELECT a.status,a.broadcast_enabled,r.role,r.is_active
           FROM authorizations a LEFT JOIN admin_roles r ON r.authorization_id=a.id
           WHERE a.id=%s""", (parse_id(actor),),
    ).fetchone()
    owner = bool(row and row["role"] == "owner" and row["is_active"])
    if (not row or row["status"] not in {"active", "attention_required"}
            or not (owner or row["broadcast_enabled"] or preview)):
        raise PermissionError("您尚未开通群发功能。")
    return owner


def can_view_all(conn, actor):
    return is_owner(conn, actor) or bool(conn.execute(
        "SELECT 1 FROM admin_page_permissions WHERE authorization_id=%s "
        "AND permission IN ('creator-roster','creator-manage')",
        (actor,),
    ).fetchone())


def access(conn, actor, cm_id=None, *, manage=False, read_only=False):
    actor = parse_id(actor)
    if not conn.execute("SELECT 1 FROM authorizations WHERE id=%s AND status='active'", (actor,)).fetchone():
        raise PermissionError("请重新登录。")
    can_manage = is_owner(conn, actor) or bool(conn.execute(
        "SELECT 1 FROM admin_page_permissions WHERE authorization_id=%s AND permission='creator-manage'",
        (actor,),
    ).fetchone())
    cm_id = parse_id(cm_id) if cm_id else actor
    if (manage or actor != cm_id) and not can_manage and not (read_only and not manage and can_view_all(conn, actor)):
        raise PermissionError("只能处理您自己的达人候选。")
    if not authorized_cm(conn, cm_id):
        raise PermissionError("该负责人的聊天授权不可用，请先完成授权。")
    return cm_id, can_manage
