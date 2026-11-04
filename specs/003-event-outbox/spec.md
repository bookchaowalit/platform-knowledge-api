# SPEC-003 — Document-store events through a transactional outbox (ADR-0008 M5)

Standard: ADR-0004 — TDD + SDD (architecture-docs, `docs/architecture/decisions/ADR-0004-tdd-sdd-standard.md`).

## Document control

| Field | Value |
|---|---|
| Spec ID / version | SPEC-003 / 1.0 |
| Status | approved scope (owner: "Start M5", 2026-11-04); implemented |
| Repository / path | bookchaowalit/platform-knowledge-api / `specs/003-event-outbox/spec.md` |
| Owner (maker) | Claude Code (messaging session) |
| Reviewer (checker) | Codex read-only review (Stop hook) |
| Approver | Owner (bookchaowalit) |
| Requirement links | architecture-docs ADR-0008 (messaging layer), rollout step M5 |
| Main-only | yes (ADR-0004 rule 2, v1.1) |
| Created | 2026-11-04 |

## 1. Problem and intent

- **Problem:** other parts of the system (the lake, future consumers) cannot learn that a
  document changed without polling the store.
- **Outcome:** every store write that adds a version also records one ADR-0008 envelope in
  `store_outbox` in the same transaction; a relay publishes it to Kafka `platform.events.v1`,
  and the hourly lake sink lands it in Bronze (`bronze/domain=platform/dataset=events`).

## 2. Requirements

| ID | Requirement |
|---|---|
| R1 | A `put` that adds a version writes `knowledge.document.updated`; a `delete` writes `knowledge.document.deleted`; an identical write and a rejected write (version or path conflict) write nothing |
| R2 | Event and version commit or roll back together (same transaction) |
| R3 | Events carry metadata only, never content. Public documents: space, path, version, sha256, size, media type. Private documents: space, version, media type, visibility and an HMAC-SHA256 path key (`KNOWLEDGE_EVENT_KEY`, held only by the service); no plain path hash, content hash or size, which could confirm a guessed path or short text |
| R4 | The relay publishes in insertion order per key, marks a row sent only after the broker ack, backs a failed row off (1 … 64 min) and holds later rows of that key |
| R5 | One relay at a time (transaction-scoped advisory lock in an explicit transaction) |
| R6 | Least privilege: the runtime role may only INSERT into `store_outbox` (plus SELECT of `id`, which `ON CONFLICT (id)` needs); the relay role may only SELECT and UPDATE it; Kafka user `knowledge-producer` may only write `platform.events.v1` |

## 3. Design

- `outbox.py`: envelope builder, `enqueue`, `relay_once`, `KafkaPublisher` (confluent-kafka),
  `kafka_config_from_env` (`KNOWLEDGE_KAFKA_*`). `document_store.PostgresStore` calls `enqueue`
  inside `put`/`delete`. The in-memory store has no outbox.
- Event id `knowledge.document.<verb>:<uuid4>` (nothing derived from the path); message key
  `<space>:<path key>` (public: `<space>:<sha256(path)>`; private without the key: `<space>`).
- `scripts/store_migrate.py --relay-role knowledge_relay` creates `store_outbox` and the grants.
- `scripts/relay_outbox.py` drains the outbox; `.github/workflows/relay-outbox.yml` runs it hourly
  at :55 through the jobs queue (platform-jobs-worker job `knowledge-relay`), before the lake sink
  at :05, and reports heartbeat `knowledge-relay`.

## 4. Test plan

`tests/test_outbox.py` against Postgres (embedded pgserver in CI, or
`KNOWLEDGE_STORE_TEST_DATABASE_URL`): R1–R5, the publisher/config units, and R6 with the
migration's own grants on throwaway roles (a test as superuser missed the `ON CONFLICT` grant
on 2026-11-04 and production writes failed for about 15 minutes).
