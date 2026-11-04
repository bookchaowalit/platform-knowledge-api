"""SPEC-003: document-store events go through a transactional outbox to Kafka (ADR-0008)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import outbox
from document_store import PostgresStore, StoreError

SPACE = "knowledge-base"
KEY = b"test-event-key"


@pytest.fixture
def pg(database_url: str) -> Iterator[PostgresStore]:
    store = PostgresStore(
        database_url,
        spaces=(SPACE, "architecture-docs"),
        schema=f"t_{uuid.uuid4().hex[:12]}",
        event_key=KEY,
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


def test_private_events_carry_only_a_keyed_path_hash(pg: PostgresStore) -> None:
    pg.put(SPACE, "diary/2026-11-04.md", b"private text")
    pg.put("architecture-docs", "README.md", b"public", visibility="public")
    private, public = (r["envelope"] for r in rows(pg))
    assert set(private["data"]) == {
        "space",
        "version",
        "media_type",
        "visibility",
        "path_key",
    }
    assert private["data"]["path_key"] == outbox.path_key(
        SPACE, "diary/2026-11-04.md", KEY
    )
    # Not the plain hash: a guessed path cannot be confirmed without the service's key.
    plain = hashlib.sha256(f"{SPACE}\0diary/2026-11-04.md".encode()).hexdigest()
    assert plain not in json.dumps(private)
    assert "diary" not in json.dumps(private)
    assert public["data"]["path"] == "README.md"
    assert public["data"]["size"] == 6


def test_without_the_key_private_events_share_the_space_key() -> None:
    key, envelope = outbox.document_event(
        space=SPACE,
        path="diary/x.md",
        version=1,
        sha256="s",
        size=1,
        media_type="text/markdown",
        visibility="private",
        deleted=False,
    )
    assert key == SPACE
    assert "path_key" not in envelope["data"]


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
    key_a = f"{SPACE}:{outbox.path_key(SPACE, 'a.md', KEY)}"
    publisher = Recorder(fail_keys={key_a})
    result = relay(pg, publisher)
    assert (result.sent, result.failed) == (1, 1)
    assert [key for _, key, _ in publisher.sent] == [
        f"{SPACE}:{outbox.path_key(SPACE, 'b.md', KEY)}"
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


def test_least_privilege_roles_can_write_and_relay(database_url: str) -> None:
    """R6 with the migration's own grants: the runtime role writes, the relay role relays."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from store_migrate import apply_grants

    suffix = uuid.uuid4().hex[:8]
    runtime, relay_role = f"rt_{suffix}", f"rl_{suffix}"
    owner = PostgresStore(database_url, spaces=(SPACE,), schema=f"t_{suffix}")
    owner.migrate()
    try:
        with psycopg.connect(database_url, autocommit=True) as conn:
            for role in (runtime, relay_role):
                conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'pw'")
            apply_grants(conn, owner.schema, runtime, relay_role)
        params = conninfo_to_dict(database_url)
        as_runtime = make_conninfo("", **{**params, "user": runtime, "password": "pw"})
        as_relay = make_conninfo("", **{**params, "user": relay_role, "password": "pw"})
        writer = PostgresStore(
            as_runtime, spaces=(SPACE,), schema=owner.schema, event_key=KEY
        )
        try:
            writer.put(SPACE, "a.md", b"1")
            writer.delete(SPACE, "a.md")
        finally:
            writer.close()
        with psycopg.connect(as_relay, autocommit=True) as conn:
            result = outbox.relay_once(conn, owner.schema, Recorder())
        assert (result.sent, result.failed) == (2, 0)
        with (
            psycopg.connect(as_runtime) as conn,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            conn.execute(f"SELECT envelope FROM {owner.schema}.store_outbox")
    finally:
        owner.drop_schema()
        owner.close()
        with psycopg.connect(database_url, autocommit=True) as conn:
            for role in (runtime, relay_role):
                conn.execute(f"DROP ROLE IF EXISTS {role}")


def test_a_batch_stops_taking_rows_when_its_time_budget_is_spent(
    pg: PostgresStore,
) -> None:
    pg.put(SPACE, "a.md", b"1")
    with pg.pool.connection() as conn:
        conn.autocommit = True
        result = outbox.relay_once(conn, pg.schema, Recorder(), time_budget=0)
    assert (result.sent, result.failed) == (0, 0)
    assert (
        rows(pg)[0]["sent_at"] is None
    )  # left for the next batch, nothing rolled back


def test_writers_of_one_message_key_are_serialised_until_commit(
    pg: PostgresStore,
) -> None:
    with pg.pool.connection() as conn, conn.transaction():
        outbox.enqueue(
            conn,
            pg.schema,
            SPACE,
            outbox.document_event(
                space=SPACE,
                path="x.md",
                version=1,
                sha256="s",
                size=1,
                media_type="text/markdown",
                visibility="private",
                deleted=False,
            )[1],
        )
        held = conn.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
        ).fetchone()
    assert held is not None
    assert held[0] == 1


def test_the_script_keeps_draining_after_a_failed_head_succeeds(
    pg: PostgresStore,
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pg.put(SPACE, "a.md", b"1")
    pg.put(SPACE, "a.md", b"2")
    with pg.pool.connection() as conn:
        # The head failed once and its backoff has passed: only it is eligible in batch 1.
        conn.execute(
            f"UPDATE {pg.schema}.store_outbox SET attempts = 1,"
            " next_attempt_at = now() - interval '1 minute'"
            f" WHERE seq = (SELECT min(seq) FROM {pg.schema}.store_outbox)"
        )
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import relay_outbox

    recorder = Recorder()
    recorder.close = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(relay_outbox, "KafkaPublisher", lambda config: recorder)
    monkeypatch.setattr(relay_outbox, "kafka_config_from_env", dict)
    monkeypatch.setenv("KNOWLEDGE_RELAY_DATABASE_URL", database_url)
    assert relay_outbox.main(["--schema", pg.schema]) == 0
    assert json.loads(capsys.readouterr().out)["sent"] == 2
    assert [value["data"]["version"] for _, _, value in recorder.sent] == [1, 2]


def test_the_script_keeps_draining_past_a_batch_of_only_failures(
    pg: PostgresStore,
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in ("a.md", "b.md", "c.md"):
        pg.put(SPACE, name, b"1")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import relay_outbox

    failing = {f"{SPACE}:{outbox.path_key(SPACE, n, KEY)}" for n in ("a.md", "b.md")}
    recorder = Recorder(fail_keys=failing)
    recorder.close = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(relay_outbox, "KafkaPublisher", lambda config: recorder)
    monkeypatch.setattr(relay_outbox, "kafka_config_from_env", dict)
    monkeypatch.setenv("KNOWLEDGE_RELAY_DATABASE_URL", database_url)
    # Batch 1 = a, b (both fail); batch 2 must still send c.
    assert relay_outbox.main(["--schema", pg.schema, "--batch-size", "2"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert (out["sent"], out["failed"]) == (1, 2)
