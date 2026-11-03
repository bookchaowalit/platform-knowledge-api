"""SPEC-002 AC-11/AC-12: store_sync.py against the real API handler (in-memory store)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

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
    (tmp_path / ".env").write_text("SECRET=x", encoding="utf-8")
    (tmp_path / "ok.md").write_text("ok", encoding="utf-8")
    files = store_sync.collect(tmp_path, paths=[".env", "ok.md", "missing.md"])
    assert [f.path for f in files] == ["ok.md"]


def test_pull_writes_each_document_inside_the_target(
    api: KnowledgeApi, tmp_path: Path
) -> None:
    client = client_for(api)
    client.put(SPACE, "diary/2026/11/03/a.md", "บันทึก".encode(), if_version=0)
    client.put(SPACE, "assets/x.png", b"\x89PNG\x00\xff", if_version=0)
    client.put(SPACE, "notes/c.md", b"c", if_version=0)
    out = tmp_path / "snapshot"
    count = store_sync.pull(client, SPACE, out)
    assert count == 3
    assert (out / "diary/2026/11/03/a.md").read_text(encoding="utf-8") == "บันทึก"
    assert (out / "assets/x.png").read_bytes() == b"\x89PNG\x00\xff"
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
