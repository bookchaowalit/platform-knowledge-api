#!/usr/bin/env python3
"""Move documents between a directory and the hosted document store (SPEC-002).

    uv run python scripts/store_sync.py push   --space knowledge-base --root DIR [--include diary/entries]
    uv run python scripts/store_sync.py verify --space knowledge-base --root DIR [--include diary/entries]
    uv run python scripts/store_sync.py pull   --space knowledge-base --out DIR

``push`` uploads Git-tracked files of DIR (plus every file under each ``--include``
directory, for git-ignored content such as private diary entries) and skips files whose
sha256 already matches. ``verify`` proves parity: missing, different and extra paths.
``pull`` writes a read-only snapshot, for example for the catalog generator.

Tokens are minted like the knowledge MCP's (``EMPIRE_KNOWLEDGE_API``,
``SOLO_WEB_PUBLIC_ORIGIN`` and the owner key files); nothing secret is printed, and no
document body or path is sent anywhere but the API host.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import os
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from document_store import StoreError, check_path  # noqa: E402 - after the repo root is on sys.path


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalFile:
    path: str
    source: Path
    sha256: str


@dataclass
class PushReport:
    written: int = 0
    unchanged: int = 0
    failed: list[str] = field(default_factory=list)


@dataclass
class Parity:
    matched: int = 0
    missing: list[str] = field(default_factory=list)
    different: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)


class StoreClient:
    """The store API over HTTP, with a fresh token per call; error bodies are never echoed."""

    def __init__(
        self,
        http: httpx.Client,
        *,
        read_token: Callable[[], str],
        write_token: Callable[[], str],
        page: int = 1000,
    ) -> None:
        self.http, self.read_token, self.write_token, self.page = (
            http,
            read_token,
            write_token,
            page,
        )

    def _call(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        *,
        write: bool = False,
        body: Any = None,
    ) -> dict[str, Any]:
        token = (self.write_token if write else self.read_token)()
        try:
            response = self.http.request(
                method,
                path,
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError:
            raise SyncError("the store API could not be reached") from None
        if response.status_code >= 400:
            code = ""
            with contextlib.suppress(ValueError, KeyError, TypeError):
                code = str(response.json()["error"]["code"])
            raise SyncError(f"store API answered {response.status_code} {code}".strip())
        result: dict[str, Any] = response.json()
        return result

    def list_all(self, space: str, prefix: str = "") -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        offset = 0
        while True:
            page = self._call(
                "GET",
                "/api/v1/store/documents",
                {
                    "space": space,
                    "prefix": prefix,
                    "limit": self.page,
                    "offset": offset,
                },
            )
            for meta in page["documents"]:
                found[meta["path"]] = meta
            offset += len(page["documents"])
            if not page["documents"] or offset >= page["total"]:
                return found

    def get(self, space: str, path: str) -> bytes:
        document = self._call(
            "GET", "/api/v1/store/document", {"space": space, "path": path}
        )["document"]
        if document["encoding"] == "utf-8":
            return str(document["content"]).encode("utf-8")
        return base64.b64decode(document["content_base64"])

    def put(
        self, space: str, path: str, content: bytes, *, if_version: int | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "content_base64": base64.b64encode(content).decode("ascii")
        }
        if if_version is not None:
            body["if_version"] = if_version
        return self._call(
            "PUT",
            "/api/v1/store/document",
            {"space": space, "path": path},
            write=True,
            body=body,
        )


def git_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [p for p in result.stdout.decode("utf-8").split("\0") if p]


def included_files(root: Path, includes: Iterable[str]) -> list[str]:
    paths: list[str] = []
    for include in includes:
        base = (root / include).resolve()
        if not base.is_relative_to(root.resolve()) or not base.is_dir():
            raise SyncError(f"--include {include} is not a directory inside the root")
        paths.extend(
            str(path.relative_to(root.resolve()))
            for path in base.rglob("*")
            if path.is_file()
        )
    return paths


def collect(root: Path, *, paths: Iterable[str]) -> list[LocalFile]:
    """Readable files under root whose path the store accepts, sorted and de-duplicated."""
    files: dict[str, LocalFile] = {}
    for path in paths:
        try:
            check_path(path)
        except StoreError:
            continue
        source = root / path
        # Refuse symlinks anywhere on the way, not just the last component.
        if (
            not source.is_file()
            or source.is_symlink()
            or not source.resolve().is_relative_to(root.resolve())
        ):
            continue
        files[path] = LocalFile(
            path, source, hashlib.sha256(source.read_bytes()).hexdigest()
        )
    return [files[p] for p in sorted(files)]


def push(client: StoreClient, space: str, files: list[LocalFile]) -> PushReport:
    remote = client.list_all(space)
    report = PushReport()
    for local in files:
        meta = remote.get(local.path)
        if meta and meta["sha256"] == local.sha256:
            report.unchanged += 1
            continue
        try:
            client.put(
                space,
                local.path,
                local.source.read_bytes(),
                if_version=int(meta["version"]) if meta else 0,
            )
            report.written += 1
        except SyncError:
            report.failed.append(local.path)
    return report


def verify(client: StoreClient, space: str, files: list[LocalFile]) -> Parity:
    remote = client.list_all(space)
    parity = Parity()
    local_paths = set()
    for local in files:
        local_paths.add(local.path)
        meta = remote.get(local.path)
        if meta is None:
            parity.missing.append(local.path)
        elif meta["sha256"] != local.sha256:
            parity.different.append(local.path)
        else:
            parity.matched += 1
    parity.extra = sorted(set(remote) - local_paths)
    return parity


def safe_target(out: Path, path: str) -> Path:
    try:
        check_path(path)
    except StoreError:
        raise SyncError("the store returned a path that is not safe to write") from None
    target = (out / path).resolve()
    if not target.is_relative_to(out.resolve()):
        raise SyncError("the store returned a path outside the snapshot")
    return target


def pull(client: StoreClient, space: str, out: Path) -> int:
    """Write a fresh snapshot. out must be new or empty, so no file deleted in the
    store can linger from an earlier pull."""
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise SyncError("pull needs a new or empty --out directory")
    # The snapshot holds private documents: owner-only directories and files.
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    out.chmod(0o700)
    remote = client.list_all(space)
    for path in sorted(remote):
        target = safe_target(out, path)
        for parent in reversed(target.relative_to(out.resolve()).parents[:-1]):
            (out.resolve() / parent).mkdir(mode=0o700, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(client.get(space, path))
    return len(remote)


def connect() -> StoreClient:
    import knowledge_mcp as mcp

    try:
        url = mcp.api_url(mcp.URL_KEY)
        mcp.mint()
    except mcp.ConfigError as exc:
        raise SyncError(str(exc)) from None
    http = httpx.Client(base_url=url, timeout=60, follow_redirects=False)
    return StoreClient(
        http,
        read_token=mcp.mint,
        write_token=lambda: mcp.mint(scope=mcp.WRITE_SCOPE),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync documents with the hosted store")
    parser.add_argument("command", choices=["push", "verify", "pull"])
    parser.add_argument("--space", default="knowledge-base")
    parser.add_argument("--root", type=Path, help="source directory (push, verify)")
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="also every file under this directory",
    )
    parser.add_argument("--out", type=Path, help="snapshot directory (pull)")
    args = parser.parse_args(argv)
    try:
        client = connect()
        if args.command == "pull":
            if not args.out:
                parser.error("pull needs --out")
            print(f"pulled {pull(client, args.space, args.out)} documents")
            return 0
        if not args.root:
            parser.error(f"{args.command} needs --root")
        root = args.root.resolve()
        files = collect(
            root, paths=[*git_files(root), *included_files(root, args.include)]
        )
        if args.command == "push":
            report = push(client, args.space, files)
            print(
                f"written {report.written}, unchanged {report.unchanged}, failed {len(report.failed)}"
            )
            for path in report.failed:
                print(f"  failed: {path}")
            return 1 if report.failed else 0
        parity = verify(client, args.space, files)
        print(
            f"matched {parity.matched}, missing {len(parity.missing)}, "
            f"different {len(parity.different)}, extra {len(parity.extra)}"
        )
        for label, paths in (
            ("missing", parity.missing),
            ("different", parity.different),
            ("extra", parity.extra),
        ):
            for path in paths:
                print(f"  {label}: {path}")
        return 0 if not (parity.missing or parity.different or parity.extra) else 1
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
