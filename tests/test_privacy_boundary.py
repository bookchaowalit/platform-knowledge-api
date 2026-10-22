"""Privacy boundary rules: which catalog records may ever be served, and what is never returned."""

from __future__ import annotations

from typing import Any

import pytest

from server import safe_alias, safe_path


def doc(path: str, **fields: Any) -> dict[str, Any]:
    return {"path": path, **fields}


@pytest.mark.parametrize(
    "path",
    [
        "docs/guide.md",
        "docs/guide.mdx",
        "architecture/README.md",
        "finance/tax/README.md",
        "clients/readme.md",
    ],
)
def test_curated_markdown_paths_are_served(path):
    assert safe_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "docs/.git/notes.md",
        "home/.ssh/keys.md",
        "ops/credentials/aws.md",
        "ops/secrets/list.md",
        "team/private/plan.md",
        "app/.env",
        "app/.env.production/notes.md",
        "data/raw/export.md",
        "data/lake/summary.md",
        "engagements/acme/notes.md",
        "projects/product/engineering/book-dev/x/README.md",
        "decisions/records/0001.md",
        "docs/diagram.png",
        "diary/entries/2026-10-22.md",
        "finance/tax/2026.md",
        "clients/acme/contract.md",
        "Finance/Invoices/2026-01.md",
    ],
)
def test_secret_raw_private_and_non_markdown_paths_are_never_served(path):
    assert not safe_path(path)


@pytest.mark.parametrize(
    "alias",
    [
        "https://example.com/x",
        "mailto:a@b.c",
        "/etc/passwd",
        "\\\\share\\x",
        "",
        "x" * 121,
        42,
    ],
)
def test_aliases_that_are_uris_absolute_paths_or_too_long_are_dropped(alias):
    assert safe_alias(alias) is None


def test_a_plain_label_alias_is_kept_trimmed():
    assert safe_alias("  Getting started  ") == "Getting started"


def test_unsafe_records_are_dropped_and_counted_as_excluded(make_api):
    api = make_api(
        doc("docs/a.md", title="A"),
        doc("diary/entries/2026-10-22.md"),
        doc("../escape.md"),
        doc("docs/b.txt"),
        "not a record",
    )
    payload = api.handle("GET", "/api/v1/knowledge").payload
    assert [d["path"] for d in payload["documents"]] == ["docs/a.md"]
    assert payload["catalog"]["excluded_records"] == 4


def test_an_excluded_record_cannot_be_fetched_by_path(make_api):
    api = make_api(doc("docs/a.md"), doc("diary/entries/2026-10-22.md"))
    response = api.handle(
        "GET", "/api/v1/knowledge/document?path=diary/entries/2026-10-22.md"
    )
    assert response.status == 404
    assert response.payload["error"]["code"] == "document_not_found"


def test_canonical_urls_and_unsafe_aliases_never_leave_the_service(make_api):
    api = make_api(
        doc(
            "docs/a.md",
            canonical="https://private.example/a",
            aliases=["Start", "https://private.example/a", "/abs/path"],
        )
    )
    detail = api.handle("GET", "/api/v1/knowledge/document?path=docs/a.md").payload
    assert detail["document"]["canonical"] is None
    assert detail["document"]["aliases"] == ["Start"]
    assert "private.example" not in str(detail)
