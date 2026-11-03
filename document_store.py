"""Hosted document store (SPEC-002): document bodies with full version history.

Each space (for example ``knowledge-base``) holds documents by relative path. Every write
and delete adds a version row, so nothing is ever overwritten in place. Two
implementations share one contract: ``PostgresStore`` for the service and ``MemoryStore``
for tests and local development. Paths are checked here, before any storage call, so
neither implementation can store a traversing or secret-looking path.
"""

from __future__ import annotations

import base64
import builtins
import hashlib
import mimetypes
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock
from typing import Any, Protocol

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_PATH_LENGTH = 500
MAX_LIST_LIMIT = 1000
VISIBILITIES = frozenset({"private", "public"})
MEDIA_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
SPACE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# Directory or file names that look like credentials or repository internals are refused,
# so a careless sync can never upload them.
FORBIDDEN_SEGMENTS = frozenset({".git", ".ssh", ".gnupg", ".aws"})
# Every .env* name (.env, .env.local, .envrc, .env-prod, ...) is refused.
FORBIDDEN_NAME_RE = re.compile(r"^(\.env.*|.*\.(pem|key|p12|pfx))$", re.IGNORECASE)
TEXT_MEDIA = {
    ".md": "text/markdown",
    ".mdx": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".py": "text/x-python",
    ".ts": "text/plain",
    ".log": "text/plain",
    ".txt": "text/plain",
    ".svg": "image/svg+xml",
}


class StoreError(Exception):
    """An expected store failure with a safe public message (never a path or body)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class DocumentMeta:
    path: str
    sha256: str
    size: int
    media_type: str
    visibility: str
    version: int
    updated_at: datetime
    deleted: bool = False

    def to_payload(self, *, with_deleted: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "media_type": self.media_type,
            "visibility": self.visibility,
            "version": self.version,
            "updated_at": self.updated_at.astimezone(UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        if with_deleted:
            payload["deleted"] = self.deleted
        return payload


@dataclass(frozen=True)
class Document:
    meta: DocumentMeta
    content: bytes = field(repr=False)

    def to_payload(self) -> dict[str, Any]:
        payload = self.meta.to_payload()
        if is_text(self.meta.media_type, self.content):
            payload["encoding"] = "utf-8"
            payload["content"] = self.content.decode("utf-8")
        else:
            payload["encoding"] = "base64"
            payload["content_base64"] = base64.b64encode(self.content).decode("ascii")
        return payload


@dataclass(frozen=True)
class WriteResult:
    meta: DocumentMeta
    changed: bool


@dataclass(frozen=True)
class Page:
    documents: list[DocumentMeta]
    total: int


class Store(Protocol):
    def list(
        self, space: str, *, prefix: str = "", limit: int = 100, offset: int = 0
    ) -> Page: ...

    def get(self, space: str, path: str, *, version: int | None = None) -> Document: ...

    def history(self, space: str, path: str) -> builtins.list[DocumentMeta]: ...

    def put(
        self,
        space: str,
        path: str,
        content: bytes,
        *,
        media_type: str | None = None,
        visibility: str = "private",
        if_version: int | None = None,
    ) -> WriteResult: ...

    def delete(
        self, space: str, path: str, *, if_version: int | None = None
    ) -> DocumentMeta: ...

    def ping(self) -> None: ...


def is_text(media_type: str, content: bytes) -> bool:
    textual = media_type.startswith("text/") or media_type in {
        "application/json",
        "application/yaml",
        "image/svg+xml",
    }
    if not textual:
        return False
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def guess_media_type(path: str) -> str:
    suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return (
        TEXT_MEDIA.get(suffix)
        or mimetypes.guess_type(path)[0]
        or "application/octet-stream"
    )


def check_path(path: str) -> str:
    invalid = StoreError(400, "invalid_path", "path is not a valid document path")
    if (
        not isinstance(path, str)
        or not 0 < len(path) <= MAX_PATH_LENGTH
        or path.startswith("/")
        or path.endswith("/")
        or "\\" in path
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in path)
    ):
        raise invalid
    segments = path.split("/")
    for segment in segments:
        if segment in {"", ".", ".."} or segment.lower() in FORBIDDEN_SEGMENTS:
            raise invalid
    if FORBIDDEN_NAME_RE.match(segments[-1]):
        raise invalid
    return path


def check_prefix(prefix: str) -> str:
    if prefix == "":
        return prefix
    probe = prefix.removesuffix("/")
    check_path(probe)
    return prefix


def check_page(limit: int, offset: int) -> None:
    if not 1 <= limit <= MAX_LIST_LIMIT:
        raise StoreError(400, "invalid_limit", "limit must be between 1 and 1000")
    if offset < 0:
        raise StoreError(400, "invalid_offset", "offset must not be negative")


def check_write(
    content: bytes, media_type: str | None, visibility: str, path: str
) -> str:
    if len(content) > MAX_DOCUMENT_BYTES:
        raise StoreError(413, "document_too_large", "document is larger than 10 MiB")
    if visibility not in VISIBILITIES:
        raise StoreError(400, "invalid_visibility", "visibility is private or public")
    resolved = media_type or guess_media_type(path)
    if not MEDIA_TYPE_RE.fullmatch(resolved) or len(resolved) > 100:
        raise StoreError(400, "invalid_media_type", "media_type is not valid")
    return resolved


def check_if_version(if_version: int | None) -> None:
    if if_version is not None and (
        isinstance(if_version, bool)
        or not isinstance(if_version, int)
        or if_version < 0
    ):
        raise StoreError(400, "invalid_if_version", "if_version must be 0 or more")


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def conflict() -> StoreError:
    return StoreError(
        409, "version_conflict", "the document changed since that version"
    )


def not_found() -> StoreError:
    return StoreError(404, "not_found", "document not found")


class _Spaces:
    def __init__(self, spaces: tuple[str, ...]) -> None:
        if not spaces or not all(SPACE_RE.fullmatch(s) for s in spaces):
            raise ValueError("store spaces must be non-empty kebab-case names")
        self.spaces = frozenset(spaces)

    def check_space(self, space: str) -> None:
        if space not in self.spaces:
            raise StoreError(404, "unknown_space", "space not found")


class MemoryStore(_Spaces):
    """In-process store with the same behaviour as PostgresStore (tests, local use)."""

    def __init__(self, *, spaces: tuple[str, ...]) -> None:
        super().__init__(spaces)
        self._lock = RLock()
        # (space, path) -> list of (meta, content or None for a delete), oldest first.
        self._versions: dict[
            tuple[str, str], list[tuple[DocumentMeta, bytes | None]]
        ] = {}

    def ping(self) -> None:
        return

    def _current(self, space: str, path: str) -> tuple[DocumentMeta, bytes] | None:
        versions = self._versions.get((space, path))
        if not versions or versions[-1][1] is None:
            return None
        meta, content = versions[-1]
        assert content is not None
        return meta, content

    def _last_version(self, space: str, path: str) -> int:
        versions = self._versions.get((space, path))
        return versions[-1][0].version if versions else 0

    def list(
        self, space: str, *, prefix: str = "", limit: int = 100, offset: int = 0
    ) -> Page:
        self.check_space(space)
        check_prefix(prefix)
        check_page(limit, offset)
        with self._lock:
            rows = [
                current[0]
                for (row_space, path) in self._versions
                if row_space == space
                and path.startswith(prefix)
                and (current := self._current(space, path))
            ]
        rows.sort(key=lambda meta: meta.path)
        return Page(rows[offset : offset + limit], len(rows))

    def get(self, space: str, path: str, *, version: int | None = None) -> Document:
        self.check_space(space)
        check_path(path)
        with self._lock:
            if version is None:
                current = self._current(space, path)
                if current is None:
                    raise not_found()
                return Document(*current)
            for meta, content in self._versions.get((space, path), []):
                if meta.version == version and content is not None:
                    return Document(meta, content)
        raise not_found()

    def history(self, space: str, path: str) -> builtins.list[DocumentMeta]:
        self.check_space(space)
        check_path(path)
        with self._lock:
            versions = [meta for meta, _ in self._versions.get((space, path), [])]
        if not versions:
            raise not_found()
        return versions

    def put(
        self,
        space: str,
        path: str,
        content: bytes,
        *,
        media_type: str | None = None,
        visibility: str = "private",
        if_version: int | None = None,
    ) -> WriteResult:
        self.check_space(space)
        check_path(path)
        resolved = check_write(content, media_type, visibility, path)
        check_if_version(if_version)
        sha = digest(content)
        with self._lock:
            current = self._current(space, path)
            current_version = current[0].version if current else 0
            if if_version is not None and if_version != current_version:
                raise conflict()
            if (
                current
                and current[0].sha256 == sha
                and current[0].media_type == resolved
                and current[0].visibility == visibility
            ):
                return WriteResult(current[0], changed=False)
            meta = DocumentMeta(
                path,
                sha,
                len(content),
                resolved,
                visibility,
                self._last_version(space, path) + 1,
                datetime.now(UTC),
            )
            self._versions.setdefault((space, path), []).append((meta, bytes(content)))
        return WriteResult(meta, changed=True)

    def delete(
        self, space: str, path: str, *, if_version: int | None = None
    ) -> DocumentMeta:
        self.check_space(space)
        check_path(path)
        check_if_version(if_version)
        with self._lock:
            current = self._current(space, path)
            if current is None:
                raise not_found()
            if if_version is not None and if_version != current[0].version:
                raise conflict()
            old = current[0]
            meta = DocumentMeta(
                path,
                old.sha256,
                0,
                old.media_type,
                old.visibility,
                old.version + 1,
                datetime.now(UTC),
                deleted=True,
            )
            self._versions[(space, path)].append((meta, None))
        return meta


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS {schema}.store_documents (
    space text NOT NULL,
    path text NOT NULL,
    version integer NOT NULL,
    sha256 text NOT NULL,
    size integer NOT NULL,
    media_type text NOT NULL,
    visibility text NOT NULL CHECK (visibility IN ('private', 'public')),
    content bytea NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (space, path)
);
CREATE TABLE IF NOT EXISTS {schema}.store_versions (
    space text NOT NULL,
    path text NOT NULL,
    version integer NOT NULL,
    sha256 text NOT NULL,
    size integer NOT NULL,
    media_type text NOT NULL,
    visibility text NOT NULL CHECK (visibility IN ('private', 'public')),
    content bytea,
    deleted boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (space, path, version)
);
"""

META_COLUMNS = "path, sha256, size, media_type, visibility, version, updated_at"


class PostgresStore(_Spaces):
    """Postgres store: the current row in store_documents, every version in store_versions."""

    def __init__(
        self,
        database_url: str,
        *,
        spaces: tuple[str, ...],
        schema: str = "public",
        min_size: int = 0,
        max_size: int = 4,
    ) -> None:
        super().__init__(spaces)
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
            raise ValueError("schema must be a plain lower-case identifier")
        from psycopg_pool import ConnectionPool

        self.schema = schema
        # Neon suspends idle compute: check each connection before use and allow a
        # cold resume (about 6-9 s) inside the connect and wait timeouts.
        self.pool = ConnectionPool(
            database_url,
            min_size=min_size,
            max_size=max_size,
            timeout=20,
            check=ConnectionPool.check_connection,
            kwargs={"connect_timeout": 15, "autocommit": False},
            open=True,
        )

    def close(self) -> None:
        self.pool.close()

    def migrate(self) -> None:
        with self.pool.connection() as conn:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema}")
            conn.execute(SCHEMA_SQL.format(schema=self.schema))

    def drop_schema(self) -> None:
        with self.pool.connection() as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {self.schema} CASCADE")

    def ping(self) -> None:
        with self.pool.connection() as conn:
            conn.execute("SELECT 1")

    @property
    def _documents(self) -> str:
        return f"{self.schema}.store_documents"

    @property
    def _versions(self) -> str:
        return f"{self.schema}.store_versions"

    @staticmethod
    def _meta(row: tuple[Any, ...], *, deleted: bool = False) -> DocumentMeta:
        path, sha256, size, media_type, visibility, version, updated_at = row[:7]
        return DocumentMeta(
            path, sha256, size, media_type, visibility, version, updated_at, deleted
        )

    def list(
        self, space: str, *, prefix: str = "", limit: int = 100, offset: int = 0
    ) -> Page:
        self.check_space(space)
        check_prefix(prefix)
        check_page(limit, offset)
        # starts_with avoids LIKE wildcards in the prefix.
        where = "space = %s AND starts_with(path, %s)"
        with self.pool.connection() as conn:
            total_row = conn.execute(
                f"SELECT count(*) FROM {self._documents} WHERE {where}",
                (space, prefix),
            ).fetchone()
            rows = conn.execute(
                f"SELECT {META_COLUMNS} FROM {self._documents} WHERE {where}"
                ' ORDER BY path COLLATE "C" LIMIT %s OFFSET %s',
                (space, prefix, limit, offset),
            ).fetchall()
        total = int(total_row[0]) if total_row else 0
        return Page([self._meta(row) for row in rows], total)

    def get(self, space: str, path: str, *, version: int | None = None) -> Document:
        self.check_space(space)
        check_path(path)
        with self.pool.connection() as conn:
            if version is None:
                row = conn.execute(
                    f"SELECT {META_COLUMNS}, content FROM {self._documents}"
                    " WHERE space = %s AND path = %s",
                    (space, path),
                ).fetchone()
            else:
                row = conn.execute(
                    f"SELECT path, sha256, size, media_type, visibility, version,"
                    f" created_at, content FROM {self._versions}"
                    " WHERE space = %s AND path = %s AND version = %s AND NOT deleted",
                    (space, path, version),
                ).fetchone()
        if row is None:
            raise not_found()
        return Document(self._meta(row), bytes(row[7]))

    def history(self, space: str, path: str) -> builtins.list[DocumentMeta]:
        self.check_space(space)
        check_path(path)
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT path, sha256, size, media_type, visibility, version,"
                f" created_at, deleted FROM {self._versions}"
                " WHERE space = %s AND path = %s ORDER BY version",
                (space, path),
            ).fetchall()
        if not rows:
            raise not_found()
        return [self._meta(row, deleted=bool(row[7])) for row in rows]

    def _lock_path(self, conn: Any, space: str, path: str) -> None:
        # Serialise writers of one path, including the first create of a new path.
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{self.schema}:{space}:{path}",),
        )

    def put(
        self,
        space: str,
        path: str,
        content: bytes,
        *,
        media_type: str | None = None,
        visibility: str = "private",
        if_version: int | None = None,
    ) -> WriteResult:
        self.check_space(space)
        check_path(path)
        resolved = check_write(content, media_type, visibility, path)
        check_if_version(if_version)
        sha = digest(content)
        with self.pool.connection() as conn, conn.transaction():
            self._lock_path(conn, space, path)
            current = conn.execute(
                f"SELECT {META_COLUMNS} FROM {self._documents}"
                " WHERE space = %s AND path = %s",
                (space, path),
            ).fetchone()
            current_meta = self._meta(current) if current else None
            current_version = current_meta.version if current_meta else 0
            if if_version is not None and if_version != current_version:
                raise conflict()
            if (
                current_meta
                and current_meta.sha256 == sha
                and current_meta.media_type == resolved
                and current_meta.visibility == visibility
            ):
                return WriteResult(current_meta, changed=False)
            last = conn.execute(
                f"SELECT coalesce(max(version), 0) FROM {self._versions}"
                " WHERE space = %s AND path = %s",
                (space, path),
            ).fetchone()
            version = int(last[0] if last else 0) + 1
            row = conn.execute(
                f"INSERT INTO {self._documents}"
                " (space, path, version, sha256, size, media_type, visibility, content,"
                " updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())"
                " ON CONFLICT (space, path) DO UPDATE SET version = EXCLUDED.version,"
                " sha256 = EXCLUDED.sha256, size = EXCLUDED.size,"
                " media_type = EXCLUDED.media_type, visibility = EXCLUDED.visibility,"
                " content = EXCLUDED.content, updated_at = EXCLUDED.updated_at"
                f" RETURNING {META_COLUMNS}",
                (
                    space,
                    path,
                    version,
                    sha,
                    len(content),
                    resolved,
                    visibility,
                    content,
                ),
            ).fetchone()
            conn.execute(
                f"INSERT INTO {self._versions}"
                " (space, path, version, sha256, size, media_type, visibility, content)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    space,
                    path,
                    version,
                    sha,
                    len(content),
                    resolved,
                    visibility,
                    content,
                ),
            )
        assert row is not None
        return WriteResult(self._meta(row), changed=True)

    def delete(
        self, space: str, path: str, *, if_version: int | None = None
    ) -> DocumentMeta:
        self.check_space(space)
        check_path(path)
        check_if_version(if_version)
        with self.pool.connection() as conn, conn.transaction():
            self._lock_path(conn, space, path)
            current = conn.execute(
                f"DELETE FROM {self._documents} WHERE space = %s AND path = %s"
                f" RETURNING {META_COLUMNS}",
                (space, path),
            ).fetchone()
            if current is None:
                raise not_found()
            old = self._meta(current)
            if if_version is not None and if_version != old.version:
                raise conflict()  # rolls the DELETE back with the transaction
            row = conn.execute(
                f"INSERT INTO {self._versions}"
                " (space, path, version, sha256, size, media_type, visibility, content,"
                " deleted) VALUES (%s, %s, %s, %s, 0, %s, %s, NULL, true)"
                " RETURNING path, sha256, size, media_type, visibility, version,"
                " created_at",
                (
                    space,
                    path,
                    old.version + 1,
                    old.sha256,
                    old.media_type,
                    old.visibility,
                ),
            ).fetchone()
        assert row is not None
        return self._meta(row, deleted=True)
