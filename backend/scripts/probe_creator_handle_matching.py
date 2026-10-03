"""At most five real reviews while matching is disabled; never apply business links."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import ai_dispatch, creator_data  # noqa: E402
from app import creator_handle_matching as matching  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connection  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, choices=range(1, 6), default=5)
    args = parser.parse_args()
    settings = get_settings()
    handles, version = matching.catalog()
    with connection() as conn:
        config = matching._config(conn)
        if not config or config["enabled"] or not config["api_key_ciphertext"]:
            raise SystemExit("Probe requires configured credentials and matching disabled.")
        jobs = conn.execute(
            """SELECT j.* FROM creator_handle_jobs j
            WHERE j.status='failed' AND j.reason_code='invalid_output' AND j.pack_ciphertext IS NOT NULL
              AND j.contact_id<>ALL(%s::uuid[])
              AND EXISTS(SELECT 1 FROM contact_roles r WHERE r.contact_id=j.contact_id
                AND r.role='creator' AND r.status='confirmed')
            ORDER BY EXISTS(SELECT 1 FROM contact_social_accounts l WHERE l.contact_id=j.contact_id
              AND l.relationship='owner' AND l.status='confirmed') DESC,
              (j.stats->>'messages')::int DESC,j.id LIMIT %s""",
            (list(matching.evidence.RESTRICTED_CONTACTS), args.limit),
        ).fetchall()
    successful, supported = 0, 0
    for job in jobs:
        pack = json.loads(creator_data.cipher(settings).decrypt(job["pack_ciphertext"]))
        with connection() as conn, conn.transaction():
            current_config = matching._config(conn, lock=True)
            if (
                not current_config
                or current_config["enabled"]
                or matching._config_hash(current_config) != matching._config_hash(config)
                or job["catalog_version"] != version
                or any(h["handle"] not in handles for h in pack["handles"])
                or not matching.evidence.still_valid(conn, job["refs"], settings)
                or not ai_dispatch.can_claim(conn)
            ):
                print(json.dumps({"job_id": str(job["id"]), "status": "preflight_blocked"}))
                continue
            reservation = uuid4()
            conn.execute(
                """INSERT INTO creator_handle_requests(id,job_id,quota_scope,reserved_tokens)
                VALUES(%s,%s,%s,%s)""",
                (
                    reservation,
                    job["id"],
                    matching.evidence_hash([config["provider"], config["project_id"], config["model"]]),
                    len(creator_data.encode(pack).encode()) + matching.MAX_OUTPUT + 512,
                ),
            )
        result, telemetry, error, diagnostic = matching.review(pack, config, job["contact_id"], settings)
        usage = telemetry.get("usage", {})
        with connection() as conn, conn.transaction():
            matching.record_response(conn, reservation, pack, telemetry, error, diagnostic, settings)
            if usage.get("input_tokens") is not None and usage.get("output_tokens") is not None:
                conn.execute(
                    "UPDATE creator_handle_requests SET billed_tokens=%s WHERE id=%s",
                    (usage["input_tokens"] + usage["output_tokens"], reservation),
                )
        if error:
            print(
                json.dumps(
                    {
                        "job_id": str(job["id"]),
                        "status": "failed",
                        "error_code": matching._output_error(error),
                        "usage": usage,
                    }
                )
            )
            break  # A probe failure must not repeat the previous full-queue failure.
        successful += 1
        own = [
            link for link in result["links"] if link["decision"] == "supported" and link["relation"] == "self_account"
        ]
        supported += len(own)
        print(
            json.dumps(
                {
                    "job_id": str(job["id"]),
                    "status": "validated",
                    "usage": usage,
                    "decisions": [link["decision"] for link in result["links"]],
                    "self_account_supported": len(own),
                    "validation_errors": [
                        link["validation_error"] for link in result["links"] if link.get("validation_error")
                    ],
                },
                ensure_ascii=False,
            )
        )
    print(
        json.dumps(
            {
                "sample_size": len(jobs),
                "validated": successful,
                "self_account_supported": supported,
                "business_links_written": 0,
                "matching_enabled": False,
            }
        )
    )
    return 0 if successful == args.limit else 1


if __name__ == "__main__":
    raise SystemExit(main())
