# 09 — ERD

ข้อมูลที่ persist มีสองแบบ: (1) ตาราง Postgres ของ document store (SPEC-002) และ (2) ไฟล์ catalog JSON (`solo-empire.document-catalog.v1`) ที่อ่านอย่างเดียว

## Document store (Postgres, `document_store.py` `SCHEMA_SQL`)

```mermaid
erDiagram
  STORE_DOCUMENTS ||--|{ STORE_VERSIONS : "current row is one of"
  STORE_DOCUMENTS {
    text space PK
    text path PK
    integer version
    text sha256
    integer size
    text media_type
    text visibility "private or public"
    bytea content
    timestamptz updated_at
  }
  STORE_VERSIONS {
    text space PK
    text path PK
    integer version PK
    text sha256
    integer size
    text media_type
    text visibility "private or public"
    bytea content "null for a delete"
    boolean deleted
    timestamptz created_at
  }
```

ไม่มี foreign key จริงในฐานข้อมูล: ความสัมพันธ์คือ `(space, path)` เดียวกัน และ `store_documents.version` = version ล่าสุดที่ไม่ใช่ delete; เมื่อ delete แถวใน `store_documents` ถูกลบและเพิ่มแถว `deleted=true` ใน `store_versions`

Roles (ops record + `scripts/store_migrate.py`): `knowledge_owner` (DDL), `knowledge_runtime` (SELECT/INSERT/UPDATE/DELETE บนสองตาราง), `db_backup` (SELECT)

## Catalog file (read-only JSON)

```mermaid
erDiagram
  CATALOG ||--|{ DOCUMENT : "documents"
  DOCUMENT ||--o| DOCUMENT : "parent"
  DOCUMENT }o--o{ DOCUMENT : "related"
  CATALOG {
    string schema_version "solo-empire.document-catalog.v1"
    string generated_by
    object scope "included, excluded"
  }
  DOCUMENT {
    string path "unique, md or mdx"
    string title
    string description
    string document_type
    string status
    string root_area
    string created
    string updated
    string parent
    string_list tags
    string_list aliases
    string_list related
    boolean has_frontmatter
    int internal_link_count
  }
```

Sources: `document_store.py` (`SCHEMA_SQL`, `PostgresStore.put`, `delete`), `scripts/store_migrate.py`, `server.py` (`CATALOG_SCHEMA_VERSION`, `CatalogStore._load`), `contracts/knowledge.v1.md` (Document summary fields), `scripts/validate_catalog.py`, `data/document-catalog.example.json`, `specs/002-document-store/spec.md` §4, ops record: hosted-document-store (2026-11-03)

## Open questions

- ไม่มี index เพิ่มนอกจาก primary key: list ด้วย `prefix` ใช้ `starts_with(path, ...)` บน PK; พอหรือไม่เมื่อ space โต
- ไม่มี retention ของ `store_versions`
