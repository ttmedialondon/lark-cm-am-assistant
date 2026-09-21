"""Read-service timing; print aggregates only, never identities, bodies or credentials.

Run with the deployed application's normal environment and backend on PYTHONPATH.
This is not an HTTP load test. It performs one summary warm-up and three samples.
Business data is not modified. Normal summary-view audits are preserved.
"""

import hashlib
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

from fastapi.encoders import jsonable_encoder

from app import db, offhours_summary


class ReadMeter:
    def __init__(self, conn):
        self.conn, self.queries = conn, 0
        self.locations = Counter()

    def execute(self, query, params=None):
        # The service may take SHARE locks while verifying evidence. Permit those
        # reads and their usual audit, but never modify business state.
        audit = (str(query).lstrip().startswith("INSERT INTO audit_events(")
                 and params and len(params) == 4 and params[2] == "creator_workflow_offhours_summary_read")
        if not audit and str(query).split(None, 1)[0].upper() not in {"SELECT", "WITH"}:
            raise RuntimeError("diagnostic refused a non-read statement")
        self.queries += 1
        caller = sys._getframe(1)
        self.locations[f"{Path(caller.f_code.co_filename).name}:{caller.f_lineno}"] += 1
        return self.conn.execute(query, params)


def main():
    print(json.dumps({"source": offhours_summary.__file__}), flush=True)
    connect = []
    for _ in range(20):
        started = time.perf_counter()
        with db.connection() as conn:
            conn.execute("SELECT 1").fetchone()
        connect.append((time.perf_counter() - started) * 1000)
    print(json.dumps({"connect_select_close_median_ms": round(statistics.median(connect), 3),
                      "samples": len(connect)}), flush=True)

    with db.connection() as conn:
        conn.execute("SET statement_timeout='15s'")
        conn.execute("SET lock_timeout='2s'")
        actor = conn.execute(
            "SELECT a.id FROM authorizations a JOIN admin_roles r ON r.authorization_id=a.id "
            "WHERE a.status='active' AND r.role='owner' AND r.is_active ORDER BY a.id LIMIT 1",
        ).fetchone()
        run = conn.execute("SELECT responsible_authorization_id,report_date,period FROM offhours_summary_runs "
                           "WHERE period IS NOT NULL ORDER BY window_start DESC,created_at DESC LIMIT 1").fetchone()
        if not actor or not run:
            print(json.dumps({"summary": "no eligible fixture"}), flush=True)
            return
        samples = []
        for index in range(4):
            meter = ReadMeter(conn)
            started = time.perf_counter()
            view = offhours_summary.get_summary_view(
                meter, actor["id"], cm_id=run["responsible_authorization_id"],
                day=run["report_date"], period=run["period"],
            )
            read_ms = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            body = json.dumps(jsonable_encoder(view), ensure_ascii=False, separators=(",", ":")).encode()
            hashlib.sha256(body).hexdigest()
            encode_ms = (time.perf_counter() - started) * 1000
            if index:
                samples.append({"read_ms": round(read_ms, 3), "queries": meter.queries,
                                "serialize_etag_ms": round(encode_ms, 3), "response_bytes": len(body),
                                "top_query_locations": meter.locations.most_common(8)})
            conn.commit()  # Preserve the standard view audit; release all SHARE locks per sample.
    print(json.dumps({"summary_samples": samples, "read_median_ms": statistics.median(
        sample["read_ms"] for sample in samples)}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"diagnostic_error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
