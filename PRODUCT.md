# Product

<!-- impeccable:product-schema 1 -->

## Platform

api

## Users

- The Solo Empire owner, through the `/knowledge` module of Solo Web
  (`bookchaowalit/life-dashboard-web`), which calls this API through its BFF with a short-lived
  service JWT.
- The owner's agents, through this repository's read-only MCP server (`knowledge-mcp`, tools
  `knowledge.search` and `knowledge.document.get`).
- architecture-docs, which generates the catalog snapshot (`task knowledge-api:catalog:sync`); it
  writes the file, it does not call the API.

## Product Purpose

Search and browse the metadata of the owner's Markdown documents (title, description, type,
status, area, tags, parent, related and child documents) without opening the documents. Markdown
stays canonical; this service serves only a generated catalog of it.

Success: the `/knowledge` page finds a document by title, path, tag or description, shows its
place in the document graph, and never shows a private, secret or raw-data path.

## Positioning

A read-only, metadata-only projection of a curated catalog, owner-private, not public data. It
never reads document bodies, databases or nested repositories, and it never returns external
canonical URLs. One owner, one catalog; no multi-tenant access.

## Operating Context

- Production: `https://solo-knowledge-api-production.up.railway.app` (`/health`; Railway health
  check `/ready`), Railway project `platform-knowledge-api`, service `solo-knowledge-api`,
  Singapore, serverless (the first read after idle is slower).
- The real catalog (`data/document-catalog.json`) is private and not in git; it is deployed with
  `railway up --ci --no-gitignore --service solo-knowledge-api` from a checkout that holds it.
  `data/document-catalog.example.json` is the public example.
- Monitored by `platform-uptime-worker` (`knowledge-api`); errors go to Sentry when `SENTRY_DSN`
  is set, without personal data.
- Catalog size and freshness in production [ต้องยืนยัน].

## Capabilities and Constraints

- `GET /health`: public liveness. `GET /ready`: 200 only when the catalog loads and the JWT
  configuration is valid.
- `GET /api/v1/knowledge?q=&root_area=&document_type=&status=&tag=&limit=&offset=`: summary
  (totals, linked documents, graph edges), shelves per root area, type/status/tag signals and one
  page of documents. With `q`, results rank by title (exact, prefix, contains), then path, tag and
  description; without it they sort by area and title. `limit` 1–100 (default 60), `offset`
  0–5,000; `q` at most 120 characters.
- `GET /api/v1/knowledge/document?path=`: one document's metadata with aliases, related and child
  paths, and up to 40 neighbours. Paths are normalised; absolute, traversing or empty paths are
  400 `invalid_path`, unknown ones 404.
- Privacy boundary: records under `.git`, `.ssh`, `.aws`, `credentials`, `secrets`, `private`,
  `.env*`, raw/runtime/lake data, engagements, book-dev clones, decision records, diary entries,
  private finance folders and clients (except README files) are dropped, as is anything that is
  not `.md`/`.mdx`. Aliases that look like URIs or absolute paths are dropped; `canonical` is
  always null.
- Limits: catalog at most 20 MiB and 20,000 records; titles 240 characters, descriptions 360, 16
  tags, 8 aliases, 20 related and 20 child paths. An unsupported schema or duplicate paths make
  the API answer 503 instead of a partial list. The catalog reloads when its file changes.
- Reads need an RS256 JWT from Solo Web (`aud=knowledge-api`, `scope=knowledge:read`,
  `tenant_id=owner`, allowlisted `sub`, at most 120 seconds); otherwise 503 `reads_disabled`, 401
  or 403. Other methods are 405; unexpected errors are a generic 500 reported to Sentry.
- Out of scope: document bodies, full-text search, editing, and a full Markdown link graph.

## Measures

- Catalog load failures in production (503 `catalog_*`; target: 0).
- 5xx rate and uptime on `/health` (Sentry and `platform-uptime-worker`).
- Catalog age at read time (`catalog.modified_at`) [ต้องยืนยัน: no freshness target set].
