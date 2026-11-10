# 06 — C4 Container

```mermaid
C4Container
  title platform-knowledge-api - containers
  System_Ext(callers, "Callers", "Solo Web BFF, knowledge MCP, Knowledge Hub, store-mirror, diary")
  Container_Boundary(railway, "Railway project platform-knowledge-api, service solo-knowledge-api") {
    Container(http, "HTTP server", "Python 3.12 ThreadingHTTPServer", "server.py KnowledgeRequestHandler, port PORT or 3430")
    Container(catalog, "Catalog snapshot", "JSON file in image", "/app/data/document-catalog.json, git-ignored, reload on mtime")
    Container(auth, "service_auth", "Vendored library", "RS256 verify, issuer, audience, subjects, scope")
  }
  ContainerDb(pg, "Neon platform-knowledge", "Postgres 16", "store_documents, store_versions")
  Container_Ext(mcp, "knowledge-mcp", "Python stdio MCP", "knowledge_mcp.py, mints 60 s JWT")
  Container_Ext(cli, "Owner scripts", "Python CLI", "deploy_catalog.py, store_sync.py, store_migrate.py, validate_catalog.py")
  System_Ext(sentry, "Sentry", "errors only")

  Rel(callers, http, "HTTPS JSON")
  Rel(mcp, http, "httpx, no redirects")
  Rel(cli, http, "store_sync and verify calls")
  Rel(cli, catalog, "stages and railway up --no-gitignore")
  Rel(cli, pg, "store_migrate DDL as owner role")
  Rel(http, auth, "check_read_auth")
  Rel(http, catalog, "CatalogStore snapshot")
  Rel(http, pg, "PostgresStore pool max 4, runtime role")
  Rel(http, sentry, "capture on 500")
```

Sources: `Dockerfile`, `.railway/railway.ts`, `.railway/railway.ts`, `server.py` (`main`, `KnowledgeRequestHandler`, `CatalogStore`, `store_from_env`), `document_store.py` (`PostgresStore.__init__`), `vendor/service-auth/service_auth/__init__.py`, `knowledge_mcp.py`, `scripts/*.py`, `_sentry.py`, `pyproject.toml`

## Open questions

- ไม่มี

