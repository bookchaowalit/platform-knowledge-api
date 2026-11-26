"""SPEC-002 AC-11/AC-12: store_sync.py against the real API handler (in-memory store)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import store_sync  # noqa: E402 - after scripts/ is on sys.path

from document_store import MemoryStore  # noqa: E402
from server import CATALOG_SCHEMA_VERSION, KnowledgeApi  # noqa: E402

SPACE = "knowledge-base"


@pytest.fixture
def api(tmp_path: Path) -> KnowledgeApi:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps({"schema_version": CATALOG_SCHEMA_VERSION, "documents": []}),
        encoding="utf-8",
    )
    return KnowledgeApi(
        catalog, token="", environment="development", store=MemoryStore(spaces=(SPACE,))
    )


def client_for(api: KnowledgeApi) -> store_sync.StoreClient:
    def handle(request: httpx.Request) -> httpx.Response:
        response = api.handle(
            request.method,
            str(request.url.raw_path, "ascii"),
            headers=dict(request.headers),
            body=request.content,
        )
        return httpx.Response(response.status, json=response.payload)

    http = httpx.Client(
        transport=httpx.MockTransport(handle), base_url="https://api.test"
    )
    return store_sync.StoreClient(
        http, read_token=lambda: "r", write_token=lambda: "w", page=2
    )


def test_push_skips_unchanged_files_and_verify_proves_parity(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    source = tmp_path / "kb"
    (source / "notes").mkdir(parents=True)
    (source / "notes" / "a.md").write_text("# A\n", encoding="utf-8")
    (source / "notes" / "b.md").write_text("# B\n", encoding="utf-8")
    (source / "img.png").write_bytes(b"\x89PNG\x00\xff")
    client = client_for(api)
    client.put(SPACE, "notes/a.md", b"# A\n", if_version=0)

    files = store_sync.collect(source, paths=["notes/a.md", "notes/b.md", "img.png"])
    report = store_sync.push(client, SPACE, files)
    assert (report.written, report.unchanged, report.failed) == (2, 1, [])

    parity = store_sync.verify(client, SPACE, files)
    assert (parity.missing, parity.different, parity.extra) == ([], [], [])
    assert parity.matched == 3

    (source / "notes" / "b.md").write_text("# B changed\n", encoding="utf-8")
    changed = store_sync.collect(source, paths=["notes/a.md", "notes/b.md"])
    drift = store_sync.verify(client, SPACE, changed)
    assert (drift.different, drift.extra) == (["notes/b.md"], ["img.png"])
    again = store_sync.push(client, SPACE, changed)
    assert (again.written, again.unchanged) == (1, 1)


def test_collect_skips_paths_the_store_would_refuse(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / ".env").write_text("SECRET=x", encoding="utf-8")
    (root / "ok.md").write_text("ok", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.md").write_text("not ours", encoding="utf-8")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    # A folder inside the root that links to a refused one.
    (root / ".aws").mkdir()
    (root / ".aws" / "config").write_text("[default]", encoding="utf-8")
    (root / "notes").mkdir()
    (root / "notes" / "config").symlink_to(root / ".aws", target_is_directory=True)
    files = store_sync.collect(
        root,
        paths=[
            ".env",
            "ok.md",
            "missing.md",
            "linked/private.md",
            "notes/config/config",
        ],
    )
    assert [f.path for f in files] == ["ok.md"]


def test_pull_writes_each_document_inside_the_target(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    client = client_for(api)
    client.put(SPACE, "diary/2026/11/03/a.md", "บันทึก".encode(), if_version=0)
    client.put(SPACE, "assets/x.png", b"\x89PNG\x00\xff", if_version=0)
    client.put(SPACE, "notes/c.md", b"c", if_version=0)
    # Changed after the listing: the snapshot keeps the listed version.
    listed = client.list_all

    def list_then_change(space: str, prefix: str = "") -> dict:
        result = listed(space, prefix)
        client.put(SPACE, "notes/c.md", b"c changed", if_version=1)
        return result

    client.list_all = list_then_change  # type: ignore[method-assign]
    out = tmp_path / "snapshot"
    count = store_sync.pull(client, SPACE, out)
    assert count == 3
    assert (out / "diary/2026/11/03/a.md").read_text(encoding="utf-8") == "บันทึก"
    assert (out / "assets/x.png").read_bytes() == b"\x89PNG\x00\xff"
    assert (out / "notes/c.md").read_bytes() == b"c"
    # Private content: owner-only directories and files.
    assert (
        out.stat().st_mode & 0o777,
        (out / "diary/2026").stat().st_mode & 0o777,
    ) == (0o700, 0o700)
    assert (out / "notes/c.md").stat().st_mode & 0o777 == 0o600
    with pytest.raises(store_sync.SyncError):
        store_sync.safe_target(out, "../escape.md")
    # A second pull into the same directory is refused, so stale files cannot linger.
    with pytest.raises(store_sync.SyncError, match="new or empty"):
        store_sync.pull(client, SPACE, out)


def test_the_cli_pushes_verifies_and_pulls(
    api: KnowledgeApi,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import subprocess

    source = tmp_path / "repo"
    (source / "diary" / "entries").mkdir(parents=True)
    (source / "a.md").write_text("a", encoding="utf-8")
    (source / "diary" / "entries" / "d.md").write_text("private", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "a.md"], check=True)
    monkeypatch.setattr(store_sync, "connect", lambda: client_for(api))

    root = ["--root", str(source), "--include", "diary/entries"]
    assert store_sync.main(["verify", *root]) == 1
    assert store_sync.main(["push", *root]) == 0
    assert store_sync.main(["verify", *root]) == 0
    assert "matched 2, missing 0, different 0, extra 0" in capsys.readouterr().out
    assert store_sync.main(["pull", "--out", str(tmp_path / "snap")]) == 0
    assert (tmp_path / "snap" / "diary" / "entries" / "d.md").read_text() == "private"
    assert (
        store_sync.main(["push", "--root", str(source), "--include", "../outside"]) == 2
    )
    with pytest.raises(SystemExit):
        store_sync.main(["pull"])


def test_api_errors_are_reported_by_status_and_code_only(api: KnowledgeApi) -> None:
    client = client_for(api)
    with pytest.raises(
        store_sync.SyncError, match=r"^store API answered 404 unknown_space$"
    ):
        client.list_all("no-such-space")

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret detail")

    broken = store_sync.StoreClient(
        httpx.Client(transport=httpx.MockTransport(down), base_url="https://api.test"),
        read_token=lambda: "r",
        write_token=lambda: "w",
    )
    with pytest.raises(store_sync.SyncError, match="could not be reached") as caught:
        broken.get(SPACE, "a.md")
    assert "secret" not in str(caught.value)


def test_list_all_does_not_skip_a_document_when_one_is_deleted_mid_listing(
    api: KnowledgeApi,
) -> None:
    client = client_for(api)  # page size 2
    for path in ("a.md", "b.md", "c.md", "d.md", "e.md"):
        client.put(SPACE, path, path.encode(), if_version=0)
    call = client._call
    store = api.store
    assert store is not None
    deleted: list[str] = []

    def delete_after_first_page(
        method: str, route: str, params: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        result = call(method, route, params, **kwargs)
        if route.endswith("/documents") and not deleted:
            deleted.append(store.delete(SPACE, "a.md").path)
        return result

    client._call = delete_after_first_page  # type: ignore[assignment]
    assert sorted(client.list_all(SPACE)) == ["a.md", "b.md", "c.md", "d.md", "e.md"]


def test_a_listing_that_straddles_a_file_folder_swap_is_taken_again(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    client = client_for(api)
    client.put(SPACE, "a", b"file", if_version=0)
    listings = [
        {"a": {"version": 1}, "a/b": {"version": 1}},
        {"a": {"version": 1}},
    ]
    client.list_all = lambda space, prefix="": listings.pop(0)  # type: ignore[method-assign]
    assert store_sync.pull(client, SPACE, tmp_path / "snap") == 1
    assert (tmp_path / "snap" / "a").read_bytes() == b"file"
    assert store_sync.clashes(["x", "x/y", "xy", "z/w"]) == ["x"]
    client.list_all = lambda space, prefix="": {"a": {}, "a/b": {}}  # type: ignore[method-assign]
    with pytest.raises(store_sync.SyncError, match="kept changing"):
        store_sync.pull(client, SPACE, tmp_path / "snap2")


def test_push_with_prune_mirrors_deletes_and_renames(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    client = client_for(api)
    for path in ("keep.md", "gone.md", "a"):
        client.put(SPACE, path, path.encode(), if_version=0)
    source = tmp_path / "docs"
    (source / "a").mkdir(parents=True)
    (source / "keep.md").write_bytes(b"keep.md")
    # `a` became a folder: the old file must go before a/b.md can be written.
    (source / "a" / "b.md").write_bytes(b"b")
    files = store_sync.collect(source, paths=["keep.md", "a/b.md"])
    plain = store_sync.push(client, SPACE, files)
    assert plain.failed == ["a/b.md"]  # without prune the old file blocks it
    report = store_sync.push(client, SPACE, files, prune=True)
    assert (report.deleted, report.written, report.unchanged, report.failed) == (
        2,
        1,
        1,
        [],
    )
    parity = store_sync.verify(client, SPACE, files)
    assert (parity.missing, parity.different, parity.extra) == ([], [], [])
    # Deletes keep history: the old file is still readable at its version.
    store = api.store
    assert store is not None
    assert [v.deleted for v in store.history(SPACE, "gone.md")] == [False, True]


def test_push_refuses_a_path_swapped_for_a_symlink_after_collect(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    source = tmp_path / "kb"
    (source / "notes").mkdir(parents=True)
    (source / "notes" / "a.md").write_text("# A\n", encoding="utf-8")
    (source / "notes" / "b.md").write_text("# B\n", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOKEN=hunter2\n", encoding="utf-8")
    files = store_sync.collect(source, paths=["notes/a.md", "notes/b.md"])

    (source / "notes" / "a.md").unlink()
    (source / "notes" / "a.md").symlink_to(secret)
    (source / "notes" / "b.md").write_text("# B changed\n", encoding="utf-8")
    client = client_for(api)
    report = store_sync.push(client, SPACE, files)

    assert (report.written, report.failed) == (0, ["notes/a.md", "notes/b.md"])
    assert client.list_all(SPACE) == {}


def test_push_keeps_going_when_a_file_vanishes_after_collect(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    source = tmp_path / "kb"
    source.mkdir()
    (source / "a.md").write_text("# A\n", encoding="utf-8")
    (source / "b.md").write_text("# B\n", encoding="utf-8")
    files = store_sync.collect(source, paths=["a.md", "b.md"])
    (source / "a.md").unlink()
    report = store_sync.push(client_for(api), SPACE, files)
    assert (report.written, report.failed) == (1, ["a.md"])


def test_pull_refuses_paths_that_alias_one_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"

    class Aliasing:
        """On a case-insensitive filesystem A.md and a.md are one file; here writing the first
        document also creates the second path, which is what that aliasing looks like."""

        def get(self, space: str, path: str, version: int | None = None) -> bytes:
            (out / "b.md").write_bytes(b"first")
            return path.encode()

    listing = {
        "a.md": {"version": 1, "sha256": "x"},
        "b.md": {"version": 1, "sha256": "y"},
    }
    monkeypatch.setattr(store_sync, "consistent_listing", lambda client, space: listing)
    with pytest.raises(store_sync.SyncError, match="collides"):
        store_sync.pull(Aliasing(), SPACE, out)  # type: ignore[arg-type]
    assert (out / "b.md").read_bytes() == b"first"


def limited_client(
    api: KnowledgeApi, *, limit_first: int, retry_after: str | None
) -> tuple[store_sync.StoreClient, list[float], list[str]]:
    """A client whose transport answers 429 for the first `limit_first` requests."""
    waits: list[float] = []
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        if len(seen) <= limit_first:
            headers = {} if retry_after is None else {"Retry-After": retry_after}
            return httpx.Response(
                429, headers=headers, json={"error": {"code": "rate_limited"}}
            )
        response = api.handle(
            request.method,
            str(request.url.raw_path, "ascii"),
            headers=dict(request.headers),
            body=request.content,
        )
        return httpx.Response(response.status, json=response.payload)

    http = httpx.Client(
        transport=httpx.MockTransport(handle), base_url="https://api.test"
    )
    client = store_sync.StoreClient(
        http, read_token=lambda: "r", write_token=lambda: "w", sleep=waits.append
    )
    return client, waits, seen


def test_rate_limited_writes_wait_for_retry_after_and_succeed(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    (tmp_path / "a.md").write_text("# A\n", encoding="utf-8")
    client, waits, _ = limited_client(api, limit_first=2, retry_after="3")
    report = store_sync.push(
        client, SPACE, store_sync.collect(tmp_path, paths=["a.md"])
    )
    assert report.failed == []
    assert report.written == 1
    assert waits == [3.0, 3.0]
    assert client_for(api).get(SPACE, "a.md") == b"# A\n"


def test_rate_limit_backoff_without_header_is_bounded() -> None:
    response = httpx.Response(429)
    assert store_sync.retry_wait(response, 0) == 1.0
    assert store_sync.retry_wait(response, 3) == 8.0
    assert store_sync.retry_wait(response, 10) == store_sync.MAX_RETRY_WAIT
    huge = httpx.Response(429, headers={"Retry-After": "3600"})
    assert store_sync.retry_wait(huge, 0) == store_sync.MAX_RETRY_WAIT


def test_a_rate_limit_that_never_lifts_is_reported(api: KnowledgeApi) -> None:
    client, waits, _ = limited_client(api, limit_first=10_000, retry_after="1")
    with pytest.raises(
        store_sync.SyncError, match=r"^store API answered 429 rate_limited$"
    ):
        client.list_all(SPACE)
    assert len(waits) == store_sync.MAX_RATE_RETRIES


def test_pull_reuses_unchanged_files_from_an_earlier_snapshot(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    client = client_for(api)
    client.put(SPACE, "a.md", b"same", if_version=0)
    client.put(SPACE, "b.md", b"old", if_version=0)
    first = tmp_path / "first"
    assert store_sync.pull(client, SPACE, first) == 2
    client.put(SPACE, "b.md", b"new", if_version=1)
    client.put(SPACE, "c.md", b"added", if_version=0)
    (first / "a.md").chmod(0o600)

    fetched: list[str] = []
    get = client.get

    def counting_get(space: str, path: str, version: int | None = None) -> bytes:
        fetched.append(path)
        return get(space, path, version=version)

    client.get = counting_get  # type: ignore[method-assign]
    second = tmp_path / "second"
    assert store_sync.pull(client, SPACE, second, reuse=first) == 3
    # Only changed or new documents come from the store; a.md is copied (sha256 matched).
    assert sorted(fetched) == ["b.md", "c.md"]
    assert (second / "a.md").read_bytes() == b"same"
    assert (second / "b.md").read_bytes() == b"new"
    assert (second / "c.md").read_bytes() == b"added"


def test_pull_reuse_never_trusts_a_tampered_earlier_file(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    client = client_for(api)
    client.put(SPACE, "a.md", b"truth", if_version=0)
    first = tmp_path / "first"
    store_sync.pull(client, SPACE, first)
    (first / "a.md").write_bytes(b"edited locally")
    second = tmp_path / "second"
    store_sync.pull(client, SPACE, second, reuse=first)
    assert (second / "a.md").read_bytes() == b"truth"


def test_pull_reuse_skips_symlinks_loops_and_oversized_files(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    client = client_for(api)
    client.put(SPACE, "a.md", b"truth", if_version=0)
    client.put(SPACE, "b.md", b"bee", if_version=0)
    client.put(SPACE, "c.md", b"sea", if_version=0)
    first = tmp_path / "first"
    store_sync.pull(client, SPACE, first)
    (first / "a.md").unlink()
    (first / "a.md").symlink_to(first / "a.md")  # a loop
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"bee")
    (first / "b.md").unlink()
    (first / "b.md").symlink_to(outside)  # same bytes, but a symlink: not trusted
    (first / "c.md").write_bytes(b"x" * 1024)  # larger than the listed size: never read
    second = tmp_path / "second"
    assert store_sync.pull(client, SPACE, second, reuse=first) == 3
    assert (second / "a.md").read_bytes() == b"truth"
    assert (second / "b.md").read_bytes() == b"bee"
    assert (second / "c.md").read_bytes() == b"sea"


def test_pull_reuse_never_reads_a_file_whose_size_differs(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    client = client_for(api)
    client.put(SPACE, "a.md", b"small", if_version=0)
    first = tmp_path / "first"
    store_sync.pull(client, SPACE, first)
    (first / "a.md").write_bytes(b"x" * 4096)
    read: list[Path] = []
    original = Path.read_bytes

    def tracking(self: Path) -> bytes:
        read.append(self)
        return original(self)

    import unittest.mock

    with unittest.mock.patch.object(Path, "read_bytes", tracking):
        store_sync.pull(client, SPACE, tmp_path / "second", reuse=first)
    assert first / "a.md" not in read


def test_pull_reuse_ignores_a_symlinked_reuse_folder(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    client = client_for(api)
    client.put(SPACE, "a.md", b"truth", if_version=0)
    real = tmp_path / "real"
    store_sync.pull(client, SPACE, real)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    fetched: list[str] = []
    get = client.get

    def counting_get(space: str, path: str, version: int | None = None) -> bytes:
        fetched.append(path)
        return get(space, path, version=version)

    client.get = counting_get  # type: ignore[method-assign]
    store_sync.pull(client, SPACE, tmp_path / "second", reuse=link)
    assert fetched == ["a.md"]


# SPEC-001 (architecture-docs) D3: a working copy with a manifest, committed with if_version.


def working_copy(api: KnowledgeApi, tmp_path: Path, docs: dict[str, bytes]) -> Path:
    import subprocess

    client = client_for(api)
    for path, body in docs.items():
        client.put(SPACE, path, body, if_version=0)
    copy = tmp_path / "copy"
    manifest = tmp_path / "manifest.json"
    assert store_sync.pull(client, SPACE, copy, manifest=manifest) == len(docs)
    subprocess.run(["git", "init", "-q", str(copy)], check=True)
    subprocess.run(["git", "-C", str(copy), "add", "-A", "-f"], check=True)
    return copy


def test_pull_records_versions_and_commit_writes_only_changes(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    copy = working_copy(api, tmp_path, {"a.md": b"a", "b.md": b"b", "c.md": b"c"})
    manifest = tmp_path / "manifest.json"
    recorded = json.loads(manifest.read_text())
    assert recorded["space"] == SPACE
    assert recorded["documents"]["a.md"]["version"] == 1
    assert oct(manifest.stat().st_mode & 0o777) == "0o600"

    (copy / "a.md").write_bytes(b"a2")
    (copy / "c.md").unlink()
    (copy / "new").mkdir()
    (copy / "new" / "d.md").write_bytes(b"d")
    (copy / ".gitignore").write_text("*.swp\n")
    (copy / "junk.swp").write_bytes(b"editor")
    client = client_for(api)

    dry = store_sync.commit(client, SPACE, copy, manifest, dry_run=True)
    assert (dry.changed, dry.added, dry.deleted) == (
        ["a.md"],
        [".gitignore", "new/d.md"],
        ["c.md"],
    )
    assert dry.written == []
    assert client.list_all(SPACE)["a.md"]["version"] == 1

    plan = store_sync.commit(client, SPACE, copy, manifest)
    assert sorted(plan.written) == [".gitignore", "a.md", "c.md", "new/d.md"]
    remote = client.list_all(SPACE)
    assert remote["a.md"]["version"] == 2
    assert remote["b.md"]["version"] == 1
    assert "c.md" not in remote
    assert "junk.swp" not in remote
    # The manifest follows: a second commit has nothing to do.
    again = store_sync.commit(client, SPACE, copy, manifest)
    assert again.touched == []
    assert again.written == []


def test_commit_refuses_everything_when_one_document_changed_meanwhile(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    copy = working_copy(api, tmp_path, {"a.md": b"a", "b.md": b"b"})
    manifest = tmp_path / "manifest.json"
    client = client_for(api)
    client.put(SPACE, "b.md", b"someone else", if_version=1)  # a concurrent writer

    (copy / "a.md").write_bytes(b"mine")
    (copy / "b.md").write_bytes(b"mine too")
    plan = store_sync.commit(client, SPACE, copy, manifest)
    assert plan.conflicts == ["b.md"]
    assert plan.written == []
    assert client.get(SPACE, "a.md") == b"a"  # nothing written, not even the clean one
    assert client.get(SPACE, "b.md") == b"someone else"


def test_a_write_that_lands_between_check_and_put_is_refused_by_if_version(
    api: KnowledgeApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = working_copy(api, tmp_path, {"a.md": b"a"})
    manifest = tmp_path / "manifest.json"
    client = client_for(api)
    listed = client.list_all

    def list_then_race(space: str, prefix: str = "") -> dict:
        found = listed(space, prefix)
        client_for(api).put(SPACE, "a.md", b"raced", if_version=1)
        return found

    monkeypatch.setattr(client, "list_all", list_then_race)
    (copy / "a.md").write_bytes(b"mine")
    plan = store_sync.commit(client, SPACE, copy, manifest)
    assert plan.conflicts == ["a.md"]
    assert plan.written == []
    assert client.get(SPACE, "a.md") == b"raced"
    assert store_sync.report_commit(plan, dry_run=False) == 1


def test_commit_refuses_oversized_and_refused_paths_before_writing(
    api: KnowledgeApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = working_copy(api, tmp_path, {"a.md": b"a"})
    manifest = tmp_path / "manifest.json"
    client = client_for(api)
    monkeypatch.setattr(store_sync, "MAX_DOCUMENT_BYTES", 4)
    (copy / "a.md").write_bytes(b"small")  # 5 bytes > the patched limit
    (copy / "b.md").write_bytes(b"ok")
    plan = store_sync.commit(client, SPACE, copy, manifest)
    assert plan.too_large == ["a.md"]
    assert plan.written == []
    assert "b.md" not in client.list_all(SPACE)

    monkeypatch.setattr(store_sync, "MAX_DOCUMENT_BYTES", 10 * 1024 * 1024)
    (copy / "secret.pem").write_bytes(b"key")
    plan = store_sync.commit(client, SPACE, copy, manifest)
    assert plan.refused == ["secret.pem"]
    assert plan.written == []
    # A refused file is not mistaken for a deleted document either.
    assert "a.md" not in plan.deleted


def test_commit_needs_a_manifest_of_the_same_space(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    copy = working_copy(api, tmp_path, {"a.md": b"a"})
    client = client_for(api)
    with pytest.raises(store_sync.SyncError, match="manifest"):
        store_sync.commit(client, "other", copy, tmp_path / "manifest.json")
    with pytest.raises(store_sync.SyncError, match="pull a working copy"):
        store_sync.commit(client, SPACE, copy, tmp_path / "missing.json")


def test_revert_writes_an_old_version_back_as_the_newest(api: KnowledgeApi) -> None:
    client = client_for(api)
    client.put(SPACE, "a.md", b"one", if_version=0)
    client.put(SPACE, "a.md", b"two", if_version=1)
    assert store_sync.revert(client, SPACE, "a.md", 1) == 3
    assert client.get(SPACE, "a.md") == b"one"
    client.delete(SPACE, "a.md", if_version=3)
    assert (
        store_sync.revert(client, SPACE, "a.md", 2) == 5
    )  # a deleted document comes back
    assert client.get(SPACE, "a.md") == b"two"


def test_the_cli_commits_and_reverts(
    api: KnowledgeApi,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    copy = working_copy(api, tmp_path, {"a.md": b"a"})
    monkeypatch.setattr(store_sync, "connect", lambda: client_for(api))
    args = [
        "commit",
        "--root",
        str(copy),
        "--manifest",
        str(tmp_path / "manifest.json"),
    ]
    assert store_sync.main(args) == 0
    assert "nothing to commit" in capsys.readouterr().out
    (copy / "a.md").write_bytes(b"a2")
    assert store_sync.main([*args, "--dry-run"]) == 0
    assert "dry run: 1 documents would be written" in capsys.readouterr().out
    assert store_sync.main(args) == 0
    assert "written 1 of 1" in capsys.readouterr().out
    assert store_sync.main(["revert", "--path", "a.md", "--version", "1"]) == 0
    assert "as version 3" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        store_sync.main(["commit", "--root", str(copy)])


def test_commit_refuses_a_root_other_than_the_pulled_working_copy(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    copy = working_copy(api, tmp_path, {"a.md": b"a", "sub/b.md": b"b"})
    client = client_for(api)
    with pytest.raises(store_sync.SyncError, match="not the working copy"):
        # From sub/ every document outside it would look deleted.
        store_sync.commit(client, SPACE, copy / "sub", tmp_path / "manifest.json")
    assert sorted(client.list_all(SPACE)) == ["a.md", "sub/b.md"]


def test_the_manifest_never_lives_among_the_documents(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    client = client_for(api)
    client.put(SPACE, "a.md", b"a", if_version=0)
    copy = tmp_path / "copy"
    with pytest.raises(store_sync.SyncError, match="outside the working copy"):
        store_sync.pull(client, SPACE, copy, manifest=copy / "manifest.json")
    assert store_sync.pull(client, SPACE, copy, manifest=copy / ".git" / "m.json") == 1


def test_an_added_path_that_clashes_with_a_remaining_document_writes_nothing(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    copy = working_copy(api, tmp_path, {"docs/a.md": b"a"})
    client = client_for(api)
    client.put(SPACE, "docs/b.md", b"someone else's", if_version=0)  # after the pull
    (copy / "docs" / "a.md").unlink()
    (copy / "docs").rmdir()
    (copy / "docs").write_bytes(b"now a file")
    plan = store_sync.commit(client, SPACE, copy, tmp_path / "manifest.json")
    assert plan.conflicts == ["docs"]
    assert plan.written == []
    assert client.get(SPACE, "docs/a.md") == b"a"  # the delete never ran


def test_an_oversized_file_is_reported_without_being_read(
    api: KnowledgeApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = working_copy(api, tmp_path, {"a.md": b"a"})
    monkeypatch.setattr(store_sync, "MAX_DOCUMENT_BYTES", 4)
    (copy / "big.bin").write_bytes(b"0123456789")
    read = Path.read_bytes

    def guarded(self: Path) -> bytes:
        assert self.name != "big.bin", "an oversized file was read"
        return read(self)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    plan = store_sync.commit(client_for(api), SPACE, copy, tmp_path / "manifest.json")
    assert plan.too_large == ["big.bin"]
    assert plan.written == []


def test_a_frozen_set_writes_only_those_files_with_those_bytes(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    import hashlib

    copy = working_copy(api, tmp_path, {"a.md": b"a", "b.md": b"b"})
    client = client_for(api)
    manifest = tmp_path / "manifest.json"
    (copy / "a.md").write_bytes(b"reviewed")
    frozen = {
        "a.md": hashlib.sha256(b"reviewed").hexdigest(),
        "b.md": hashlib.sha256(b"b").hexdigest(),
    }
    (copy / "a.md").write_bytes(b"edited after the review")
    (copy / "extra.md").write_bytes(b"never reviewed")
    plan = store_sync.commit(client, SPACE, copy, manifest, expected=frozen)
    assert plan.drifted == ["a.md"]
    assert plan.written == []
    (copy / "a.md").write_bytes(b"reviewed")
    plan = store_sync.commit(client, SPACE, copy, manifest, expected=frozen)
    assert plan.written == ["a.md"]
    assert "extra.md" not in client.list_all(SPACE)  # outside the frozen set


def test_commit_needs_the_root_to_be_its_own_git_repository(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    client = client_for(api)
    client.put(SPACE, "a.md", b"a", if_version=0)
    copy = tmp_path / "plain"
    store_sync.pull(client, SPACE, copy, manifest=tmp_path / "m.json")  # no git init
    with pytest.raises(store_sync.SyncError, match="top of its own git repository"):
        store_sync.commit(client, SPACE, copy, tmp_path / "m.json")
    assert "a.md" in client.list_all(SPACE)


def test_the_first_refused_write_stops_the_rest(
    api: KnowledgeApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = working_copy(api, tmp_path, {"a.md": b"a", "b.md": b"b"})
    client = client_for(api)
    listed = client.list_all

    def list_then_race(space: str, prefix: str = "") -> dict:
        found = listed(space, prefix)
        client_for(api).put(SPACE, "a.md", b"raced", if_version=1)
        return found

    monkeypatch.setattr(client, "list_all", list_then_race)
    (copy / "a.md").write_bytes(b"mine")
    (copy / "b.md").write_bytes(b"mine too")
    plan = store_sync.commit(client, SPACE, copy, tmp_path / "manifest.json")
    assert plan.conflicts == ["a.md"]
    assert plan.written == []
    assert client.get(SPACE, "b.md") == b"b"  # never reached


def test_a_manifest_from_another_store_is_refused(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    copy = working_copy(api, tmp_path, {"a.md": b"a"})
    other = client_for(api)
    other.http.base_url = "https://staging.test"
    (copy / "a.md").write_bytes(b"x")
    with pytest.raises(store_sync.SyncError, match="another store"):
        store_sync.commit(other, SPACE, copy, tmp_path / "manifest.json")


def test_revert_keeps_the_media_type_of_the_restored_version(
    api: KnowledgeApi,
) -> None:
    client = client_for(api)
    client.put(SPACE, "notes", b"# one", if_version=0, media_type="text/markdown")
    client.put(SPACE, "notes", b"# two", if_version=1, media_type="text/markdown")
    store_sync.revert(client, SPACE, "notes", 1)
    _body, meta = client.get_document(SPACE, "notes")
    assert meta["media_type"] == "text/markdown"
