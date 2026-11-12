"""SPEC-002 check for ``http.server`` handlers, on top of ``gateway.py`` (byte-identical to the cellkit cells).

``result_for`` returns None for an open path or when the check is off, else ``valid``/``missing``/``invalid``
(reported, route family only). The body is read through ``read_body`` only when the header alone does not
settle it and the body is one the gateway could have sent, so a direct caller cannot make the service
buffer a large body; the handler keeps what ``read_body`` returned for its own use.

``extra_open`` adds this service's own probe and public paths (exact, or a prefix ending in ``/``), for
example ``/ready``, or provider callbacks that never come through the gateway. A request with more than one
``Authorization`` header is ``invalid``: the MAC covers one value and the service must not see another.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

import gateway


class Headers(Protocol):
    def get(self, name: str, default: None = None, /) -> str | None: ...

    def get_all(self, name: str, failobj: None = None, /) -> list[str] | None: ...


def _open(path: str, extra_open: tuple[str, ...]) -> bool:
    return gateway.is_open(path) or any(
        path == p or (p.endswith("/") and path.startswith(p)) for p in extra_open
    )


def result_for(
    check: gateway.GatewayCheck | None,
    method: str,
    target: str,
    headers: Headers,
    read_body: Callable[[], bytes],
    extra_open: tuple[str, ...] = (),
) -> str | None:
    path = urlsplit(target).path
    if check is None or _open(path, extra_open):
        return None
    header = headers.get(gateway.HEADER)
    result = gateway.precheck(header, time.time())
    if result is None and len(headers.get_all("Authorization") or []) > 1:
        result = "invalid"
    if result is None and gateway.body_too_big(
        headers.get("Content-Length"), headers.get("Transfer-Encoding")
    ):
        result = "invalid"
    if result is None:
        result = gateway.check(
            check.key,
            check.system,
            header,
            method,
            target,
            headers.get("Authorization") or "",
            time.time(),
            headers.get("Content-Type") or "",
            read_body(),
        )
    gateway.report(result, method, path)
    return result


def refused(
    check: gateway.GatewayCheck | None, target: str, result: str | None
) -> bool:
    return (
        check is not None
        and result is not None
        and check.refuses(urlsplit(target).path, result)
    )
