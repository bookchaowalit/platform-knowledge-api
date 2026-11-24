# Verify recipe (platform-knowledge-api)

The `verify` skill follows this file. Same checks as CI (`platform-ci-workflows/ci-python.yml`,
Python 3.12, coverage gate 88 % at 2 decimals).

## Checks

```bash
unset KNOWLEDGE_STORE_TEST_DATABASE_URL     # default: the embedded pgserver; see below
uv sync --frozen --all-extras --python 3.12 # pgserver installs only below 3.13
uvx ruff@0.17.1 check . && uvx ruff@0.17.1 format --check .
.venv/bin/mypy .
.venv/bin/python -m pytest -q --cov --cov-report=
.venv/bin/coverage report --precision=2 --fail-under=88   # CI's gate: 87.7% fails
```

Store tests use `KNOWLEDGE_STORE_TEST_DATABASE_URL`, else an embedded `pgserver`, else they skip:
say so in the report if they skipped. Set that URL only to a database you just created and can
throw away: the migration tests create roles and write to its `public` schema. MCP tests use a fake
transport; no network needed.

## End to end

Use a throwaway local database only. Do not start the server unless every hosted URL is unset
**and** env files are off (`--no-env-file`, or `UV_ENV_FILE` would load them back):

```bash
unset KNOWLEDGE_STORE_DATABASE_URL KNOWLEDGE_STORE_OWNER_DATABASE_URL KNOWLEDGE_RELAY_DATABASE_URL \
  KNOWLEDGE_EVENT_KEY KNOWLEDGE_API_HOST KNOWLEDGE_API_PORT PORT UV_ENV_FILE GATEWAY_ASSERTION_MODE
export KNOWLEDGE_API_ENV=development
# for store changes: a local Postgres you just started (pgserver or docker), then create the tables
# export KNOWLEDGE_STORE_DATABASE_URL=postgresql://localhost:5433/knowledge_scratch
# psql "$KNOWLEDGE_STORE_DATABASE_URL" -c 'CREATE ROLE knowledge_rt NOLOGIN'
# KNOWLEDGE_STORE_OWNER_DATABASE_URL="$KNOWLEDGE_STORE_DATABASE_URL" \
#   uv run --no-sync --no-env-file python scripts/store_migrate.py --runtime-role knowledge_rt
uv run --no-sync --no-env-file python server.py --host 127.0.0.1 --port 3430 \
  --catalog data/document-catalog.example.json & pid=$!
until curl -fsS 127.0.0.1:3430/health; do sleep 0.5; done
# ... exercise, then:
kill $pid
```

Then exercise what changed: for store changes, a write, a read, and a write with a stale
`if_version` (expect 409 and no new version).

Auth is a separate run: without JWT config the server is `auth=development` and an unauthenticated
read succeeds, so it proves nothing. Generate a throwaway RSA key and set `KNOWLEDGE_API_AUTH_ISSUER`
(a test name), `KNOWLEDGE_API_AUTH_PUBLIC_KEY` and `KNOWLEDGE_API_AUTH_SUBJECTS`; confirm
`curl 127.0.0.1:3430/ready` shows `"auth": "ready"`; then a request without a token must get 401 and
one with an RS256 token from that key (`aud` `knowledge-api`, `tenant_id` `owner`, `scope`
`knowledge:read` or `knowledge:write`, lifetime at most 120 s, `iat`/`nbf`/`exp`/`jti`) must pass.

## Never

- `scripts/deploy_catalog.py` without `--dry-run` (runs `railway up` to production).
- `scripts/store_sync.py push --prune`, `scripts/store_migrate.py` with the owner URL,
  `scripts/relay_outbox.py` with the relay URL.
- MCP with `KNOWLEDGE_MCP_WRITE=1` against the live `EMPIRE_KNOWLEDGE_API`.
