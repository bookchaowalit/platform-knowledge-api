# 11 — State machines

## Stored document (per space + path)

```mermaid
stateDiagram-v2
  [*] --> Absent
  Absent --> Current: "PUT, if_version 0 or none, version 1"
  Current --> Current: "PUT changed bytes, version N+1"
  Current --> Current: "PUT identical, no-op"
  Current --> Deleted: "DELETE, deleted version N+1"
  Deleted --> Current: "PUT again, next version"
  Current --> Current: "stale if_version, 409"
```

## Store readiness probe (`_store_status`)

```mermaid
stateDiagram-v2
  [*] --> disabled: "no KNOWLEDGE_STORE_DATABASE_URL"
  [*] --> checking: "store configured"
  checking --> available: "ping ok"
  checking --> unavailable: "ping fails"
  available --> available: "next /ready probe ok"
  available --> unavailable: "probe fails"
  unavailable --> available: "probe ok"
```

## Service readiness (`readiness`)

```mermaid
stateDiagram-v2
  [*] --> not_ready
  not_ready --> ready: "catalog parses and auth policy ready"
  ready --> not_ready: "catalog unreadable or invalid key"
```

Catalog document `status` เป็น string อิสระจาก catalog (default `unclassified`) ไม่ใช่ lifecycle ในโค้ด จึงไม่วาด

Sources: `document_store.py` (`put`, `delete`, `conflict`), `server.py` (`_store_status`, `_ready`), `vendor/service-auth/service_auth/__init__.py` (`readiness`), `tests/test_document_store.py::test_every_write_and_delete_keeps_history`

## Open questions

- `visibility` private → public ทำได้ผ่าน API ด้วย write token แต่ MCP ห้าม: ใครคือ "owner step" ที่ publish (ไม่มีเครื่องมือใน repo)
