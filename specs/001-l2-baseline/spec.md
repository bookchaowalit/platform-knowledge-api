# SPEC-001 — L2 baseline: locked dependencies, strict types, domain rules under test

Standard: ADR-0004 — TDD + SDD (architecture-docs, `docs/architecture/decisions/ADR-0004-tdd-sdd-standard.md`).
Big Loop Plan Wave 2 (solo-empire `repository-catalog/roadmaps/BIG-LOOP-PLAN-2026-10-21.md`).

## Document control

| Field | Value |
|---|---|
| Spec ID / version | SPEC-001 / 1.0 |
| Status | in review (implemented; owner approval pending) |
| Repository / path | bookchaowalit/platform-knowledge-api / `specs/001-l2-baseline/spec.md` |
| Owner (maker) | Claude Code (Wave 2 cells session) |
| Reviewer (checker) | Fresh Claude reviewer session (no maker context) |
| Approver | Owner (bookchaowalit): pending, see the Wave 2 report owner actions |
| Requirement links | Big Loop DoD L2: `PRODUCT.md`, ≥ 3 test files, Sentry, type check; L1 lockfile |
| Main-only | yes: committed straight to `main`, so the red run is local (ADR-0004 rule 2, v1.1) |
| Created / approved | 2026-10-22 / pending owner |

## 1. Problem and intent

- **Problem:** The read-only knowledge API serves owner-private document catalog metadata to Solo Web /knowledge; its privacy boundary, query validation and catalog graph/search rules were only partly covered by one unittest file, with no lockfile, type check or coverage gate.
- **Outcome:** the image installs exactly `uv.lock` (`uv sync --frozen`), CI runs Ruff, mypy
  (strict on `src/`) and a coverage gate on shared CI v1.1.1, and the cell's own rules have
  tests of their own beyond the generic contract suite.
- **Why now:** Wave 2 of the Big Loop Plan; milestone M1 is 2026-11-15.

## 2. Scope

| In scope | Out of scope (non-goals) |
|---|---|
| Lock file and frozen install, CI caller bump, Ruff/mypy config and fixes, rule tests, `PRODUCT.md` | New endpoints or fields; schema changes |

## 3. Behaviour — scenarios

### AC-01 — Secret, raw, private and non-Markdown paths are never served

- **Given** catalog paths under .git, .ssh, secrets, .env, data/raw, engagements, diary entries, private finance, clients, or a non-.md file
- **When** safe_path checks them
- **Then** each is rejected
- **Test:** `tests/test_privacy_boundary.py::test_secret_raw_private_and_non_markdown_paths_are_never_served` · level: unit / API with local fakes

### AC-02 — Unsafe records are dropped and counted

- **Given** a catalog with one safe record and four unsafe or malformed ones
- **When** GET /api/v1/knowledge
- **Then** only the safe record is listed and excluded_records is 4
- **Test:** `tests/test_privacy_boundary.py::test_unsafe_records_are_dropped_and_counted_as_excluded` · level: unit / API with local fakes

### AC-03 — Canonical URLs and URI aliases never leave the service

- **Given** a record with a canonical URL and URI/absolute aliases
- **When** its detail is read
- **Then** canonical is null and only the plain alias remains
- **Test:** `tests/test_privacy_boundary.py::test_canonical_urls_and_unsafe_aliases_never_leave_the_service` · level: unit / API with local fakes

### AC-04 — Paging bounds

- **Given** limit outside 1-100 or offset outside 0-5000 or non-integers
- **When** parsed
- **Then** 400 invalid_limit / invalid_offset
- **Test:** `tests/test_query_validation.py::test_a_limit_outside_1_to_100_or_not_an_integer_is_invalid_limit` · level: unit / API with local fakes

### AC-05 — Traversing or absolute paths are invalid

- **Given** an absolute, traversing, empty, NUL or oversized path
- **When** normalize_path runs
- **Then** 400 invalid_path
- **Test:** `tests/test_query_validation.py::test_absolute_traversing_empty_or_oversized_paths_are_invalid` · level: unit / API with local fakes

### AC-06 — Bad query parameters have stable codes

- **Given** the API
- **When** a request has an out-of-range or oversized parameter or no path
- **Then** 400 with invalid_limit, invalid_offset, invalid_field, missing_path or invalid_path
- **Test:** `tests/test_query_validation.py::test_bad_query_parameters_are_a_400_with_a_stable_code` · level: unit / API with local fakes

### AC-07 — References resolve only inside the catalog

- **Given** relative, suffix-less, anchored, external and unknown references
- **When** resolve_reference runs
- **Then** known catalog paths are returned, everything else is None
- **Test:** `tests/test_catalog_graph_and_search.py::test_references_resolve_only_to_paths_in_the_catalog` · level: unit / API with local fakes

### AC-08 — Document graph

- **Given** an index with two children, one relating to the other
- **When** details and overview are read
- **Then** parent, deduplicated related, children, neighbours and edge counts match
- **Test:** `tests/test_catalog_graph_and_search.py::test_parent_related_and_children_form_the_document_graph` · level: unit / API with local fakes

### AC-09 — Search ranking

- **Given** documents matching a query by exact title, prefix, contains, path, tag and description
- **When** GET /api/v1/knowledge?q=plan
- **Then** they are returned in that order and non-matches are left out
- **Test:** `tests/test_catalog_graph_and_search.py::test_search_ranks_title_matches_above_path_tag_and_description` · level: unit / API with local fakes

### AC-10 — Unsupported catalog fails closed

- **Given** a catalog with another schema_version or non-list documents, or duplicate paths
- **When** the catalog is read
- **Then** 503 catalog_schema_unsupported / catalog_schema_invalid
- **Test:** `tests/test_catalog_graph_and_search.py::test_an_unsupported_catalog_is_a_503_not_a_partial_list` · level: unit / API with local fakes

### AC-11 — Oversized catalog refused

- **Given** a catalog file over 20 MiB
- **When** the store loads
- **Then** catalog_too_large before reading
- **Test:** `tests/test_catalog_graph_and_search.py::test_a_catalog_over_20_mib_is_refused_before_it_is_read` · level: unit / API with local fakes

## 4. Interfaces and data

| Interface | Change | Contract |
|---|---|---|
| G | E | T |
| G | E | T |
| G | E | T |
| G | E | T |
| M | C | P |

- **Data model / migration:** N/A — no schema change.
- **Backward compatibility:** same routes and payloads; only tooling, locks, typing fixes and tests changed unless a scenario above says otherwise.

## 5. Non-functional requirements

| NFR | Target | How it is tested |
|---|---|---|
| Performance | N/A — no hot-path change beyond one comparison per declared range | — |
| Security / privacy | Authorization unchanged; Sentry sends no PII and scrubs tokens/URLs | existing auth tests, Sentry tests |
| Reliability / observability | Reproducible install from the lock file; opt-in Sentry error reporting | image rehearsal before push; Sentry tests |
| Accessibility / UX | N/A — API only | — |

## 6. Test plan (TDD)

- **Test levels:** unit and API tests with local fakes; no hosted service is called.
- **Test data:** generated in each test; no production data.
- **Coverage:** CI gate `coverage-threshold: "88"` (measured 90.78% on 2026-10-22, minus 2 points).
- **Not test-first (exceptions):** tests that pin behaviour the service already had
  (characterization tests, marked below) were written after the code by nature; tooling and
  config changes are covered by CI itself.

### Red / green log

| Scenario | Test | Red: commit / run + failure message | Green: commit / run | Refactor note |
|---|---|---|---|---|
| AC-01 | `tests/test_privacy_boundary.py::test_secret_raw_private_and_non_markdown_paths_are_never_served` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-02 | `tests/test_privacy_boundary.py::test_unsafe_records_are_dropped_and_counted_as_excluded` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-03 | `tests/test_privacy_boundary.py::test_canonical_urls_and_unsafe_aliases_never_leave_the_service` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-04 | `tests/test_query_validation.py::test_a_limit_outside_1_to_100_or_not_an_integer_is_invalid_limit` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-05 | `tests/test_query_validation.py::test_absolute_traversing_empty_or_oversized_paths_are_invalid` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-06 | `tests/test_query_validation.py::test_bad_query_parameters_are_a_400_with_a_stable_code` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-07 | `tests/test_catalog_graph_and_search.py::test_references_resolve_only_to_paths_in_the_catalog` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-08 | `tests/test_catalog_graph_and_search.py::test_parent_related_and_children_form_the_document_graph` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-09 | `tests/test_catalog_graph_and_search.py::test_search_ranks_title_matches_above_path_tag_and_description` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-10 | `tests/test_catalog_graph_and_search.py::test_an_unsupported_catalog_is_a_503_not_a_partial_list` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-11 | `tests/test_catalog_graph_and_search.py::test_a_catalog_over_20_mib_is_refused_before_it_is_read` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |

## 7. Risks and open questions

| # | Risk or question | Impact | Owner | Resolution / date |
|---|---|---|---|---|
| 1 | Stored rows written before a range rule existed may already be inverted | a patch touching one of those dates is refused until both are fixed | owner | patches that do not touch the range still work |

## 8. Change log

| Version | Date | Change | Scenarios affected | Approved by |
|---|---|---|---|---|
| 1.0 | 2026-10-22 | Implemented with the Wave 2 baseline | all | pending owner |

## Approval

| Gate | Role | Name / date | Decision | Evidence |
|---|---|---|---|---|
| Implemented (DoD) | Reviewer (checker) | Fresh Claude reviewer / 2026-10-22 | see Wave 2 report | CI run of the pushed tip |
| Implemented (DoD) | Approver | Owner | pending | Wave 2 report |
