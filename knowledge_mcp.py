"""MCP server for the knowledge catalog: read-only tools over its authenticated HTTP API.

The backend trusts only short-lived RS256 service JWTs from Solo Web (``aud=knowledge-api``, ``scope=knowledge:read``,
``tenant_id=owner``, an allowlisted owner ``sub``). Backend auth is not relaxed. This process mints that same token
for itself, as the BFF does, instead of holding a long-lived credential::

    EMPIRE_KNOWLEDGE_API=https://...        env, else the owner-only ~/.config/empire-platform/client.env
    SOLO_WEB_PUBLIC_ORIGIN=https://...   the JWT issuer (Solo Web's public origin; not a secret)
    SOLO_WEB_SERVICE_KEY_FILE=...        signing key, default ~/.config/solo-web/service-private.pem (mode 600)
    SOLO_WEB_OWNER_SUBJECTS_FILE=...     default ~/.config/solo-web/owner-subjects.txt (mode 600); the first subject is used
    EMPIRE_TOKEN=...                     optional: use a ready service JWT instead of minting (never needs the key)

The key and subject are read from files on every mint, held only in memory, and never logged or returned. Tokens
live 60 seconds and are fixed to this service's audience and scope: no tool argument can change who, which
audience or which scope. Tools take no identity arguments, and unknown arguments are rejected before any HTTP call.

    knowledge-mcp                             stdio server (see .mcp.json)
"""

from __future__ import annotations

import inspect
import os
import re
import time
import urllib.parse
import uuid
from collections.abc import Callable
from datetime import date, datetime  # noqa: F401 - used by the tool models
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolFailed(ToolError):
    """Reaches the MCP client as a tool error carrying only a fixed message mapped from the HTTP status."""


class ConfigError(RuntimeError):
    pass


# --- configuration (vendored from solo-cell-kit cellkit/mcp.py; do not import across repos) -----------


def client_file(environ: Any) -> dict[str, str]:
    override = (environ.get("EMPIRE_CLIENT_CONFIG") or "").strip()
    path = (
        Path(override).expanduser()
        if override
        else Path.home() / ".config" / "empire-platform" / "client.env"
    )
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return {}
    if mode & 0o077:
        raise ConfigError(
            f"refusing client config {path}: group/world accessible; run chmod 600"
        )
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    return values


def api_url(key: str, environ: Any = None) -> str:
    """The API base URL from ``key`` (env, else client.env): https only, http only on loopback."""
    environ = os.environ if environ is None else environ
    value = (environ.get(key) or "").strip() or client_file(environ).get(key, "")
    if not value:
        raise ConfigError(f"the API is not configured: set {key} (env or client.env)")
    parts = urllib.parse.urlsplit(value)
    host = (parts.hostname or "").lower()
    if (
        parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or "?" in value
        or "#" in value
    ):
        raise ConfigError(f"{key} must not carry credentials, a query or a fragment")
    if not (parts.scheme == "https" and host) and not (
        parts.scheme == "http" and host in LOOPBACK
    ):
        raise ConfigError(
            f"{key} must be https (http only for 127.0.0.1, ::1 or localhost)"
        )
    return value


def clean_token(value: str) -> str:
    """The token without surrounding whitespace; any inner whitespace or control character is refused."""
    token = value.strip()
    if not token or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in token):
        raise ConfigError(
            "the token has an unexpected format"
        ) from None  # never echo it
    return token


AUTOMATION = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
FORBIDDEN_ARGUMENTS = frozenset(
    {
        "actor",
        "actor_id",
        "workspace",
        "workspace_id",
        "tenant",
        "tenant_id",
        "delegation",
        "token",
        "authorization",
        "owner_id",
    }
)
IDEMPOTENCY_KEY = r"^[A-Za-z0-9._:-]{8,128}$"


def read_owner_file(path: Path, what: str) -> str:
    if path.stat().st_mode & 0o077:
        raise ConfigError(
            f"{what} file {path} must be readable by its owner only (chmod 600)"
        )
    return path.read_text(encoding="utf-8").strip()


# --- the server -----------------------------------------------------------------------------------


class StrictMCPServer(MCPServer):
    """MCPServer that validates arguments against a strict model before any tool runs."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.argument_models: dict[str, type[Strict]] = {}

    async def list_tools(self):  # type: ignore[override]
        tools = await super().list_tools()
        for tool in tools:
            model = self.argument_models.get(tool.name)
            if model is not None:
                tool.input_schema = {
                    **model.model_json_schema(),
                    "additionalProperties": False,
                }
        return tools

    async def call_tool(  # type: ignore[override]
        self, name: str, arguments: dict[str, Any], context: Any = None
    ):
        model = self.argument_models.get(name)
        if model is None:
            raise ToolFailed("unknown_tool")
        try:
            model.model_validate(arguments or {})
            valid = True
        except ValidationError:
            valid = False  # raised below, outside the handler: no cause or context keeps the rejected input
        if not valid:
            raise ToolFailed(
                "invalid_arguments"
            ) from None  # never names the argument or echoes its value
        return await super().call_tool(name, arguments, context)


REASONS = {
    400: "invalid",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "invalid",
    409: "conflict",
    410: "not_found",
    413: "invalid",
    422: "invalid",
    429: "rate_limited",
}


def failure(status: int) -> str:
    """A fixed message from the status alone: upstream text and detail fields are never included."""
    return f"{status}: {'upstream_error' if status >= 500 else REASONS.get(status, 'error')}"


class Toolbox:
    """Registers tools on a strict server. Every tool is one HTTP call with the caller's own bearer."""

    def __init__(
        self,
        name: str,
        instructions: str,
        client: httpx.Client,
        token: Callable[[], str],
        *,
        read_only: bool = False,
        empire_headers: bool = False,
        server: StrictMCPServer | None = None,
    ) -> None:
        self.server = server or StrictMCPServer(name, instructions=instructions)
        self.client, self.token, self.read_only, self.empire_headers = (
            client,
            token,
            read_only,
            empire_headers,
        )

    def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        idempotency_key: str | None = None,
    ) -> Any:
        try:
            bearer = clean_token(self.token())
        except ConfigError:
            raise ToolFailed(
                "401: unauthorized"
            ) from None  # no token, or one with an unexpected format
        headers = {"Authorization": f"Bearer {bearer}"}
        if self.empire_headers:
            headers |= {
                "X-Empire-Channel": "mcp",
                "X-Correlation-Id": f"mcp-{uuid.uuid4().hex}",
            }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self.client.request(
                method, path, headers=headers, params=params, json=body
            )
        except httpx.HTTPError:
            raise ToolFailed(
                "503: unavailable"
            ) from None  # never echo the transport error
        if response.status_code >= 400:
            raise ToolFailed(failure(response.status_code))
        return response.json() if response.content else {"ok": True}

    def tool(
        self, name: str, model: type[Strict], description: str, *, write: bool = False
    ):
        def register(handler: Callable[[Any], Any]) -> Callable[[Any], Any]:
            banned = FORBIDDEN_ARGUMENTS & set(model.model_fields)
            if banned:
                raise ValueError(
                    f"{name}: identity arguments are not allowed: {sorted(banned)}"
                )
            if write and self.read_only:
                return handler

            def function(**kwargs: Any) -> Any:
                return handler(model.model_validate(kwargs))

            params = [
                inspect.Parameter(
                    field,
                    inspect.Parameter.KEYWORD_ONLY,
                    default=inspect.Parameter.empty
                    if info.is_required()
                    else info.default,
                    annotation=info.annotation,
                )
                for field, info in model.model_fields.items()
            ]
            function.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
            function.__annotations__ = {p.name: p.annotation for p in params}
            function.__name__ = name.replace(".", "_")
            self.server.argument_models[name] = model
            self.server.tool(name=name, description=description)(function)
            return handler

        return register


def query(
    args: BaseModel, rename: dict[str, str] | None = None, drop: tuple[str, ...] = ()
) -> dict[str, Any]:
    """The set arguments as query parameters (``rename`` maps argument names to the API's names)."""
    values = args.model_dump(mode="json", exclude_none=True, exclude=set(drop))
    return {(rename or {}).get(key, key): value for key, value in values.items()}


def new_key() -> str:
    return f"mcp-{uuid.uuid4().hex}"


class Id(Strict):
    id: int = Field(gt=0)


class Search(Strict):
    q: str | None = Field(default=None, max_length=120)
    root_area: str | None = Field(default=None, max_length=80)
    document_type: str | None = Field(default=None, max_length=80)
    status: str | None = Field(default=None, max_length=100)
    tag: str | None = Field(default=None, max_length=60)
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=5000)


class DocumentRef(Strict):
    path: str = Field(min_length=1, max_length=500)


def build_server(
    client: httpx.Client,
    token: Callable[[], str],
    *,
    read_only: bool = True,
    server: StrictMCPServer | None = None,
) -> Toolbox:
    box = Toolbox(
        "knowledge",
        "Solo Empire knowledge catalog (metadata only; document bodies stay in their "
        "workspaces). Read-only.",
        client,
        token,
        read_only=read_only,
        server=server,
    )

    @box.tool(
        "knowledge.search",
        Search,
        "Search the document catalog metadata by text, area, type, status or tag.",
    )
    def search(a: Search) -> Any:
        return box.call("GET", "/api/v1/knowledge", params=query(a))

    @box.tool(
        "knowledge.document.get",
        DocumentRef,
        "One document's metadata and its parent, related and child documents.",
    )
    def document_get(a: DocumentRef) -> Any:
        return box.call("GET", "/api/v1/knowledge/document", params=query(a))

    return box


AUDIENCE = "knowledge-api"
SCOPE = "knowledge:read"
URL_KEY = "EMPIRE_KNOWLEDGE_API"
TOKEN_LIFETIME = 60
SUBJECT = re.compile(r"^[^\s\x00-\x1f\x7f]{1,512}$")
HOME_CONFIG = Path.home() / ".config" / "solo-web"


def issuer(environ: Any) -> str:
    value = (environ.get("SOLO_WEB_PUBLIC_ORIGIN") or "").strip()
    parts = urllib.parse.urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
        or value != f"https://{parts.netloc}"
    ):
        raise ConfigError(
            "SOLO_WEB_PUBLIC_ORIGIN must be Solo Web's https origin (no path, query or credentials)"
        )
    return value


def mint(environ: Any = None, now: float | None = None) -> str:
    """A fresh service JWT for this service only, signed with Solo Web's service key."""
    import jwt  # PyJWT with cryptography, the same library the backends verify with

    environ = os.environ if environ is None else environ
    if environ.get("EMPIRE_TOKEN"):
        return clean_token(environ["EMPIRE_TOKEN"])
    origin = issuer(environ)
    key_path = Path(
        environ.get("SOLO_WEB_SERVICE_KEY_FILE") or HOME_CONFIG / "service-private.pem"
    ).expanduser()
    subjects_path = Path(
        environ.get("SOLO_WEB_OWNER_SUBJECTS_FILE")
        or HOME_CONFIG / "owner-subjects.txt"
    ).expanduser()
    try:
        key = read_owner_file(key_path, "service key")
        lines = [
            line.strip()
            for line in read_owner_file(subjects_path, "owner subjects").splitlines()
            if line.strip()
        ]
    except OSError:
        raise ConfigError(
            "the Solo Web service key or owner subjects file cannot be read"
        ) from None
    if not lines or not SUBJECT.fullmatch(lines[0]):
        raise ConfigError("the owner subjects file has no usable subject")
    issued = int(time.time() if now is None else now)
    claims = {
        "iss": origin,
        "aud": AUDIENCE,
        "sub": lines[0],
        "tenant_id": "owner",
        "scope": SCOPE,
        "iat": issued,
        "nbf": issued,
        "exp": issued + TOKEN_LIFETIME,
        "jti": str(uuid.uuid4()),
    }
    try:
        return jwt.encode(claims, key, algorithm="RS256", headers={"typ": "JWT"})
    except Exception:  # noqa: BLE001 - a bad key must not surface its text
        raise ConfigError(
            "the Solo Web service key is not a usable RSA private key"
        ) from None


def main() -> None:
    try:
        url = api_url(URL_KEY)
        mint()  # fail at start-up, not on the first call
    except ConfigError as exc:
        raise SystemExit(str(exc)) from None
    client = httpx.Client(base_url=url, timeout=30, follow_redirects=False)
    build_server(client, mint).server.run("stdio")


if __name__ == "__main__":
    main()
