"""Shared fixture: a KnowledgeApi over a throwaway catalog file (development auth, no network)."""

from __future__ import annotations

import json
from collections.abc import Callable
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
