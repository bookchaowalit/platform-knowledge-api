"""MCP server tests: fake HTTP transport and a throwaway key; no network and no Solo Web."""

import asyncio
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from service_auth import AuthPolicy

import knowledge_mcp as mod
from knowledge_mcp import build_server

API_KEY = mod.URL_KEY
READS = {"knowledge.search", "knowledge.document.get"}
WRITES = set()
ARGS = {"knowledge.document.get": {"path": "docs/a.md"}}
SAMPLES = [
    (
        "knowledge.search",
        {"q": "plan", "tag": "x", "limit": 5},
        "GET",
        "/api/v1/knowledge",
        {"q": "plan", "tag": "x", "limit": "5", "offset": "0"},
    ),
    (
        "knowledge.document.get",
        {"path": "docs/a.md"},
        "GET",
        "/api/v1/knowledge/document",
        {"path": "docs/a.md"},
    ),
]
DEFAULT_BODY = {"items": []}
ORIGIN = "https://solo-web.example"
SUB = "owner-subject-1"

DEFAULT_BODY = globals().get("DEFAULT_BODY", {"items": []})
IDENTITY = {
    "actor": "x",
    "workspace_id": "w",
    "workspace": "w",
    "tenant_id": "t",
    "delegation": "d",
    "token": "t",
    "authorization": "Bearer x",
    "sub": "s",
    "actor_id": "a",
    "tenant": "t",
    "owner_id": "o",
}


class Fake:
    """A fake HTTP transport: records every request and answers with ``body``."""

    def __init__(self, status: int = 200, body: object | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.status, self.body = status, DEFAULT_BODY if body is None else body

    def client(self) -> httpx.Client:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(self.status, json=self.body)

        return httpx.Client(
            transport=httpx.MockTransport(handle), base_url="https://api.test"
        )


def server(fake: Fake, read_only: bool = False):
    return build_server(fake.client(), lambda: "tok-123", read_only=read_only).server


def call(srv, name: str, args: dict | None = None):
    return asyncio.run(srv.call_tool(name, args or {}))


def names(srv) -> set[str]:
    return {tool.name for tool in asyncio.run(srv.list_tools())}


def test_tools_list_matches_the_contract():
    srv = server(Fake())
    assert names(srv) == READS | WRITES
    for tool in asyncio.run(srv.list_tools()):
        assert tool.input_schema["additionalProperties"] is False


def test_read_only_registers_only_read_tools():
    assert names(server(Fake(), read_only=True)) == READS


def test_unknown_arguments_are_rejected_before_any_http_call():
    fake = Fake()
    srv = server(fake)
    for name in sorted(READS | WRITES):
        with pytest.raises(Exception, match="invalid_arguments"):
            call(srv, name, {**ARGS.get(name, {}), "surprise": 1})
    assert fake.requests == []


def test_identity_arguments_are_never_accepted():
    fake = Fake()
    srv = server(fake)
    for name in sorted(READS | WRITES):
        for key, value in IDENTITY.items():
            with pytest.raises(Exception, match="invalid_arguments"):
                call(srv, name, {**ARGS.get(name, {}), key: value})
    assert fake.requests == []
    for name in READS | WRITES:
        tool = next(t for t in asyncio.run(srv.list_tools()) if t.name == name)
        assert not set(IDENTITY) & set(tool.input_schema["properties"])


def test_every_call_carries_the_callers_bearer_and_nothing_else_of_identity():
    fake = Fake()
    srv = server(fake)
    for name in sorted(READS | WRITES):
        call(srv, name, ARGS.get(name, {}))
    assert len(fake.requests) == len(READS | WRITES)
    for request in fake.requests:
        assert request.headers["authorization"] == "Bearer tok-123"
        assert "tok-123" not in str(request.url)
        assert request.url.host == "api.test"


def test_sample_calls_map_to_the_api():
    for name, args, method, path, params in SAMPLES:
        fake = Fake()
        call(server(fake), name, args)
        request = fake.requests[-1]
        assert (request.method, request.url.path) == (method, path), name
        assert dict(request.url.params) == params, name


@pytest.mark.parametrize(
    "status,message",
    [
        (400, "400: invalid"),
        (401, "401: unauthorized"),
        (403, "403: forbidden"),
        (404, "404: not_found"),
        (409, "409: conflict"),
        (422, "422: invalid"),
        (429, "429: rate_limited"),
        (500, "500: upstream_error"),
        (503, "503: upstream_error"),
        (418, "418: error"),
    ],
)
def test_errors_are_a_fixed_message_from_the_status_only(status, message):
    secret = {
        "detail": {
            "error": "forbidden",
            "reason": "leak-reason",
            "secret": "tok-123 do-not-leak",
        },
        "error": {"code": "leak-code", "message": "do-not-leak"},
    }
    name = sorted(READS)[0]
    with pytest.raises(Exception) as caught:
        call(server(Fake(status=status, body=secret)), name, ARGS.get(name, {}))
    assert str(caught.value).endswith(": " + message)
    assert "leak" not in str(caught.value) and "tok-123" not in str(caught.value)


def test_token_with_whitespace_or_control_characters_is_refused_without_echo():
    name = sorted(READS)[0]
    for bad in (
        "two words-abcdefgh",
        "line\nbreak-abcdefgh",
        "tab\there-abcdefgh",
        "nul\x00byte-abcdefgh",
        "   ",
    ):
        fake = Fake()
        srv = build_server(fake.client(), lambda bad=bad: bad).server
        with pytest.raises(Exception) as caught:
            call(srv, name, ARGS.get(name, {}))
        assert str(caught.value).endswith(
            "401: unauthorized"
        ) and "abcdefgh" not in str(caught.value)
        assert fake.requests == []


def test_no_tool_exposes_a_forbidden_argument_name():
    forbidden = {
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
    assert forbidden == set(mod.FORBIDDEN_ARGUMENTS)
    for tool in asyncio.run(server(Fake()).list_tools()):
        assert not forbidden & set(tool.input_schema["properties"]), tool.name
    with pytest.raises(ValueError):

        class Bad(mod.Strict):
            actor: str = "x"

        mod.Toolbox("x", "x", Fake().client(), lambda: "t").tool("x.bad", Bad, "bad")(
            lambda a: a
        )


def test_transport_failure_does_not_echo_the_error():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Authorization: Bearer tok-123 refused")

    srv = build_server(
        httpx.Client(transport=httpx.MockTransport(boom), base_url="https://api.test"),
        lambda: "tok-123",
    ).server
    with pytest.raises(Exception) as caught:
        call(srv, sorted(READS)[0], ARGS.get(sorted(READS)[0], {}))
    assert "tok-123" not in str(caught.value) and "503" in str(caught.value)


@pytest.fixture(scope="module")
def keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    return pem, public


@pytest.fixture
def env(tmp_path, keys):
    key_file, subjects = (
        tmp_path / "service-private.pem",
        tmp_path / "owner-subjects.txt",
    )
    key_file.write_text(keys[0])
    subjects.write_text(f"\n{SUB}\nsecond-subject\n")
    key_file.chmod(0o600)
    subjects.chmod(0o600)
    return {
        "SOLO_WEB_PUBLIC_ORIGIN": ORIGIN,
        "SOLO_WEB_SERVICE_KEY_FILE": str(key_file),
        "SOLO_WEB_OWNER_SUBJECTS_FILE": str(subjects),
    }


def test_minted_token_is_accepted_by_the_backends_own_verifier(env, keys):
    token = mod.mint(env)
    policy = AuthPolicy(ORIGIN, mod.AUDIENCE, keys[1], frozenset({SUB}), mod.SCOPE)
    identity = policy.authenticate({"authorization": f"Bearer {token}"})
    assert (identity.subject, identity.tenant_id, identity.scope) == (
        SUB,
        "owner",
        mod.SCOPE,
    )
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["aud"] == mod.AUDIENCE and 0 < claims["exp"] - claims["iat"] <= 60
    assert mod.mint(env) != token  # a fresh jti every time


def test_minted_token_is_rejected_for_another_audience_scope_or_subject(env, keys):
    token = mod.mint(env)
    for audience, scope, subjects in (
        ("other-api", mod.SCOPE, {SUB}),
        (mod.AUDIENCE, "other:read", {SUB}),
        (mod.AUDIENCE, mod.SCOPE, {"someone-else"}),
    ):
        policy = AuthPolicy(ORIGIN, audience, keys[1], frozenset(subjects), scope)
        with pytest.raises(Exception):
            policy.authenticate({"authorization": f"Bearer {token}"})


def test_mint_refuses_loose_files_and_bad_origin_without_echoing_secrets(env, tmp_path):
    key_file = tmp_path / "service-private.pem"
    key_file.chmod(0o644)
    with pytest.raises(mod.ConfigError, match="owner only") as caught:
        mod.mint(env)
    assert "BEGIN" not in str(caught.value)
    key_file.chmod(0o600)
    for origin in (
        "http://solo-web.example",
        "https://solo-web.example/x",
        "https://u:p@solo-web.example",
        "",
        "x",
    ):
        with pytest.raises(mod.ConfigError):
            mod.mint({**env, "SOLO_WEB_PUBLIC_ORIGIN": origin})
    (tmp_path / "owner-subjects.txt").write_text("\n  \n")
    with pytest.raises(mod.ConfigError, match="no usable subject"):
        mod.mint(env)
    (tmp_path / "owner-subjects.txt").write_text(f"{SUB} extra\n")
    with pytest.raises(mod.ConfigError, match="no usable subject") as caught:
        mod.mint(env)
    assert SUB not in str(caught.value)
    key_file.write_text("not a key SECRETKEYTEXT")
    with pytest.raises(mod.ConfigError) as caught:
        mod.mint({**env, "SOLO_WEB_OWNER_SUBJECTS_FILE": str(tmp_path / "missing")})
    assert "SECRETKEYTEXT" not in str(caught.value)


def test_a_bad_key_does_not_leak_its_text(env, tmp_path):
    (tmp_path / "service-private.pem").write_text("not a key SECRETKEYTEXT")
    with pytest.raises(mod.ConfigError) as caught:
        mod.mint(env)
    assert "SECRETKEYTEXT" not in str(caught.value)


def test_ready_token_overrides_minting_and_is_cleaned():
    assert mod.mint({"EMPIRE_TOKEN": " ready.jwt.token \n"}) == "ready.jwt.token"
    with pytest.raises(mod.ConfigError):
        mod.mint({"EMPIRE_TOKEN": "has space"})


def test_every_tool_call_sends_a_freshly_minted_token(env, keys, monkeypatch):
    fake = Fake()
    srv = build_server(fake.client(), lambda: mod.mint(env)).server
    name = sorted(READS)[0]
    call(srv, name, ARGS.get(name, {}))
    call(srv, name, ARGS.get(name, {}))
    seen = [r.headers["authorization"].removeprefix("Bearer ") for r in fake.requests]
    assert len(set(seen)) == 2
    policy = AuthPolicy(ORIGIN, mod.AUDIENCE, keys[1], frozenset({SUB}), mod.SCOPE)
    for token in seen:
        policy.authenticate({"authorization": f"Bearer {token}"})


def test_url_must_be_https_or_loopback(tmp_path):
    assert mod.api_url(API_KEY, {API_KEY: "https://x.example"}) == "https://x.example"
    assert mod.api_url(API_KEY, {API_KEY: "http://127.0.0.1:9"}) == "http://127.0.0.1:9"
    for bad in (
        "http://example.com",
        "https://u:p@x.example",
        "https://x.example?a=1",
        "https://x.example#f",
    ):
        with pytest.raises(mod.ConfigError):
            mod.api_url(API_KEY, {API_KEY: bad})
    with pytest.raises(mod.ConfigError):
        mod.api_url(API_KEY, {"EMPIRE_CLIENT_CONFIG": str(tmp_path / "none")})


def _chain(exc: BaseException):
    seen = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        yield exc
        exc = exc.__cause__ or exc.__context__


def test_validation_errors_are_fixed_and_name_no_argument_or_value():
    srv = server(Fake())
    rejected = {}
    for name in sorted(READS | WRITES):
        tool = next(t for t in asyncio.run(srv.list_tools()) if t.name == name)
        attempts = [
            {"UNTRUSTED_MARKER": "VALUE_MARKER"},
            {**ARGS.get(name, {}), "UNTRUSTED_MARKER": 1},
        ]
        attempts += [
            {**ARGS.get(name, {}), field: {"k": "VALUE_MARKER"}}
            for field in tool.input_schema["properties"]
        ]
        for args in attempts:
            try:
                call(srv, name, args)
            except Exception as exc:  # noqa: BLE001
                rejected[name] = rejected.get(name, 0) + 1
                assert str(exc).endswith("invalid_arguments") and "MARKER" not in str(
                    exc
                )
                assert exc.__cause__ is None and exc.__context__ is None
                for link in _chain(exc):
                    assert "MARKER" not in str(link) and "MARKER" not in repr(link)
    assert set(rejected) == READS | WRITES
    with pytest.raises(Exception) as caught:
        call(srv, "x.UNTRUSTED_MARKER", {"a": "VALUE_MARKER"})
    assert "MARKER" not in str(caught.value) + repr(caught.value)
