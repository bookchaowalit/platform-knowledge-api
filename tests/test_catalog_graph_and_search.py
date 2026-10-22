"""Catalog rules: defaults and clipping, the parent/related/child graph, search ranking, fail closed."""

from __future__ import annotations

import os
from typing import Any

import pytest

from server import ApiError, CatalogStore, resolve_reference


def doc(path: str, **fields: Any) -> dict[str, Any]:
    return {"path": path, **fields}


def overview(api, query: str = "") -> dict:
    response = api.handle("GET", "/api/v1/knowledge" + query)
    assert response.status == 200, response.payload
    return response.payload


def detail(api, path: str) -> dict:
    response = api.handle("GET", f"/api/v1/knowledge/document?path={path}")
    assert response.status == 200, response.payload
    return response.payload


def test_missing_fields_get_safe_defaults(make_api):
    (document,) = overview(make_api(doc("docs/getting-started_guide.md")))["documents"]
    assert document["title"] == "getting started guide"
    assert document["document_type"] == "document"
    assert document["status"] == "unclassified"
    assert document["root_area"] == "(root)"
    assert document["description"] is None
    assert document["tags"] == []
    assert len(document["id"]) == 12


def test_long_fields_and_lists_are_clipped(make_api):
    api = make_api(
        doc(
            "docs/a.md",
            title="t" * 300,
            description="d" * 400,
            tags=[f"tag{i}" + "x" * 80 for i in range(20)] + [7],
            aliases=[f"alias {i}" for i in range(12)],
        )
    )
    document = detail(api, "docs/a.md")["document"]
    assert len(document["title"]) == 240
    assert len(document["description"]) == 360
    assert len(document["tags"]) == 16
    assert all(len(tag) == 60 for tag in document["tags"])
    assert len(document["aliases"]) == 8


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("b.md", "docs/b.md"),
        ("b", "docs/b.md"),
        ("../guides/c.md#setup", "guides/c.md"),
        ("<b.md?x=1>", "docs/b.md"),
        ("guides/c.md", "guides/c.md"),
        ("https://example.com/b.md", None),
        ("mailto:a@b.c", None),
        ("#anchor", None),
        ("missing.md", None),
        (None, None),
    ],
)
def test_references_resolve_only_to_paths_in_the_catalog(reference, expected):
    known = {"docs/a.md", "docs/b.md", "guides/c.md"}
    assert resolve_reference("docs/a.md", reference, known) == expected


def test_parent_related_and_children_form_the_document_graph(make_api):
    api = make_api(
        doc("docs/index.md", title="Index"),
        doc("docs/a.md", title="A", parent="index.md", related=["b.md", "b", "x.md"]),
        doc("docs/b.md", title="B", parent="index.md"),
    )
    a = detail(api, "docs/a.md")
    assert a["document"]["parent"] == "docs/index.md"
    assert a["document"]["related"] == [
        "docs/b.md"
    ]  # resolved, deduplicated, unknown dropped
    assert [(n["relation"], n["document"]["path"]) for n in a["neighbors"]] == [
        ("parent", "docs/index.md"),
        ("related", "docs/b.md"),
    ]
    index = detail(api, "docs/index.md")["document"]
    assert index["children"] == ["docs/a.md", "docs/b.md"]
    summary = overview(api)["summary"]
    assert summary["graph_edges"] == 3  # two parent links and one related link
    assert summary["linked_documents"] == 2  # a (related) and index (children)


def test_search_ranks_title_matches_above_path_tag_and_description(make_api):
    api = make_api(
        doc("docs/notes.md", title="Notes", description="mentions plan"),
        doc("docs/x.md", title="Other", tags=["plan"]),
        doc("plan/y.md", title="Y"),
        doc("docs/z.md", title="Planning ahead"),
        doc("docs/w.md", title="Plan"),
        doc("docs/v.md", title="The plan"),
        doc("docs/none.md", title="Unrelated"),
    )
    paths = [d["path"] for d in overview(api, "?q=plan")["documents"]]
    assert paths == [
        "docs/w.md",  # exact title
        "docs/z.md",  # title prefix
        "docs/v.md",  # title contains
        "plan/y.md",  # path
        "docs/x.md",  # tag
        "docs/notes.md",  # description
    ]


def test_without_a_query_documents_sort_by_area_then_title(make_api):
    api = make_api(
        doc("b/z.md", title="alpha", root_area="b"),
        doc("a/y.md", title="Beta", root_area="a"),
        doc("a/x.md", title="alpha", root_area="a"),
    )
    assert [d["path"] for d in overview(api)["documents"]] == [
        "a/x.md",
        "a/y.md",
        "b/z.md",
    ]


def test_filters_and_paging_narrow_the_matched_documents_only(make_api):
    api = make_api(
        *(
            doc(f"docs/{i}.md", title=f"D{i}", tags=["t"] if i % 2 else [])
            for i in range(6)
        )
    )
    payload = overview(api, "?tag=t&limit=2&offset=1")
    assert payload["summary"]["documents_total"] == 6
    assert payload["summary"]["documents_matched"] == 3
    assert [d["path"] for d in payload["documents"]] == ["docs/3.md", "docs/5.md"]


def test_shelves_group_by_root_area_with_the_most_common_status(make_api):
    api = make_api(
        doc("a/1.md", root_area="team-notes", status="draft"),
        doc("a/2.md", root_area="team-notes", status="draft"),
        doc("a/3.md", root_area="team-notes", status="current"),
        doc("b/1.md", root_area="docs", status="current"),
    )
    assert overview(api)["shelves"] == [
        {
            "key": "team-notes",
            "label": "Team Notes",
            "count": 3,
            "linked": 0,
            "top_status": "draft",
        },
        {
            "key": "docs",
            "label": "Docs",
            "count": 1,
            "linked": 0,
            "top_status": "current",
        },
    ]


@pytest.mark.parametrize(
    ("catalog", "code"),
    [
        ({"schema_version": "other.v1"}, "catalog_schema_unsupported"),
        ({"documents": {"path": "a.md"}}, "catalog_schema_invalid"),
    ],
)
def test_an_unsupported_catalog_is_a_503_not_a_partial_list(make_api, catalog, code):
    api = make_api(**catalog)
    response = api.handle("GET", "/api/v1/knowledge")
    assert response.status == 503
    assert response.payload["error"]["code"] == code


def test_duplicate_paths_after_normalisation_are_a_503(make_api):
    api = make_api(doc("docs/a.md"), doc("docs\\a.md"))
    response = api.handle("GET", "/api/v1/knowledge")
    assert response.status == 503
    assert response.payload["error"]["code"] == "catalog_schema_invalid"


def test_a_catalog_over_20_mib_is_refused_before_it_is_read(tmp_path):
    path = tmp_path / "big.json"
    with path.open("wb") as handle:
        handle.truncate(20 * 1024 * 1024 + 1)
    with pytest.raises(ApiError, match="exceeds the supported size"):
        CatalogStore(path).snapshot()


def test_the_catalog_reloads_when_its_mtime_changes(make_api, tmp_path):
    api = make_api(doc("docs/a.md"))
    assert overview(api)["summary"]["documents_total"] == 1
    path = tmp_path / "document-catalog.json"
    first = os.stat(path).st_mtime_ns
    make_api(doc("docs/a.md"), doc("docs/b.md"))
    os.utime(path, ns=(first + 10**9, first + 10**9))
    assert overview(api)["summary"]["documents_total"] == 2
