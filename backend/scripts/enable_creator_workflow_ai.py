"""Explicit owner-only rollout of the new candidate queue, using an existing encrypted feature credential."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import creator_workflow, creator_workflow_ai  # noqa: E402
from app.db import connection  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor-id", type=UUID, required=True)
    parser.add_argument("--responsible-id", type=UUID, required=True)
    parser.add_argument("--continuous", action="store_true")
    parser.add_argument("--chat-limit", type=int, default=25)
    args = parser.parse_args()
    with connection() as conn, conn.transaction():
        scope = creator_workflow._scope(conn, args.actor_id, args.responsible_id, lock=True)
        if not scope["is_owner"]:
            raise PermissionError("Only the current owner may enable this feature.")
        creator_workflow_ai._authorize(conn, args.actor_id, args.responsible_id)
        existing = conn.execute("SELECT 1 FROM ai_feature_configs WHERE feature='creator_workflow'").fetchone()
        if not existing:
            copied = conn.execute(
                """INSERT INTO ai_feature_configs(feature,provider,project_id,model,enabled,
                api_key_ciphertext,key_suffix,key_updated_at,key_updated_by_authorization_id,auto_confirm_threshold)
                SELECT 'creator_workflow',provider,project_id,model,TRUE,
                    api_key_ciphertext,key_suffix,NOW(),%s,auto_confirm_threshold
                FROM ai_feature_configs WHERE feature='contact_assignment' AND api_key_ciphertext IS NOT NULL
                RETURNING feature""",
                (args.actor_id,),
            ).fetchone()
            if not copied:
                raise ValueError("No reusable encrypted OpenAI project credential exists; configure the new feature.")
        else:
            conn.execute("UPDATE ai_feature_configs SET enabled=TRUE,updated_at=NOW() WHERE feature='creator_workflow'")
        creator_workflow._audit(
            conn,
            args.actor_id,
            "ai_feature_enabled",
            {"responsible_id": args.responsible_id, "continuous": args.continuous, "chat_limit": args.chat_limit},
        )
    run = creator_workflow_ai.queue_scan(
        args.actor_id, args.responsible_id, chat_limit=args.chat_limit, continuous=args.continuous
    )
    print(
        json.dumps(
            {"run_id": str(run["id"]), "status": run["status"], "total_chats": run["total_chats"]}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
