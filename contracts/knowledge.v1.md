# Knowledge v1 contract

Schema identifier: `solo-empire.knowledge.v1`.
Source identifier: `solo-empire-document-catalog`.
Input projection: `solo-empire.document-catalog.v1`.

## Overview

`GET /api/v1/knowledge`

Optional query fields: `q` (120 characters), `root_area` (80),
`document_type` (80), `status` (100), `tag` (60), `limit` (default 60,
1–100), `offset` (default 0, 0–5,000). Filters combine with AND. Search is
case-insensitive across title, path, description, and tags, with title matches
ranked first. Without search, ordering is area, title, path. Unknown filters
yield an empty result; malformed limits/paths yield 400.

Response fields:

- `schema_version`, `generated_at` (UTC request time), `mode: "live"`, `source`.
- `catalog`: input `schema_version`, `generated_by`, `scope` (`included` text
  and `excluded` text array), `modified_at` (catalog file mtime, NOT document
  freshness), `excluded_records`.
- `summary`: integer `documents_total`, `documents_matched`, `with_frontmatter`,
  `linked_documents`, `root_areas`, `status_values`, `graph_edges`. All except
  `documents_matched` describe the complete curated catalog. `graph_edges`
  counts resolved outgoing parent/related references; it is not all body links.
- `shelves`: `{key, label, count, linked, top_status}[]` across all areas.
- `signals`: `document_types`, `statuses`, `top_tags`, each `{value, count}[]`.
  Tags show the top 18; type and status lists are complete.
- `filters`: normalized query fields including `limit` and `offset`.
- `documents`: the requested page of document summaries, at most `limit`.

## Document detail

`GET /api/v1/knowledge/document?path=<URL-encoded repository-relative path>`

Only known `.md` / `.mdx` catalog paths are accepted. Absolute paths and
traversal segments are rejected. No file is opened based on this parameter.

Response: common schema/time/mode/source fields, `document`, `neighbors`,
and `boundary` (metadata-only statement). Each neighbor has `relation`
(`parent`, `related`, or `child`) and a document summary. The same neighbor
may legitimately occur with different relations.

Document summary fields:

- Strings: `id` (stable hash of path), `path`, `title`, `document_type`,
  `status`, `root_area`.
- Nullable strings: `description`, `created`, `updated`, `parent`.
- `tags`: string array; `has_frontmatter`: boolean.
- Nonnegative integers: `internal_link_count`, `related_count`.

Detail additionally includes `aliases`, `related`, `children` (string arrays)
and `canonical` (always null; unreviewed external URLs are excluded). Aliases
are plain human labels; URI schemes and absolute paths are removed.
No response includes document bodies or arbitrary source fields.

## Errors and transport

Errors: `{ "error": { "code": "...", "message": "..." } }`.
400 invalid query, 401 missing/invalid bearer, 403 disallowed HTTP Origin,
404 unknown route/document, 405 mutation attempt, 503 missing/invalid catalog
or scoped authorization configuration, 500 safe generic failure. GET and OPTIONS only.
Responses use `Cache-Control: no-store`. OPTIONS has no body.

The frontend proxy exposes only the two Knowledge GET paths. It validates the owner session before
minting a short-lived, service-scoped backend JWT. Frontend network failures return 503 with a message.

## Authorization, readiness and snapshot revision

Production GET reads require owner-scoped RS256 JWT authorization using
`KNOWLEDGE_API_AUTH_ISSUER`, `KNOWLEDGE_API_AUTH_PUBLIC_KEY`, and
`KNOWLEDGE_API_AUTH_SUBJECTS`. Required audience is `knowledge-api`, scope is
`knowledge:read`, tenant is `owner`, and subject is an allowed OIDC owner.
Required `iat/nbf/exp/jti` constrain lifetime to 120 seconds (5 seconds leeway).
See [runtime configuration](../README.md#scoped-authorization-and-readiness).
Errors keep the v1 envelope: 401 invalid token, 403 owner/tenant/scope denial,
503 missing or invalid JWT configuration. Static tokens are local-development only.

`GET /health` remains public liveness. `GET /ready` returns safe
`status`, `source` (available/unavailable), and `auth`
(ready/development/disabled); 200 requires parsed source and configured JWT,
otherwise 503. No metadata rows appear in readiness responses.
`catalog.revision` is the SHA-256 source checksum, an additive v1
field. Source date and reload semantics remain unchanged.
