# Verify recipe (platform-knowledge-api)

The `verify` skill follows this file. Same checks as CI (`platform-ci-workflows/ci-python.yml`,
coverage gate 88 %).

## Checks

```bash
uv sync --frozen --all-extras
uvx ruff@0.17.1 check . && uvx ruff@0.17.1 format --check .
uv run mypy .
uv run python -m pytest -q --cov --cov-report=term --cov-fail-under=88
```

Store tests use `KNOWLEDGE_STORE_TEST_DATABASE_URL`, else an embedded `pgserver`, else they skip:
say so in the report if they skipped. MCP tests use a fake transport; no network needed.

## End to end

Use a throwaway local database only: unset any hosted URL first, so a store smoke test can never
write to the live store.

```bash
unset KNOWLEDGE_STORE_DATABASE_URL KNOWLEDGE_STORE_OWNER_DATABASE_URL KNOWLEDGE_RELAY_DATABASE_URL
# for store changes: a local Postgres you own, e.g. a pgserver or docker postgres you just started
# export KNOWLEDGE_STORE_DATABASE_URL=postgresql://localhost:5433/knowledge_scratch
uv run python server.py --catalog data/document-catalog.example.json   # 127.0.0.1:3430
curl -fsS localhost:3430/health
```

Then exercise what changed: for store changes, a write, a read, a write with a stale `if_version`
(expect 409 and no new version), and a request without a token (expect 401/403). Without JWT config
the server reports `auth=development`; never test auth rules in that mode alone.

## Never

- `scripts/deploy_catalog.py` without `--dry-run` (runs `railway up` to production).
- `scripts/store_sync.py push --prune`, `scripts/store_migrate.py` with the owner URL,
  `scripts/relay_outbox.py` with the relay URL.
- MCP with `KNOWLEDGE_MCP_WRITE=1` against the live `EMPIRE_KNOWLEDGE_API`.
