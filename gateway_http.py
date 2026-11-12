"""SPEC-002 check for ``http.server`` handlers, on top of ``gateway.py`` (byte-identical to the cellkit cells).

``result_for`` returns None for an open path or when the check is off, else ``valid``/``missing``/``invalid``
(reported, route family only). The body is read through ``read_body`` only when the header alone does not
settle it and the body is one the gateway could have sent, so a direct caller cannot make the service
buffer a large body; the handler keeps what ``read_body`` returned for its own use.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

import gateway


class Headers(Protocol):
    def get(self, name: str, default: None = None, /) -> str | None: ...


def result_for(check: gateway.GatewayCheck | None, method: str, target: str, headers: Headers,
               read_body: Callable[[], bytes]) -> str | None:
    path = urlsplit(target).path
    if check is None or gateway.is_open(path):
        return None
    header = headers.get(gateway.HEADER)
    result = gateway.precheck(header, time.time())
    if result is None and gateway.body_too_big(headers.get("Content-Length"), headers.get("Transfer-Encoding")):
        result = "invalid"
    if result is None:
        result = gateway.check(check.key, check.system, header, method, target, headers.get("Authorization") or "",
                               time.time(), headers.get("Content-Type") or "", read_body())
    gateway.report(result, method, path)
    return result


def refused(check: gateway.GatewayCheck | None, target: str, result: str | None) -> bool:
    return check is not None and result is not None and check.refuses(urlsplit(target).path, result)
