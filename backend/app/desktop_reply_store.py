"""CM-scoped, encrypted 24-hour reply receipts; never replay uncertain paid work."""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Mapping
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from .crypto import TokenCipher

_ERROR_MESSAGES = {
    "provider_error": "AI 服务暂时无法完成请求，请使用新的请求重试。",
    "provider_rate_limit": "AI 服务限流，请稍后使用新的请求生成。",
    "provider_quota": "AI 配额不足，请联系管理员。",
    "provider_timeout": "AI 请求超时，是否产生费用尚不确定；本请求不会自动重跑。",
    "invalid_ai_response": "AI 返回格式无效，本请求不会自动重跑。",
    "feature_disabled": "桌面回复功能当前未启用。",
    "actor_inactive": "账号授权已失效，请重新登录。",
    "unauthorized": "登录已失效，请重新登录。",
    "csrf_invalid": "请求校验失败，请刷新页面。",
    "cancelled": "请求已取消，本请求不会自动重跑。",
    "abandoned": "处理期限已过，计费结果不确定；本请求不会自动重跑。",
    "conversation_not_found": "没有找到当前账号可访问的会话。",
    "conversation_ambiguous": "会话名称对应多个会话，需要明确选择。",
    "conversation_forbidden": "当前账号无权访问此会话。",
    "context_unavailable": "当前会话的上下文暂不可用。",
}


class ActorInactiveError(PermissionError):
    """A receipt cannot be written or read after its CM's authorization ends."""


def safe_error(code):
    code = code if code in _ERROR_MESSAGES else "provider_error"
    return code, _ERROR_MESSAGES[code]


def result_cipher(master_key: bytes) -> TokenCipher:
    key = hmac.digest(master_key, b"lark-message-stats/desktop-reply-result-key/v1", "sha256")
    return TokenCipher(key, b"lark-message-stats/desktop-reply-result/v1")


def _active(conn, actor_id: UUID) -> None:
    # This short row lock prevents revocation racing the receipt transaction.
    if not conn.execute(
        "SELECT id FROM authorizations WHERE id=%s AND status='active' FOR SHARE", (actor_id,)
    ).fetchone():
        raise ActorInactiveError("账号授权已失效，请重新登录。")
    conn.execute(
        "DELETE FROM desktop_reply_requests WHERE actor_id=%s AND expires_at <= clock_timestamp()",
        (actor_id,),
    )


def _ids(actor_id, request_id):
    return UUID(str(actor_id)), UUID(str(request_id))


def _receipt(conn, actor_id, request_id):
    row = conn.execute(
        "SELECT * FROM desktop_reply_requests WHERE actor_id=%s AND request_id=%s FOR UPDATE",
        (actor_id, request_id),
    ).fetchone()
    if row and row["status"] == "running":
        abandoned = conn.execute(
            """UPDATE desktop_reply_requests SET status='abandoned', error_code='abandoned',
                   error_message=%s, error_http_status=409, updated_at=clock_timestamp()
               WHERE actor_id=%s AND request_id=%s AND status='running'
                   AND lease_until <= clock_timestamp() RETURNING *""",
            (_ERROR_MESSAGES["abandoned"], actor_id, request_id),
        ).fetchone()
        row = abandoned or row
    return row


def _claim_status(row):
    if not row:
        return None
    return dict(row, claim_status={"succeeded": "cached", "running": "running"}.get(row["status"], "failed"))


def claim(conn, actor_id, request_id, request_hash: str) -> dict:
    """Caller commits this short transaction before invoking AI; exactly one owner wins."""
    actor_id, request_id = _ids(actor_id, request_id)
    if not isinstance(request_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", request_hash):
        raise ValueError("request_hash must be a lowercase SHA-256 digest")
    _active(conn, actor_id)
    row = conn.execute(
        """INSERT INTO desktop_reply_requests(actor_id,request_id,request_hash,status,fence_token,lease_until)
           VALUES(%s,%s,%s,'running',%s,clock_timestamp()+INTERVAL '120 seconds')
           ON CONFLICT(actor_id,request_id) DO NOTHING RETURNING *""",
        (actor_id, request_id, request_hash, uuid4()),
    ).fetchone()
    if row:
        return dict(row, claim_status="new")
    row = _receipt(conn, actor_id, request_id)
    if row["request_hash"] != request_hash:
        return dict(row, claim_status="conflict")
    return _claim_status(row)


def read_request(conn, actor_id, request_id) -> dict | None:
    actor_id, request_id = _ids(actor_id, request_id)
    _active(conn, actor_id)
    return _claim_status(_receipt(conn, actor_id, request_id))


def _telemetry(usage, latency_ms):
    counters = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = (usage or {}).get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1:
            counters[key] = value
    if latency_ms is not None and (type(latency_ms) is not int or not 0 <= latency_ms <= 2**31 - 1):
        raise ValueError("latency_ms must be a nonnegative 32-bit integer")
    return Jsonb(counters), latency_ms


def finish_success(conn, actor_id, request_id, fence_token, result: Mapping, cipher: TokenCipher,
                   *, usage=None, latency_ms=None) -> bool:
    actor_id, request_id = _ids(actor_id, request_id)
    _active(conn, actor_id)
    _receipt(conn, actor_id, request_id)
    counters, latency_ms = _telemetry(usage, latency_ms)
    sealed = cipher.encrypt(json.dumps(dict(result), ensure_ascii=False, separators=(",", ":")))
    return conn.execute(
        """UPDATE desktop_reply_requests SET status='succeeded', result_ciphertext=%s, usage=%s,
               latency_ms=%s,updated_at=clock_timestamp()
           WHERE actor_id=%s AND request_id=%s AND fence_token=%s AND status='running'
               AND lease_until>clock_timestamp() AND expires_at>clock_timestamp() RETURNING request_id""",
        (sealed, counters, latency_ms, actor_id, request_id, UUID(str(fence_token))),
    ).fetchone() is not None


def finish_error(conn, actor_id, request_id, fence_token, *, code: str, message: str = "",
                 http_status: int = 500, status: str = "failed", usage=None, latency_ms=None) -> bool:
    actor_id, request_id = _ids(actor_id, request_id)
    _active(conn, actor_id)
    _receipt(conn, actor_id, request_id)
    if status not in {"failed", "cancelled"} or type(http_status) is not int or not 400 <= http_status <= 599:
        raise ValueError("invalid terminal error status")
    # Only fixed messages enter storage; provider errors can contain tokens or prompt text.
    code, message = safe_error("cancelled" if status == "cancelled" else code)
    counters, latency_ms = _telemetry(usage, latency_ms)
    return conn.execute(
        """UPDATE desktop_reply_requests SET status=%s,error_code=%s,error_message=%s,error_http_status=%s,
               usage=%s,latency_ms=%s,updated_at=clock_timestamp()
           WHERE actor_id=%s AND request_id=%s AND fence_token=%s AND status='running'
               AND lease_until>clock_timestamp() AND expires_at>clock_timestamp() RETURNING request_id""",
        (status, code, message, http_status, counters, latency_ms,
         actor_id, request_id, UUID(str(fence_token))),
    ).fetchone() is not None
