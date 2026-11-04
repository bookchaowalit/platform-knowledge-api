"""SPEC-003: document-store events go through a transactional outbox to Kafka (ADR-0008)."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

import outbox
from document_store import PostgresStore, StoreError

SPACE = "knowledge-base"


@pytest.fixture
def pg(database_url: str) -> Iterator[PostgresStore]:
    store = PostgresStore(
        database_url,
        spaces=(SPACE, "architecture-docs"),
        schema=f"t_{uuid.uuid4().hex[:12]}",
    )
    store.migrate()
    try:
        yield store
    finally:
        store.drop_schema()
        store.close()


def rows(pg: PostgresStore) -> list[dict[str, Any]]:
    with pg.pool.connection() as conn:
        found = conn.execute(
            f"SELECT topic, message_key, envelope, attempts, sent_at, next_attempt_at, last_error"
            f" FROM {pg.schema}.store_outbox ORDER BY seq"
        ).fetchall()
    keys = (
        "topic",
        "message_key",
        "envelope",
        "attempts",
        "sent_at",
        "next_attempt_at",
        "last_error",
    )
    return [dict(zip(keys, row, strict=True)) for row in found]


class Recorder:
    def __init__(self, fail_keys: set[str] | None = None):
        self.sent: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_keys = fail_keys or set()

    def publish(self, topic: str, key: str, value: dict[str, Any]) -> None:
        if key in self.fail_keys:
            raise TimeoutError("broker did not acknowledge")
        self.sent.append((topic, key, value))


def relay(
    pg: PostgresStore, publisher: Recorder, batch_size: int = 100
) -> outbox.RelayResult:
    with pg.pool.connection() as conn:
        conn.autocommit = True
        return outbox.relay_once(conn, pg.schema, publisher, batch_size)


def test_each_changing_write_adds_one_event_and_identical_writes_add_none(
    pg: PostgresStore,
) -> None:
    pg.put(SPACE, "notes/a.md", b"one")
    pg.put(SPACE, "notes/a.md", b"one")  # identical: no version, no event
    pg.put(SPACE, "notes/a.md", b"two")
    pg.delete(SPACE, "notes/a.md")
    events = [r["envelope"] for r in rows(pg)]
    assert [e["type"] for e in events] == [
        "knowledge.document.updated",
        "knowledge.document.updated",
        "knowledge.document.deleted",
    ]
    assert [e["data"]["version"] for e in events] == [1, 2, 3]
    assert set(events[0]) == {
        "id",
        "type",
        "version",
        "occurred_at",
        "producer",
        "correlation_id",
        "causation_id",
        "data",
    }
    assert all(r["topic"] == "platform.events.v1" for r in rows(pg))


def test_private_paths_are_hashed_and_public_paths_are_sent(pg: PostgresStore) -> None:
    pg.put(SPACE, "diary/2026-11-04.md", b"private text")
    pg.put("architecture-docs", "README.md", b"public", visibility="public")
    private, public = (r["envelope"] for r in rows(pg))
    assert "path" not in private["data"]
    assert private["data"]["path_sha256"] == outbox.path_digest(
        SPACE, "diary/2026-11-04.md"
    )
    assert "private text" not in json.dumps(private)
    assert public["data"]["path"] == "README.md"


def test_a_rejected_write_leaves_no_event(pg: PostgresStore) -> None:
    pg.put(SPACE, "a.md", b"one")
    with pytest.raises(StoreError):
        pg.put(SPACE, "a.md", b"two", if_version=5)
    with pytest.raises(StoreError):
        pg.delete(SPACE, "a.md", if_version=9)
    assert len(rows(pg)) == 1


def test_relay_publishes_in_order_and_marks_rows_sent(pg: PostgresStore) -> None:
    for n in range(3):
        pg.put(SPACE, "a.md", f"v{n}".encode())
    publisher = Recorder()
    result = relay(pg, publisher)
    assert (result.sent, result.failed) == (3, 0)
    assert [value["data"]["version"] for _, _, value in publisher.sent] == [1, 2, 3]
    assert all(r["sent_at"] is not None for r in rows(pg))
    assert relay(pg, Recorder()).sent == 0  # nothing left


def test_a_failed_key_backs_off_and_holds_its_later_events_but_not_other_keys(
    pg: PostgresStore,
) -> None:
    pg.put(SPACE, "a.md", b"1")
    pg.put(SPACE, "b.md", b"1")
    pg.put(SPACE, "a.md", b"2")
    key_a = f"{SPACE}:{outbox.path_digest(SPACE, 'a.md')}"
    publisher = Recorder(fail_keys={key_a})
    result = relay(pg, publisher)
    assert (result.sent, result.failed) == (1, 1)
    assert [key for _, key, _ in publisher.sent] == [
        f"{SPACE}:{outbox.path_digest(SPACE, 'b.md')}"
    ]
    first_a = rows(pg)[0]
    assert first_a["attempts"] == 1
    assert first_a["last_error"] == "TimeoutError"
    assert first_a["next_attempt_at"] is not None
    # Still in backoff: neither a.md event is eligible on the next run.
    assert relay(pg, Recorder()).sent == 0


def test_a_second_relay_skips_while_the_first_holds_the_lock(pg: PostgresStore) -> None:
    pg.put(SPACE, "a.md", b"1")
    with pg.pool.connection() as holder, holder.transaction():
        holder.execute(outbox._statements(pg.schema)["lock"])
        assert relay(pg, Recorder()).skipped


def test_relay_rejects_an_unsafe_schema() -> None:
    with pytest.raises(ValueError, match="plain lower-case identifier"):
        outbox.relay_once(object(), "x; DROP TABLE y", Recorder())


def test_kafka_config_reads_knowledge_variables() -> None:
    config = outbox.kafka_config_from_env(
        {
            "KNOWLEDGE_KAFKA_BOOTSTRAP": "b:1",
            "KNOWLEDGE_KAFKA_USERNAME": "knowledge-producer",
            "KNOWLEDGE_KAFKA_PASSWORD": "p",
            "KNOWLEDGE_KAFKA_CA_PEM": "PEM",
        }
    )
    assert config["security.protocol"] == "SASL_SSL"
    assert config["sasl.username"] == "knowledge-producer"
    with open(config["ssl.ca.location"]) as handle:
        assert handle.read() == "PEM"
    os.unlink(config["ssl.ca.location"])


def test_kafka_publisher_waits_for_the_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    class Producer:
        err: Any = None

        def __init__(self, config: dict[str, Any]):
            self.config = config

        def produce(self, topic, key, value, on_delivery):
            self.callback = on_delivery

        def poll(self, timeout):
            self.callback(self.err, None)

        def flush(self, timeout):
            return 0

    import confluent_kafka

    monkeypatch.setattr(confluent_kafka, "Producer", Producer)
    publisher = outbox.KafkaPublisher({"bootstrap.servers": "b:1"}, timeout=1)
    producer: Any = publisher.producer
    assert producer.config["enable.idempotence"] is True
    producer.err = None
    publisher.publish("platform.events.v1", "k", {"id": "x"})

    class Err:
        def name(self) -> str:
            return "TOPIC_AUTHORIZATION_FAILED"

    producer.err = Err()
    with pytest.raises(RuntimeError, match="TOPIC_AUTHORIZATION_FAILED"):
        publisher.publish("platform.events.v1", "k", {"id": "y"})
    publisher.close()
