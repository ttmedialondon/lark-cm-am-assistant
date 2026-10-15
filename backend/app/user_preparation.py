"""Small, read-only preparation state for business pages, not sync diagnostics."""


def status_for(conn, authorization_id):
    row = conn.execute("""SELECT a.status, a.body_consent_at,
        j.status job_status, j.discovery_status,
        EXISTS(SELECT 1 FROM sync_jobs WHERE authorization_id=a.id AND status='succeeded') established,
        EXISTS(SELECT 1 FROM authorization_chats ac JOIN chats c ON c.chat_id=ac.chat_id
            WHERE ac.authorization_id=a.id AND ac.is_visible AND ac.sync_enabled
              AND COALESCE(c.chat_status,'normal')='normal' AND ac.sync_status<>'not_readable'
              AND NOT ac.backfill_complete
              AND (ac.recent_coverage_start_at IS NULL OR ac.last_incremental_at IS NULL)) pending
        FROM authorizations a LEFT JOIN LATERAL (
            SELECT status,discovery_status FROM sync_jobs WHERE authorization_id=a.id
            ORDER BY created_at DESC LIMIT 1
        ) j ON TRUE WHERE a.id=%s""", (authorization_id,)).fetchone()
    needs_authorization = not row or row['status'] != 'active' or not row['body_consent_at'] or (
        row['job_status'] == 'attention_required'
    )
    preparing = bool(not needs_authorization and not row['established']
                     and row['job_status'] in ('queued', 'running')
                     and (row['discovery_status'] != 'succeeded' or row['pending']))
    if needs_authorization:
        status, badge, detail = '需要重新连接 Lark', 'warning', '请重新连接后继续使用，已有资料会保留。'
    elif row['job_status'] == 'failed':
        status, badge, detail = '资料暂时无法更新', 'warning', '已有内容仍可使用。若持续出现，请联系管理员。'
    elif preparing:
        status, badge, detail = '正在为你准备工作台', 'primary', (
            '首次使用需要一些时间整理聊天和达人资料。无需停留在此页面，已有内容可照常使用。'
        )
    else:
        status, badge, detail = 'Lark 已连接', 'success', ''
    return {'preparing': preparing, 'needs_authorization': needs_authorization,
            'status': status, 'badge': badge, 'detail': detail}
