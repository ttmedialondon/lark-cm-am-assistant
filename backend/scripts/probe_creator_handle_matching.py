"""Review up to five explicit creators while matching is disabled; never apply business links."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import ai_dispatch, creator_imports  # noqa: E402
from app import creator_handle_matching as matching  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connection  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contact-id", type=UUID, action="append", required=True)
    args = parser.parse_args()
    contacts = list(dict.fromkeys(args.contact_id))
    if not 1 <= len(contacts) <= 5 or any(str(c) in matching.evidence.RESTRICTED_CONTACTS for c in contacts):
        raise SystemExit("Select one to five unrestricted confirmed creators.")
    settings = get_settings()
    handles, version = matching.reference_catalog()
    with connection() as conn:
        config = matching._config(conn)
        if not config or config["enabled"] or config["provider"] != "openai" or not config["api_key_ciphertext"]:
            raise SystemExit("Probe requires configured OpenAI credentials and matching disabled.")
        confirmed = conn.execute(
            "SELECT contact_id FROM contact_roles WHERE contact_id=ANY(%s::uuid[]) "
            "AND role='creator' AND status='confirmed'",
            (contacts,),
        ).fetchall()
        if {row["contact_id"] for row in confirmed} != set(contacts):
            raise SystemExit("All selected contacts must be confirmed creators.")
    successful, supported, eligible = 0, 0, 0
    for contact_id in contacts:
        with connection() as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            built = matching.evidence.build(conn, settings, contact_id, handles)
        pack, refs, stats = built["pack"], built["refs"], built["stats"]
        if not pack:
            print(json.dumps({"contact_id": str(contact_id), "status": built["reason"]}), flush=True)
            break
        with connection() as conn:
            # Keep the shared concurrency gate honest without creating a worker task.
            # A session lock is released even if the process or provider call fails.
            locked = conn.execute(
                "SELECT pg_try_advisory_lock(%s) AS locked", (ai_dispatch._ADMISSION_LOCK,)
            ).fetchone()
            conn.commit()
            if not locked["locked"]:
                print(json.dumps({"contact_id": str(contact_id), "status": "admission_busy"}), flush=True)
                break
            with conn.transaction():
                # Do not reverse the worker's config-before-admission lock order.
                current = matching._config(conn)
                current_handles, current_version = matching.reference_catalog()
                if (
                    not current
                    or current["enabled"]
                    or matching._config_hash(current) != matching._config_hash(config)
                    or current_version != version
                    or not matching._catalog_matches(pack, current_handles)
                    or not matching.evidence.still_valid(conn, refs, settings)
                    or not pack["creator_known"]
                    or not conn.execute(
                        "SELECT 1 FROM contact_roles WHERE contact_id=%s AND role='creator' AND status='confirmed'",
                        (contact_id,),
                    ).fetchone()
                    or not ai_dispatch.can_claim(conn)
                ):
                    print(json.dumps({"contact_id": str(contact_id), "status": "preflight_blocked"}), flush=True)
                    break
                reservation = uuid4()
                conn.execute(
                    "INSERT INTO creator_handle_requests(id,job_id) VALUES(%s,NULL)",
                    (reservation,),
                )
            result, telemetry, error, diagnostic = matching.review(pack, config, contact_id, settings)
            usage = telemetry.get("usage", {})
            diagnostic = diagnostic or {}
            diagnostic["probe"] = {
                "contact_id": str(contact_id),
                "prompt_version": matching.evidence.PROMPT_VERSION,
                "pack_fingerprint": built["fingerprint"],
                "catalog_version": version,
                "handles": pack["handles"],
                "evidence_refs": refs,
                "stats": stats,
            }
            with conn.transaction():
                matching.record_response(conn, reservation, pack, telemetry, error, diagnostic, settings)
                if usage.get("input_tokens") is not None and usage.get("output_tokens") is not None:
                    conn.execute(
                        "UPDATE creator_handle_requests SET billed_tokens=%s WHERE id=%s",
                        (usage["input_tokens"] + usage["output_tokens"], reservation),
                    )
            # Commit paid-call diagnostics before fallible eligibility checks.
            # A catalog/database failure must not erase the known usage.
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                current = matching._config(conn)
                current_handles, current_version = matching.reference_catalog()
                fresh = bool(
                    current
                    and not current["enabled"]
                    and matching._config_hash(current) == matching._config_hash(config)
                    and current_version == version
                    and matching._catalog_matches(pack, current_handles)
                    and matching.evidence.still_valid(conn, refs, settings)
                    and conn.execute(
                        "SELECT 1 FROM contact_roles WHERE contact_id=%s AND role='creator' AND status='confirmed'",
                        (contact_id,),
                    ).fetchone()
                )
                candidates = [
                    link for link in (result or {}).get("links", []) if matching.auto_link_eligible(pack, link, stats)
                ]
                by_handle = {h["id"]: h["handle"] for h in pack["handles"]}
                conflicts = creator_imports._conflicts(
                    conn,
                    [(None, contact_id, by_handle[link["h"]]) for link in candidates],
                )
                passed = [
                    link["h"] for link, conflict in zip(candidates, conflicts, strict=True) if fresh and not conflict
                ]
        if error:
            print(
                json.dumps(
                    {
                        "contact_id": str(contact_id),
                        "request_id": str(reservation),
                        "status": "failed",
                        "error_code": matching._output_error(error),
                        "usage": usage,
                    }
                ),
                flush=True,
            )
            break  # Never repeat a failed probe across the full queue.
        successful += 1
        own = [
            link for link in result["links"] if link["decision"] == "supported" and link["relation"] == "self_account"
        ]
        supported += len(own)
        eligible += len(passed)
        print(
            json.dumps(
                {
                    "contact_id": str(contact_id),
                    "request_id": str(reservation),
                    "status": "validated",
                    "usage": usage,
                    "handles": pack["handles"],
                    "links": result["links"],
                    "evidence_eligible_candidates": passed,
                    "evidence_fresh": fresh,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    print(
        json.dumps(
            {
                "sample_size": len(contacts),
                "validated": successful,
                "self_account_supported": supported,
                "evidence_eligible_candidates": eligible,
                "business_links_written": 0,
                "matching_enabled_by_probe": False,
            }
        ),
        flush=True,
    )
    return 0 if successful == len(contacts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
