# SPEC-002 — Hosted document store: bodies live in the service, not in Git

Standard: ADR-0004 — TDD + SDD (architecture-docs, `docs/architecture/decisions/ADR-0004-tdd-sdd-standard.md`).

## Document control

| Field | Value |
|---|---|
| Spec ID / version | SPEC-002 / 1.0 |
| Status | approved scope (owner: "Start phase 1", 2026-11-03); implementation in progress |
| Repository / path | bookchaowalit/platform-knowledge-api / `specs/002-document-store/spec.md` |
| Owner (maker) | Claude Code (knowledge consolidation session) |
| Reviewer (checker) | Codex read-only review (Stop hook) + fresh reviewer before cutover |
| Approver | Owner (bookchaowalit) |
| Requirement links | Owner decision 2026-11-03: repositories hold code and the registry only; content lives on hosted services. Phase 1 of 3 (knowledge-base); phase 2 Knowledge Hub, phase 3 architecture-docs |
| Main-only | yes: committed straight to `main`, red runs are local (ADR-0004 rule 2, v1.1) |
| Created | 2026-11-03 |

## 1. Problem and intent

- **Problem:** document bodies live in Git checkouts (knowledge-base 853 Markdown files and 355
  other files, about 24 MB) while the hosted API serves only their metadata. Every note has two
  homes, private diary entries exist on one laptop only (git-ignored, never backed up), and a
  duplicate copy drifted between knowledge-base and Knowledge Hub.
- **Outcome:** the knowledge API stores the documents themselves. Agents and tools read and write
  them over the authenticated API and the `knowledge` MCP; `task diary` writes there; the Git
  repository is archived once parity is proven.
- **Why now:** owner decision 2026-11-03 ("everything should live on the hosted hub").

## 2. Scope

| In scope (phase 1) | Out of scope (later phases or non-goals) |
|---|---|
| Postgres-backed store in this service: documents + full version history | A web editor (phase 2 decides; MCP and CLI first) |
| `GET/PUT/DELETE` store endpoints, list by prefix, optimistic concurrency | Knowledge Hub reading from the store (phase 2) |
| Separate `knowledge:write` scope; reads keep `knowledge:read` | architecture-docs content and its tooling (phase 3) |
| MCP tools to list, read and write store documents | Full-text search of bodies (catalog search stays metadata) |
| `scripts/store_sync.py`: push a directory, verify parity, pull a snapshot | Multi-tenant access; public anonymous reads |
| Import knowledge-base (tracked files + local diary entries) | Deleting the repository (owner approval after parity) |
| `task diary` writes to the store when configured | |
| Nightly encrypted backup via platform-db-backup | |

## 3. Behaviour — scenarios

### AC-01 — A document round-trips with its exact bytes
- **Given** an empty `knowledge-base` space
- **When** PUT `/api/v1/store/document?space=knowledge-base&path=notes/a.md` with UTF-8 text, then GET it
- **Then** the GET returns the same content, `sha256`, `media_type`, `visibility: private` and `version: 1`
- **Test:** `tests/test_document_store.py::test_a_document_round_trips_with_its_exact_bytes`

### AC-02 — Binary files are stored as base64 and returned unchanged
- **Given** a PNG body sent as `content_base64`
- **When** it is read back
- **Then** `content_base64` decodes to the same bytes and `content` is absent
- **Test:** `tests/test_document_store.py::test_binary_files_round_trip_as_base64`

### AC-03 — Every write keeps the previous version
- **Given** a document at version 1
- **When** it is written again and then deleted
- **Then** versions 1, 2 and 3 (the delete) are listed, version 1 still returns its original content, and a plain GET is 404
- **Test:** `tests/test_document_store.py::test_every_write_and_delete_keeps_history`

### AC-04 — Concurrent edits do not silently overwrite
- **Given** a document at version 2
- **When** a PUT says `if_version: 1`, or a create says `if_version: 0` for an existing path
- **Then** 409 `version_conflict` and the stored version is unchanged
- **Test:** `tests/test_document_store.py::test_a_stale_if_version_is_a_409`

### AC-05 — Writing the same bytes is a no-op
- **Given** a document at version 1
- **When** the identical content and metadata are PUT again
- **Then** the response is version 1 with `changed: false` and no history row is added
- **Test:** `tests/test_document_store.py::test_an_identical_write_does_not_add_a_version`

### AC-06 — Unsafe paths and unknown spaces are refused
- **Given** paths that are absolute, traversing, empty, over 500 characters, contain NUL or a backslash, or name `.git/`, `.env*`, `.ssh/`, `*.pem`, `*.key`; and a space not in `KNOWLEDGE_STORE_SPACES`
- **When** they are written or read
- **Then** 400 `invalid_path` / 404 `unknown_space`, and nothing is stored
- **Test:** `tests/test_document_store.py::test_unsafe_paths_and_unknown_spaces_are_refused`

### AC-07 — Size limits
- **Given** a body over 10 MiB, or a request over 16 MiB
- **When** it is PUT
- **Then** 413 `document_too_large` / `request_too_large` before anything is stored
- **Test:** `tests/test_document_store.py::test_oversized_documents_are_a_413`

### AC-08 — Listing by prefix pages through metadata only
- **Given** documents under `diary/` and `notes/`
- **When** GET `/api/v1/store/documents?space=knowledge-base&prefix=diary/`
- **Then** only `diary/` paths are listed, in path order, with sha256, size, media type, visibility, version and updated time, never content; deleted documents are left out
- **Test:** `tests/test_document_store.py::test_listing_by_prefix_returns_metadata_only`

### AC-09 — Writes need the write scope
- **Given** production auth
- **When** a PUT or DELETE carries a `knowledge:read` token, or a GET carries a `knowledge:write` token
- **Then** the write is 403 and the read is 403; each scope only does its own job
- **Test:** `tests/test_store_http.py::test_writes_need_the_write_scope_and_reads_the_read_scope`

### AC-10 — No store configured is a clean 503, not a crash
- **Given** `KNOWLEDGE_STORE_DATABASE_URL` is unset
- **When** a store route is called
- **Then** 503 `store_unavailable`; `/ready` still reports the catalog and shows `store: disabled`
- **Test:** `tests/test_store_http.py::test_without_a_database_the_store_is_a_503`

### AC-11 — The sync script pushes only changes and proves parity
- **Given** a directory with three files, one already stored with the same sha256
- **When** `store_sync.py push` then `store_sync.py verify` run
- **Then** two files are written, one skipped, and verify reports 0 missing, 0 different, 0 extra
- **Test:** `tests/test_store_sync.py::test_push_skips_unchanged_files_and_verify_proves_parity`

### AC-12 — Pull writes a snapshot that the catalog generator can read
- **Given** stored documents
- **When** `store_sync.py pull --out DIR` runs
- **Then** DIR holds each document at its path with the stored bytes, and refuses to write outside DIR
- **Test:** `tests/test_store_sync.py::test_pull_writes_each_document_inside_the_target`

### AC-13 — MCP tools list, read and write through the API
- **Given** the MCP server with a write-capable token source
- **When** `knowledge.store.list`, `knowledge.store.get`, `knowledge.store.put` are called
- **Then** each makes exactly one API call with its own scope, and identity arguments are refused
- **Test:** `tests/test_mcp.py::test_store_tools_call_the_api_with_their_own_scope`

## 4. Interfaces and data

| Interface | Change | Contract |
|---|---|---|
| `GET /api/v1/store/documents` | new | `space`, `prefix`, `limit` 1–1000, `offset` ≥ 0 → `{documents: [meta], total}` |
| `GET /api/v1/store/document` | new | `space`, `path`, optional `version` → meta + `content` (UTF-8 text) or `content_base64` |
| `GET /api/v1/store/history` | new | `space`, `path` → `{versions: [meta + deleted]}` |
| `PUT /api/v1/store/document` | new | query `space`, `path`; JSON `{content | content_base64, media_type?, visibility?, if_version?}` → meta + `changed` |
| `DELETE /api/v1/store/document` | new | `space`, `path`, optional `if_version` → meta of the delete version |
| MCP `knowledge.store.list/get/put/delete` | new | as above, one call each |

- **Data model / migration:** `store_documents(space, path, version, sha256, size, media_type, visibility, content bytea, updated_at)` primary key (space, path); `store_versions(space, path, version, sha256, size, media_type, visibility, content bytea null for a delete, deleted, created_at)` primary key (space, path, version). Created idempotently by `scripts/store_migrate.py` with the owner role (`CREATE TABLE IF NOT EXISTS`); the service never runs DDL, and the runtime role gets only `SELECT, INSERT, UPDATE, DELETE` on the two tables.
- **Storage choice:** bodies, including images and CSVs, sit in Postgres `bytea`. The whole space is about 24 MB, well inside the free Neon 0.5 GB, and one database keeps one backup and one restore path. R2 for large media is a later option.
- **Backward compatibility:** existing catalog routes, payloads and the read-only MCP tools are unchanged.

## 5. Non-functional requirements

| NFR | Target | How it is tested |
|---|---|---|
| Security / privacy | owner-only RS256 JWT; separate write scope; no body or path in logs or Sentry; secret-like paths refused | AC-06, AC-09; Sentry module unchanged (no free text) |
| Reliability | atomic write (row + history in one transaction); pool checks connections (Neon suspends idle compute) | AC-03/04 against real Postgres |
| Durability | nightly age-encrypted backup to R2 (platform-db-backup) + Neon history | backup run evidence |
| Performance | Railway Singapore next to Neon Singapore; list of 1,000 rows < 1 s warm | live smoke |

## 6. Test plan (TDD)

- **Levels:** store contract tests run against an in-memory store and the same tests against a real Postgres: `KNOWLEDGE_STORE_TEST_DATABASE_URL` if set, else an embedded Postgres 16 from the `pgserver` dev dependency; HTTP, sync and MCP tests use the in-memory store.
- **CI:** the shared workflow has no database service; the embedded Postgres runs inside the test job, so CI exercises both halves.
- **Coverage:** keep the CI gate at 88%.

### Red / green log

| Scenario | Red | Green | Note |
|---|---|---|---|
| AC-01–AC-08 | `tests/test_document_store.py` collection error: `ModuleNotFoundError: No module named 'document_store'` (local, 2026-11-03) | 48 passed, memory + Postgres 16 (local) | path lock key had a NUL byte (`text fields cannot contain NUL`); fixed |
| AC-09, AC-10 | `tests/test_store_http.py` collection error: `MAX_REQUEST_BYTES` / `store` not defined (local) | 18 passed (local) | readiness assertion corrected: dev mode without auth is not_ready by design |
| AC-11, AC-12 | written with `scripts/store_sync.py`; first run was the import error of the missing module | 3 passed, then CLI tests added | — |
| AC-13 | `tests/test_mcp.py`: 9 failed (unknown tools, `mint(scope=)`, `WRITE_SCOPE`) (local) | 29 passed (local) | — |
| all | — | 230 passed, coverage 93%, ruff + mypy clean (local, embedded Postgres) | CI run of the pushed tip |

## 7. Risks and open questions

| # | Risk or question | Impact | Owner | Resolution |
|---|---|---|---|---|
| 1 | Editing offline is no longer possible | notes wait for connectivity | owner | accepted in the 2026-11-03 decision; `pull` gives a read-only snapshot |
| 2 | The hosted service is down | documents are unreadable until it recovers | owner | backups prevent loss; uptime monitor alerts |
| 3 | Catalog generation reads files from disk | the catalog would lose knowledge-base after archive | Claude | generate from a `store_sync.py pull` snapshot |
| 4 | Archiving knowledge-base | irreversible-looking step | owner | only after verify shows exact parity and the owner says yes |

## 8. Change log

| Version | Date | Change | Approved by |
|---|---|---|---|
| 1.0 | 2026-11-03 | Phase 1 scope | owner (scope) |
| 1.1 | 2026-11-03 | Limits 10 MiB per document / 16 MiB per request: knowledge-base holds a 5.06 MiB CSV | AC-07 | owner (scope) |
