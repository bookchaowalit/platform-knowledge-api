"""SPEC-002 for the http.server handler: open paths, report, enforce, body handling (gateway_http.py)."""

from __future__ import annotations

import email.message
import logging
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gateway  # noqa: E402
import gateway_http  # noqa: E402
import server  # noqa: E402 - sets the 16 MiB knowledge body limit

KEY = bytes(range(32))
SYSTEM = "knowledge"


def headers(**values: str) -> email.message.Message:
    m = email.message.Message()
    for name, value in values.items():
        m[name.replace("_", "-")] = value
    return m


def signed(
    method: str, target: str, auth: str = "Bearer t", body: bytes = b"", ctype: str = ""
) -> str:
    return gateway.expected(
        KEY, SYSTEM, method, target, int(time.time()), "req-1", auth, ctype, body
    )


def check(mode: str = "report") -> gateway.GatewayCheck:
    return gateway.GatewayCheck(SYSTEM, mode, KEY)  # type: ignore[arg-type]


def no_body() -> bytes:
    raise AssertionError("the body must not be read")


def test_off_and_open_paths_are_not_checked():
    assert gateway_http.result_for(None, "GET", "/api/v1/x", headers(), no_body) is None
    for path in ("/health", "/readyz", "/openapi.json", "/.well-known/jwks.json"):
        assert (
            gateway_http.result_for(check("enforce"), "GET", path, headers(), no_body)
            is None
        )
        assert not gateway_http.refused(check("enforce"), path, None)


def test_report_logs_and_serves(caplog):
    caplog.set_level(logging.INFO, logger="cellkit.gateway")
    target = "/api/v1/things?q=1"
    good = headers(X_Gateway_Assertion=signed("GET", target), Authorization="Bearer t")
    assert gateway_http.result_for(check(), "GET", target, good, lambda: b"") == "valid"
    assert (
        gateway_http.result_for(
            check(), "GET", target, headers(Authorization="Bearer t"), no_body
        )
        == "missing"
    )
    assert not gateway_http.refused(check(), target, "missing")
    lines = [r.getMessage() for r in caplog.records]
    assert "gateway_assertion result=valid method=GET path=/api/v1" in lines
    assert all("q=1" not in line and "Bearer" not in line for line in lines)


def test_enforce_refuses_missing_invalid_and_tampered():
    target = "/api/v1/things"
    assert gateway_http.refused(check("enforce"), target, "missing")
    tampered = headers(
        X_Gateway_Assertion=signed("GET", target), Authorization="Bearer other"
    )
    result = gateway_http.result_for(
        check("enforce"), "GET", target, tampered, lambda: b""
    )
    assert result == "invalid"
    assert gateway_http.refused(check("enforce"), target, result)
    good = headers(X_Gateway_Assertion=signed("GET", target), Authorization="Bearer t")
    assert not gateway_http.refused(
        check("enforce"),
        target,
        gateway_http.result_for(check("enforce"), "GET", target, good, lambda: b""),
    )


def test_body_is_signed_and_a_big_or_chunked_body_is_never_read():
    body = b'{"a":1}'
    target = "/api/v1/things"
    good = headers(
        X_Gateway_Assertion=signed("POST", target, body=body, ctype="application/json"),
        Authorization="Bearer t",
        Content_Type="application/json",
        Content_Length=str(len(body)),
    )
    assert (
        gateway_http.result_for(check(), "POST", target, good, lambda: body) == "valid"
    )
    assert (
        gateway_http.result_for(check(), "POST", target, good, lambda: b'{"a":2}')
        == "invalid"
    )
    forged = signed("POST", target)
    assert (
        gateway.MAX_BODY_BYTES == 16 * 1024 * 1024
    )  # the knowledge route's gateway limit
    big = headers(
        X_Gateway_Assertion=forged, Content_Length=str(gateway.MAX_BODY_BYTES + 1)
    )
    chunked = headers(X_Gateway_Assertion=forged, Transfer_Encoding="chunked")
    assert gateway_http.result_for(check(), "POST", target, big, no_body) == "invalid"
    assert (
        gateway_http.result_for(check(), "POST", target, chunked, no_body) == "invalid"
    )


def test_the_handler_refuses_a_direct_call_in_enforce_and_keeps_health_open():
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    handler = type(
        "Handler", (server.KnowledgeRequestHandler,), {"gateway": check("enforce")}
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(base + "/api/v1/knowledge", timeout=10)
        assert refused.value.code in (
            401,
            403,
        )  # auth first (401 without a token), never served
        with urllib.request.urlopen(base + "/health", timeout=10) as response:
            assert response.status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_main_installs_the_check_with_the_route_key():
    assert 'GatewayCheck.from_env("knowledge")' in Path(server.__file__).read_text()
