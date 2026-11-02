#!/usr/bin/env python3
"""Refresh the private catalog snapshot, deploy it, and prove the live API serves it.

The catalog is owner-private metadata and never enters git. This is the one
repeatable path from the generator's output to the running service:

1. freshness: ``scripts/document_catalog.py --check`` in architecture-docs
   (only when the source comes from ``SOLO_ARCHITECTURE_DOCS_ROOT``);
2. validate the source and stage it atomically as ``data/document-catalog.json``;
3. ``railway up --ci --no-gitignore`` (uploads the git-ignored snapshot);
4. wait for ``/ready``, then read ``catalog.revision`` with a minted service JWT
   and require it to equal the staged snapshot's SHA-256.

    uv run python scripts/deploy_catalog.py [--source FILE] [--dry-run] [--no-freshness-check]

Environment: ``SOLO_ARCHITECTURE_DOCS_ROOT`` (default source), ``EMPIRE_KNOWLEDGE_API``
(or ``--url``), ``SOLO_WEB_PUBLIC_ORIGIN`` and the MCP key files (see README),
``KNOWLEDGE_API_RAILWAY_PROJECT`` / ``_ENVIRONMENT`` / ``_SERVICE``,
``SOLO_ARCHITECTURE_DOCS_PYTHON`` (the generator's interpreter; default python3 on PATH
outside this repository's virtualenv).
No token, key or catalog row is printed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "data" / "document-catalog.json"
DOCS_CATALOG = Path("data") / "indexes" / "document-catalog.json"

Run = Callable[..., subprocess.CompletedProcess]
Fetch = Callable[..., tuple[int, Any]]


class DeployError(Exception):
    """A safe, printable reason the deploy stopped."""


@dataclass(frozen=True)
class Target:
    project: str
    environment: str
    service: str


@dataclass(frozen=True)
class Staged:
    revision: str
    documents: int


def _load(name: str, path: Path) -> Any:
    """A sibling module by path (scripts/ and the repo root are not packages)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def revision_of(path: Path) -> str:
    # Same formula as the server's catalog.revision (text read as UTF-8, re-encoded).
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def docs_env(environ: Mapping[str, str]) -> tuple[str, dict[str, str]]:
    """The interpreter and environment for architecture-docs' own scripts.

    Its generator output differs between Python versions, so it must not run on this
    repository's virtualenv (``uv run`` puts it first on PATH).
    """
    venv_bin = str(Path(sys.prefix) / "bin")
    env = {k: v for k, v in environ.items() if k != "VIRTUAL_ENV"}
    env["PATH"] = os.pathsep.join(
        part for part in environ.get("PATH", "").split(os.pathsep) if part != venv_bin
    )
    override = (environ.get("SOLO_ARCHITECTURE_DOCS_PYTHON") or "").strip()
    return override or shutil.which("python3", path=env["PATH"]) or "python3", env


def check_fresh(docs_root: Path, *, run: Run, environ: Mapping[str, str]) -> None:
    python, env = docs_env(environ)
    result = run(
        [python, "scripts/document_catalog.py", "--check"], cwd=docs_root, env=env
    )
    if result.returncode != 0:
        raise DeployError(
            "catalog is stale: run `task docs:catalog` in architecture-docs first"
        )


def stage(source: Path, dest: Path) -> Staged:
    try:
        payload = _load(
            "validate_catalog", ROOT / "scripts" / "validate_catalog.py"
        ).load_validated(source)
    except (OSError, ValueError) as error:
        raise DeployError(f"source rejected: {error}") from None
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".catalog-", suffix=".json")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(source.read_bytes())
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    staged = Staged(revision_of(dest), len(payload["documents"]))
    if staged.revision != revision_of(source):
        raise DeployError("staged snapshot differs from its source")
    return staged


def railway_up(repo: Path, target: Target, *, run: Run) -> None:
    cmd = ["railway", "up", "--ci", "--no-gitignore"]
    if target.project:
        cmd += ["--project", target.project]
    cmd += ["--environment", target.environment, "--service", target.service]
    if run(cmd, cwd=repo).returncode != 0:
        raise DeployError("railway up failed (see its output above)")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return (
            None  # a 3xx becomes an HTTPError; the bearer token never leaves this host
        )


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch_json(url: str, token: str | None = None) -> tuple[int, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        request.add_unredirected_header("Authorization", "Bearer " + token)
    try:
        with _OPENER.open(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, {}
    except (urllib.error.URLError, TimeoutError, ValueError):
        return 0, {}


def verify(
    base: str,
    expected: str,
    *,
    token: Callable[[], str],
    fetch: Fetch,
    attempts: int = 20,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    base = base.rstrip("/")
    for _ in range(attempts):
        if fetch(base + "/ready")[0] == 200:
            break
        sleep(10)
    else:
        raise DeployError("service never became ready (/ready not 200)")
    seen = "none"
    for _ in range(attempts):
        # A fresh token each time: they live 60 seconds. Old instances may still answer briefly.
        status, body = fetch(base + "/api/v1/knowledge?limit=1", token())
        catalog = body.get("catalog") if isinstance(body, dict) else None
        seen = (
            catalog.get("revision", "none")
            if isinstance(catalog, dict)
            else f"HTTP {status}"
        )
        if status == 200 and seen == expected:
            return
        sleep(10)
    raise DeployError(f"live revision {seen} != staged {expected}")


def _mcp() -> Any:
    """knowledge_mcp holds the URL policy and JWT minting; it needs the `mcp` extra."""
    try:
        return _load("knowledge_mcp", ROOT / "knowledge_mcp.py")
    except ModuleNotFoundError as error:
        sys.modules.pop("knowledge_mcp", None)
        raise DeployError(
            f"{error.name or 'a dependency'} is missing: run `uv sync --extra mcp` first"
        ) from None


def _mint() -> str:
    return _mcp().mint()


def _api_url(environ: Mapping[str, str]) -> str:
    module = _mcp()
    try:
        return module.api_url("EMPIRE_KNOWLEDGE_API", environ)
    except module.ConfigError as error:
        raise DeployError(str(error)) from None


def main(
    argv: list[str] | None = None,
    *,
    dest: Path = DEST,
    run: Run = subprocess.run,
    fetch: Fetch = fetch_json,
    token: Callable[[], str] = _mint,
    sleep: Callable[[float], None] = time.sleep,
    environ: Mapping[str, str] = os.environ,
) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source", type=Path, help="catalog JSON to deploy")
    parser.add_argument("--url", help="API base URL (default EMPIRE_KNOWLEDGE_API)")
    parser.add_argument("--dry-run", action="store_true", help="stage only, no deploy")
    parser.add_argument(
        "--no-freshness-check", action="store_true", help="skip the generator check"
    )
    args = parser.parse_args(argv)
    docs_root = (environ.get("SOLO_ARCHITECTURE_DOCS_ROOT") or "").strip()
    try:
        source = args.source or (Path(docs_root) / DOCS_CATALOG if docs_root else None)
        if source is None:
            raise DeployError("set --source or SOLO_ARCHITECTURE_DOCS_ROOT")
        if docs_root and not args.source and not args.no_freshness_check:
            check_fresh(Path(docs_root), run=run, environ=environ)
        staged = stage(source, dest)
        print(f"staged {staged.documents} documents, revision {staged.revision}")
        if args.dry_run:
            return 0
        # --url goes through the same https-only policy as EMPIRE_KNOWLEDGE_API.
        base = _api_url(
            {**environ, "EMPIRE_KNOWLEDGE_API": args.url} if args.url else environ
        )
        target = Target(
            environ.get("KNOWLEDGE_API_RAILWAY_PROJECT", ""),
            environ.get("KNOWLEDGE_API_RAILWAY_ENVIRONMENT", "production"),
            environ.get("KNOWLEDGE_API_RAILWAY_SERVICE", "solo-knowledge-api"),
        )
        railway_up(ROOT, target, run=run)
        verify(base, staged.revision, token=token, fetch=fetch, sleep=sleep)
    except DeployError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"OK: live API serves revision {staged.revision}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
