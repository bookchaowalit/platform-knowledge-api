#!/usr/bin/env python3
"""Read-only metadata API for the Solo Empire Knowledge Atlas.

The catalog is the safe projection for this service.  Markdown remains the
durable source for humans and AI agents; this API never reads or returns a
document body, private database row, or credential.

Run locally from the repository root with::

    python3 server.py

Set ``KNOWLEDGE_API_CATALOG_PATH`` to use another generated catalog and
the ``KNOWLEDGE_API_AUTH_*`` policy before exposing the service outside local development.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import posixpath
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock, Thread
from typing import Any
from urllib.parse import parse_qs, urlsplit

from service_auth import AuthError, AuthPolicy, check_read_auth, is_loopback, readiness

from _sentry import capture, init_sentry
from document_store import PostgresStore, Store, StoreError

init_sentry()

SERVICE_NAME = "solo-empire-knowledge-api"
SCHEMA_VERSION = "solo-empire.knowledge.v1"
CATALOG_SCHEMA_VERSION = "solo-empire.document-catalog.v1"
DEFAULT_LIMIT = 60
MAX_LIMIT = 100
MAX_OFFSET = 5_000
MAX_QUERY_LENGTH = 120
MAX_PATH_LENGTH = 500
# Store writes (SPEC-002): a 10 MiB document is about 13.4 MiB as base64 JSON.
STORE_DOCUMENT = "/api/v1/store/document"
MAX_REQUEST_BYTES = 16 * 1024 * 1024
# /ready waits this long at most for the store probe (Docker HEALTHCHECK times out at 3 s).
STORE_PROBE_WAIT = 1.0
WRITE_FIELDS = frozenset(
    {"content", "content_base64", "media_type", "visibility", "if_version"}
)
SAFE_SEGMENT_RE = re.compile(r"^[^\x00]+$")
ALIAS_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


class ApiError(Exception):
    """An expected client or service error with a safe public message."""

    def __init__(
        self, status: int, message: str, *, code: str = "request_error"
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


@dataclass(frozen=True)
class Response:
    status: int
    payload: dict[str, Any]


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_catalog_path() -> Path:
    configured = os.environ.get("KNOWLEDGE_API_CATALOG_PATH")
    return (
        Path(configured).expanduser()
        if configured
        else Path(__file__).resolve().parent / "data" / "document-catalog.json"
    )


def bounded_text(
    value: Any, field: str, *, max_length: int, required: bool = False
) -> str | None:
    if value is None:
        if required:
            raise ApiError(400, f"{field} is required", code="invalid_field")
        return None
    if not isinstance(value, str):
        raise ApiError(400, f"{field} must be text", code="invalid_field")
    result = value.strip()
    if required and not result:
        raise ApiError(400, f"{field} is required", code="invalid_field")
    if len(result) > max_length:
        raise ApiError(400, f"{field} is too long", code="invalid_field")
    return result


def first(query: Mapping[str, list[str]], field: str) -> str | None:
    """The first value of a query parameter, or None when it is absent."""
    values = query.get(field)
    return values[0] if values else None


def parse_limit(value: str | None) -> int:
    if value in (None, ""):
        return DEFAULT_LIMIT
    try:
        result = int(value)
    except ValueError as exc:
        raise ApiError(400, "limit must be an integer", code="invalid_limit") from exc
    if result < 1 or result > MAX_LIMIT:
        raise ApiError(
            400, f"limit must be between 1 and {MAX_LIMIT}", code="invalid_limit"
        )
    return result


def parse_offset(value: str | None) -> int:
    if value in (None, ""):
        return 0
    try:
        result = int(value)
    except ValueError as exc:
        raise ApiError(400, "offset must be an integer", code="invalid_offset") from exc
    if result < 0 or result > MAX_OFFSET:
        raise ApiError(
            400, f"offset must be between 0 and {MAX_OFFSET}", code="invalid_offset"
        )
    return result


def normalize_path(value: Any, *, field: str = "path") -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(400, f"{field} must be a path", code="invalid_path")
    candidate = value.strip().replace("\\", "/")
    if not candidate or len(candidate) > MAX_PATH_LENGTH or candidate.startswith("/"):
        raise ApiError(400, f"{field} is invalid", code="invalid_path")
    if not SAFE_SEGMENT_RE.fullmatch(candidate) or any(
        part in {"", ".", ".."} for part in candidate.split("/")
    ):
        raise ApiError(400, f"{field} is invalid", code="invalid_path")
    normalized = posixpath.normpath(candidate)
    if normalized == "." or normalized.split("/", 1)[0] == "..":
        raise ApiError(400, f"{field} is invalid", code="invalid_path")
    return normalized


def clip(value: Any, max_length: int) -> str:
    if value is None:
        return ""
    return str(value).strip()[:max_length]


def safe_path(path: str) -> bool:
    """Keep the curated metadata boundary; metadata is still private to the owner."""

    normalized = path.replace("\\", "/")
    folded = normalized.casefold()
    parts = folded.split("/")
    if any(
        part in {".git", ".ssh", ".aws", "credentials", "secrets", "private"}
        or part == ".env"
        or part.startswith(".env.")
        for part in parts
    ):
        return False
    excluded_prefixes = (
        "data/generated/",
        "data/raw/",
        "data/runtime/",
        "data/backups/",
        "data/lake/",
        "data/lake-local/",
    )
    if folded.startswith(excluded_prefixes):
        return False
    if folded.startswith(
        ("engagements/", "projects/product/engineering/book-dev/", "decisions/records/")
    ):
        return False
    if not folded.endswith((".md", ".mdx")):
        return False
    private_prefixes = (
        "diary/entries/",
        "finance/accounts/",
        "finance/assets/",
        "finance/budgets/",
        "finance/cash-flow/",
        "finance/expense/",
        "finance/inbox/",
        "finance/income/",
        "finance/insurance/",
        "finance/invoices/",
        "finance/investments/",
        "finance/liabilities/",
        "finance/reconciliation/",
        "finance/tax/",
        "clients/",
    )
    return not (
        folded.startswith(private_prefixes) and not folded.endswith("/readme.md")
    )


def safe_alias(value: Any) -> str | None:
    """Accept a human label, never an external URI or absolute path."""
    if not isinstance(value, str):
        return None
    label = value.strip()
    if (
        not label
        or len(label) > 120
        or label.startswith(("/", "\\"))
        or ALIAS_SCHEME_RE.match(label)
    ):
        return None
    return label


def resolve_reference(
    base_path: str, reference: Any, known_paths: set[str]
) -> str | None:
    if not isinstance(reference, str):
        return None
    value = reference.strip().strip("<>")
    if not value or value.startswith(("http://", "https://", "mailto:", "#")):
        return None
    value = value.split("#", 1)[0].split("?", 1)[0].strip()
    if not value:
        return None
    direct = value.replace("\\", "/")
    candidates = [direct]
    if not direct.endswith(".md") and not direct.endswith(".mdx"):
        candidates.append(f"{direct}.md")
    relative = [
        posixpath.normpath(posixpath.join(posixpath.dirname(base_path), item))
        for item in candidates
    ]
    candidates = relative + candidates
    for candidate in candidates:
        if candidate in known_paths:
            return candidate
    return None


class CatalogStore:
    """Reload a generated catalog when its mtime changes, without reading document bodies."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or default_catalog_path()).expanduser()
        self._mtime_ns: int | None = None
        self._rows: list[dict[str, Any]] = []
        self._by_path: dict[str, dict[str, Any]] = {}
        self._meta: dict[str, Any] = {}
        self._lock = RLock()

    def _load(self) -> None:
        try:
            stat = self.path.stat()
            if stat.st_size > 20 * 1024 * 1024:
                raise ApiError(
                    503,
                    "knowledge catalog exceeds the supported size",
                    code="catalog_too_large",
                )
            if self._mtime_ns == stat.st_mtime_ns and self._rows:
                return
            source_text = self.path.read_text(encoding="utf-8")
            payload = json.loads(source_text)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(
                503, "knowledge catalog is unavailable", code="catalog_unavailable"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != CATALOG_SCHEMA_VERSION
        ):
            raise ApiError(
                503,
                "knowledge catalog schema is unsupported",
                code="catalog_schema_unsupported",
            )
        source_rows = payload.get("documents")
        if not isinstance(source_rows, list):
            raise ApiError(
                503,
                "knowledge catalog documents are unavailable",
                code="catalog_schema_invalid",
            )
        if len(source_rows) > 20_000:
            raise ApiError(
                503,
                "knowledge catalog exceeds the supported record count",
                code="catalog_too_large",
            )

        rows: list[dict[str, Any]] = []
        for source in source_rows:
            if not isinstance(source, Mapping):
                continue
            try:
                path = normalize_path(source.get("path"))
            except ApiError:
                continue
            if not path or not safe_path(path):
                continue
            tags = [
                clip(item, 60)
                for item in (source.get("tags") or [])
                if isinstance(item, str)
            ][:16]
            related_refs = [
                item for item in (source.get("related") or []) if isinstance(item, str)
            ][:20]
            parent_ref = (
                source.get("parent") if isinstance(source.get("parent"), str) else None
            )
            row = {
                "path": path,
                "title": clip(
                    source.get("title")
                    or Path(path).stem.replace("-", " ").replace("_", " "),
                    240,
                ),
                "description": clip(source.get("description"), 360),
                "document_type": clip(source.get("document_type") or "document", 80),
                "status": clip(source.get("status") or "unclassified", 100),
                "root_area": clip(source.get("root_area") or "(root)", 80),
                "tags": tags,
                "aliases": [
                    alias
                    for item in (source.get("aliases") or [])
                    if (alias := safe_alias(item))
                ][:8],
                "canonical": None,
                "has_frontmatter": bool(source.get("has_frontmatter")),
                "internal_link_count": max(
                    0, int(source.get("internal_link_count") or 0)
                ),
                "created": clip(source.get("created"), 40),
                "updated": clip(source.get("updated"), 40),
                "_related_refs": related_refs,
                "_parent_ref": parent_ref,
            }
            rows.append(row)

        by_path = {row["path"]: row for row in rows}
        if len(by_path) != len(rows):
            raise ApiError(
                503,
                "knowledge catalog contains duplicate paths",
                code="catalog_schema_invalid",
            )
        known_paths = set(by_path)
        children: dict[str, list[str]] = defaultdict(list)
        for row in rows:
            related = [
                resolved
                for ref in row.pop("_related_refs", [])
                if (resolved := resolve_reference(row["path"], ref, known_paths))
            ]
            parent = resolve_reference(
                row["path"], row.pop("_parent_ref", None), known_paths
            )
            row["related"] = list(dict.fromkeys(related))[:20]
            row["parent"] = parent
            if parent:
                children[parent].append(row["path"])
            row["id"] = hashlib.sha256(row["path"].encode("utf-8")).hexdigest()[:12]
        for row in rows:
            row["children"] = sorted(children.get(row["path"], []))[:20]

        self._rows = rows
        self._by_path = by_path
        self._meta = {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "revision": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            "generated_by": clip(payload.get("generated_by"), 160),
            "scope": {
                "included": clip((payload.get("scope") or {}).get("included"), 240)
                if isinstance(payload.get("scope"), Mapping)
                else "",
                "excluded": [
                    clip(item, 120)
                    for item in ((payload.get("scope") or {}).get("excluded") or [])
                    if isinstance(item, str)
                ][:16]
                if isinstance(payload.get("scope"), Mapping)
                else [],
            },
            "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "excluded_records": len(source_rows) - len(rows),
        }
        self._mtime_ns = stat.st_mtime_ns

    def snapshot(
        self,
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
        # Publish one generation atomically. Callers never mutate these objects.
        with self._lock:
            self._load()
            return self._rows, self._by_path, self._meta


def store_from_env() -> Store | None:
    """The Postgres store when KNOWLEDGE_STORE_DATABASE_URL is set, else None (store off)."""
    url = os.environ.get("KNOWLEDGE_STORE_DATABASE_URL", "").strip()
    if not url:
        return None
    spaces = tuple(
        item.strip()
        for item in os.environ.get("KNOWLEDGE_STORE_SPACES", "knowledge-base").split(
            ","
        )
        if item.strip()
    )
    event_key = os.environ.get("KNOWLEDGE_EVENT_KEY", "").strip().encode() or None
    return PostgresStore(url, spaces=spaces, event_key=event_key)


class KnowledgeApi:
    """Transport-independent, read-only handler used by HTTP and tests."""

    def __init__(
        self,
        catalog_path: str | Path | None = None,
        *,
        token: str | None = None,
        environment: str | None = None,
        allowed_origins: tuple[str, ...] | None = None,
        auth_policy: AuthPolicy | None = None,
        write_policy: AuthPolicy | None = None,
        store: Store | None = None,
    ) -> None:
        self.catalog = CatalogStore(catalog_path)
        self.store = store if store is not None else store_from_env()
        self._probe_lock = RLock()
        self._probe: Thread | None = None
        self._store_state = "checking"
        self.token = (
            token
            if token is not None
            else os.environ.get("KNOWLEDGE_API_TOKEN", "").strip()
        )
        self.environment = (
            (environment or os.environ.get("KNOWLEDGE_API_ENV", "development"))
            .strip()
            .lower()
        )
        self.auth_policy = auth_policy or AuthPolicy.from_env(
            "KNOWLEDGE_API_", "knowledge-api", "knowledge:read"
        )
        # Same trusted keys and owner subjects; only the scope differs, so a read token
        # can never write and a write token can never read.
        self.write_policy = write_policy or AuthPolicy.from_env(
            "KNOWLEDGE_API_", "knowledge-api", "knowledge:write"
        )
        configured_origins = os.environ.get(
            "KNOWLEDGE_API_CORS_ORIGINS", "http://localhost:3300,http://localhost:3000"
        )
        self.allowed_origins = allowed_origins or tuple(
            item.strip() for item in configured_origins.split(",") if item.strip()
        )

    def _check_read_auth(
        self, headers: Mapping[str, str], policy: AuthPolicy | None = None
    ) -> None:
        try:
            check_read_auth(
                policy or self.auth_policy,
                headers,
                environment=self.environment,
                token=self.token,
            )
        except AuthError as exc:
            raise ApiError(exc.status, exc.message, code=exc.code) from None

    def _ready(self) -> Response:
        status, payload = readiness(self.auth_policy, self.environment, self.catalog)
        # The store is reported, not gated on: a cold or missing database must not take
        # the catalog down with it.
        payload["store"] = self._store_status()
        return Response(status, payload)

    def _store_status(self) -> str:
        """The store's last probe result; /ready never waits on a cold database.

        One background probe runs at a time and /ready waits at most STORE_PROBE_WAIT
        seconds for it, so a Neon resume (6-9 s) or pool wait cannot hold up the
        health check. Until a probe finishes the status is "checking"."""
        if self.store is None:
            return "disabled"
        store = self.store
        with self._probe_lock:
            probe = self._probe
            if probe is None or not probe.is_alive():

                def run() -> None:
                    try:
                        store.ping()
                        result = "available"
                    except Exception:  # noqa: BLE001 - any failure is "unavailable"; the error text stays private
                        result = "unavailable"
                    self._store_state = result

                probe = Thread(target=run, daemon=True)
                self._probe = probe
                probe.start()
        probe.join(STORE_PROBE_WAIT)
        return self._store_state

    @staticmethod
    def _public_document(
        row: Mapping[str, Any], *, detail: bool = False
    ) -> dict[str, Any]:
        payload = {
            "id": row["id"],
            "path": row["path"],
            "title": row["title"],
            "description": row["description"] or None,
            "document_type": row["document_type"],
            "status": row["status"],
            "root_area": row["root_area"],
            "tags": list(row["tags"]),
            "has_frontmatter": bool(row["has_frontmatter"]),
            "internal_link_count": int(row["internal_link_count"]),
            "created": row["created"] or None,
            "updated": row["updated"] or None,
            "parent": row.get("parent"),
            "related_count": len(row.get("related", [])),
        }
        if detail:
            payload["aliases"] = list(row.get("aliases", []))
            payload["canonical"] = row.get("canonical") or None
            payload["related"] = list(row.get("related", []))
            payload["children"] = list(row.get("children", []))
        return payload

    @staticmethod
    def _score(row: Mapping[str, Any], query: str) -> int:
        needle = query.casefold()
        title = str(row["title"]).casefold()
        path = str(row["path"]).casefold()
        description = str(row["description"]).casefold()
        tags = " ".join(row.get("tags", [])).casefold()
        score = 0
        if title == needle:
            score += 100
        elif title.startswith(needle):
            score += 70
        elif needle in title:
            score += 50
        if needle in path:
            score += 28
        if needle in tags:
            score += 18
        if needle in description:
            score += 10
        return score

    @staticmethod
    def _shelves(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[row["root_area"]].append(row)
        shelves: list[dict[str, Any]] = []
        for root_area, items in grouped.items():
            statuses = Counter(item["status"] for item in items)
            shelves.append(
                {
                    "key": root_area,
                    "label": root_area.replace("-", " ").title(),
                    "count": len(items),
                    "linked": sum(
                        1
                        for item in items
                        if item["internal_link_count"]
                        or item.get("related")
                        or item.get("children")
                    ),
                    "top_status": statuses.most_common(1)[0][0]
                    if statuses
                    else "unclassified",
                }
            )
        return sorted(shelves, key=lambda item: (-item["count"], item["key"]))

    def _overview(self, query: Mapping[str, list[str]]) -> Response:
        q = (
            bounded_text(first(query, "q"), "q", max_length=MAX_QUERY_LENGTH)
            if query.get("q")
            else None
        )
        root_area = (
            bounded_text(first(query, "root_area"), "root_area", max_length=80)
            if query.get("root_area")
            else None
        )
        document_type = (
            bounded_text(
                first(query, "document_type"),
                "document_type",
                max_length=80,
            )
            if query.get("document_type")
            else None
        )
        status = (
            bounded_text(first(query, "status"), "status", max_length=100)
            if query.get("status")
            else None
        )
        tag = (
            bounded_text(first(query, "tag"), "tag", max_length=60)
            if query.get("tag")
            else None
        )
        limit = parse_limit(first(query, "limit"))
        offset = parse_offset(first(query, "offset"))
        rows, _, metadata = self.catalog.snapshot()
        filtered = [
            row
            for row in rows
            if (not root_area or row["root_area"] == root_area)
            and (not document_type or row["document_type"] == document_type)
            and (not status or row["status"] == status)
            and (not tag or tag in row["tags"])
            and (not q or self._score(row, q) > 0)
        ]
        if q:
            filtered.sort(key=lambda row: row["path"])
            filtered.sort(key=lambda row: str(row["updated"] or ""), reverse=True)
            filtered.sort(key=lambda row: self._score(row, q), reverse=True)
        else:
            filtered.sort(
                key=lambda row: (row["root_area"], row["title"].casefold(), row["path"])
            )
        statuses = Counter(row["status"] for row in rows)
        types = Counter(row["document_type"] for row in rows)
        tags = Counter(tag_name for row in rows for tag_name in row["tags"])
        linked = sum(
            1
            for row in rows
            if row["internal_link_count"] or row.get("related") or row.get("children")
        )
        graph_edges = sum(
            len(row.get("related", [])) + (1 if row.get("parent") else 0)
            for row in rows
        )
        page = filtered[offset : offset + limit]
        return Response(
            200,
            {
                "schema_version": SCHEMA_VERSION,
                "generated_at": utc_now(),
                "mode": "live",
                "source": "solo-empire-document-catalog",
                "catalog": metadata,
                "summary": {
                    "documents_total": len(rows),
                    "documents_matched": len(filtered),
                    "with_frontmatter": sum(
                        1 for row in rows if row["has_frontmatter"]
                    ),
                    "linked_documents": linked,
                    "root_areas": len({row["root_area"] for row in rows}),
                    "status_values": len(statuses),
                    "graph_edges": graph_edges,
                },
                "shelves": self._shelves(rows),
                "signals": {
                    "document_types": [
                        {"value": key, "count": count}
                        for key, count in sorted(
                            types.items(), key=lambda item: (-item[1], item[0])
                        )
                    ],
                    "statuses": [
                        {"value": key, "count": count}
                        for key, count in statuses.most_common()
                    ],
                    "top_tags": [
                        {"value": key, "count": count}
                        for key, count in tags.most_common(18)
                    ],
                },
                "filters": {
                    "q": q or "",
                    "root_area": root_area or "",
                    "document_type": document_type or "",
                    "status": status or "",
                    "tag": tag or "",
                    "limit": limit,
                    "offset": offset,
                },
                "documents": [self._public_document(row) for row in page],
            },
        )

    def _detail(self, query: Mapping[str, list[str]]) -> Response:
        raw_path = first(query, "path")
        path = normalize_path(raw_path, field="path")
        if not path:
            raise ApiError(400, "path is required", code="missing_path")
        _, by_path, _ = self.catalog.snapshot()
        row = by_path.get(path)
        if row is None:
            raise ApiError(
                404, "document metadata not found", code="document_not_found"
            )
        neighbors: list[dict[str, Any]] = []
        if row.get("parent") and (parent := by_path.get(row["parent"])):
            neighbors.append(
                {"relation": "parent", "document": self._public_document(parent)}
            )
        for relation, paths in (
            ("related", row.get("related", [])),
            ("child", row.get("children", [])),
        ):
            neighbors.extend(
                {"relation": relation, "document": self._public_document(other)}
                for other_path in paths
                if (other := by_path.get(other_path))
            )
        return Response(
            200,
            {
                "schema_version": SCHEMA_VERSION,
                "generated_at": utc_now(),
                "mode": "live",
                "source": "solo-empire-document-catalog",
                "document": self._public_document(row, detail=True),
                "neighbors": neighbors[:40],
                "boundary": "metadata-only; document bodies remain in their owning workspace",
            },
        )

    @staticmethod
    def _one(query: Mapping[str, list[str]], name: str, code: str) -> str:
        values = query.get(name, [])
        if len(values) != 1 or not values[0]:
            raise ApiError(400, f"{name} is required", code=code)
        return values[0]

    @staticmethod
    def _int(
        query: Mapping[str, list[str]], name: str, default: int | None
    ) -> int | None:
        values = query.get(name, [])
        if not values:
            return default
        if len(values) != 1 or not values[0].isdigit() or len(values[0]) > 9:
            raise ApiError(
                400, f"{name} must be a non-negative integer", code=f"invalid_{name}"
            )
        return int(values[0])

    @staticmethod
    def _write_body(body: bytes | None) -> dict[str, Any]:
        try:
            payload = json.loads(body or b"")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "body must be JSON", code="invalid_json") from None
        invalid = ApiError(
            400, "body does not match the write contract", code="invalid_body"
        )
        if not isinstance(payload, dict) or set(payload) - WRITE_FIELDS:
            raise invalid
        if ("content" in payload) == ("content_base64" in payload):
            raise invalid
        if "content" in payload and not isinstance(payload["content"], str):
            raise invalid
        if "content_base64" in payload:
            raw = payload["content_base64"]
            try:
                if not isinstance(raw, str):
                    raise ValueError
                payload["content_bytes"] = base64.b64decode(raw, validate=True)
            except (ValueError, binascii.Error):
                raise invalid from None
        else:
            payload["content_bytes"] = payload["content"].encode("utf-8")
        for name in ("media_type", "visibility"):
            if name in payload and not isinstance(payload[name], str):
                raise invalid
        version = payload.get("if_version")
        if version is not None and (
            isinstance(version, bool) or not isinstance(version, int)
        ):
            raise invalid
        return payload

    def _store_route(
        self,
        method: str,
        path: str,
        query: Mapping[str, list[str]],
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> Response:
        routes = {
            "/api/v1/store/documents": {"GET"},
            "/api/v1/store/document": {"GET", "PUT", "DELETE"},
            "/api/v1/store/history": {"GET"},
        }
        if path not in routes:
            raise ApiError(404, "route not found", code="not_found")
        writing = method in {"PUT", "DELETE"}
        self._check_read_auth(
            headers, self.write_policy if writing else self.auth_policy
        )
        if method not in routes[path]:
            raise ApiError(405, "method not allowed", code="method_not_allowed")
        if self.store is None:
            raise ApiError(
                503, "document store is not configured", code="store_unavailable"
            )
        store = self.store
        space = self._one(query, "space", "missing_space")
        if path == "/api/v1/store/documents":
            page = store.list(
                space,
                prefix=(query.get("prefix") or [""])[0],
                limit=self._int(query, "limit", 100) or 0,
                offset=self._int(query, "offset", 0) or 0,
                after=(query.get("after") or [""])[0],
            )
            return Response(
                200,
                {
                    "space": space,
                    "documents": [meta.to_payload() for meta in page.documents],
                    "total": page.total,
                },
            )
        doc_path = self._one(query, "path", "missing_path")
        if path == "/api/v1/store/history":
            versions = store.history(space, doc_path)
            return Response(
                200,
                {
                    "space": space,
                    "versions": [m.to_payload(with_deleted=True) for m in versions],
                },
            )
        if method == "GET":
            document = store.get(
                space, doc_path, version=self._int(query, "version", None)
            )
            return Response(200, {"space": space, "document": document.to_payload()})
        if method == "DELETE":
            meta = store.delete(
                space, doc_path, if_version=self._int(query, "if_version", None)
            )
            return Response(
                200, {"space": space, "document": meta.to_payload(with_deleted=True)}
            )
        payload = self._write_body(body)
        result = store.put(
            space,
            doc_path,
            payload["content_bytes"],
            media_type=payload.get("media_type"),
            visibility=payload.get("visibility", "private"),
            if_version=payload.get("if_version"),
        )
        return Response(
            201 if result.changed and result.meta.version == 1 else 200,
            {
                "space": space,
                "document": result.meta.to_payload(),
                "changed": result.changed,
            },
        )

    def preauthorize(
        self, method: str, target: str, headers: Mapping[str, str]
    ) -> Response | None:
        """Check authorization before a request body is read; None when it may proceed."""
        request_headers = {str(k).lower(): str(v) for k, v in headers.items()}
        path = urlsplit(target).path.rstrip("/") or "/"
        if path.startswith("/api/v1/store/"):
            policy = (
                self.write_policy if method in {"PUT", "DELETE"} else self.auth_policy
            )
        elif path.startswith("/api/v1/knowledge"):
            policy = self.auth_policy
        else:
            return None
        try:
            self._check_read_auth(request_headers, policy)
        except ApiError as exc:
            return Response(
                exc.status, {"error": {"code": exc.code, "message": exc.message}}
            )
        return None

    def handle(
        self,
        method: str,
        target: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> Response:
        request_headers = {
            str(key).lower(): str(value) for key, value in (headers or {}).items()
        }
        parsed = urlsplit(target)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"
        try:
            if method == "OPTIONS":
                return Response(204, {})
            if method == "GET" and path == "/ready":
                return self._ready()
            if method == "GET" and path == "/health":
                return Response(
                    200,
                    {
                        "status": "ok",
                        "service": SERVICE_NAME,
                        "schema_version": SCHEMA_VERSION,
                        "timestamp": utc_now(),
                    },
                )
            if path.startswith("/api/v1/store/"):
                # Keep blank values: `if_version=` must be refused, not read as absent.
                store_query = parse_qs(parsed.query, keep_blank_values=True)
                return self._store_route(
                    method, path, store_query, request_headers, body
                )
            if path.startswith("/api/v1/knowledge"):
                self._check_read_auth(request_headers)
                if method != "GET":
                    raise ApiError(
                        405,
                        "knowledge metadata is read-only",
                        code="method_not_allowed",
                    )
            if method == "GET" and path == "/api/v1/knowledge":
                return self._overview(query)
            if method == "GET" and path == "/api/v1/knowledge/document":
                return self._detail(query)
            raise ApiError(404, "route not found", code="not_found")
        except (ApiError, StoreError) as exc:
            return Response(
                exc.status, {"error": {"code": exc.code, "message": exc.message}}
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return Response(
                503,
                {
                    "error": {
                        "code": "catalog_unavailable",
                        "message": "Knowledge catalog could not be read",
                    }
                },
            )
        except Exception as exc:  # noqa: BLE001 - last-resort 500 that never echoes internal error text
            capture(exc)  # no-op unless SENTRY_DSN is set
            return Response(
                500,
                {
                    "error": {
                        "code": "internal_error",
                        "message": "Knowledge service could not complete the request",
                    }
                },
            )


class KnowledgeRequestHandler(BaseHTTPRequestHandler):
    api = KnowledgeApi()

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _dispatch(self) -> None:
        origin = self.headers.get("Origin")
        if origin and origin not in self.api.allowed_origins:
            self._write(
                Response(
                    403,
                    {
                        "error": {
                            "code": "origin_not_allowed",
                            "message": "origin is not allowed",
                        }
                    },
                ),
                origin=None,
            )
            return
        body: bytes | None = None
        if self.command in {"PUT", "POST", "PATCH", "DELETE"}:
            # Authorize before reading: an anonymous client never makes the server read 16 MiB.
            refusal = self.api.preauthorize(
                self.command, self.path, dict(self.headers.items())
            )
            # Only a store PUT has a body; every other route is answered without reading one.
            target = urlsplit(self.path).path.rstrip("/")
            if refusal is None and self.command == "PUT" and target == STORE_DOCUMENT:
                body, refusal = self._read_body()
            if refusal is not None:
                self.close_connection = True
                self._write(refusal, origin=origin)
                return
        response = self.api.handle(
            self.command, self.path, headers=dict(self.headers.items()), body=body
        )
        self._write(response, origin=origin)

    def _read_body(self) -> tuple[bytes | None, Response | None]:
        if self.headers.get("Transfer-Encoding"):
            return None, Response(
                411,
                {
                    "error": {
                        "code": "length_required",
                        "message": "send Content-Length",
                    }
                },
            )
        raw = self.headers.get("Content-Length", "0").strip()
        if not raw.isdigit():
            return None, Response(
                400,
                {
                    "error": {
                        "code": "invalid_length",
                        "message": "invalid Content-Length",
                    }
                },
            )
        length = int(raw)
        if length > MAX_REQUEST_BYTES:
            return None, Response(
                413,
                {
                    "error": {
                        "code": "request_too_large",
                        "message": "request body is larger than 16 MiB",
                    }
                },
            )
        return (self.rfile.read(length) if length else b""), None

    def _write(self, response: Response, *, origin: str | None) -> None:
        encoded = (
            b""
            if response.status == HTTPStatus.NO_CONTENT
            else json.dumps(
                response.payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        )
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(encoded)))
        if origin and origin in self.api.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        if self.command == "OPTIONS":
            self.send_header(
                "Access-Control-Allow-Methods", "GET, PUT, DELETE, OPTIONS"
            )
            self.send_header(
                "Access-Control-Allow-Headers", "Accept, Authorization, Content-Type"
            )
        self.end_headers()
        if response.status != HTTPStatus.NO_CONTENT:
            self.wfile.write(encoded)

    def do_GET(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self._dispatch()

    do_POST = _dispatch
    do_PUT = _dispatch
    do_PATCH = _dispatch
    do_DELETE = _dispatch


def main() -> None:
    parser = argparse.ArgumentParser(description="Solo Empire Knowledge Atlas API")
    parser.add_argument(
        "--host", default=os.environ.get("KNOWLEDGE_API_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(
            os.environ.get("KNOWLEDGE_API_PORT", os.environ.get("PORT", "3430"))
        ),
    )
    parser.add_argument(
        "--catalog",
        default=None,
        help="isolated catalog JSON path; defaults to KNOWLEDGE_API_CATALOG_PATH or data/document-catalog.json",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.catalog:
        KnowledgeRequestHandler.api = KnowledgeApi(args.catalog)
    if not is_loopback(args.host):
        KnowledgeRequestHandler.api.environment = "production"
        if not KnowledgeRequestHandler.api.auth_policy.ready:
            parser.error(
                "a non-loopback --host requires valid KNOWLEDGE_API_AUTH_* configuration"
            )
    server = ThreadingHTTPServer((args.host, args.port), KnowledgeRequestHandler)
    print(f"{SERVICE_NAME} running on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
