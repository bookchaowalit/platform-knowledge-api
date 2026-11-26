"""Tests for scripts/deploy_catalog.py (refresh, deploy and verify the catalog snapshot)."""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_catalog.py"
SPEC = importlib.util.spec_from_file_location("deploy_catalog", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
deploy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = deploy  # dataclasses look their module up here
SPEC.loader.exec_module(deploy)

SCHEMA = "solo-empire.document-catalog.v1"


def write_catalog(path: Path, *paths: str) -> str:
    text = json.dumps(
        {"schema_version": SCHEMA, "documents": [{"path": p} for p in paths]}
    )
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Recorder:
    def __init__(self, returncode: int = 0):
        self.calls: list[tuple[list[str], str | None]] = []
        self.envs: list[dict] = []
        self.returncode = returncode

    def __call__(self, cmd, cwd=None, env=None, **_kwargs):
        self.calls.append((list(cmd), str(cwd) if cwd else None))
        self.envs.append(dict(env or {}))
        return subprocess.CompletedProcess(cmd, self.returncode, "", "")


class StageTest(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.source = root / "source.json"
        self.dest = root / "data" / "document-catalog.json"
        self.dest.parent.mkdir()

    def test_revision_matches_server_formula(self):
        expected = write_catalog(self.source, "a.md")
        assert deploy.revision_of(self.source) == expected

    def test_stage_copies_and_returns_revision(self):
        expected = write_catalog(self.source, "a.md", "b.md")
        staged = deploy.stage(self.source, self.dest)
        assert staged.revision == expected
        assert staged.documents == 2
        assert self.dest.read_bytes() == self.source.read_bytes()
        assert list(self.dest.parent.iterdir()) == [self.dest]

    def test_invalid_source_leaves_destination_untouched(self):
        self.dest.write_text("previous", encoding="utf-8")
        self.source.write_text(json.dumps({"schema_version": SCHEMA}), "utf-8")
        with pytest.raises(deploy.DeployError):
            deploy.stage(self.source, self.dest)
        assert self.dest.read_text(encoding="utf-8") == "previous"

    def test_missing_source_is_a_deploy_error(self):
        with pytest.raises(deploy.DeployError):
            deploy.stage(self.source, self.dest)


def _legacy_docs() -> Path:
    """A document tree that still carries its own scripts/ (before SPEC-001 phase 1)."""
    docs = Path(tempfile.mkdtemp())
    (docs / "scripts").mkdir()
    (docs / "scripts" / "document_catalog.py").write_text("# fixture\n")
    return docs


class FreshnessTest(unittest.TestCase):
    def test_runs_generator_check_in_docs_root(self):
        docs = _legacy_docs()
        run = Recorder()
        env = {"SOLO_ARCHITECTURE_DOCS_PYTHON": "/opt/py/bin/python3"}
        deploy.check_fresh(docs, run=run, environ=env)
        assert run.calls == [
            (
                ["/opt/py/bin/python3", "scripts/document_catalog.py", "--check"],
                str(docs.resolve()),
            )
        ]

    def test_document_tree_without_scripts_uses_the_architecture_tools(self):
        # SPEC-001: the generator lives in platform-ops-cli/architecture and is told where the documents are.
        docs = Path(tempfile.mkdtemp())
        tools = Path(tempfile.mkdtemp())
        (tools / "scripts").mkdir()
        (tools / "scripts" / "document_catalog.py").write_text("# fixture\n")
        run = Recorder()
        env = {
            "SOLO_ARCHITECTURE_DOCS_PYTHON": "/opt/py/bin/python3",
            "SOLO_ARCHITECTURE_TOOLS": str(tools),
        }
        deploy.check_fresh(docs, run=run, environ=env)
        assert run.calls == [
            (
                [
                    "/opt/py/bin/python3",
                    str((tools / "scripts" / "document_catalog.py").resolve()),
                    "--check",
                ],
                str(docs.resolve()),
            )
        ]
        assert run.envs[0]["SOLO_ARCHITECTURE_ROOT"] == str(docs.resolve())

    def test_relative_roots_are_made_absolute_before_the_subprocess(self):
        # The subprocess runs with cwd=docs_root, so a relative path would be resolved twice.
        base = Path(tempfile.mkdtemp())
        (base / "docs").mkdir()
        (base / "tools" / "scripts").mkdir(parents=True)
        (base / "tools" / "scripts" / "document_catalog.py").write_text("# fixture\n")
        run = Recorder()
        old = os.getcwd()
        os.chdir(base)
        try:
            deploy.check_fresh(
                Path("docs"), run=run, environ={"SOLO_ARCHITECTURE_TOOLS": "tools"}
            )
        finally:
            os.chdir(old)
        command, cwd = run.calls[0]
        assert command[1] == str(
            (base / "tools" / "scripts" / "document_catalog.py").resolve()
        )
        assert cwd == str((base / "docs").resolve())
        assert run.envs[0]["SOLO_ARCHITECTURE_ROOT"] == str((base / "docs").resolve())

    def test_default_tools_come_from_the_main_clone_of_a_linked_worktree(self):
        # empire layout: worktrees/<name> is a linked worktree of repositories/.../platform-knowledge-api
        base = Path(tempfile.mkdtemp())
        main = (
            base
            / "repositories"
            / "public"
            / "book-products"
            / "platform-knowledge-api"
        )
        (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
        worktree = base / "worktrees" / "wt"
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text(
            f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n"
        )
        tools = base / "repositories" / "public" / "platform-ops-cli" / "architecture"
        assert deploy.architecture_tools({}, repo_root=worktree) == tools
        assert deploy.architecture_tools({}, repo_root=main) == tools

    def test_plain_sibling_tools_come_before_the_empire_layout(self):
        base = Path(tempfile.mkdtemp())
        repo = base / "platform-knowledge-api"
        repo.mkdir()
        sibling = base / "platform-ops-cli" / "architecture"
        (sibling / "scripts").mkdir(parents=True)
        (sibling / "scripts" / "document_catalog.py").write_text("# fixture\n")
        assert deploy.architecture_tools({}, repo_root=repo) == sibling

    def test_relative_gitdir_of_a_linked_worktree(self):
        base = Path(tempfile.mkdtemp())
        main = (
            base
            / "repositories"
            / "public"
            / "book-products"
            / "platform-knowledge-api"
        )
        (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
        worktree = base / "worktrees" / "wt"
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text(
            "gitdir: ../../repositories/public/book-products/platform-knowledge-api/.git/worktrees/wt\n"
        )
        tools = (
            base / "repositories" / "public" / "platform-ops-cli" / "architecture"
        ).resolve()
        assert deploy.architecture_tools({}, repo_root=worktree) == tools

    def test_regenerate_hint_is_shell_quoted(self):
        docs = Path(tempfile.mkdtemp()) / "my docs"
        docs.mkdir()
        tools = Path(tempfile.mkdtemp()) / "my tools"
        (tools / "scripts").mkdir(parents=True)
        (tools / "scripts" / "document_catalog.py").write_text("# fixture\n")
        with pytest.raises(deploy.DeployError) as error:
            deploy.check_fresh(
                docs,
                run=Recorder(returncode=1),
                environ={"SOLO_ARCHITECTURE_TOOLS": str(tools)},
            )
        assert f"SOLO_ARCHITECTURE_ROOT='{docs.resolve()}'" in str(error.value)

    def test_sibling_without_the_generator_falls_back_to_the_empire_layout(self):
        base = Path(tempfile.mkdtemp())
        repo = base / "public" / "book-products" / "platform-knowledge-api"
        repo.mkdir(parents=True)
        (base / "public" / "book-products" / "platform-ops-cli" / "architecture").mkdir(
            parents=True
        )
        empire = base / "public" / "platform-ops-cli" / "architecture"
        assert deploy.architecture_tools({}, repo_root=repo) == empire

    def test_source_flag_ignores_an_unexpandable_docs_root(self):
        source = Path(tempfile.mkdtemp()) / "catalog.json"
        write_catalog(source, "a.md")
        dest = Path(tempfile.mkdtemp()) / "document-catalog.json"
        code = deploy.main(
            ["--dry-run", "--source", str(source)],
            dest=dest,
            run=Recorder(),
            fetch=lambda u, token=None: (200, {}),
            token=lambda: "t",
            sleep=lambda _s: None,
            environ={"SOLO_ARCHITECTURE_DOCS_ROOT": "~no-such-user-xyz/docs"},
        )
        assert code == 0

    def test_regenerate_hint_runs_in_the_checked_folder(self):
        docs = Path(tempfile.mkdtemp())
        tools = Path(tempfile.mkdtemp())
        (tools / "scripts").mkdir()
        (tools / "scripts" / "document_catalog.py").write_text("# fixture\n")
        with pytest.raises(deploy.DeployError) as error:
            deploy.check_fresh(
                docs,
                run=Recorder(returncode=1),
                environ={
                    "SOLO_ARCHITECTURE_TOOLS": str(tools),
                    "SOLO_ARCHITECTURE_DOCS_PYTHON": ".venv/bin/python",
                },
            )
        assert f"cd {docs.resolve()} && " in str(error.value)

    def test_unexpandable_tools_setting_is_a_deploy_error(self):
        with pytest.raises(deploy.DeployError, match="SOLO_ARCHITECTURE_TOOLS"):
            deploy.architecture_tools(
                {"SOLO_ARCHITECTURE_TOOLS": "~no-such-user-xyz/architecture"}
            )

    def test_regenerate_hint_names_the_interpreter_the_check_used(self):
        docs = Path(tempfile.mkdtemp())
        tools = Path(tempfile.mkdtemp())
        (tools / "scripts").mkdir()
        (tools / "scripts" / "document_catalog.py").write_text("# fixture\n")
        bindir = Path(tempfile.mkdtemp())
        fake = bindir / "python3"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        environ = {
            "SOLO_ARCHITECTURE_TOOLS": str(tools),
            "SOLO_ARCHITECTURE_DOCS_PYTHON": "python3",
            "PATH": f"{bindir}:/usr/bin:/bin",
        }
        with pytest.raises(deploy.DeployError) as error:
            deploy.check_fresh(docs, run=Recorder(returncode=1), environ=environ)
        assert str(fake) in str(error.value)

    def test_hint_resolves_relative_path_entries_from_the_docs_root(self):
        docs = Path(tempfile.mkdtemp())
        (docs / "bin").mkdir()
        fake = docs / "bin" / "python3"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        tools = Path(tempfile.mkdtemp())
        (tools / "scripts").mkdir()
        (tools / "scripts" / "document_catalog.py").write_text("# fixture\n")
        environ = {
            "SOLO_ARCHITECTURE_TOOLS": str(tools),
            "SOLO_ARCHITECTURE_DOCS_PYTHON": "python3",
            "PATH": "bin:/nonexistent",
        }
        with pytest.raises(deploy.DeployError) as error:
            deploy.check_fresh(docs, run=Recorder(returncode=1), environ=environ)
        assert str(fake.resolve()) in str(error.value)

    def test_stale_message_names_the_generator_and_root_that_failed(self):
        docs = Path(tempfile.mkdtemp())
        tools = Path(tempfile.mkdtemp())
        (tools / "scripts").mkdir()
        (tools / "scripts" / "document_catalog.py").write_text("# fixture\n")
        with pytest.raises(deploy.DeployError) as error:
            deploy.check_fresh(
                docs,
                run=Recorder(returncode=1),
                environ={"SOLO_ARCHITECTURE_TOOLS": str(tools)},
            )
        message = str(error.value)
        assert f"SOLO_ARCHITECTURE_ROOT={docs.resolve()}" in message
        assert (
            f"{(tools / 'scripts' / 'document_catalog.py').resolve()} --write"
            in message
        )

    def test_missing_architecture_tools_is_a_deploy_error(self):
        docs = Path(tempfile.mkdtemp())
        run = Recorder()
        with pytest.raises(deploy.DeployError, match="SOLO_ARCHITECTURE_TOOLS"):
            deploy.check_fresh(
                docs, run=run, environ={"SOLO_ARCHITECTURE_TOOLS": str(docs / "absent")}
            )
        assert run.calls == []

    def test_generator_never_runs_on_this_repos_venv(self):
        # Its output differs between Python versions, so it must use the docs repo's own python3.
        venv_bin = str(Path(sys.prefix) / "bin")
        run = Recorder()
        deploy.check_fresh(
            _legacy_docs(),
            run=run,
            environ={"PATH": venv_bin + ":/usr/bin:/bin", "VIRTUAL_ENV": sys.prefix},
        )
        assert run.envs[0]["PATH"].split(":")[0] != venv_bin
        assert "VIRTUAL_ENV" not in run.envs[0]
        assert not run.calls[0][0][0].startswith(venv_bin)

    def test_stale_generator_output_aborts(self):
        with pytest.raises(deploy.DeployError):
            deploy.check_fresh(_legacy_docs(), run=Recorder(returncode=1), environ={})


class RailwayTest(unittest.TestCase):
    def test_command_names_target_and_keeps_ignored_catalog(self):
        run = Recorder()
        deploy.railway_up(
            Path("/repo"),
            deploy.Target("proj", "production", "solo-knowledge-api"),
            run=run,
        )
        cmd, cwd = run.calls[0]
        assert cwd == "/repo"
        assert cmd[:4] == ["railway", "up", "--ci", "--no-gitignore"]
        assert "--service" in cmd
        assert cmd[cmd.index("--project") + 1] == "proj"

    def test_project_is_optional_for_a_linked_checkout(self):
        run = Recorder()
        deploy.railway_up(
            Path("/repo"), deploy.Target("", "production", "svc"), run=run
        )
        assert "--project" not in run.calls[0][0]

    def test_failed_upload_aborts(self):
        with pytest.raises(deploy.DeployError):
            deploy.railway_up(
                Path("/repo"), deploy.Target("", "p", "s"), run=Recorder(1)
            )


class VerifyTest(unittest.TestCase):
    def fetcher(self, responses):
        calls = []

        def fetch(url, token=None):
            calls.append((url, token))
            return responses.pop(0) if len(responses) > 1 else responses[0]

        return fetch, calls

    def test_waits_for_ready_then_matching_revision(self):
        fetch, calls = self.fetcher(
            [
                (503, {}),
                (200, {"status": "ready"}),
                (200, {"catalog": {"revision": "old"}}),
                (200, {"catalog": {"revision": "new"}}),
            ]
        )
        deploy.verify(
            "https://api.example",
            "new",
            token=lambda: "t",
            fetch=fetch,
            attempts=5,
            sleep=lambda _s: None,
        )
        assert calls[0] == ("https://api.example/ready", None)
        assert calls[-1] == ("https://api.example/api/v1/knowledge?limit=1", "t")

    def test_never_ready_fails(self):
        fetch, _ = self.fetcher([(503, {})])
        with pytest.raises(deploy.DeployError, match="ready"):
            deploy.verify(
                "https://api.example",
                "new",
                token=lambda: "t",
                fetch=fetch,
                attempts=3,
                sleep=lambda _s: None,
            )

    def test_revision_mismatch_fails_without_printing_token(self):
        fetch, _ = self.fetcher([(200, {}), (200, {"catalog": {"revision": "old"}})])
        with pytest.raises(deploy.DeployError) as ctx:
            deploy.verify(
                "https://api.example",
                "new",
                token=lambda: "secret-token",
                fetch=fetch,
                attempts=3,
                sleep=lambda _s: None,
            )
        assert "old" in str(ctx.value)
        assert "secret-token" not in str(ctx.value)


class MainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.source = self.root / "source.json"
        self.dest = self.root / "dest.json"
        self.revision = write_catalog(self.source, "a.md")

    def run_main(self, *args, run=None, fetch=None):
        return deploy.main(
            ["--source", str(self.source), *args],
            dest=self.dest,
            run=run or Recorder(),
            fetch=fetch or (lambda url, token=None: (200, {})),
            token=lambda: "t",
            sleep=lambda _s: None,
            environ={},
        )

    def test_dry_run_stages_but_never_deploys(self):
        run = Recorder()
        assert self.run_main("--dry-run", "--no-freshness-check", run=run) == 0
        assert run.calls == []
        assert self.dest.exists()

    def test_url_flag_uses_the_api_url_policy(self):
        run = Recorder()
        code = self.run_main(
            "--no-freshness-check", "--url", "http://api.example", run=run
        )
        assert code == 1
        assert run.calls == []

    def test_full_run_deploys_and_verifies(self):
        run = Recorder()
        fetch = lambda url, token=None: (  # noqa: E731
            200,
            {"catalog": {"revision": self.revision}},
        )
        code = self.run_main(
            "--no-freshness-check", "--url", "https://api.example", run=run, fetch=fetch
        )
        assert code == 0
        assert run.calls[0][0][:2] == ["railway", "up"]

    def test_source_required(self):
        assert (
            deploy.main(
                [],
                dest=self.dest,
                run=Recorder(),
                fetch=lambda u, token=None: (200, {}),
                token=lambda: "t",
                sleep=lambda _s: None,
                environ={},
            )
            == 1
        )

    def test_source_from_docs_root_env_runs_freshness_check(self):
        docs = self.root / "architecture-docs"
        (docs / "data" / "indexes").mkdir(parents=True)
        write_catalog(docs / "data" / "indexes" / "document-catalog.json", "a.md")
        (docs / "scripts").mkdir()
        (docs / "scripts" / "document_catalog.py").write_text("# fixture\n")
        run = Recorder()
        code = deploy.main(
            ["--dry-run"],
            dest=self.dest,
            run=run,
            fetch=lambda u, token=None: (200, {}),
            token=lambda: "t",
            sleep=lambda _s: None,
            environ={"SOLO_ARCHITECTURE_DOCS_ROOT": str(docs)},
        )
        assert code == 0
        assert run.calls[0][1] == str(docs.resolve())

    def test_tilde_docs_root_is_checked_and_staged_from_the_same_tree(self):
        docs = self.root / "architecture-docs"
        (docs / "data" / "indexes").mkdir(parents=True)
        write_catalog(docs / "data" / "indexes" / "document-catalog.json", "a.md")
        (docs / "scripts").mkdir()
        (docs / "scripts" / "document_catalog.py").write_text("# fixture\n")
        run = Recorder()
        with unittest.mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            code = deploy.main(
                ["--dry-run"],
                dest=self.dest,
                run=run,
                fetch=lambda u, token=None: (200, {}),
                token=lambda: "t",
                sleep=lambda _s: None,
                environ={"SOLO_ARCHITECTURE_DOCS_ROOT": "~/architecture-docs"},
            )
        assert code == 0
        assert run.calls[0][1] == str(docs.resolve())
        assert self.dest.is_file()


class FetchTest(unittest.TestCase):
    """fetch_json against real local HTTP servers."""

    def serve(self, handler):
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def test_token_never_follows_a_redirect(self):
        seen: list[str | None] = []

        class Sink(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args):
                pass

        sink = self.serve(Sink)

        class Redirect(Sink):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", sink + "/stolen")
                self.end_headers()

        status, _ = deploy.fetch_json(self.serve(Redirect) + "/x", "secret-token")
        assert status == 302
        assert seen == []

    def test_json_body_and_bearer(self):
        seen: list[str | None] = []

        class Api(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def log_message(self, *_args):
                pass

        assert deploy.fetch_json(self.serve(Api) + "/x", "t") == (200, {"ok": True})
        assert seen == ["Bearer t"]


class MissingExtraTest(unittest.TestCase):
    def test_missing_mcp_extra_is_a_clear_deploy_error(self):
        saved = sys.modules.pop("knowledge_mcp", None)
        self.addCleanup(
            lambda: sys.modules.update({"knowledge_mcp": saved}) if saved else None
        )
        real_import = __import__

        def no_httpx(name, *args, **kwargs):
            if name == "httpx":
                raise ModuleNotFoundError("No module named 'httpx'")
            return real_import(name, *args, **kwargs)

        with (
            unittest.mock.patch("builtins.__import__", no_httpx),
            pytest.raises(deploy.DeployError, match="uv sync --extra mcp"),
        ):
            deploy._api_url({"EMPIRE_KNOWLEDGE_API": "https://api.example"})
        assert "knowledge_mcp" not in sys.modules or saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
