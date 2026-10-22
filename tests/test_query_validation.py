"""Query validation rules: paging bounds, path normalisation and text limits, as 400s with codes."""

from __future__ import annotations

import pytest

from server import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_OFFSET,
    MAX_PATH_LENGTH,
    MAX_QUERY_LENGTH,
    ApiError,
    normalize_path,
    parse_limit,
    parse_offset,
)


def test_missing_or_empty_paging_uses_the_defaults():
    assert parse_limit(None) == DEFAULT_LIMIT
    assert parse_limit("") == DEFAULT_LIMIT
    assert parse_offset(None) == 0
    assert parse_offset("") == 0


def test_paging_accepts_its_inclusive_bounds():
    assert parse_limit("1") == 1
    assert parse_limit(str(MAX_LIMIT)) == MAX_LIMIT
    assert parse_offset("0") == 0
    assert parse_offset(str(MAX_OFFSET)) == MAX_OFFSET


@pytest.mark.parametrize("value", ["0", str(MAX_LIMIT + 1), "-1", "ten", "1.5"])
def test_a_limit_outside_1_to_100_or_not_an_integer_is_invalid_limit(value):
    with pytest.raises(ApiError) as caught:
        parse_limit(value)
    assert (caught.value.status, caught.value.code) == (400, "invalid_limit")


@pytest.mark.parametrize("value", ["-1", str(MAX_OFFSET + 1), "x"])
def test_an_offset_outside_0_to_5000_or_not_an_integer_is_invalid_offset(value):
    with pytest.raises(ApiError) as caught:
        parse_offset(value)
    assert (caught.value.status, caught.value.code) == (400, "invalid_offset")


def test_a_path_is_trimmed_and_backslashes_become_slashes():
    assert normalize_path("  docs\\guides\\a.md ") == "docs/guides/a.md"
    assert normalize_path(None) is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "/etc/passwd.md",
        "../secret.md",
        "docs/../../x.md",
        "docs/./a.md",
        "docs//a.md",
        "docs/a\x00.md",
        "d" * (MAX_PATH_LENGTH + 1),
        42,
    ],
)
def test_absolute_traversing_empty_or_oversized_paths_are_invalid(value):
    with pytest.raises(ApiError) as caught:
        normalize_path(value)
    assert (caught.value.status, caught.value.code) == (400, "invalid_path")


@pytest.fixture
def api(make_api):
    return make_api({"path": "docs/a.md", "title": "A"})


@pytest.mark.parametrize(
    ("target", "code"),
    [
        ("/api/v1/knowledge?limit=0", "invalid_limit"),
        ("/api/v1/knowledge?offset=5001", "invalid_offset"),
        (f"/api/v1/knowledge?q={'x' * (MAX_QUERY_LENGTH + 1)}", "invalid_field"),
        (f"/api/v1/knowledge?tag={'t' * 61}", "invalid_field"),
        (f"/api/v1/knowledge?root_area={'r' * 81}", "invalid_field"),
        ("/api/v1/knowledge/document", "missing_path"),
        ("/api/v1/knowledge/document?path=../a.md", "invalid_path"),
    ],
)
def test_bad_query_parameters_are_a_400_with_a_stable_code(api, target, code):
    response = api.handle("GET", target)
    assert response.status == 400
    assert response.payload["error"]["code"] == code


def test_filters_echo_back_trimmed_with_the_paging_in_use(api):
    payload = api.handle(
        "GET", "/api/v1/knowledge?q=%20A%20&limit=5&offset=0&tag="
    ).payload
    assert payload["filters"] == {
        "q": "A",
        "root_area": "",
        "document_type": "",
        "status": "",
        "tag": "",
        "limit": 5,
        "offset": 0,
    }
