from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_offhours_summary import pg, seeded  # noqa: F401

from app import offhours_summary as summaries


def test_v5_replay_preserves_reports_and_usage_and_bounds_the_window(pg, seeded):  # noqa: F811
    s = seeded
    s.save()
    pg.execute('UPDATE offhours_summary_settings SET recurring_enabled=TRUE')
    now = datetime.now(UTC)
    today = now.astimezone(summaries.LONDON).date()
    cases = {
        'recent_done': (today - timedelta(days=1), now - timedelta(days=1), 'succeeded', 'v4', s.ids['cm']),
        'recent_lease': (today - timedelta(days=6), now - timedelta(days=6), 'running', 'v4', s.ids['cm']),
        'outside': (today - timedelta(days=7), now - timedelta(days=7), 'running', 'v4', s.ids['cm']),
        'future_date': (today + timedelta(days=1), now + timedelta(days=1), 'running', 'v4', s.ids['cm']),
        'future_period': (today, now + timedelta(hours=1), 'queued', 'v4', s.ids['cm']),
        'new_policy': (today - timedelta(days=3), now - timedelta(days=3), 'succeeded', 'v5', s.ids['cm']),
        'inactive': (today - timedelta(days=2), now - timedelta(days=2), 'running', 'v4', s.ids['reviewer']),
        'no_consent': (today - timedelta(days=2), now - timedelta(days=2), 'running', 'v4', s.ids['tester']),
    }
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (s.ids['reviewer'],))
    pg.execute('UPDATE authorizations SET body_consent_at=NULL WHERE id=%s', (s.ids['tester'],))
    ids = {}
    for name, (day, start, status, policy, cm) in cases.items():
        run, task = uuid4(), uuid4()
        ids[name] = run, task
        pg.execute('''INSERT INTO offhours_summary_runs(id,requested_by_authorization_id,
            responsible_authorization_id,window_start,window_end,report_date,period,coverage,refresh_until)
            VALUES(%s,%s,%s,%s,%s,%s,'work',%s::jsonb,%s)''',
            (run, s.ids['owner'], cm, start, start + timedelta(hours=1), day,
             '{"policy":"shift-handling-' + policy + '","messages":9}', start + timedelta(hours=1)))
        pg.execute('''INSERT INTO offhours_summary_tasks(id,run_id,chat_id,input_fingerprint,cutoff,status,
            lease_until,attempts,result_ciphertext,checkpoint_ciphertext,usage)
            VALUES(%s,%s,'chat',%s,%s,%s,NOW()+INTERVAL '10 minutes',1,'old-result','old-checkpoint',
                '{"input_tokens":123,"output_tokens":45}'::jsonb)''', (task, run, name, start, status))

    lease = {'id': ids['recent_lease'][1], 'attempts': 1}
    summaries._current(pg, lease)
    pg.execute("UPDATE ai_feature_configs SET enabled=FALSE WHERE feature='offhours_summary'")
    configs = pg.execute('SELECT to_jsonb(c) FROM ai_feature_configs c ORDER BY feature').fetchall()
    people = pg.execute('SELECT * FROM contact_assignments ORDER BY id').fetchall()
    messages = pg.execute('SELECT * FROM chat_messages ORDER BY message_id').fetchall()
    before = {name: pg.execute('SELECT * FROM offhours_summary_tasks WHERE id=%s', (task,)).fetchone()
              for name, (_, task) in ids.items()}
    migration = Path(__file__).parents[1] / 'migrations' / '090_summary_reply_and_resolution.sql'
    pg.execute(migration.read_text())

    for name, (run, task) in ids.items():
        after = pg.execute('SELECT * FROM offhours_summary_tasks WHERE id=%s', (task,)).fetchone()
        if name in {'recent_done', 'recent_lease'}:
            assert not after['is_current'] and after['lease_until'] is None
            assert after['status'] == ('succeeded' if name == 'recent_done' else 'cancelled')
            for column in ('result_ciphertext', 'checkpoint_ciphertext', 'usage', 'attempts'):
                assert after[column] == before[name][column]
            refreshed = pg.execute('SELECT * FROM offhours_summary_runs WHERE id=%s', (run,)).fetchone()
            assert refreshed['next_check_at'] <= now + timedelta(seconds=10)
            assert refreshed['refresh_until'] > now + timedelta(hours=23)
            assert refreshed['coverage'] == {'policy': 'shift-handling-v4', 'messages': 9}
        else:
            assert after == before[name]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(summaries, '_enabled', lambda conn: True)
        with pytest.raises(summaries.SummaryInputError):
            summaries._current(pg, lease)
    assert pg.execute('SELECT to_jsonb(c) FROM ai_feature_configs c ORDER BY feature').fetchall() == configs
    assert pg.execute('SELECT * FROM contact_assignments ORDER BY id').fetchall() == people
    assert pg.execute('SELECT * FROM chat_messages ORDER BY message_id').fetchall() == messages
    assert pg.execute('SELECT next_period_check_at FROM offhours_summary_settings').fetchone()[
        'next_period_check_at'] is None
