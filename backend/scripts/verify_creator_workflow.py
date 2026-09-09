"""Read-only production checks; never creates login sessions, candidates, or confirmations."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import creator_workflow, creator_workflow_routes, main  # noqa: E402
from app.db import connection  # noqa: E402

REMI = UUID("24d1ef13-3762-4359-ba11-d26c03cf1f7f")
ACTORS = {
    "Shuxuan Li": UUID("9d5f4370-6463-4c3a-a2ed-67753a1cabc0"),
    "Shang Ma": UUID("1225b4a0-fbd8-431e-962e-25e66943a8a0"),
    "Remi": REMI,
}


def main_check():
    for name, actor_id in ACTORS.items():
        with connection() as conn:
            auth = conn.execute("SELECT id,display_name,status FROM authorizations WHERE id=%s", (actor_id,)).fetchone()
        assert auth and auth["status"] == "active"
        app = FastAPI()
        app.include_router(
            creator_workflow_routes.create_router(
                current_authorization=lambda _, authorization=auth: authorization,
                render_template=main._template,
                csrf_token=lambda _: "verification-not-a-session-token",
            )
        )
        client = TestClient(app)
        page = client.get(f"/dashboard/creators?responsible_id={REMI}")
        assert page.status_code == 200, (name, page.status_code)
        assert '<option value="ai">AI</option>' in page.text
        assert '<option value="excel">Excel</option>' in page.text
        assert 'id="cw-evidence-dialog"' not in page.text
        data = client.get(f"/dashboard/creators/data?responsible_id={REMI}")
        assert data.status_code == 200, (name, data.status_code)
        scope = creator_workflow.get_scope(actor_id, REMI)
        assert scope["can_write"] is (actor_id == REMI)
        search = client.get(f"/dashboard/creators/users?responsible_id={REMI}&q=a")
        assert search.status_code == (200 if actor_id == REMI else 403)
        print(
            json.dumps(
                {
                    "actor": name,
                    "page_status": page.status_code,
                    "readonly": scope["readonly"],
                    "summary": data.json()["summary"],
                    "search_status": search.status_code,
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main_check()
