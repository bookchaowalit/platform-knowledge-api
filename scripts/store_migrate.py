#!/usr/bin/env python3
"""Create the document store tables with the owner role and grant the runtime role (SPEC-002).

    KNOWLEDGE_STORE_OWNER_DATABASE_URL=... uv run python scripts/store_migrate.py --runtime-role knowledge_runtime

Idempotent. The runtime role (the one in KNOWLEDGE_STORE_DATABASE_URL) gets only SELECT,
INSERT, UPDATE and DELETE on the two store tables, never DDL. Nothing secret is printed.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from document_store import PostgresStore

ROLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate the knowledge document store")
    parser.add_argument("--runtime-role", required=True)
    args = parser.parse_args(argv)
    url = os.environ.get("KNOWLEDGE_STORE_OWNER_DATABASE_URL", "").strip()
    if not url:
        parser.error("set KNOWLEDGE_STORE_OWNER_DATABASE_URL (the owner role)")
    if not ROLE_RE.fullmatch(args.runtime_role):
        parser.error("--runtime-role must be a plain lower-case identifier")
    store = PostgresStore(url, spaces=("migration",))
    try:
        store.migrate()
        with store.pool.connection() as conn:
            role = args.runtime_role
            conn.execute(f"GRANT USAGE ON SCHEMA {store.schema} TO {role}")
            conn.execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON"
                f" {store.schema}.store_documents, {store.schema}.store_versions TO {role}"
            )
    finally:
        store.close()
    print("document store schema is current; runtime grants applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
