# 03 — User flows

repo นี้เป็น API ไม่มีหน้าจอ; "user flow" คือ journey ของแต่ละ caller ผ่าน route จริงใน `server.py`

## Owner ค้นเอกสารผ่าน Solo Web /knowledge

```mermaid
flowchart LR
  O["Owner opens /knowledge in Solo Web"] --> S["BFF checks owner session, mints 120 s JWT knowledge:read"]
  S --> Q["GET /api/v1/knowledge?q=&root_area=&tag="]
  Q --> R{"status"}
  R -->|"200"| L["Shelves, signals, one page of documents"]
  L --> D["GET /api/v1/knowledge/document?path="]
  D --> N["Metadata, aliases, related, children, neighbours"]
  R -->|"400"| E1["invalid_limit / invalid_offset / invalid_field"]
  R -->|"401 / 403"| E2["unauthorized / forbidden"]
  R -->|"503"| E3["catalog_* or reads_disabled"]
```

## Agent อ่าน/เขียนผ่าน knowledge MCP

```mermaid
flowchart TD
  A["Agent calls knowledge.store.get"] --> M1["MCP mints 60 s token, scope read"]
  M1 --> G["GET /api/v1/store/document"]
  G --> V["Response has version N"]
  V --> P["knowledge.store.put with if_version N"]
  P --> W{"KNOWLEDGE_MCP_WRITE=1?"}
  W -->|"no"| X["Tool not registered"]
  W -->|"yes"| M2["Mint token, scope write"]
  M2 --> PUT["PUT /api/v1/store/document"]
  PUT --> C{"result"}
  C -->|"200 / 201"| OK["New version, changed true or false"]
  C -->|"409"| CF["version_conflict: read again"]
  C -->|"400 / 413"| BAD["invalid_path / document_too_large"]
```

## Owner ย้ายหรือสำรอง directory ด้วย store_sync

```mermaid
flowchart LR
  P["store_sync push --root DIR"] --> LS["list_all remote"]
  LS --> PR{"--prune?"}
  PR -->|"yes"| DEL["DELETE paths not in source, with if_version"]
  PR -->|"no"| UP
  DEL --> UP["PUT only files whose sha256 differs"]
  UP --> VF["store_sync verify: missing, different, extra = 0"]
  VF --> PL["store_sync pull --out empty dir: snapshot"]
```

Sources: `server.py` (`_overview`, `_detail`, `_store_route`), `contracts/knowledge.v1.md`, `knowledge_mcp.py` (`build_server`, `mint`, `TOKEN_LIFETIME`, `main`), `scripts/store_sync.py` (`push`, `verify`, `pull`), `PRODUCT.md`, `README.md`

## Open questions

- หน้า Solo Web /knowledge และ Knowledge Hub /private อยู่คนละ repo (life-dashboard-web, content-knowledgehub-web) จึงไม่ได้วาดหน้าจอที่นี่
