"""Shared fixture: a KnowledgeApi over a throwaway catalog file (development auth, no network)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from server import CATALOG_SCHEMA_VERSION, KnowledgeApi

MakeApi = Callable[..., KnowledgeApi]


@pytest.fixture
def make_api(tmp_path: Path) -> MakeApi:
    def make(*documents: dict[str, Any], **catalog: Any) -> KnowledgeApi:
        path = tmp_path / "document-catalog.json"
        payload = {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "documents": list(documents),
            **catalog,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return KnowledgeApi(path, token="", environment="development")

    return make


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
