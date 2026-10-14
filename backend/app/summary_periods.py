"""London work/off-hours windows and bounded, idempotent seven-day scheduling."""

import json
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
                       "WHERE c.responsible_authorization_id=a.id AND c.ended_at IS NULL)) ORDER BY a.id").fetchall()
    event_rules, rules_digest = summaries._event_rules(conn, summaries.get_settings())
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
                    responsible_authorization_id,window_start,window_end,report_date,period,next_check_at,refresh_until,
                    event_rules_ciphertext,event_rules_digest)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(responsible_authorization_id,report_date,period) WHERE period IS NOT NULL DO NOTHING""",
                    (uuid4(), owner['id'], cm['id'], start, end, day, period, now,
                     max(end, now) + timedelta(days=1), event_rules, rules_digest))
    conn.execute("UPDATE offhours_summary_settings SET next_period_check_at=%s WHERE id",
                 (now + timedelta(minutes=15),))


def backfill_due(conn, now):
    """Consume one durable admission signal without reopening unrelated old reports."""
    state = conn.execute("SELECT recurring_enabled FROM offhours_summary_settings WHERE id").fetchone()
    if not state or not state['recurring_enabled'] or not summaries._enabled(conn):
        return 0
    signal = conn.execute("""SELECT * FROM offhours_summary_backfills WHERE not_before<=%s
        ORDER BY not_before,requested_at,contact_id,responsible_authorization_id LIMIT 1""", (now,)).fetchone()
    if not signal:
        return 0
    owner = conn.execute("""SELECT a.id FROM authorizations a JOIN admin_roles r ON r.authorization_id=a.id
        WHERE a.status='active' AND r.role='owner' AND r.is_active ORDER BY a.id LIMIT 1""").fetchone()
    if not owner:
        return 0
    person, cm = signal['contact_id'], signal['responsible_authorization_id']
    params = (person, cm, signal['version'])
    eligible = conn.execute("""SELECT EXISTS(SELECT 1 FROM contact_roles WHERE contact_id=%s
        AND role='creator' AND status='confirmed') AND
        EXISTS(SELECT 1 FROM contact_assignments WHERE contact_id=%s
            AND responsible_authorization_id=%s AND ended_at IS NULL) AS valid""",
        (person, person, cm)).fetchone()['valid']
    if not eligible:
        conn.execute("""DELETE FROM offhours_summary_backfills WHERE contact_id=%s
            AND responsible_authorization_id=%s AND version=%s""", params)
        return 1
    try:
        summaries._authorize(conn, owner['id'], owner_only=True, cm_id=cm)
        roster = summaries._roster(conn, cm)
        chats = summaries._related_chats(conn, roster, cm_id=cm)
    except (PermissionError, summaries.SummaryInputError):
        roster, chats = [], []
    if not any(row['contact_id'] == person for row in roster):
        conn.execute("""UPDATE offhours_summary_backfills SET not_before=%s WHERE contact_id=%s
            AND responsible_authorization_id=%s AND version=%s""", (now + timedelta(minutes=15), *params))
        return 0
    # Retain the full roster in shared groups; only filter the conversations to visit.
    affected = [chat for chat in chats if person in chat['contact_ids']]
    deferred, blocked, waiting_sync, inserted = False, False, False, 0
    event_rules, rules_digest = summaries._event_rules(conn, summaries.get_settings())
    run_ids = []
    for offset in range(7):
        day = now.astimezone(summaries.LONDON).date() - timedelta(days=offset)
        for period in ('work', 'off'):
            start, end = window(day, period)
            if start >= now:
                continue
            run = conn.execute("""INSERT INTO offhours_summary_runs(id,requested_by_authorization_id,
                responsible_authorization_id,window_start,window_end,report_date,period,next_check_at,refresh_until,
                event_rules_ciphertext,event_rules_digest)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(responsible_authorization_id,report_date,period) WHERE period IS NOT NULL
                DO UPDATE SET requested_by_authorization_id=EXCLUDED.requested_by_authorization_id
                RETURNING *,id AS run_id""",
                (uuid4(), owner['id'], cm, start, end, day, period,
                 now if now < end else None, end + timedelta(days=1) if now < end else end,
                 event_rules, rules_digest)).fetchone()
            run_ids.append(run['id'])
            count = summaries._schedule_run(conn, run, now, contact_ids={person}, roster=roster, chats=chats)
            deferred |= count is None
            inserted += count or 0
            # Keep the signal until affected analysis finishes. A later source change
            # must be able to rebuild a cancelled task even in a stopped old report.
            deferred |= bool(conn.execute("""SELECT 1 FROM offhours_summary_tasks WHERE run_id=%s
                AND is_current AND chat_id=ANY(%s::text[]) AND status IN ('queued','running') LIMIT 1""",
                (run['id'], [chat['chat_id'] for chat in affected])).fetchone())
            blocked |= bool(conn.execute("""SELECT 1 FROM offhours_summary_tasks WHERE run_id=%s
                AND is_current AND chat_id=ANY(%s::text[]) AND status IN ('failed','invalid','cancelled') LIMIT 1""",
                (run['id'], [chat['chat_id'] for chat in affected])).fetchone())
            # Closed periods wait briefly for known conversations to finish syncing.
            # The one-day grace is bounded; missing data remains a report gap, not a fact.
            if end <= now and now < signal['requested_at'] + timedelta(days=1):
                waiting_sync |= any(not summaries._covers_window(chat, start, end) for chat in affected)
    from . import summary_dialogues

    summary_dialogues.schedule_merges(conn, summaries.get_settings())
    deferred |= bool(conn.execute("""SELECT 1 FROM offhours_summary_runs r WHERE r.id=ANY(%s::uuid[]) AND (
        EXISTS(SELECT 1 FROM offhours_summary_tasks t WHERE t.run_id=r.id AND t.contact_id=%s
            AND t.is_current AND t.status IN ('queued','running')) OR (
        EXISTS(SELECT 1 FROM offhours_summary_tasks t WHERE t.run_id=r.id AND t.is_current
            AND t.chat_id=ANY(%s::text[]) AND t.status='succeeded') AND
        NOT EXISTS(SELECT 1 FROM offhours_summary_tasks t WHERE t.run_id=r.id AND t.is_current
            AND t.chat_id=ANY(%s::text[]) AND t.status IN ('failed','invalid','cancelled')) AND
        NOT EXISTS(SELECT 1 FROM offhours_summary_tasks t WHERE t.run_id=r.id
            AND t.contact_id=%s AND t.is_current))) LIMIT 1""",
        (run_ids, person, [chat['chat_id'] for chat in affected],
         [chat['chat_id'] for chat in affected], person)).fetchone())
    blocked |= bool(conn.execute("""SELECT 1 FROM offhours_summary_tasks WHERE run_id=ANY(%s::uuid[])
        AND contact_id=%s AND is_current AND status IN ('failed','invalid','cancelled') LIMIT 1""",
        (run_ids, person)).fetchone())
    if deferred or waiting_sync or (blocked and now < signal['requested_at'] + timedelta(days=1)):
        delay = timedelta(seconds=30) if deferred else timedelta(minutes=15)
        conn.execute("""UPDATE offhours_summary_backfills SET not_before=%s WHERE contact_id=%s
            AND responsible_authorization_id=%s AND version=%s""", (now + delay, *params))
        return inserted
    removed = conn.execute("""DELETE FROM offhours_summary_backfills WHERE contact_id=%s
        AND responsible_authorization_id=%s AND version=%s RETURNING contact_id""", params).fetchone()
    if removed:
        conn.execute("""INSERT INTO audit_events(id,authorization_id,action,metadata)
            VALUES(%s,%s,'creator_summary_backfill_scheduled',%s::jsonb)""",
            (uuid4(), owner['id'], json.dumps({
                'contact_id': str(person), 'cm_id': str(cm), 'new_tasks': inserted, 'days': 7,
                'has_failed_tasks': blocked})))
    return inserted or int(bool(removed))
