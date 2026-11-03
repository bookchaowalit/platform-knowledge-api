# 19 — Service dependency map

```mermaid
flowchart LR
  subgraph UP["Upstream callers"]
    WEB["Solo Web BFF"]
    MCP["knowledge MCP"]
    HUB["Knowledge Hub /private"]
    MIR["architecture-docs store-mirror"]
    DIA["platform-ops-cli diary"]
    UPT["platform-uptime-worker"]
  end
  K["solo-knowledge-api"]
  subgraph DOWN["Runtime dependencies"]
    NEON[("Neon platform-knowledge")]
    CATF[("Catalog JSON in image")]
    SEN["Sentry"]
  end
  subgraph BUILD["Build and deploy dependencies"]
    AD["architecture-docs generator"]
    RW["Railway CLI and builder"]
    UV["uv 0.13.1, PyPI, uv.lock"]
    SA["vendor/service-auth"]
  end
  BK["ops-backups db-backup"] --> NEON
  WEB --> K
  MCP --> K
  HUB --> K
  MIR --> K
  DIA --> K
  UPT --> K
  K -->|"hard for store routes, soft for /ready"| NEON
  K -->|"hard for catalog routes and /ready"| CATF
  K -->|"optional"| SEN
  AD --> CATF
  RW --> K
  UV --> K
  SA --> K
```

ไม่ผ่าน edge gateway: ops record api-gateway-edge (2026-11-03) ระบุว่า client ของ Knowledge sign ด้วย solo-web service key ซึ่ง gateway ไม่ trust จึงเรียกตรง

Sources: `server.py` (`store_from_env`, `_ready`, `CatalogStore`), `Dockerfile`, `pyproject.toml`, `scripts/deploy_catalog.py`, `PRODUCT.md`, architecture-docs `.github/workflows/store-mirror.yml`, ops records hosted-document-store, api-gateway-edge, prod-baseline-monitoring-backups (2026-11-03)

## Open questions

- Neon free compute quota เต็มต่อ project (ops record neon-platform-quota): DB นี้เป็น project ของตัวเอง แต่ไม่มี alert ใน repo ถ้า quota เต็ม
