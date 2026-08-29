"""Apply ordered SQL migrations and record them transactionally."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MIGRATIONS = ROOT / "migrations"


def _connection():
    # The script may be executed directly, so resolve the project import only after sys.path is set.
    from app.db import connection

    return connection()


def main() -> int:
    files = sorted(MIGRATIONS.glob("*.sql"))
    if not files:
        print("No migrations found.")
        return 0

    with _connection() as conn, conn.transaction():
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        applied = {row["filename"] for row in conn.execute("SELECT filename FROM schema_migrations")}
        for path in files:
            if path.name in applied:
                continue
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
            print(f"Applied {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
