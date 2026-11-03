# 01 — Business process (As-Is / To-Be)

platform-knowledge-api เป็น service เดียวที่ทำสองงาน: (1) ให้ค้น **metadata** ของ document catalog (สร้างจาก architecture-docs แล้ว deploy แยก ไม่อยู่ใน git) และ (2) ตั้งแต่ 2026-11-03 เป็น **hosted document store** (SPEC-002) ที่เก็บตัวเอกสารพร้อม version history ใน Postgres

## As-Is (จากโค้ดและ ops record วันนี้)

```mermaid
flowchart TD
  subgraph CATALOG["Catalog lane: manual, not on CD"]
    A1["Owner edits Markdown in architecture-docs"] --> A2["store_sync pull: knowledge-base snapshot"]
    A2 --> A3["architecture-docs document_catalog.py --federated OUT.json"]
    A3 --> A4["deploy_catalog.py --dry-run: freshness check, validate, stage"]
    A4 --> A5["deploy_catalog.py: railway up --ci --no-gitignore"]
    A5 --> A6{"/ready 200 and live revision = staged sha256?"}
    A6 -->|"yes"| A7["Catalog live for Solo Web /knowledge and MCP knowledge.search"]
    A6 -->|"no"| A8["DeployError, owner investigates"]
  end
  subgraph STORE["Store lane: SPEC-002"]
    B1["Agent or owner via MCP knowledge.store.put"] --> B3["PUT /api/v1/store/document"]
    B2["platform-ops-cli diary writes diary/entries"] --> B3
    B4["architecture-docs store-mirror.yml on push to main"] --> B5["store_sync push --prune then verify"]
    B5 --> B3
    B3 --> B6[("Neon platform-knowledge: store_documents + store_versions")]
    B6 --> B7["Knowledge Hub /private reads spaces"]
    B6 --> B8["store_sync pull snapshot feeds catalog build"]
  end
  B8 -.-> A2
```

Sources: `README.md` (Deploy, Document store), `PRODUCT.md`, `scripts/deploy_catalog.py`, `scripts/store_sync.py`, `server.py`, `document_store.py`, `knowledge_mcp.py`, `specs/002-document-store/spec.md`, architecture-docs `.github/workflows/store-mirror.yml`, ops record: knowledge-api-hosted (2026-11-03), ops record: hosted-document-store (2026-11-03)

## To-Be (จาก specs/002-document-store/spec.md และ ops record เท่านั้น)

SPEC-002 วางไว้ 3 phase: phase 1 knowledge-base (ทำแล้ว 2026-11-03, repo archive แล้ว), phase 2 Knowledge Hub อ่านจาก store, phase 3 architecture-docs และ tooling ย้ายเข้า store

```mermaid
flowchart LR
  P1["Phase 1: knowledge-base in store, repo archived"] --> P2["Phase 2: Knowledge Hub reads from store"]
  P2 --> P3["Phase 3: architecture-docs content and tooling"]
  P3 --> GOAL["Repos hold code and registry only; content on hosted services"]
```

Sources: `specs/002-document-store/spec.md` §1, §2, ops record: hosted-document-store (2026-11-03)

หมายเหตุ: ops record ระบุว่า 2026-11-03 owner ตัดสินใจเพิ่มว่า architecture-docs ยังเป็น Git-first (one-way mirror) และ Knowledge Hub public pages ยังอยู่ใน Git ซึ่งขัดกับ phase 2/3 ใน SPEC-002

## Open questions

- SPEC-002 phase 2/3 ยังเป็นแผนอยู่หรือถูกแทนด้วยการตัดสินใจ 2026-11-03 ("architecture-docs stays Git-first")? spec ยังไม่ได้อัปเดต
- Catalog lane ยังเป็น manual ทั้งหมด: จะมี schedule หรือ CD สำหรับ catalog หรือไม่
