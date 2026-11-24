# Review guide (platform-knowledge-api)

The Codex Stop-hook review reads this file from the default branch, never from the change under
review. Owner-written; keep it under 16 KiB.

## What failure looks like here

This service holds the owner's private documents and the catalog every agent reads. Treat these as
`[P0]`/`[P1]`:

- **Private content leaking.** A document, path or body reachable without the right token, or
  written as `visibility: public` by a path that should only write private (the MCP is
  `Literal["private"]`, `knowledge_mcp.py`; threat model `docs/diagrams/13-threat-model.md`).
- **Lost updates.** A write or delete that ignores `if_version`, or a `version_conflict` (409) that
  still changes the stored version (`specs/002-document-store/spec.md`). Every version is kept.
- **Weak auth.** The static `KNOWLEDGE_API_TOKEN` accepted in production, partial JWT config that
  fails open, MCP writes on without `KNOWLEDGE_MCP_WRITE=1`, or the write scope checked after the
  body is read.
- **Unsafe store paths.** `check_path` letting through traversal, `.git/`, `.ssh/`, `.env*`,
  `*.pem`, `*.key`.
- **Outbox broken.** Event and version not committed in one transaction, a row marked sent before
  the broker ack, ordering per key lost, or two relays able to run at once (advisory lock)
  (`specs/003-event-outbox/spec.md` R2-R6).
- **Content in logs, Sentry or events.** Private events carry only the HMAC path key; logs and
  Sentry carry no body or path; the relay prints counts only.

## Severity

- `[P0]` private data exposed, auth bypass, data loss (a version overwritten or deleted).
- `[P1]` wrong behaviour an agent or the owner will hit (wrong 409, broken MCP tool, relay stall).
- `[P2]` edge case, or changed behaviour without a test in `tests/`.
- `[P3]` nit. **At most 3 nits.**

## Skip

- `uv.lock`, `*.egg-info/`, `__pycache__/`, `DOCPACK/pdf/`.
- `vendor/` is third-party-shaped but **not** skipped: `vendor/service-auth` verifies every JWT, so a
  change there is reviewed like `server.py` auth code.
- `data/document-catalog.json` must never be committed: flag it as `[P0]` if it appears in a diff.

## Evidence

Every finding gives `file:line` and input -> wrong result. Start the summary with the count.
