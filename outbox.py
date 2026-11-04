"""ADR-0008 outbox for the document store (SPEC-003).

Every store write that changes a document also writes one envelope to `store_outbox` in
the same transaction; `scripts/relay_outbox.py` later publishes unsent rows to Kafka
(`platform.events.v1`). Copied from architecture-docs/messaging (relay.py), as the
payment cell did. Events carry metadata only, never document content; a private
document's path is sent as a hash.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

TOPIC = "platform.events.v1"
PRODUCER = "knowledge-api"
PUBLISH_TIMEOUT_SECONDS = 15.0

OUTBOX_SQL = """
CREATE TABLE IF NOT EXISTS {schema}.store_outbox (
    seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id text NOT NULL UNIQUE,
    topic text NOT NULL,
    message_key text NOT NULL,
    envelope jsonb NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    next_attempt_at timestamptz,
    sent_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS store_outbox_unsent ON {schema}.store_outbox (seq) WHERE sent_at IS NULL;
"""

_SCHEMA_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def path_key(space: str, path: str, event_key: bytes | None) -> str | None:
    """Keyed hash of a path (HMAC-SHA256 with KNOWLEDGE_EVENT_KEY, held only by this service).

    A plain hash of a guessable path (``diary/<date>.md``) could be confirmed by anyone who
    reads the events, so private paths are never hashed without the key.
    """
    if not event_key:
        return None
    return hmac.new(event_key, f"{space}\0{path}".encode(), hashlib.sha256).hexdigest()


def document_event(
    *,
    space: str,
    path: str,
    version: int,
    sha256: str,
    size: int,
    media_type: str,
    visibility: str,
    deleted: bool,
    event_key: bytes | None = None,
    now: datetime | None = None,
) -> tuple[str, dict[str, Any]]:
    """The message key and envelope for one document version (metadata only).

    Public documents carry their path, content hash and size. Private documents carry only
    space, keyed path hash, version, media type and visibility: no content hash or size,
    which could confirm a guess at a short private text.
    """
    verb = "deleted" if deleted else "updated"
    # Random id: an event id derived from the path would leak it; the insert happens once,
    # in the transaction that creates the version.
    event_id = f"knowledge.document.{verb}:{uuid.uuid4()}"
    keyed = path_key(space, path, event_key)
    data: dict[str, Any] = {
        "space": space,
        "version": version,
        "media_type": media_type,
        "visibility": visibility,
    }
    if keyed:
        data["path_key"] = keyed
    if visibility == "public":
        data.update({"path": path, "sha256": sha256, "size": size})
        message_key = f"{space}:{hashlib.sha256(path.encode()).hexdigest()}"
    else:
        # Without the key, private events of a space share one key (order kept per space).
        message_key = f"{space}:{keyed}" if keyed else space
    envelope = {
        "id": event_id,
        "type": f"knowledge.document.{verb}",
        "version": 1,
        "occurred_at": (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "producer": PRODUCER,
        "correlation_id": event_id,
        "causation_id": None,
        "data": data,
    }
    return message_key, envelope


def enqueue(conn: Any, schema: str, message_key: str, envelope: dict[str, Any]) -> None:
    """Insert in the caller's transaction; a replayed write of the same version is a no-op."""
    conn.execute(
        f"INSERT INTO {schema}.store_outbox (id, topic, message_key, envelope)"
        " VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT (id) DO NOTHING",
        (
            envelope["id"],
            TOPIC,
            message_key,
            json.dumps(envelope, separators=(",", ":")),
        ),
    )


class Publisher(Protocol):
    def publish(self, topic: str, key: str, value: dict[str, Any]) -> None:
        """Return once the broker acknowledged the message; raise otherwise."""


@dataclass
class RelayResult:
    sent: int
    failed: int
    skipped: bool = False


def _statements(schema: str) -> dict[str, str]:
    table = f"{schema}.store_outbox"
    return {
        "lock": f"SELECT pg_try_advisory_xact_lock(hashtext('adr0008-knowledge-outbox-relay:{schema}'))",
        # Each publish may wait PUBLISH_TIMEOUT_SECONDS inside the transaction.
        "idle": "SELECT set_config('idle_in_transaction_session_timeout', '60s', true)",
        # A key whose oldest unsent row has failed sends only that row; a failed row
        # waits for its backoff, so rows that keep failing cannot fill every batch.
        "select": (
            f"SELECT o.id, o.topic, o.message_key, o.envelope FROM {table} o"
            " WHERE o.sent_at IS NULL AND (o.next_attempt_at IS NULL OR o.next_attempt_at <= now())"
            f" AND NOT EXISTS (SELECT 1 FROM {table} f WHERE f.topic = o.topic"
            " AND f.message_key = o.message_key AND f.sent_at IS NULL AND f.attempts > 0"
            " AND f.seq < o.seq) ORDER BY o.seq LIMIT %s"
        ),
        "sent": f"UPDATE {table} SET sent_at = now(), attempts = attempts + 1, last_error = NULL WHERE id = %s",
        # Backoff 1, 2, 4 … 64 minutes from the failure itself (clock_timestamp).
        "failed": (
            f"UPDATE {table} SET attempts = attempts + 1, last_error = %s,"
            " next_attempt_at = clock_timestamp() + interval '1 minute' * power(2, least(attempts, 6))"
            " WHERE id = %s"
        ),
    }


def relay_once(
    conn: Any, schema: str, publisher: Publisher, batch_size: int = 100
) -> RelayResult:
    """Publish one batch in one explicit transaction (holds the relay lock throughout)."""
    if not _SCHEMA_RE.fullmatch(schema):
        raise ValueError("schema must be a plain lower-case identifier")
    sql = _statements(schema)
    sent = failed = 0
    with conn.transaction():
        conn.execute(sql["idle"])
        locked = conn.execute(sql["lock"]).fetchone()
        if not locked or not locked[0]:
            return RelayResult(0, 0, skipped=True)
        rows = conn.execute(sql["select"], (batch_size,)).fetchall()
        blocked: set[tuple[str, str]] = set()
        for row_id, topic, key, envelope in rows:
            if (topic, key) in blocked:
                continue  # an earlier event for this key failed in this run; keep the order
            value = json.loads(envelope) if isinstance(envelope, str) else envelope
            try:
                publisher.publish(topic, key, value)
            except Exception as exc:  # noqa: BLE001 - recorded on the row (type only), retried after backoff
                conn.execute(sql["failed"], (type(exc).__name__, row_id))
                blocked.add((topic, key))
                failed += 1
                continue
            conn.execute(sql["sent"], (row_id,))
            sent += 1
    return RelayResult(sent, failed)


def oldest_unsent_seconds(conn: Any, schema: str) -> float:
    if not _SCHEMA_RE.fullmatch(schema):
        raise ValueError("schema must be a plain lower-case identifier")
    row = conn.execute(
        f"SELECT coalesce(extract(epoch FROM now() - min(created_at)), 0) FROM {schema}.store_outbox"
        " WHERE sent_at IS NULL"
    ).fetchone()
    return float(row[0]) if row else 0.0


class KafkaPublisher:
    """confluent-kafka producer (kafka-python-ng cannot authenticate to Kafka 4.x brokers)."""

    def __init__(
        self, config: dict[str, Any], timeout: float = PUBLISH_TIMEOUT_SECONDS
    ):
        from confluent_kafka import Producer

        self.timeout = timeout
        self.producer = Producer({**config, "acks": "all", "enable.idempotence": True})

    def publish(self, topic: str, key: str, value: dict[str, Any]) -> None:
        result: dict[str, Any] = {}
        self.producer.produce(
            topic,
            key=key.encode(),
            value=json.dumps(value, separators=(",", ":")).encode(),
            on_delivery=lambda err, _message: result.setdefault("err", err),
        )
        deadline = time.monotonic() + self.timeout
        while "err" not in result and time.monotonic() < deadline:
            self.producer.poll(0.2)
        if "err" not in result:
            raise TimeoutError("broker did not acknowledge")
        if result["err"] is not None:
            raise RuntimeError(result["err"].name())

    def close(self) -> None:
        self.producer.flush(5)


def kafka_config_from_env(environ: Mapping[str, str] = os.environ) -> dict[str, Any]:
    """SASL_SSL settings from KNOWLEDGE_KAFKA_* (Infisical /aiven: knowledge-producer)."""
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".pem") as ca:
        ca.write(environ["KNOWLEDGE_KAFKA_CA_PEM"])
    return {
        "bootstrap.servers": environ["KNOWLEDGE_KAFKA_BOOTSTRAP"],
        "security.protocol": "SASL_SSL",
        "sasl.mechanism": environ.get(
            "KNOWLEDGE_KAFKA_SASL_MECHANISM", "SCRAM-SHA-256"
        ),
        "sasl.username": environ["KNOWLEDGE_KAFKA_USERNAME"],
        "sasl.password": environ["KNOWLEDGE_KAFKA_PASSWORD"],
        "ssl.ca.location": ca.name,
    }
