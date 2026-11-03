# Core 22 SDLC diagram pack — platform-knowledge-api

ชุดภาพ Core 22 ตาม SDLC Diagram Catalog v2 วาดจากโค้ดและ config ใน repo นี้ (ณ `origin/main` `797a53b`, 2026-11-03) ร่วมกับ ops record ที่ระบุชื่อ service นี้
repo นี้คือ Python service ตัวเดียวบน Railway (`solo-knowledge-api`) ทำสองงาน: catalog metadata search (อ่านอย่างเดียว, catalog ไม่อยู่ใน git) และ hosted document store (SPEC-002, Neon Postgres) พร้อม MCP server `knowledge-mcp` และ owner scripts

ทุกไฟล์มี `Sources:` ใต้ภาพ และ "Open questions" สำหรับสิ่งที่ยืนยันจากโค้ดไม่ได้

| # | Item | Status | File | Sources read |
|---|---|---|---|---|
| 01 | Business process As-Is / To-Be | done | [01-business-process.md](01-business-process.md) | `README.md`, `PRODUCT.md`, `scripts/*.py`, SPEC-002, store-mirror.yml, ops records |
| 02 | Use cases | done | [02-use-cases.md](02-use-cases.md) | `server.py`, `service_auth`, `knowledge_mcp.py`, scripts |
| 03 | User flows | done | [03-user-flows.md](03-user-flows.md) | `server.py`, `contracts/knowledge.v1.md`, `knowledge_mcp.py`, `store_sync.py` |
| 04 | Traceability (RTM) | done | [04-traceability.md](04-traceability.md) | `specs/001`, `specs/002`, code, `tests/`, `git log` |
| 05 | C4 Context | done | [05-c4-context.md](05-c4-context.md) | `PRODUCT.md`, `README.md`, code, ops records |
| 06 | C4 Container | done | [06-c4-container.md](06-c4-container.md) | `Dockerfile`, `railway.toml`, `.railway/railway.ts`, code |
| 07 | C4 Component | done | [07-c4-component.md](07-c4-component.md) | `server.py`, `document_store.py`, `service_auth`, `_sentry.py` |
| 08 | ADRs | done (5) | [08-adr/](08-adr/) | code, `Dockerfile`, specs, `git log`, ops records |
| 09 | ERD | done | [09-erd.md](09-erd.md) | `document_store.py` `SCHEMA_SQL`, `store_migrate.py`, catalog contract |
| 10 | Sequences | done (4) | [10-sequences.md](10-sequences.md) | `server.py`, `document_store.py`, `deploy_catalog.py`, `store_sync.py`, store-mirror.yml |
| 11 | State machines | done | [11-state-machines.md](11-state-machines.md) | `document_store.py`, `server.py`, `service_auth` |
| 12 | API & integrations | done (contract reused) | [12-api-integration.md](12-api-integration.md) → [`contracts/knowledge.v1.md`](../../contracts/knowledge.v1.md) | `server.py`, `knowledge_mcp.py`, `README.md`, `pyproject.toml` |
| 13 | Threat model | done | [13-threat-model.md](13-threat-model.md) | code, tests, store-mirror.yml |
| 14 | Backup & DR | done | [14-backup-dr.md](14-backup-dr.md) | SPEC-002, scripts, ops records |
| 15 | CI/CD pipeline | done | [15-cicd-pipeline.md](15-cicd-pipeline.md) | `ci.yml`, platform-ci-workflows `ci-python.yml` / `security.yml` @ `87dc5e1c`, `renovate.json` |
| 16 | Deployment | done | [16-deployment.md](16-deployment.md) | `Dockerfile`, `railway.toml`, `.railway/railway.ts`, `service.yaml`, ops records |
| 17 | Test coverage matrix | done | [17-test-coverage-matrix.md](17-test-coverage-matrix.md) | `tests/`, `ci.yml`, SPEC-002 §6 |
| 18 | Git & release workflow | done | [18-git-release-workflow.md](18-git-release-workflow.md) | `git log --first-parent`, `gh pr list` |
| 19 | Service dependency map | done | [19-service-dependency-map.md](19-service-dependency-map.md) | code, `Dockerfile`, ops records |
| 20 | Observability | done | [20-observability.md](20-observability.md) | `_sentry.py`, `server.py`, `Dockerfile`, `PRODUCT.md`, ops records |
| 21 | Incident response | done | [21-incident-response.md](21-incident-response.md) | `README.md`, `deploy_catalog.py`, SPEC-002 §7, ops records |
| 22 | DORA | done (3/5 computed from git/gh; recovery from ops record; production deploy count not computable) | [22-dora.md](22-dora.md) | `git log`, `gh run list` (31 runs) |

## ADR list

- [ADR-0001](08-adr/ADR-0001-stdlib-http-server-python.md) Python stdlib HTTP server, no framework
- [ADR-0002](08-adr/ADR-0002-railway-manual-deploy-gitignored-catalog.md) Railway serverless, manual deploy with git-ignored catalog
- [ADR-0003](08-adr/ADR-0003-owner-rs256-jwt-scope-split.md) Owner-only RS256 JWT, read/write scope split, extra keys
- [ADR-0004](08-adr/ADR-0004-postgres-bytea-versioned-store.md) Postgres bytea store with full version history
- [ADR-0005](08-adr/ADR-0005-sentry-errors-only-scrubbed.md) Sentry errors only, scrubbed

## Findings worth the owner's attention

- store-mirror ใน architecture-docs pin `store_sync` ที่ `ab053c9`; main คือ `797a53b` (แก้ path check ใน `document_store.py`) (ดู 10, 15)
- `contracts/knowledge.v1.md` ("GET and OPTIONS only"), `service.yaml` (`deploy.target: unassigned`, `writes: []`) และ README ("live deployment remain pending") ล้าสมัยหลัง SPEC-002 (ดู 12, 16)
- push เข้า main ไม่ deploy: fix ของ store วันที่ 2026-11-03 ถึง production เมื่อไรไม่มีบันทึกใน repo (ดู 18, 22)
- ไม่มี actor/subject ต่อ version ใน `store_versions` และไม่มี request log (ดู 13)
- backup nightly ของ target `knowledge` ยัง unverified; RTO/RPO ไม่ได้กำหนด (ดู 14)
- SPEC-002 phase 2/3 ขัดกับการตัดสินใจ 2026-11-03 ที่ให้ architecture-docs เป็น Git-first (ดู 01)

Validate: `node ~/work/core22/tools/validate.mjs docs/diagrams/*.md docs/diagrams/08-adr/*.md`
