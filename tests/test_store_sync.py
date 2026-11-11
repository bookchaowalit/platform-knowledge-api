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
