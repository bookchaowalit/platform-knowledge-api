"""SPEC-002 store contract: the same scenarios against the in-memory store and, when
KNOWLEDGE_STORE_TEST_DATABASE_URL is set, a real Postgres (see the spec's test plan)."""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from document_store import (
    MAX_DOCUMENT_BYTES,
    MemoryStore,
    PostgresStore,
    Store,
    StoreError,
)

SPACE = "knowledge-base"


@pytest.fixture(scope="session")
def database_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """KNOWLEDGE_STORE_TEST_DATABASE_URL, else an embedded Postgres (pgserver), else skip."""
    configured = os.environ.get("KNOWLEDGE_STORE_TEST_DATABASE_URL", "")
    if configured:
        yield configured
        return
    try:
        import pgserver
    except ImportError:
        pytest.skip(
            "no Postgres: set KNOWLEDGE_STORE_TEST_DATABASE_URL or install pgserver"
        )
    server = pgserver.get_server(
        str(tmp_path_factory.mktemp("pg")), cleanup_mode="stop"
    )
    try:
        yield str(server.get_uri())
    finally:
        server.cleanup()


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> Iterator[Store]:
    if request.param == "memory":
        yield MemoryStore(spaces=(SPACE,))
        return
    url = request.getfixturevalue("database_url")
    # Each test gets its own schema, so runs never see each other's rows.
    schema = f"t_{uuid.uuid4().hex[:12]}"
    pg = PostgresStore(url, spaces=(SPACE,), schema=schema)
    pg.migrate()
    try:
        yield pg
    finally:
        pg.drop_schema()
        pg.close()


def test_a_document_round_trips_with_its_exact_bytes(store: Store) -> None:
    text = "# บันทึก\n\nline two\n"
    written = store.put(SPACE, "notes/a.md", text.encode())
    read = store.get(SPACE, "notes/a.md")
    assert read.content == text.encode()
    assert read.meta.sha256 == written.meta.sha256
    assert read.meta.media_type == "text/markdown"
    assert read.meta.visibility == "private"
    assert read.meta.version == 1
    assert written.changed is True


def test_binary_files_round_trip_as_base64(store: Store) -> None:
    png = b"\x89PNG\r\n\x1a\n\x00\x00\xff\xfe"
    store.put(SPACE, "assets/x.png", png)
    read = store.get(SPACE, "assets/x.png")
    assert read.content == png
    assert read.meta.media_type == "image/png"
    payload = read.to_payload()
    assert "content" not in payload
    assert payload["encoding"] == "base64"


def test_every_write_and_delete_keeps_history(store: Store) -> None:
    store.put(SPACE, "notes/h.md", b"one")
    store.put(SPACE, "notes/h.md", b"two")
    store.delete(SPACE, "notes/h.md")
    versions = store.history(SPACE, "notes/h.md")
    assert [v.version for v in versions] == [1, 2, 3]
    assert [v.deleted for v in versions] == [False, False, True]
    assert store.get(SPACE, "notes/h.md", version=1).content == b"one"
    with pytest.raises(StoreError) as missing:
        store.get(SPACE, "notes/h.md")
    assert missing.value.status == 404
    with pytest.raises(StoreError) as deleted_version:
        store.get(SPACE, "notes/h.md", version=3)
    assert deleted_version.value.status == 404
    # A path can be written again after a delete; history continues.
    assert store.put(SPACE, "notes/h.md", b"three").meta.version == 4


def test_a_stale_if_version_is_a_409(store: Store) -> None:
    store.put(SPACE, "notes/c.md", b"one")
    store.put(SPACE, "notes/c.md", b"two", if_version=1)
    for stale in (1, 0):
        with pytest.raises(StoreError) as conflict:
            store.put(SPACE, "notes/c.md", b"three", if_version=stale)
        assert (conflict.value.status, conflict.value.code) == (409, "version_conflict")
    with pytest.raises(StoreError) as delete_conflict:
        store.delete(SPACE, "notes/c.md", if_version=1)
    assert delete_conflict.value.status == 409
    assert store.get(SPACE, "notes/c.md").meta.version == 2
    # if_version 0 means "create only" and works for a new path.
    assert store.put(SPACE, "notes/new.md", b"x", if_version=0).meta.version == 1


def test_an_identical_write_does_not_add_a_version(store: Store) -> None:
    store.put(SPACE, "notes/same.md", b"same")
    again = store.put(SPACE, "notes/same.md", b"same")
    assert (again.meta.version, again.changed) == (1, False)
    assert len(store.history(SPACE, "notes/same.md")) == 1
    # Changing only the visibility is a change.
    assert store.put(SPACE, "notes/same.md", b"same", visibility="public").changed


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/abs.md",
        "../up.md",
        "a/../b.md",
        "a//b.md",
        "a/./b.md",
        "dir/",
        "x" * 501,
        "bad\x00.md",
        "back\\slash.md",
        ".git/config",
        "sub/.git/HEAD",
        ".env",
        "app/.env.local",
        ".envrc",
        "app/.env-prod",
        "app/.env_backup",
        ".ssh/id_rsa",
        "keys/server.pem",
        "keys/server.key",
        ".env/notes.md",
        "certs.pem/readme.md",
        "notes/" + "ก" * 90 + ".md",  # 273 bytes: longer than a file name may be
    ],
)
def test_unsafe_paths_and_unknown_spaces_are_refused(store: Store, path: str) -> None:
    with pytest.raises(StoreError) as refused:
        store.put(SPACE, path, b"x")
    assert (refused.value.status, refused.value.code) == (400, "invalid_path")
    with pytest.raises(StoreError) as unknown:
        store.put("other-space", "notes/a.md", b"x")
    assert (unknown.value.status, unknown.value.code) == (404, "unknown_space")
    assert store.list(SPACE).total == 0


def test_oversized_documents_are_a_413(store: Store) -> None:
    with pytest.raises(StoreError) as too_big:
        store.put(SPACE, "big.bin", b"\0" * (MAX_DOCUMENT_BYTES + 1))
    assert (too_big.value.status, too_big.value.code) == (413, "document_too_large")
    assert store.list(SPACE).total == 0


def test_listing_by_prefix_returns_metadata_only(store: Store) -> None:
    for path in ("notes/b.md", "diary/2026/b.md", "diary/2026/a.md", "diary/gone.md"):
        store.put(SPACE, path, path.encode())
    store.delete(SPACE, "diary/gone.md")
    page = store.list(SPACE, prefix="diary/")
    assert [m.path for m in page.documents] == ["diary/2026/a.md", "diary/2026/b.md"]
    assert page.total == 2
    payload = page.documents[0].to_payload()
    assert set(payload) == {
        "path",
        "sha256",
        "size",
        "media_type",
        "visibility",
        "version",
        "updated_at",
    }
    second = store.list(SPACE, limit=1, offset=1)
    assert [m.path for m in second.documents] == ["diary/2026/b.md"]
    assert second.total == 3


def test_migration_grants_the_runtime_role_data_access_only(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psycopg

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import store_migrate

    role = f"rt_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE {role} NOLOGIN")
    monkeypatch.setenv("KNOWLEDGE_STORE_OWNER_DATABASE_URL", database_url)
    assert store_migrate.main(["--runtime-role", role]) == 0
    assert store_migrate.main(["--runtime-role", role]) == 0  # idempotent
    with psycopg.connect(database_url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT DISTINCT privilege_type FROM information_schema.role_table_grants"
            " WHERE grantee = %s AND table_name IN ('store_documents', 'store_versions')",
            (role,),
        ).fetchall()
    assert {r[0] for r in rows} == {"SELECT", "INSERT", "UPDATE", "DELETE"}
    with pytest.raises(SystemExit):
        store_migrate.main(["--runtime-role", "Bad-Role"])
    monkeypatch.delenv("KNOWLEDGE_STORE_OWNER_DATABASE_URL")
    with pytest.raises(SystemExit):
        store_migrate.main(["--runtime-role", role])


def test_a_document_and_a_folder_cannot_share_a_path(store: Store) -> None:
    store.put(SPACE, "notes/a.md", b"a")
    for path in ("notes/a.md/child.md", "notes"):
        with pytest.raises(StoreError) as clash:
            store.put(SPACE, path, b"x")
        assert (clash.value.status, clash.value.code) == (409, "path_conflict")
    # Updating an existing document is not a clash, and a deleted path frees the name.
    assert store.put(SPACE, "notes/a.md", b"a2").meta.version == 2
    store.delete(SPACE, "notes/a.md")
    assert store.put(SPACE, "notes/a.md/child.md", b"x").meta.version == 1


def test_keyset_paging_survives_writes_between_pages(store: Store) -> None:
    for path in ("a.md", "b.md", "c.md", "d.md"):
        store.put(SPACE, path, path.encode())
    first = store.list(SPACE, limit=2)
    assert [m.path for m in first.documents] == ["a.md", "b.md"]
    # A delete before the cursor would make offset=2 skip c.md; `after` does not.
    store.delete(SPACE, "a.md")
    second = store.list(SPACE, limit=2, after=first.documents[-1].path)
    assert [m.path for m in second.documents] == ["c.md", "d.md"]
    assert second.total == 3
    with pytest.raises(StoreError) as bad:
        store.list(SPACE, after="../x")
    assert bad.value.code == "invalid_path"


def test_a_write_locks_its_parent_paths_too(database_url: str) -> None:
    schema = f"t_{uuid.uuid4().hex[:12]}"
    pg = PostgresStore(database_url, spaces=(SPACE,), schema=schema)
    pg.migrate()
    try:
        with pg.pool.connection() as conn, conn.transaction():
            pg._lock_path(conn, SPACE, "a/b/c.md")
            held = conn.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                " AND pid = pg_backend_pid()"
            ).fetchone()
        # a, a/b and a/b/c.md: a writer of `a` or `a/b` waits on the same lock.
        assert held is not None
        assert held[0] == 3
    finally:
        pg.drop_schema()
        pg.close()
