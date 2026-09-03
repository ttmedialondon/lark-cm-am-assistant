from contextlib import nullcontext

from app import lark_sync_admission


def test_rate_limit_pause_only_extends_the_global_cooldown(monkeypatch) -> None:
    calls = []

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, query, params=None):
            calls.append((query, params))

    monkeypatch.setattr(lark_sync_admission, "connection", lambda: nullcontext(Connection()))

    lark_sync_admission.LarkSyncAdmission(max_inflight=3, requests_per_second=10).pause(7)

    assert "GREATEST" in calls[0][0]
    assert "rate_limited_count = rate_limited_count + 1" in calls[0][0]
    assert calls[0][1] == (7,)
