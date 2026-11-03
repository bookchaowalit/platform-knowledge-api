"""SPEC-002 store routes over the API handler and the real HTTP transport (in-memory store)."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from service_auth import AuthPolicy

from document_store import MemoryStore
from server import (
    CATALOG_SCHEMA_VERSION,
    MAX_REQUEST_BYTES,
    KnowledgeApi,
    KnowledgeRequestHandler,
)

SPACE = "knowledge-base"
DOC = f"/api/v1/store/document?space={SPACE}&path=notes/a.md"


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    path = tmp_path / "document-catalog.json"
    path.write_text(
        json.dumps({"schema_version": CATALOG_SCHEMA_VERSION, "documents": []}),
        encoding="utf-8",
    )
    return path


def dev_api(catalog: Path, store: MemoryStore | None = None) -> KnowledgeApi:
    return KnowledgeApi(
        catalog,
        token="",
        environment="development",
        store=store or MemoryStore(spaces=(SPACE,)),
    )


def body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


def test_put_get_list_history_and_delete_over_the_api(catalog: Path) -> None:
    api = dev_api(catalog)
    created = api.handle("PUT", DOC, body=body({"content": "# A\n", "if_version": 0}))
    assert created.status == 201
    assert created.payload["document"]["version"] == 1
    assert created.payload["changed"] is True

    read = api.handle("GET", DOC)
    assert read.status == 200
    assert read.payload["document"]["content"] == "# A\n"
    assert read.payload["document"]["encoding"] == "utf-8"

    png = b"\x89PNG\r\n\x1a\n\xff"
    image = f"/api/v1/store/document?space={SPACE}&path=assets/x.png"
    encoded = base64.b64encode(png).decode()
    assert (
        api.handle("PUT", image, body=body({"content_base64": encoded})).status == 201
    )
    assert api.handle("GET", image).payload["document"]["content_base64"] == encoded

    same = api.handle("PUT", DOC, body=body({"content": "# A\n"}))
    assert (same.status, same.payload["changed"]) == (200, False)

    listed = api.handle("GET", f"/api/v1/store/documents?space={SPACE}&prefix=notes/")
    assert listed.status == 200
    assert [d["path"] for d in listed.payload["documents"]] == ["notes/a.md"]
    assert "content" not in listed.payload["documents"][0]
    assert listed.payload["total"] == 1

    stale = api.handle("PUT", DOC, body=body({"content": "x", "if_version": 7}))
    assert (stale.status, stale.payload["error"]["code"]) == (409, "version_conflict")

    deleted = api.handle("DELETE", DOC + "&if_version=1")
    assert deleted.status == 200
    assert deleted.payload["document"]["deleted"] is True
    assert api.handle("GET", DOC).status == 404
    old = api.handle("GET", DOC + "&version=1")
    assert old.payload["document"]["content"] == "# A\n"
    history = api.handle("GET", f"/api/v1/store/history?space={SPACE}&path=notes/a.md")
    assert [v["version"] for v in history.payload["versions"]] == [1, 2]


@pytest.mark.parametrize(
    ("method", "target", "payload", "status", "code"),
    [
        ("PUT", DOC, b"not json", 400, "invalid_json"),
        (
            "PUT",
            DOC,
            body({"content": "a", "content_base64": "YQ=="}),
            400,
            "invalid_body",
        ),
        ("PUT", DOC, body({}), 400, "invalid_body"),
        ("PUT", DOC, body({"content_base64": "***"}), 400, "invalid_body"),
        ("PUT", DOC, body({"content": "a", "extra": 1}), 400, "invalid_body"),
        ("PUT", DOC, body({"content": "a", "if_version": "1"}), 400, "invalid_body"),
        (
            "PUT",
            DOC,
            body({"content": "a", "visibility": "team"}),
            400,
            "invalid_visibility",
        ),
        (
            "GET",
            "/api/v1/store/document?space=knowledge-base",
            None,
            400,
            "missing_path",
        ),
        ("GET", "/api/v1/store/document?path=a.md", None, 400, "missing_space"),
        ("GET", DOC + "&version=x", None, 400, "invalid_version"),
        (
            "GET",
            "/api/v1/store/document?space=nope&path=a.md",
            None,
            404,
            "unknown_space",
        ),
        (
            "GET",
            "/api/v1/store/document?space=knowledge-base&path=../a.md",
            None,
            400,
            "invalid_path",
        ),
        (
            "GET",
            f"/api/v1/store/documents?space={SPACE}&limit=0",
            None,
            400,
            "invalid_limit",
        ),
        ("POST", DOC, body({"content": "a"}), 405, "method_not_allowed"),
    ],
)
def test_bad_store_requests_have_stable_codes(
    catalog: Path,
    method: str,
    target: str,
    payload: bytes | None,
    status: int,
    code: str,
) -> None:
    response = dev_api(catalog).handle(method, target, body=payload)
    assert (response.status, response.payload["error"]["code"]) == (status, code)


def test_without_a_database_the_store_is_a_503(
    catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KNOWLEDGE_STORE_DATABASE_URL", raising=False)
    api = KnowledgeApi(catalog, token="", environment="development")
    response = api.handle("GET", DOC)
    assert (response.status, response.payload["error"]["code"]) == (
        503,
        "store_unavailable",
    )
    # Readiness keeps its catalog and auth verdict; the store is only reported.
    ready = api.handle("GET", "/ready")
    assert ready.payload["source"] == "available"
    assert ready.payload["store"] == "disabled"
    configured = dev_api(catalog).handle("GET", "/ready")
    assert configured.payload["store"] == "available"


class Keys:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public = (
            self.key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )

    def policy(self, scope: str) -> AuthPolicy:
        return AuthPolicy(
            "https://solo.example",
            "knowledge-api",
            self.public,
            frozenset({"fixture-owner"}),
            scope,
        )

    def headers(self, scope: str) -> dict[str, str]:
        now = int(time.time())
        claims = {
            "iss": "https://solo.example",
            "aud": "knowledge-api",
            "sub": "fixture-owner",
            "tenant_id": "owner",
            "scope": scope,
            "iat": now,
            "nbf": now,
            "exp": now + 60,
            "jti": f"fixture-{scope}",
        }
        return {
            "Authorization": "Bearer " + jwt.encode(claims, self.key, algorithm="RS256")
        }


def test_writes_need_the_write_scope_and_reads_the_read_scope(catalog: Path) -> None:
    keys = Keys()
    api = KnowledgeApi(
        catalog,
        environment="production",
        auth_policy=keys.policy("knowledge:read"),
        write_policy=keys.policy("knowledge:write"),
        store=MemoryStore(spaces=(SPACE,)),
    )
    read, write = keys.headers("knowledge:read"), keys.headers("knowledge:write")
    payload = body({"content": "x"})
    assert api.handle("PUT", DOC, body=payload).status == 401
    assert api.handle("PUT", DOC, headers=read, body=payload).status == 403
    assert api.handle("DELETE", DOC, headers=read).status == 403
    assert api.handle("PUT", DOC, headers=write, body=payload).status == 201
    assert api.handle("GET", DOC, headers=write).status == 403
    assert api.handle("GET", DOC, headers=read).status == 200
    assert (
        api.handle("GET", f"/api/v1/store/documents?space={SPACE}", headers=read).status
        == 200
    )
    # The catalog keeps its own read scope.
    assert api.handle("GET", "/api/v1/knowledge", headers=write).status == 403


@pytest.fixture
def http_server(catalog: Path) -> Iterator[tuple[str, int]]:
    class Handler(KnowledgeRequestHandler):
        api = KnowledgeApi(
            catalog,
            token="test-only-token",
            environment="development",
            store=MemoryStore(spaces=(SPACE,)),
        )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield str(host), int(port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_http_transport_reads_the_request_body_within_limits(
    http_server: tuple[str, int],
) -> None:
    auth = {
        "Authorization": "Bearer test-only-token",
        "Content-Type": "application/json",
    }
    connection = HTTPConnection(*http_server, timeout=5)
    try:
        connection.request("PUT", DOC, body=body({"content": "สวัสดี"}), headers=auth)
        response = connection.getresponse()
        assert response.status == 201
        response.read()

        connection.request("GET", DOC, headers=auth)
        response = connection.getresponse()
        assert json.loads(response.read())["document"]["content"] == "สวัสดี"

        # A declared body over the limit is refused before it is read.
        connection.putrequest("PUT", DOC)
        for name, value in auth.items():
            connection.putheader(name, value)
        connection.putheader("Content-Length", str(MAX_REQUEST_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 413
        assert json.loads(response.read())["error"]["code"] == "request_too_large"
    finally:
        connection.close()

    connection = HTTPConnection(*http_server, timeout=5)
    try:
        connection.request("OPTIONS", DOC, headers={"Origin": "http://localhost:3300"})
        response = connection.getresponse()
        response.read()
        methods = response.getheader("Access-Control-Allow-Methods") or ""
        assert "PUT" in methods
        assert "DELETE" in methods
    finally:
        connection.close()


def test_readiness_never_waits_on_a_slow_store(catalog: Path) -> None:
    import threading

    release = threading.Event()

    class Slow(MemoryStore):
        def ping(self) -> None:
            release.wait(10)

    api = dev_api(catalog, Slow(spaces=(SPACE,)))
    started = time.monotonic()
    first = api.handle("GET", "/ready")
    assert time.monotonic() - started < 2
    assert first.payload["store"] == "checking"
    # A second /ready does not start a second probe while one is still running.
    assert api.handle("GET", "/ready").payload["store"] == "checking"
    release.set()
    assert api._probe is not None
    api._probe.join(2)
    assert api.handle("GET", "/ready").payload["store"] == "available"


def test_an_unauthorized_write_is_refused_before_the_body_is_read(
    catalog: Path,
) -> None:
    keys = Keys()

    class Handler(KnowledgeRequestHandler):
        api = KnowledgeApi(
            catalog,
            environment="production",
            auth_policy=keys.policy("knowledge:read"),
            write_policy=keys.policy("knowledge:write"),
            store=MemoryStore(spaces=(SPACE,)),
        )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    connection = HTTPConnection(str(host), int(port), timeout=5)
    try:
        # Declares a body it never sends: the server must answer without waiting for it.
        connection.putrequest("PUT", DOC)
        connection.putheader("Content-Length", str(MAX_REQUEST_BYTES))
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 401
        response.read()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
