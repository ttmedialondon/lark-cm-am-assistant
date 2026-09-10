"""One-off owner-requested Remi reset, 2026-09-10. Dry-run rolls back everything.

Run with Web/Worker stopped. The owner explicitly waived a new backup. Original
Lark data, Excel inputs and historical audit records must remain unchanged.
The fixed count/source guards deliberately prevent reuse against another roster.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import creator_preparation, creator_workflow  # noqa: E402
from app.db import connection  # noqa: E402

CM = UUID("24d1ef13-3762-4359-ba11-d26c03cf1f7f")
OWNER = UUID("9d5f4370-6463-4c3a-a2ed-67753a1cabc0")
SOURCE_HASH = "f647d1e6be7bd445d9f6bdeb1b1924a019af18fefa9c53f3b206dda30138edf2"
PROTECTED = (
    "contacts",
    "lark_identities",
    "chats",
    "chat_messages",
    "chat_members",
    "authorizations",
    "authorization_chats",
    "authorization_message_observations",
    "admin_roles",
    "admin_page_permissions",
    "ai_feature_configs",
    "contact_identifiers",
    "creator_workflow_import_batches",
    "creator_workflow_cm_aliases",
)
OTHER_ROWS = {
    "creator_workflow_candidates": "responsible_authorization_id IS DISTINCT FROM %(cm)s",
    "creator_account_assignments": "responsible_authorization_id<>%(cm)s",
    "contact_assignments": "responsible_authorization_id<>%(cm)s",
    "creator_workflow_sources": "candidate_id NOT IN(SELECT id FROM reset_candidates)",
    "creator_workflow_requests": "candidate_id NOT IN(SELECT id FROM reset_candidates)",
    "contact_roles": "NOT(role='creator' AND contact_id IN(SELECT contact_id FROM reset_legacy))",
    "social_accounts": "id NOT IN(SELECT social_account_id FROM reset_accounts)",
    "contact_social_accounts": "NOT EXISTS(SELECT 1 FROM reset_formal x WHERE "
    "(x.contact_id,x.social_account_id,x.relationship)=(t.contact_id,t.social_account_id,t.relationship))",
    "creator_workflow_ai_runs": "responsible_authorization_id<>%(cm)s",
    "creator_workflow_ai_tasks": "run_id NOT IN(SELECT id FROM reset_ai_runs)",
    "creator_workflow_ai_chat_checks": "responsible_authorization_id<>%(cm)s",
    "offhours_summary_runs": "responsible_authorization_id<>%(cm)s",
    "offhours_summary_tasks": "run_id NOT IN(SELECT id FROM reset_summary_runs)",
    "offhours_summary_settings": "run_id NOT IN(SELECT id FROM reset_summary_runs)",
}


def digest(conn, table, where="TRUE"):
    # Hash individual rows first so encrypted message bodies are never collected
    # into one enormous string or printed. Names come only from constants above.
    return conn.execute(
        "SELECT count(*) AS n,md5(string_agg(h,'' ORDER BY h)) AS hash FROM "
        f"(SELECT md5(to_jsonb(t)::text) AS h FROM {table} t WHERE {where}) rows",
        {"cm": CM},
    ).fetchone()


def run(*, apply=False):
    with connection() as conn, conn.transaction(force_rollback=not apply):
        conn.execute("SET LOCAL lock_timeout='10s'")
        scope = creator_workflow._scope(conn, OWNER, CM)
        assert scope["is_owner"], "Current active owner required"
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"creator-scan:{CM}",))
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('offhours-summary',0))")
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('creator-workflow-write',0))")
        source = creator_preparation.load_snapshot(conn)
        assert source and source["responsible_id"] == str(CM) and source["source_sha256"] == SOURCE_HASH
        assert len(source["items"]) == 143, "Original Excel handoff must be retained"
        for temp, table in (
            ("reset_candidates", "creator_workflow_candidates"),
            ("reset_formal", "creator_account_assignments"),
            ("reset_legacy", "contact_assignments"),
            ("reset_ai_runs", "creator_workflow_ai_runs"),
            ("reset_summary_runs", "offhours_summary_runs"),
        ):
            conn.execute(
                f"CREATE TEMP TABLE {temp} ON COMMIT DROP AS SELECT * FROM {table} "
                "WHERE responsible_authorization_id=%s",
                (CM,),
            )
        conn.execute(
            "CREATE TEMP TABLE reset_accounts ON COMMIT DROP AS "
            "SELECT social_account_id FROM reset_formal UNION SELECT social_account_id "
            "FROM reset_candidates WHERE social_account_id IS NOT NULL"
        )
        for table, expected in (("reset_candidates", 143), ("reset_formal", 84), ("reset_legacy", 115)):
            assert conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == expected, table
        assert not conn.execute(
            "SELECT 1 FROM creator_workflow_candidates WHERE id NOT IN(SELECT id "
            "FROM reset_candidates) AND merged_into IN(SELECT id FROM reset_candidates)"
        ).fetchone()
        assert not conn.execute(
            "SELECT 1 FROM creator_account_assignments WHERE responsible_authorization_id<>%s "
            "AND candidate_id IN(SELECT id FROM reset_candidates)",
            (CM,),
        ).fetchone()
        assert not conn.execute(
            "SELECT 1 FROM reset_legacy WHERE contact_id NOT IN(SELECT contact_id "
            "FROM reset_candidates WHERE contact_id IS NOT NULL)"
        ).fetchone()
        assert not conn.execute("SELECT 1 FROM creator_workflow_import_batches").fetchone(), "Recheck cached imports"
        before = {table: digest(conn, table) for table in PROTECTED}
        others = {table: digest(conn, table, where) for table, where in OTHER_ROWS.items()}
        old_audits = digest(conn, "audit_events")
        deleted = {}

        def remove(table, where):
            deleted[table] = conn.execute(f"DELETE FROM {table} t WHERE {where}", {"cm": CM}).rowcount

        remove("creator_workflow_ai_tasks", "run_id IN(SELECT id FROM reset_ai_runs)")
        remove("creator_workflow_ai_runs", "id IN(SELECT id FROM reset_ai_runs)")
        remove("creator_workflow_ai_chat_checks", "responsible_authorization_id=%(cm)s")
        remove("offhours_summary_settings", "run_id IN(SELECT id FROM reset_summary_runs)")
        remove("offhours_summary_tasks", "run_id IN(SELECT id FROM reset_summary_runs)")
        remove("offhours_summary_runs", "id IN(SELECT id FROM reset_summary_runs)")
        remove("creator_workflow_requests", "candidate_id IN(SELECT id FROM reset_candidates)")
        remove("creator_workflow_sources", "candidate_id IN(SELECT id FROM reset_candidates)")
        remove("creator_account_assignments", "id IN(SELECT id FROM reset_formal)")
        remove("creator_workflow_candidates", "id IN(SELECT id FROM reset_candidates)")
        remove("contact_assignments", "id IN(SELECT id FROM reset_legacy)")
        remove(
            "contact_social_accounts",
            "EXISTS(SELECT 1 FROM reset_formal x WHERE "
            "(x.contact_id,x.social_account_id,x.relationship)=(t.contact_id,t.social_account_id,t.relationship)) "
            "AND source IN('creator_workflow_cm_confirmation','creator_workflow_excel_verified') "
            "AND NOT EXISTS(SELECT 1 FROM creator_account_assignments x WHERE x.social_account_id=t.social_account_id) "
            "AND NOT EXISTS(SELECT 1 FROM creator_workflow_candidates x WHERE x.social_account_id=t.social_account_id) "
            "AND NOT EXISTS(SELECT 1 FROM contact_assignments x WHERE x.contact_id=t.contact_id)",
        )
        remove(
            "social_accounts",
            "id IN(SELECT social_account_id FROM reset_accounts) "
            "AND NOT EXISTS(SELECT 1 FROM contact_social_accounts x WHERE x.social_account_id=t.id) "
            "AND NOT EXISTS(SELECT 1 FROM creator_account_assignments x WHERE x.social_account_id=t.id) "
            "AND NOT EXISTS(SELECT 1 FROM creator_workflow_candidates x WHERE x.social_account_id=t.id)",
        )
        remove(
            "contact_roles",
            "role='creator' AND contact_id IN(SELECT contact_id FROM reset_legacy) "
            "AND source IN('manual_excel_and_identity_confirmed','manual_excel_import','manual_p2p_confirmed') "
            "AND NOT EXISTS(SELECT 1 FROM contact_assignments x WHERE x.contact_id=t.contact_id) "
            "AND NOT EXISTS(SELECT 1 FROM creator_account_assignments x WHERE x.contact_id=t.contact_id) "
            "AND NOT EXISTS(SELECT 1 FROM contact_social_accounts x "
            "WHERE x.contact_id=t.contact_id AND status='confirmed')",
        )
        for table, expected in {
            "creator_workflow_candidates": 143,
            "creator_account_assignments": 84,
            "contact_assignments": 115,
            "contact_roles": 115,
            "contact_social_accounts": 84,
            "social_accounts": 84,
            "creator_workflow_sources": 144,
        }.items():
            assert deleted[table] == expected, f"Unexpected/shared target in {table}: {deleted[table]}"
        for table in ("creator_workflow_candidates", "creator_account_assignments", "contact_assignments",
                      "creator_workflow_ai_runs", "offhours_summary_runs"):
            assert not conn.execute(f"SELECT 1 FROM {table} WHERE responsible_authorization_id=%s", (CM,)).fetchone()
        assert before == {table: digest(conn, table) for table in PROTECTED}, "Original data changed"
        assert others == {table: digest(conn, table, where) for table, where in OTHER_ROWS.items()}, (
            "Other scope changed"
        )
        assert old_audits == digest(conn, "audit_events"), "Historical evidence changed"
        conn.execute(
            "INSERT INTO audit_events(id,authorization_id,action,metadata) VALUES(%s,%s,%s,%s::jsonb)",
            (
                uuid4(),
                OWNER,
                "creator_roster_reset",
                json.dumps(
                    {
                        "responsible_id": str(CM),
                        "deleted": deleted,
                        "new_backup_created": False,
                        "backup_waived_by_owner": True,
                        "original_data_unchanged": True,
                        "other_scope_unchanged": True,
                        "reason": "owner_requested_logic_and_ui_reset",
                    }
                ),
            ),
        )
        assert creator_preparation.load_snapshot(conn) is None, "Old preview must be invalidated"
        return {
            "mode": "applied" if apply else "dry_run_rolled_back",
            "deleted": deleted,
            "original_data_unchanged": True,
            "other_scope_unchanged": True,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    print(json.dumps(run(apply=parser.parse_args().apply), ensure_ascii=False))
