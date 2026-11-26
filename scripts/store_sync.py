#!/usr/bin/env python3
"""Move documents between a directory and the hosted document store (SPEC-002).

    uv run python scripts/store_sync.py push   --space knowledge-base --root DIR [--include diary/entries]
    uv run python scripts/store_sync.py verify --space knowledge-base --root DIR [--include diary/entries]
    uv run python scripts/store_sync.py pull   --space knowledge-base --out DIR [--manifest FILE]
    uv run python scripts/store_sync.py commit --space architecture-docs --root DIR --manifest FILE [--dry-run]
    uv run python scripts/store_sync.py revert --space architecture-docs --path PATH --version N

``push`` uploads Git-tracked files of DIR (plus every file under each ``--include``
directory, for git-ignored content such as private diary entries) and skips files whose
sha256 already matches. ``verify`` proves parity: missing, different and extra paths.
``pull`` writes a read-only snapshot, for example for the catalog generator; with
``--manifest`` it also records each document's version, which makes the snapshot a working copy.
``commit`` writes only what changed in such a working copy (Git-visible files of DIR) since that
manifest, each with the recorded version as ``if_version``: a document someone else changed in the
meantime is refused, never overwritten, and nothing is written when any document conflicts or is
too large. ``revert`` writes an old version of one document back as a new version.

Tokens are minted like the knowledge MCP's (``EMPIRE_KNOWLEDGE_API``,
``SOLO_WEB_PUBLIC_ORIGIN`` and the owner key files); nothing secret is printed, and no
document body or path is sent anywhere but the API host.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from document_store import (  # noqa: E402 - after the repo root is on sys.path
    MAX_DOCUMENT_BYTES,
    StoreError,
    check_path,
)


class SyncError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class LocalFile:
    path: str
    source: Path
    sha256: str


@dataclass
class PushReport:
    written: int = 0
    unchanged: int = 0
    deleted: int = 0
    failed: list[str] = field(default_factory=list)


@dataclass
class Parity:
    matched: int = 0
    missing: list[str] = field(default_factory=list)
    different: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)


# The edge gateway answers 429 once a subject passes its per-minute budget; nothing was
# processed, so the same request is sent again after the wait the gateway asks for.
RATE_LIMITED = 429
MAX_RATE_RETRIES = 8
MAX_RETRY_WAIT = 60.0


def retry_wait(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait: the gateway's Retry-After when given, else exponential backoff."""
    header = response.headers.get("Retry-After", "")
    try:
        wait = float(header)
    except ValueError:
        wait = float(2**attempt)
    return min(max(wait, 0.0), MAX_RETRY_WAIT)


class StoreClient:
    """The store API over HTTP, with a fresh token per call; error bodies are never echoed.

    A 429 from the gateway is retried after its Retry-After (bounded), so a large push
    slows down to the rate limit instead of failing part-way.
    """

    def __init__(
        self,
        http: httpx.Client,
        *,
        read_token: Callable[[], str],
        write_token: Callable[[], str],
        page: int = 1000,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.http, self.read_token, self.write_token, self.page = (
            http,
            read_token,
            write_token,
            page,
        )
        self.sleep = sleep

    def _call(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        *,
        write: bool = False,
        body: Any = None,
    ) -> dict[str, Any]:
        for attempt in range(MAX_RATE_RETRIES + 1):
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
            if response.status_code != RATE_LIMITED or attempt == MAX_RATE_RETRIES:
                break
            self.sleep(retry_wait(response, attempt))
        if response.status_code >= 400:
            code = ""
            with contextlib.suppress(ValueError, KeyError, TypeError):
                code = str(response.json()["error"]["code"])
            raise SyncError(
                f"store API answered {response.status_code} {code}".strip(),
                status=response.status_code,
            )
        result: dict[str, Any] = response.json()
        return result

    def list_all(self, space: str, prefix: str = "") -> dict[str, dict[str, Any]]:
        # Keyset paging (`after` = last path seen): a write between pages can neither
        # skip nor repeat a document, as an offset would.
        found: dict[str, dict[str, Any]] = {}
        after = ""
        while True:
            page = self._call(
                "GET",
                "/api/v1/store/documents",
                {"space": space, "prefix": prefix, "limit": self.page, "after": after},
            )
            for meta in page["documents"]:
                found[meta["path"]] = meta
            if len(page["documents"]) < self.page:
                return found
            after = page["documents"][-1]["path"]

    def get(self, space: str, path: str, *, version: int | None = None) -> bytes:
        params: dict[str, Any] = {"space": space, "path": path}
        if version is not None:
            params["version"] = version
        document = self._call("GET", "/api/v1/store/document", params)["document"]
        if document["encoding"] == "utf-8":
            return str(document["content"]).encode("utf-8")
        return base64.b64decode(document["content_base64"])

    def delete(self, space: str, path: str, *, if_version: int) -> dict[str, Any]:
        return self._call(
            "DELETE",
            "/api/v1/store/document",
            {"space": space, "path": path, "if_version": if_version},
            write=True,
        )

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
        # Refuse symlinks anywhere on the way, not just the last component: a symlinked
        # folder (notes/config -> .aws) would otherwise upload a refused path under a
        # harmless name. Without any symlink the resolved path is exactly root/path.
        if not source.is_file() or source.resolve() != root.resolve() / path:
            continue
        source = (
            root.resolve() / path
        )  # symlink-free, so upload can re-check it is still exact
        files[path] = LocalFile(
            path, source, hashlib.sha256(source.read_bytes()).hexdigest()
        )
    return [files[p] for p in sorted(files)]


def push(
    client: StoreClient, space: str, files: list[LocalFile], *, prune: bool = False
) -> PushReport:
    """Write changed files. With prune, the space mirrors the source: documents that are
    no longer there are deleted first (history keeps them), so a file that became a
    folder frees its name before the new paths are written."""
    remote = client.list_all(space)
    report = PushReport()
    if prune:
        keep = {local.path for local in files}
        for path in sorted(set(remote) - keep):
            try:
                client.delete(space, path, if_version=int(remote[path]["version"]))
                report.deleted += 1
                del remote[path]
            except SyncError:
                report.failed.append(path)
    for local in files:
        meta = remote.get(local.path)
        if meta and meta["sha256"] == local.sha256:
            report.unchanged += 1
            continue
        # Re-check at upload time: a path swapped for a symlink after collect() must not leak
        # another file's bytes, so the read must stay confined and match the collected hash.
        try:
            body = (
                local.source.read_bytes()
                if local.source.resolve() == local.source
                else None
            )
        except (
            OSError
        ):  # removed or unreadable since collect(): this file fails, the rest go on
            body = None
        if body is None or hashlib.sha256(body).hexdigest() != local.sha256:
            report.failed.append(local.path)
            continue
        try:
            client.put(
                space,
                local.path,
                body,
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


def clashes(paths: Iterable[str]) -> list[str]:
    """Paths that are also another path's folder (a and a/b): never true at one instant,
    so a listing that holds both straddled a delete-and-recreate."""
    names = set(paths)
    return sorted(
        p for p in names if any(f"{p}/" == q[: len(p) + 1] for q in names if q != p)
    )


def consistent_listing(
    client: StoreClient, space: str, attempts: int = 3
) -> dict[str, dict[str, Any]]:
    for _ in range(attempts):
        remote = client.list_all(space)
        if not clashes(remote):
            return remote
    raise SyncError("the store kept changing during listing; try again")


def _reusable(
    reuse: Path | None, path: str, sha256: str | None, size: int | None = None
) -> bytes | None:
    """The bytes of ``path`` in an earlier snapshot when their SHA-256 matches the listing.

    Anything unusual means "fetch it": a symlink anywhere on the path (checked before any
    resolve, so a loop never raises), a size other than the listed one (checked before reading,
    so a huge file is never loaded), or any error.
    """
    if reuse is None or not sha256:
        return None
    try:
        check_path(path)
        if reuse.is_symlink():
            return None
        candidate = reuse
        for part in Path(path).parts:
            candidate = candidate / part
            if candidate.is_symlink():
                return None
        info = candidate.lstat()
        if not candidate.is_file() or (size is not None and info.st_size != size):
            return None
        # Never more than the listed size (+1 to notice growth after the lstat).
        limit = size if size is not None else info.st_size
        with candidate.open("rb") as handle:
            data = handle.read(limit + 1)
        if len(data) != limit:
            return None
    except (OSError, RuntimeError, StoreError, SyncError):
        return None
    return data if hashlib.sha256(data).hexdigest() == sha256 else None


def pull(
    client: StoreClient,
    space: str,
    out: Path,
    *,
    reuse: Path | None = None,
    manifest: Path | None = None,
) -> int:
    """Write a fresh snapshot. out must be new or empty, so no file deleted in the
    store can linger from an earlier pull. With ``reuse`` (an earlier snapshot), a document
    whose bytes there still match the listed SHA-256 is copied instead of fetched."""
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise SyncError("pull needs a new or empty --out directory")
    # The snapshot holds private documents: owner-only directories and files.
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    out.chmod(0o700)
    remote = consistent_listing(client, space)
    for path in sorted(remote):
        target = safe_target(out, path)
        for parent in reversed(target.relative_to(out.resolve()).parents[:-1]):
            (out.resolve() / parent).mkdir(mode=0o700, exist_ok=True)
        # O_EXCL: the folder is new, so an existing file means two store paths alias one file
        # (A.md and a.md on a case-insensitive filesystem); fail instead of overwriting.
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise SyncError(
                f"{path} collides with another document on this filesystem"
            ) from None
        with os.fdopen(fd, "wb") as handle:
            # The version from the listing: the snapshot is one consistent point in time
            # even if documents change while it is being pulled.
            listed_size = remote[path].get("size")
            data = _reusable(
                reuse,
                path,
                remote[path].get("sha256"),
                int(listed_size) if listed_size is not None else None,
            )
            if data is None:
                data = client.get(space, path, version=int(remote[path]["version"]))
            handle.write(data)
    if manifest is not None:
        write_manifest(manifest, space, remote)
    return len(remote)


MANIFEST_SCHEMA = 1


def write_manifest(
    file: Path, space: str, documents: dict[str, dict[str, Any]]
) -> None:
    """Record the version and SHA-256 each document had when it was pulled (owner-only file,
    replaced atomically so a crash never leaves half a manifest)."""
    payload = {
        "schema": MANIFEST_SCHEMA,
        "space": space,
        "documents": {
            path: {"version": int(meta["version"]), "sha256": str(meta["sha256"])}
            for path, meta in sorted(documents.items())
        },
    }
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(f".{file.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, file)


def read_manifest(file: Path, space: str) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SyncError(
            f"no readable manifest at {file}; pull a working copy first"
        ) from None
    if payload.get("schema") != MANIFEST_SCHEMA or payload.get("space") != space:
        raise SyncError(
            f"the manifest is not a schema {MANIFEST_SCHEMA} manifest of space {space}"
        )
    documents: dict[str, dict[str, Any]] = payload["documents"]
    return documents


def worktree_files(root: Path) -> list[str]:
    """Files git would track in a working copy: tracked (also when deleted) plus untracked
    files that are not ignored, so editor and build leftovers stay out of the store."""
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        check=True,
        capture_output=True,
    )
    return sorted({p for p in result.stdout.decode("utf-8").split("\0") if p})


@dataclass
class CommitPlan:
    changed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    refused: list[str] = field(
        default_factory=list
    )  # local files the store would refuse
    too_large: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def touched(self) -> list[str]:
        return sorted([*self.changed, *self.added, *self.deleted])

    @property
    def blocked(self) -> bool:
        return bool(self.refused or self.too_large or self.conflicts)


def plan_commit(
    root: Path, candidates: Iterable[str], base: dict[str, dict[str, Any]]
) -> tuple[CommitPlan, dict[str, LocalFile]]:
    candidates = list(candidates)
    files = {local.path: local for local in collect(root, paths=candidates)}
    plan = CommitPlan()
    for path in candidates:
        source = root / path
        if path not in files and (source.is_file() or source.is_symlink()):
            plan.refused.append(path)
    for path, local in files.items():
        meta = base.get(path)
        if meta is None:
            plan.added.append(path)
        elif meta["sha256"] != local.sha256:
            plan.changed.append(path)
        if (
            meta is None or meta["sha256"] != local.sha256
        ) and local.source.stat().st_size > MAX_DOCUMENT_BYTES:
            plan.too_large.append(path)
    plan.deleted = sorted(set(base) - set(files) - set(plan.refused))
    plan.changed.sort()
    plan.added.sort()
    plan.refused.sort()
    plan.too_large.sort()
    return plan, files


def commit(
    client: StoreClient,
    space: str,
    root: Path,
    manifest: Path,
    *,
    candidates: Iterable[str] | None = None,
    dry_run: bool = False,
) -> CommitPlan:
    """Write a working copy's changes since its manifest (SPEC-001 D3, R3 and R5).

    Nothing is written when a local file would be refused or is over the size limit, or when
    any touched document's current version differs from the manifest (someone else wrote it).
    Each write still carries ``if_version``, so a change that lands between the check and the
    write is refused too. The manifest follows every successful write."""
    base = read_manifest(manifest, space)
    root = root.resolve()
    plan, files = plan_commit(
        root, worktree_files(root) if candidates is None else candidates, base
    )
    if not plan.touched or plan.blocked:
        return plan
    remote = client.list_all(space)
    for path in plan.touched:
        current = remote.get(path)
        expected = base.get(path)
        current_version = int(current["version"]) if current else 0
        if current_version != (int(expected["version"]) if expected else 0):
            plan.conflicts.append(path)
    if plan.conflicts or dry_run:
        return plan
    try:
        # Deletes first: a document that became a folder frees its name for the new paths.
        for path in plan.deleted:
            try:
                client.delete(space, path, if_version=int(base[path]["version"]))
                del base[path]
                plan.written.append(path)
            except SyncError as exc:
                (plan.conflicts if exc.status == 409 else plan.failed).append(path)
        for path in [*plan.changed, *plan.added]:
            local = files[path]
            try:
                body = local.source.read_bytes()
            except OSError:
                body = None
            if body is None or hashlib.sha256(body).hexdigest() != local.sha256:
                plan.failed.append(path)  # changed while being written; push again
                continue
            expected = base.get(path)
            try:
                result = client.put(
                    space,
                    path,
                    body,
                    if_version=int(expected["version"]) if expected else 0,
                )
            except SyncError as exc:
                (plan.conflicts if exc.status == 409 else plan.failed).append(path)
                continue
            meta = result["document"]
            base[path] = {
                "version": int(meta["version"]),
                "sha256": str(meta["sha256"]),
            }
            plan.written.append(path)
    finally:
        write_manifest(manifest, space, base)
    return plan


def revert(client: StoreClient, space: str, path: str, version: int) -> int:
    """Write version ``version`` of ``path`` back as its newest version; returns that version.
    The store keeps every version, so this replaces ``git revert`` for one document."""
    check_path(path)
    old = client.get(space, path, version=version)
    current = client.list_all(space, prefix=path).get(path)
    result = client.put(
        space, path, old, if_version=int(current["version"]) if current else 0
    )
    return int(result["document"]["version"])


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


def report_commit(plan: CommitPlan, *, dry_run: bool) -> int:
    """Print the plan (paths only, never content). 0 = done or nothing to do, 1 = refused
    before writing (conflict, size, refused path), 3 = some writes failed after others."""
    for label, paths in (
        ("changed", plan.changed),
        ("added", plan.added),
        ("deleted", plan.deleted),
        ("refused path", plan.refused),
        ("too large", plan.too_large),
        ("conflict", plan.conflicts),
        ("failed", plan.failed),
    ):
        for path in paths:
            print(f"  {label}: {path}")
    if not plan.touched and not plan.blocked:
        print("nothing to commit")
        return 0
    if plan.blocked and not plan.written:
        print(f"refused: nothing written ({len(plan.touched)} documents touched)")
        return 1
    if dry_run:
        print(f"dry run: {len(plan.touched)} documents would be written")
        return 0
    print(
        f"written {len(plan.written)} of {len(plan.touched)} "
        f"(changed {len(plan.changed)}, added {len(plan.added)}, deleted {len(plan.deleted)})"
    )
    return 0 if len(plan.written) == len(plan.touched) else 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync documents with the hosted store")
    parser.add_argument(
        "command", choices=["push", "verify", "pull", "commit", "revert"]
    )
    parser.add_argument("--space", default="knowledge-base")
    parser.add_argument("--root", type=Path, help="source directory (push, verify)")
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="also every file under this directory",
    )
    parser.add_argument("--out", type=Path, help="snapshot directory (pull)")
    parser.add_argument(
        "--reuse",
        type=Path,
        help="earlier snapshot: copy documents whose SHA-256 still matches (pull)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="pull: record document versions here; commit: the working copy's manifest",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="commit: report, write nothing"
    )
    parser.add_argument("--path", help="revert: the document")
    parser.add_argument("--version", type=int, help="revert: the version to restore")
    parser.add_argument(
        "--prune",
        action="store_true",
        help="push: delete store documents that are not in the source (a mirror)",
    )
    args = parser.parse_args(argv)
    try:
        client = connect()
        if args.command == "pull":
            if not args.out:
                parser.error("pull needs --out")
            count = pull(
                client, args.space, args.out, reuse=args.reuse, manifest=args.manifest
            )
            print(f"pulled {count} documents")
            return 0
        if args.command == "revert":
            if not args.path or args.version is None:
                parser.error("revert needs --path and --version")
            new = revert(client, args.space, args.path, args.version)
            print(f"reverted {args.path} to version {args.version} as version {new}")
            return 0
        if args.command == "commit":
            if not args.root or not args.manifest:
                parser.error("commit needs --root and --manifest")
            return report_commit(
                commit(
                    client,
                    args.space,
                    args.root,
                    args.manifest,
                    dry_run=args.dry_run,
                ),
                dry_run=args.dry_run,
            )
        if not args.root:
            parser.error(f"{args.command} needs --root")
        root = args.root.resolve()
        files = collect(
            root, paths=[*git_files(root), *included_files(root, args.include)]
        )
        if args.command == "push":
            report = push(client, args.space, files, prune=args.prune)
            print(
                f"written {report.written}, unchanged {report.unchanged}, "
                f"deleted {report.deleted}, failed {len(report.failed)}"
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
