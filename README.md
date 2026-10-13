# platform-knowledge-api
Moved out of solo-empire `services/knowledge-api` on 2026-10-11 with its history (subtree split). Reads a generated document catalog JSON (`data/document-catalog.json`, configurable path via `KNOWLEDGE_API_CATALOG_PATH`). `vendor/service-auth` is a vendored copy of `bookchaowalit/service-auth`.

**Ownership.** The real catalog is private document metadata and is **not in git**: architecture-docs writes it into `data/document-catalog.json` of a local checkout (`task knowledge-api:catalog:sync`). To self-host, copy `data/document-catalog.example.json` to `data/document-catalog.json` (or point `KNOWLEDGE_API_CATALOG_PATH` at your own catalog). Check its shape with:

```bash
python3 scripts/validate_catalog.py             # schema_version, non-empty documents, unique paths
```

Test: `PYTHONPATH=vendor/service-auth python3 -m unittest discover -s tests -v`

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
python3 -m pip install ./vendor/service-auth .
KNOWLEDGE_API_CATALOG_PATH=/path/to/document-catalog.json python3 server.py
PYTHONPATH=vendor/service-auth python3 -m unittest discover -s tests -v
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

The image contains the declared catalog snapshot, service code and auth library;
no document bodies or root repository mount. Its source path is explicitly
`/app/data/document-catalog.json`. Local runs default to `data/document-catalog.json`;
`KNOWLEDGE_API_CATALOG_PATH` or `--catalog` overrides it. URL sources are not implemented.
See [service.yaml](service.yaml). Target assignment and live deployment remain pending.

## Scoped authorization and readiness

[Railway configuration](railway.toml) selects the Dockerfile and `/ready`
health check (30 seconds), with at most 3 restarts after failure. It declares
no region, resources, runtime secret values, or existing cloud deployment.

Configure `KNOWLEDGE_API_AUTH_ISSUER` with the exact HTTPS origin of Solo Web,
`KNOWLEDGE_API_AUTH_PUBLIC_KEY` with its PEM RSA public key (at least 2048 bits),
and `KNOWLEDGE_API_AUTH_SUBJECTS` with the comma-separated allowed OIDC owner
subjects. See [.env.example](.env.example); provide configuration at runtime.
The signing private key belongs only to Solo Web and is never copied to this image.

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

| Variable | Meaning |
|---|---|
| `EMPIRE_KNOWLEDGE_API` | API base URL (env, else `~/.config/empire-platform/client.env`); https, http only on loopback |
| `SOLO_WEB_PUBLIC_ORIGIN` | the JWT issuer: Solo Web's public https origin (not a secret) |
| `SOLO_WEB_SERVICE_KEY_FILE` | default `~/.config/solo-web/service-private.pem`, mode 600 |
| `SOLO_WEB_OWNER_SUBJECTS_FILE` | default `~/.config/solo-web/owner-subjects.txt`, mode 600; the first subject is used |
| `EMPIRE_TOKEN` | optional ready service JWT; used instead of minting (the key is then not read) |

Trade-off: the signing key can mint tokens for every Solo Web service, so only run this MCP as the owner on a machine
that already holds that key. A narrower alternative, if the key must not sit beside agents, is a Solo Web owner-session
endpoint that issues a per-service token; set `EMPIRE_TOKEN` from it and the key is never read.

Run: `uv venv .venv && uv pip install ./vendor/service-auth -e ".[dev]"`, then `.venv/bin/knowledge-mcp` (see `.mcp.json`).
Tests: `.venv/bin/pytest tests/test_mcp.py` (fake HTTP transport and a throwaway key; the minted token is checked with
the vendored `service_auth` verifier the API uses).
