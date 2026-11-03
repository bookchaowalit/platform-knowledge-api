# 10 — Sequences

## A. Solo Web search (catalog)

```mermaid
sequenceDiagram
  participant B as "Solo Web BFF"
  participant H as "KnowledgeRequestHandler"
  participant A as "KnowledgeApi"
  participant P as "AuthPolicy read"
  participant C as "CatalogStore"
  B->>H: "GET /api/v1/knowledge?q=plan, Bearer JWT"
  H->>H: "Origin in allowlist?"
  H->>A: "handle(GET)"
  A->>P: "check_read_auth"
  P-->>A: "Identity or 401/403/503"
  A->>C: "snapshot()"
  C->>C: "reload if mtime changed, 20 MiB cap"
  C-->>A: "rows, by_path, meta"
  A->>A: "filter, _score, _shelves, page"
  A-->>H: "200 JSON"
  H-->>B: "no-store, nosniff"
```

## B. Store write (PUT)

```mermaid
sequenceDiagram
  participant M as "knowledge MCP or store_sync"
  participant H as "KnowledgeRequestHandler"
  participant A as "KnowledgeApi"
  participant S as "PostgresStore"
  participant D as "Neon Postgres"
  M->>H: "PUT /api/v1/store/document?space=&path=, write JWT"
  H->>A: "preauthorize with write policy"
  alt "not authorized"
    A-->>H: "401/403, connection closed, body never read"
  else "authorized"
    H->>H: "_read_body: Content-Length, max 16 MiB"
    H->>A: "handle(PUT, body)"
    A->>A: "_write_body: content or content_base64"
    A->>S: "put(space, path, bytes, if_version)"
    S->>D: "BEGIN, pg_advisory_xact_lock per parent and path"
    S->>D: "SELECT current, clash check"
    alt "if_version mismatch"
      S-->>A: "409 version_conflict"
    else "same bytes"
      S-->>A: "changed false"
    else "new content"
      S->>D: "UPSERT store_documents, INSERT store_versions, COMMIT"
      S-->>A: "meta, changed true"
    end
    A-->>M: "201 first version or 200"
  end
```

## C. Catalog deploy and verify

```mermaid
sequenceDiagram
  participant O as "Owner"
  participant DC as "deploy_catalog.py"
  participant AD as "architecture-docs generator"
  participant R as "Railway CLI"
  participant K as "solo-knowledge-api"
  O->>DC: "run with SOLO_ARCHITECTURE_DOCS_ROOT"
  DC->>AD: "document_catalog.py --check, system python"
  AD-->>DC: "fresh or abort"
  DC->>DC: "validate, stage data/document-catalog.json"
  DC->>R: "railway up --ci --no-gitignore --service solo-knowledge-api"
  loop "until 200 or timeout"
    DC->>K: "GET /ready"
  end
  DC->>K: "GET /api/v1/knowledge with minted JWT"
  K-->>DC: "catalog.revision"
  DC-->>O: "OK if revision equals staged sha256"
```

## D. store-mirror from architecture-docs

```mermaid
sequenceDiagram
  participant GH as "store-mirror.yml, self-hosted runner"
  participant SS as "store_sync.py at pinned SHA"
  participant K as "solo-knowledge-api"
  GH->>GH: "is GITHUB_SHA tip of main?"
  GH->>SS: "push --space architecture-docs --prune"
  SS->>K: "list_all"
  SS->>K: "DELETE removed paths with if_version"
  SS->>K: "PUT changed files"
  GH->>SS: "verify"
  SS->>K: "list_all, compare sha256"
  GH->>GH: "remove key files always"
```

Sources: `server.py` (`_dispatch`, `preauthorize`, `_read_body`, `handle`, `_overview`, `_store_route`), `document_store.py` (`PostgresStore.put`, `_lock_path`), `scripts/deploy_catalog.py` (`check_fresh`, `stage`, `railway_up`, `verify`), `scripts/store_sync.py` (`push`, `verify`), architecture-docs `.github/workflows/store-mirror.yml`

## Open questions

- store-mirror pin `ab053c9` แต่ main ล่าสุดคือ `797a53b`: ops record บอกให้ bump SHA เมื่อ store_sync เปลี่ยน (`797a53b` แก้ `document_store.py` ที่ store_sync import)
