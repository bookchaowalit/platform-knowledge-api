# 05 — C4 Context

```mermaid
C4Context
  title platform-knowledge-api - system context
  Person(owner, "Owner", "Single allowed owner subject")
  System(kapi, "platform-knowledge-api", "Catalog metadata search and hosted document store, Railway solo-knowledge-api")
  System_Ext(soloweb, "Solo Web", "life-dashboard-web /knowledge, BFF mints service JWT")
  System_Ext(hub, "Knowledge Hub", "content-knowledgehub-web /private behind Cloudflare Access")
  System_Ext(agent, "Claude Code knowledge MCP", "knowledge_mcp.py on owner machine")
  System_Ext(archdocs, "architecture-docs", "Catalog generator and store-mirror workflow")
  System_Ext(opscli, "platform-ops-cli", "Daily diary writes diary/entries")
  SystemDb_Ext(neon, "Neon platform-knowledge", "Postgres 16, Singapore")
  System_Ext(sentry, "Sentry", "Error reports when SENTRY_DSN set")
  System_Ext(uptime, "platform-uptime-worker", "Probes the service")
  System_Ext(backup, "ops-backups db-backup", "Nightly encrypted dump to R2")

  Rel(owner, soloweb, "Searches documents")
  Rel(owner, agent, "Asks agents")
  Rel(owner, hub, "Reads private spaces")
  Rel(soloweb, kapi, "GET /api/v1/knowledge*", "HTTPS, RS256 JWT read")
  Rel(agent, kapi, "Catalog and store calls", "HTTPS, RS256 JWT read or write")
  Rel(hub, kapi, "GET /api/v1/store/*", "HTTPS, RS256 JWT read")
  Rel(archdocs, kapi, "store_sync push --prune, verify", "HTTPS, JWT write")
  Rel(opscli, kapi, "PUT diary entries", "HTTPS, JWT write")
  Rel(kapi, neon, "Reads and writes documents", "psycopg pool")
  Rel(kapi, sentry, "Unhandled errors, scrubbed")
  Rel(uptime, kapi, "GET health")
  Rel(backup, neon, "pg dump as db_backup role")
```

Sources: `PRODUCT.md`, `README.md`, `server.py`, `document_store.py`, `knowledge_mcp.py`, `_sentry.py`, `scripts/store_sync.py`, architecture-docs `.github/workflows/store-mirror.yml`, ops record: knowledge-api-hosted (2026-11-03), ops record: hosted-document-store (2026-11-03), ops record: prod-baseline-monitoring-backups (2026-11-03)

ภาพรวมระบบกลางอยู่ที่ [architecture-docs C4-DIAGRAMS.md](https://github.com/bookchaowalit/architecture-docs/blob/main/docs/architecture/C4-DIAGRAMS.md) (ยังไม่มี node ของ knowledge-api โดยตรง)

## Open questions

- Solo Web ที่เรียก API: PRODUCT.md ระบุ `bookchaowalit/life-dashboard-web`, ops record ระบุ private key อยู่บน Vercel `owner-web` และ issuer `solo-web-one.vercel.app`: ยืนยันว่าเป็นตัวเดียวกัน
- uptime worker probe `/health` หรือ `/ready`: PRODUCT.md บอกแค่ target `knowledge-api`
