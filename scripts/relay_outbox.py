#!/usr/bin/env python3
"""Publish unsent document-store events to Kafka (SPEC-003, ADR-0008).

    KNOWLEDGE_RELAY_DATABASE_URL=... KNOWLEDGE_KAFKA_*=... uv run python scripts/relay_outbox.py

Runs batches until the outbox is drained or a batch fails, then prints counts as JSON
(no URLs, no event bodies). Exit 0 when nothing failed, 1 when a row failed, 2 on setup errors.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from outbox import (
    KafkaPublisher,
    kafka_config_from_env,
    oldest_unsent_seconds,
    relay_once,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Relay the document-store outbox to Kafka"
    )
    parser.add_argument("--schema", default="public")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-batches", type=int, default=20)
    parser.add_argument("--run-seconds", type=float, default=300.0)
    args = parser.parse_args(argv)
    url = os.environ.get("KNOWLEDGE_RELAY_DATABASE_URL", "").strip()
    if not url:
        parser.error("set KNOWLEDGE_RELAY_DATABASE_URL (the relay role)")
    import psycopg

    try:
        publisher = KafkaPublisher(kafka_config_from_env())
        conn = psycopg.connect(url, connect_timeout=20, autocommit=True)
    except Exception as exc:  # noqa: BLE001 - only the error class leaves the process
        print(json.dumps({"status": "error", "error": type(exc).__name__}))
        return 2
    sent = failed = 0
    # Whole run well inside the workflow's 10-minute timeout (each batch ends within ~75 s).
    deadline = time.monotonic() + args.run_seconds
    try:
        for _ in range(args.max_batches):
            result = relay_once(conn, args.schema, publisher, args.batch_size)
            sent += result.sent
            failed += result.failed
            # Keep going while batches make progress: a short batch can still unblock later
            # rows of a key whose failed head just succeeded. Failed rows wait for backoff,
            # so a batch with no successes means nothing more is eligible now.
            if result.skipped or result.sent == 0 or time.monotonic() >= deadline:
                break
        backlog_age = oldest_unsent_seconds(conn, args.schema)
    finally:
        publisher.close()
        conn.close()
    print(
        json.dumps(
            {
                "status": "failed" if failed else "ok",
                "sent": sent,
                "failed": failed,
                "oldest_unsent_seconds": round(backlog_age),
            }
        )
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
