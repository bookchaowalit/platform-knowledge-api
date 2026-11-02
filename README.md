# platform-knowledge-api
Moved out of solo-empire `services/knowledge-api` on 2026-10-11 with its history (subtree split). Reads a generated document catalog JSON (`data/document-catalog.json`, configurable path via `KNOWLEDGE_API_CATALOG_PATH`). `vendor/service-auth` is a vendored copy of `bookchaowalit/platform-auth-sdk`.

**Ownership.** The real catalog is private document metadata and is **not in git**: architecture-docs generates it (`task docs:catalog` there) and [scripts/deploy_catalog.py](scripts/deploy_catalog.py) stages it as `data/document-catalog.json` of a local checkout and deploys it (see [Deploy](#deploy)). To self-host, copy `data/document-catalog.example.json` to `data/document-catalog.json` (or point `KNOWLEDGE_API_CATALOG_PATH` at your own catalog). Check its shape with:

```bash
python3 scripts/validate_catalog.py             # schema_version, non-empty documents, unique paths
```

Test: `uv sync --frozen --all-extras && uv run pytest -q` (lint, types and coverage: see [Run and verify](#run-and-verify))

---

# Solo Empire Knowledge API

Independently runnable, read-only metadata service for the Knowledge app.
Markdown remains canonical for humans and AI. This service reads only the
generated document catalog; it does not open document bodies, SQLite, raw lake
objects, or nested repositories. Metadata is owner-private, not public data.

## Run and verify

From this repository root. The catalog is produced outside this repository. This repo ships a snapshot at
`data/document-catalog.json` (default); replace it with a freshly generated file, or point
`KNOWLEDGE_API_CATALOG_PATH` (or `--catalog`) at any other path:

```bash
uv sync --frozen --all-extras          # dependencies from uv.lock (vendor/service-auth is a path source)
KNOWLEDGE_API_CATALOG_PATH=/path/to/document-catalog.json uv run python server.py
uvx ruff@0.17.1 check . && uvx ruff@0.17.1 format --check .
uv run mypy .                           # strict on server.py, _sentry.py, knowledge_mcp.py, scripts/
uv run pytest -q --cov                  # CI gates total coverage (see .github/workflows/ci.yml)
```

The listener defaults to `127.0.0.1:3430`. Configure `KNOWLEDGE_API_HOST`,
`KNOWLEDGE_API_PORT`, or `KNOWLEDGE_API_CATALOG_PATH` as needed. `--catalog`
accepts an isolated test catalog. No database migration or ingestion is needed.
Refresh the catalog explicitly after source changes; file mtime triggers reload.
Catalog replacement should be atomic. A request uses one consistent snapshot.

## Boundary and authentication

- `GET /health` is public liveness, not catalog readiness.
- Knowledge endpoints require scoped JWT authorization in production. Static
  `KNOWLEDGE_API_TOKEN` is supported only in local development.
- The default host is loopback. A non-loopback host requires valid scoped JWT
  configuration and forces production authorization. Metadata belongs to the
  single allowed owner; this pilot does not grant multi-tenant access.
- `KNOWLEDGE_API_CORS_ORIGINS` is an exact comma-separated origin allowlist.
  Browser use normally goes through the frontend's same-origin proxy.
- Responses are `no-store`. Request logging is disabled. Do not place tokens
  in URLs, source files, or tracked configuration.
- Arbitrary files, document bodies, private source records, and external
  canonical URLs are not returned. Parent/related references resolve only to
  paths in the curated catalog. This is not a full Markdown link graph.
- Catalog limits: 20 MiB, 20,000 records, page size 1–100, offset 0–5,000.
  Related and child paths are each capped at 20; detail neighbors at 40.

See the versioned [response contract](contracts/knowledge.v1.md) and its
machine-readable [JSON Schema](contracts/knowledge.v1.json).

This repository is the standalone home of the knowledge API (moved out of solo-empire 2026-10-11).

## Deploy

The repository root is the Docker build context (service + `vendor/service-auth` + the `data/` catalog snapshot).

```bash
docker build -t solo-knowledge-api .
railway up --ci --no-gitignore --service solo-knowledge-api   # uploads the git-ignored catalog; .railwayignore keeps the rest out
```

The service is not on CD (a push does not deploy) because the catalog is never in git.
Refresh and deploy it with one command, which stops at the first failed step. Run it with
`uv run` after `uv sync --frozen --extra mcp`: the live check reuses the MCP's URL policy and
JWT minting (httpx, mcp, PyJWT, cryptography), and a plain `uv sync` stops with that hint:

```bash
export SOLO_ARCHITECTURE_DOCS_ROOT=/path/to/architecture-docs
export EMPIRE_KNOWLEDGE_API=https://<api-host> SOLO_WEB_PUBLIC_ORIGIN=https://<solo-web-host>
export KNOWLEDGE_API_RAILWAY_PROJECT=<project-id>   # omit in a railway-linked checkout
uv run python scripts/deploy_catalog.py --dry-run   # freshness + validate + stage only
uv run python scripts/deploy_catalog.py             # ...then railway up and live verification
```

Steps: the generator check (`scripts/document_catalog.py --check` in architecture-docs) must pass;
the source is validated and staged atomically; `railway up --ci --no-gitignore` uploads it; the
script waits for `/ready` 200 and then requires the live `catalog.revision` (read with a JWT minted
like the MCP's, see [MCP server](#mcp-server-knowledge-mcp)) to equal the staged snapshot's SHA-256.
`--source FILE` deploys another catalog (no generator check); `--no-freshness-check` skips it.
The generator runs on python3 from PATH outside this repository's virtualenv, because its
output differs between Python versions; set `SOLO_ARCHITECTURE_DOCS_PYTHON` to pin another.
Environment and service default to `production` / `solo-knowledge-api`
(`KNOWLEDGE_API_RAILWAY_ENVIRONMENT`, `KNOWLEDGE_API_RAILWAY_SERVICE`). `--url` must pass the same https-only check as
`EMPIRE_KNOWLEDGE_API`, and requests never follow redirects, so the token stays on that host.
Nothing secret is printed.

The image contains the declared catalog snapshot, service code and auth library;
no document bodies or root repository mount. Its source path is explicitly
`/app/data/document-catalog.json`. Local runs default to `data/document-catalog.json`;
`KNOWLEDGE_API_CATALOG_PATH` or `--catalog` overrides it. URL sources are not implemented.
See [service.yaml](service.yaml). Target assignment and live deployment remain pending.

## Scoped authorization and readiness

[Railway configuration](railway.toml) selects the Dockerfile and `/ready`
health check (30 seconds), with at most 3 restarts after failure. It declares
no region, resources, runtime secret values, or existing cloud deployment.

Railway variable `SENTRY_DSN` turns on error reporting (unset = off, see [_sentry.py](_sentry.py)).

Configure `KNOWLEDGE_API_AUTH_ISSUER` with the exact HTTPS origin of Solo Web,
`KNOWLEDGE_API_AUTH_PUBLIC_KEY` with its PEM RSA public key (at least 2048 bits),
and `KNOWLEDGE_API_AUTH_SUBJECTS` with the comma-separated allowed OIDC owner
subjects. See [.env.example](.env.example); provide configuration at runtime.
The signing private key belongs only to Solo Web and is never copied to this image.
Optional `KNOWLEDGE_API_AUTH_EXTRA_PUBLIC_KEY` adds one more trusted PEM RSA key
(at least 2048 bits) for the local MCP below, so it never needs Solo Web's key.
Tokens signed by it pass exactly the same issuer, audience, subject, tenant,
scope and time checks. An invalid extra key fails closed (`/ready` 503).

Reads require an RS256 JWT: `iss` matches that origin, `aud=knowledge-api`,
`sub` is allowed, `tenant_id=owner`, `scope=knowledge:read`, and
`iat`, `nbf`, `exp`, `jti` are required. Lifetime is at most 120 seconds
with 5 seconds clock leeway. The verifier uses only the configured local key
and rejects remote key headers. Key rotation replaces the configured public
key and restarts the service; old signatures then fail. Missing configuration
returns `503 reads_disabled`; invalid tokens return `401 unauthorized`;
a valid token for another owner/tenant/scope returns `403 forbidden`.
Production never accepts `KNOWLEDGE_API_TOKEN`. Partial JWT configuration
fails closed even in development.

`GET /health` is public liveness. `GET /ready` returns 200 only when the
source snapshot parses and JWT configuration is valid; otherwise it returns
503 with safe source/auth states. A local fixture with auth disabled reports
`auth=development`, not deployment readiness. Metadata contains a SHA-256
`revision` of its source snapshot and preserves its original dates.
Atomic source replacement triggers reload. This does not assert provider
freshness or a live deployment.

The listener accepts PaaS `PORT`; explicit `KNOWLEDGE_API_PORT` or `--port`
takes precedence. Package installation is required; no sibling source-path
injection is used at runtime.

## MCP server (`knowledge-mcp`)

`knowledge_mcp.py` is this service's MCP server (stdio). It is read-only: `knowledge.search` (text, root area, type, status, tag; paged) and `knowledge.document.get` (one document's metadata and neighbours). Every tool is one HTTP call to the
deployed or local API; there is no file or database access, no tool takes an identity argument (actor, workspace,
tenant, token, ...), unknown arguments are rejected before any HTTP call, and errors are a fixed message from the HTTP
status (for example `403: forbidden`), never upstream text.

**Auth (backend auth is unchanged).** The API accepts only a 120-second RS256 service JWT from Solo Web
(`aud=knowledge-api`, `scope=knowledge:read`, `tenant_id=owner`, allowlisted owner `sub`). For local use the MCP mints that same
token itself, per call, as the Solo Web BFF does: it reads Solo Web's signing key and the owner subject from files
(never printed or returned), signs a 60-second token fixed to this service's audience and scope, and sends it as the
bearer. No tool argument can change the subject, audience or scope.

The key file is the MCP's own key, not Solo Web's: its public half is set as
`KNOWLEDGE_API_AUTH_EXTRA_PUBLIC_KEY` on the API, and the private half lives in
Infisical `/knowledge-mcp` and in `~/.config/solo-web/service-private.pem`.
Revoke it by unsetting that variable; Solo Web is not affected.

| Variable | Meaning |
|---|---|
| `EMPIRE_KNOWLEDGE_API` | API base URL (env, else `~/.config/empire-platform/client.env`); https, http only on loopback |
| `SOLO_WEB_PUBLIC_ORIGIN` | the JWT issuer: Solo Web's public https origin (not a secret) |
| `SOLO_WEB_SERVICE_KEY_FILE` | default `~/.config/solo-web/service-private.pem`, mode 600 |
| `SOLO_WEB_OWNER_SUBJECTS_FILE` | default `~/.config/solo-web/owner-subjects.txt`, mode 600; the first subject is used |
| `EMPIRE_TOKEN` | optional ready service JWT; used instead of minting (the key is then not read) |

Trade-off: the MCP key is trusted only by this API, but anyone holding it can read the owner's catalog metadata, so
keep it mode 600 on the owner's machine. A narrower alternative, if the key must not sit beside agents, is a Solo Web owner-session
endpoint that issues a per-service token; set `EMPIRE_TOKEN` from it and the key is never read.

Run: `uv sync --frozen --all-extras`, then `.venv/bin/knowledge-mcp` (see `.mcp.json`).
Tests: `.venv/bin/pytest tests/test_mcp.py` (fake HTTP transport and a throwaway key; the minted token is checked with
the vendored `service_auth` verifier the API uses).
