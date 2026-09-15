"""London work/off-hours windows and bounded, idempotent seven-day scheduling."""

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4

from . import offhours_summary as summaries


def window(day, period):
    if period not in ('work', 'off'):
        raise summaries.SummaryInputError('请选择上班或下班时段。')
    try:
        day = date.fromisoformat(str(day))
    except ValueError as exc:
        raise summaries.SummaryInputError('请选择有效日期。') from exc
    start = datetime.combine(day, time(10 if period == 'work' else 18), summaries.LONDON)
    end = datetime.combine(day + timedelta(days=period == 'off'),
                           time(18 if period == 'work' else 10), summaries.LONDON)
    return start.astimezone(UTC), end.astimezone(UTC)


def selection(conn, actor, cm_id=None, day=None, period=None, *, now=None):
    now = (now or datetime.now(UTC)).astimezone(summaries.LONDON)
    actor = UUID(str(actor))
    cm_id = UUID(str(cm_id)) if cm_id else actor
    # Default privileged viewers to the existing Remi report; ordinary CMs to themselves.
    if cm_id == actor and not summaries._roster(conn, actor) and actor in summaries.TEST_VIEWERS:
        cm_id = summaries.REMI_ID
    cm, owner = summaries._authorize(conn, actor, cm_id=cm_id)
    manager = owner or bool(conn.execute(
        "SELECT 1 FROM admin_page_permissions WHERE authorization_id=%s AND permission='creator-manage'",
        (actor,),
    ).fetchone())
    cms = conn.execute("SELECT id,display_name FROM authorizations WHERE status='active' "
                       "AND body_consent_at IS NOT NULL ORDER BY display_name,id").fetchall() if manager else [cm]
    period = period or ('work' if 10 <= now.hour < 18 else 'off')
    selected_day = day or (now.date() - timedelta(days=now.hour < 10)).isoformat()
    start, end = window(selected_day, period)
    dates = [(now.date() - timedelta(days=i)).isoformat() for i in range(7)]
    if str(selected_day) not in dates:
        raise summaries.SummaryInputError('仅支持查看今天及之前六天的时段。')
    return cm_id, start, end, {
        'date': str(selected_day), 'period': period, 'dates': dates,
        'cms': [{'id': str(c['id']), 'name': c['display_name'] or '未命名 CM'} for c in cms],
    }


def seed_due(conn, now):
    state = conn.execute("SELECT * FROM offhours_summary_settings WHERE id FOR UPDATE").fetchone()
    if not state or not state['recurring_enabled'] or not summaries._enabled(conn):
        return
    if state['next_period_check_at'] and state['next_period_check_at'] > now:
        return
    owner = conn.execute("SELECT a.id FROM authorizations a JOIN admin_roles r ON r.authorization_id=a.id "
                         "WHERE a.status='active' AND r.role='owner' AND r.is_active ORDER BY a.id LIMIT 1").fetchone()
    if not owner:
        return
    cms = conn.execute("SELECT a.id FROM authorizations a WHERE a.status='active' "
                       "AND a.body_consent_at IS NOT NULL AND (EXISTS (SELECT 1 FROM contact_assignments c "
                       "WHERE c.responsible_authorization_id=a.id AND c.ended_at IS NULL) OR EXISTS "
                       "(SELECT 1 FROM creator_account_assignments c WHERE c.responsible_authorization_id=a.id "
                       "AND c.ended_at IS NULL)) ORDER BY a.id").fetchall()
    for cm in cms:
        if not summaries._roster(conn, cm['id']):
            continue
        for offset in range(7):
            day = now.astimezone(summaries.LONDON).date() - timedelta(days=offset)
            for period in ('work', 'off'):
                start, end = window(day, period)
                if start >= now:
                    continue
                conn.execute("""INSERT INTO offhours_summary_runs(id,requested_by_authorization_id,
                    responsible_authorization_id,window_start,window_end,report_date,period,next_check_at,refresh_until)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(responsible_authorization_id,report_date,period) WHERE period IS NOT NULL DO NOTHING""",
                    (uuid4(), owner['id'], cm['id'], start, end, day, period, now,
                     max(end, now) + timedelta(days=1)))
    conn.execute("UPDATE offhours_summary_settings SET next_period_check_at=%s WHERE id",
                 (now + timedelta(minutes=15),))
