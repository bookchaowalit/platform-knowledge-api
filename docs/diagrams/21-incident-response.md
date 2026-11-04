# 21 — Incident response

ไม่มี runbook เฉพาะใน repo; flow นี้ประกอบจาก alert channel ที่มีจริงและขั้นตอน deploy/verify ใน README

```mermaid
flowchart TD
  A1["Uptime worker alert to Telegram"] --> TRI
  A2["Sentry error"] --> TRI
  A3["store-mirror job red on architecture-docs"] --> TRI
  A4["Agent or owner sees 503 from MCP"] --> TRI
  TRI{"Triage: GET /ready"}
  TRI -->|"source unavailable"| F1["Catalog: rebuild --federated, deploy_catalog.py"]
  TRI -->|"auth disabled"| F2["Check KNOWLEDGE_API_AUTH_* and EXTRA PEM blocks, restart"]
  TRI -->|"store unavailable"| F3["Check Neon compute and quota, runtime role"]
  TRI -->|"no response"| F4["Railway deployment state, e.g. REMOVED or NEEDS_APPROVAL; railway up"]
  F1 --> V["Verify: /ready 200 and live revision equals staged"]
  F2 --> V
  F3 --> V3["Verify store: authenticated knowledge.store.get or list returns 200; after restore, spot-check restored documents and versions"]
  V3 --> V
  F4 --> V
  V --> PM["Postmortem: record in ops memory and spec risk table"]
  F3 -->|"data loss"| RS["Restore from R2 db-backup per platform-db-backup runbook"]
  RS --> V
```

Sources: `README.md` (Deploy, Scoped authorization and readiness), `scripts/deploy_catalog.py`, `server.py` (`_ready`), `specs/002-document-store/spec.md` §7 risk 2, `PRODUCT.md`, ops records knowledge-api-hosted, prod-baseline-monitoring-backups, railway-deploy-approval-gate, neon-platform-quota (2026-11-03)

ตัวอย่างจริง: live deploy REMOVED 2026-10-29 17:12 UTC (สาเหตุไม่ทราบ) → redeploy 2026-11-02 (`8b92979`), verify ด้วย revision `a4af5b0e…` (ops record knowledge-api-hosted)

## Open questions

- ไม่มี postmortem template หรือ severity; root cause ของ 2026-10-29 ยังเปิดอยู่
- ใครได้ alert ถ้า Sentry ไม่ได้ตั้ง notification rule (ไม่อยู่ใน repo)
