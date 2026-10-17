#!/usr/bin/env python3
"""Focused offline tests for the Knowledge Atlas metadata boundary."""

from __future__ import annotations

import json
import tempfile
import unittest
import time
import subprocess
import os
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from service_auth import AuthPolicy
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))
from server import KnowledgeApi, KnowledgeRequestHandler  # noqa: E402


class KnowledgeApiTest(unittest.TestCase):
    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        handle.close()
        self.catalog_path = Path(handle.name)
        self.catalog_path.write_text(
            json.dumps(
                {
                    "schema_version": "solo-empire.document-catalog.v1",
                    "generated_by": "fixture",
                    "scope": {
                        "included": "first-party markdown",
                        "excluded": ["private bodies"],
                    },
                    "documents": [
                        {
                            "path": "docs/one.md",
                            "title": "Alpha Guide",
                            "description": "A bounded guide",
                            "document_type": "guide",
                            "status": "current",
                            "root_area": "docs",
                            "tags": ["alpha", "guide"],
                            "has_frontmatter": True,
                            "internal_link_count": 2,
                            "updated": "2026-09-08",
                            "related": ["../knowledge/two.md"],
                        },
                        {
                            "path": "knowledge/two.md",
                            "title": "Beta Notes",
                            "description": "A supporting note",
                            "document_type": "document",
                            "status": "planning",
                            "root_area": "knowledge",
                            "tags": ["beta"],
                            "has_frontmatter": False,
                            "internal_link_count": 0,
                            "parent": "docs/one.md",
                            "updated": "2026-09-07",
                        },
                        {
                            "path": "clients/acme/private.md",
                            "title": "Private Client",
                            "description": "must not enter projection",
                            "document_type": "document",
                            "status": "active",
                            "root_area": "clients",
                            "tags": ["private"],
                            "has_frontmatter": True,
                        },
                        {
                            "path": "clients/acme/README.md",
                            "title": "Client Boundary",
                            "description": "safe boundary readme",
                            "document_type": "index",
                            "status": "current",
                            "root_area": "clients",
                            "tags": ["boundary"],
                            "has_frontmatter": True,
                        },
                        {
                            "path": "Clients/Oak/README.md",
                            "title": "Case Boundary",
                            "description": "safe nested boundary readme",
                            "document_type": "index",
                            "status": "current",
                            "root_area": "clients",
                            "tags": ["boundary"],
                            "has_frontmatter": True,
                        },
                        {
                            "path": "Clients/Oak/Private.md",
                            "title": "Private Client",
                            "description": "must not enter projection",
                            "document_type": "document",
                            "status": "active",
                            "root_area": "clients",
                            "tags": ["private"],
                            "has_frontmatter": True,
                        },
                        {
                            "path": "DECISIONS/RECORDS/secret.md",
                            "title": "Decision Record",
                            "description": "must not enter projection",
                            "document_type": "document",
                            "status": "active",
                            "root_area": "decisions",
                            "tags": ["private"],
                            "has_frontmatter": True,
                        },
                        {
                            "path": ".SSH/id.md",
                            "title": "Credential",
                            "description": "must not enter projection",
                            "document_type": "document",
                            "status": "active",
                            "root_area": "hidden",
                            "tags": ["private"],
                            "has_frontmatter": True,
                        },
                        {
                            "path": "data/raw/secret.md",
                            "title": "Raw Secret",
                            "description": "must not enter projection",
                            "document_type": "document",
                            "status": "active",
                            "root_area": "data",
                            "tags": ["secret"],
                            "has_frontmatter": True,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.api = KnowledgeApi(self.catalog_path, token="", environment="development")

    def tearDown(self) -> None:
        self.catalog_path.unlink(missing_ok=True)

    def test_scoped_production_auth_and_safe_errors(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = (
            key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )
        policy = AuthPolicy(
            "https://solo.example",
            "knowledge-api",
            public,
            frozenset({"fixture-owner"}),
            "knowledge:read",
        )
        api = KnowledgeApi(
            self.catalog_path, environment="production", auth_policy=policy
        )
        now = int(time.time())
        claims = dict(
            iss="https://solo.example",
            aud="knowledge-api",
            sub="fixture-owner",
            tenant_id="owner",
            scope="knowledge:read",
            iat=now,
            nbf=now,
            exp=now + 120,
            jti="fixture-request",
        )

        def headers(changes=None):
            return {
                "Authorization": "Bearer "
                + jwt.encode({**claims, **(changes or {})}, key, algorithm="RS256")
            }

        self.assertEqual(api.handle("GET", "/health").status, 200)
        self.assertEqual(api.handle("GET", "/ready").status, 200)
        self.assertEqual(api.handle("GET", "/api/v1/knowledge").status, 401)
        response = api.handle("GET", "/api/v1/knowledge", headers=headers())
        self.assertEqual(response.status, 200)
        self.assertEqual(len(response.payload["catalog"]["revision"]), 64)
        for changes, expected in (
            ({"scope": "other:read"}, 403),
            ({"tenant_id": "other"}, 403),
            ({"sub": "other-owner"}, 403),
            ({"iss": "https://other.example"}, 401),
            ({"aud": "other-api"}, 401),
            ({"exp": now - 300}, 401),
            ({"nbf": now + 300}, 401),
        ):
            with self.subTest(changes=changes):
                denied = api.handle(
                    "GET", "/api/v1/knowledge", headers=headers(changes)
                )
                self.assertEqual(denied.status, expected)
                self.assertEqual(set(denied.payload), {"error"})
                self.assertNotIn("Bearer", json.dumps(denied.payload))
        self.assertEqual(
            api.handle("GET", "/api/v1/knowledge/document?path=docs/one.md").status, 401
        )

    def test_static_token_is_never_production_authorization(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            api = KnowledgeApi(
                self.catalog_path, token="development-only", environment="production"
            )
        self.assertEqual(
            api.handle(
                "GET",
                "/api/v1/knowledge",
                headers={"Authorization": "Bearer development-only"},
            ).status,
            503,
        )
        self.assertEqual(api.handle("GET", "/health").status, 200)
        self.assertEqual(api.handle("GET", "/ready").payload["auth"], "disabled")

    def test_readiness_and_remote_bind_fail_closed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            api = KnowledgeApi(self.catalog_path, environment="development")
        self.assertEqual(api.handle("GET", "/ready").payload["auth"], "development")
        self.assertEqual(api.handle("GET", "/ready").status, 503)
        self.catalog_path.unlink()
        self.assertEqual(api.handle("GET", "/ready").payload["source"], "unavailable")
        self.assertEqual(api.handle("GET", "/health").status, 200)
        env = {
            **os.environ,
            "KNOWLEDGE_API_TOKEN": "legacy-token",
            "KNOWLEDGE_API_ENV": "development",
        }
        for suffix in ("AUTH_ISSUER", "AUTH_PUBLIC_KEY", "AUTH_SUBJECTS"):
            env.pop("KNOWLEDGE_API_" + suffix, None)
        result = subprocess.run(
            [sys.executable, str(ROOT / "server.py"), "--host", "0.0.0.0"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("KNOWLEDGE_API_AUTH_*", result.stderr)

    def test_overview_is_bounded_searchable_and_metadata_only(self) -> None:
        response = self.api.handle("GET", "/api/v1/knowledge?limit=10")
        self.assertEqual(response.status, 200)
        payload = response.payload
        self.assertEqual(payload["schema_version"], "solo-empire.knowledge.v1")
        self.assertEqual(payload["summary"]["documents_total"], 4)
        self.assertEqual(payload["summary"]["root_areas"], 3)
        self.assertEqual(
            {row["path"] for row in payload["documents"]},
            {
                "docs/one.md",
                "knowledge/two.md",
                "clients/acme/README.md",
                "Clients/Oak/README.md",
            },
        )
        self.assertNotIn("body", payload["documents"][0])
        filtered = self.api.handle("GET", "/api/v1/knowledge?q=Alpha&tag=guide")
        self.assertEqual(
            [row["path"] for row in filtered.payload["documents"]], ["docs/one.md"]
        )

    def test_detail_resolves_relationships_without_reading_body(self) -> None:
        response = self.api.handle(
            "GET", "/api/v1/knowledge/document?path=docs%2Fone.md"
        )
        self.assertEqual(response.status, 200)
        document = response.payload["document"]
        self.assertEqual(document["related"], ["knowledge/two.md"])
        self.assertEqual(document["children"], ["knowledge/two.md"])
        self.assertEqual(response.payload["neighbors"][0]["relation"], "related")
        self.assertNotIn("body", document)

    def test_auth_and_safe_errors(self) -> None:
        protected = KnowledgeApi(self.catalog_path, token="catalog-test-token")
        self.assertEqual(protected.handle("GET", "/api/v1/knowledge").status, 401)
        self.assertEqual(
            protected.handle(
                "GET",
                "/api/v1/knowledge",
                headers={"Authorization": "Bearer catalog-test-token"},
            ).status,
            200,
        )
        production = KnowledgeApi(self.catalog_path, token="", environment="production")
        self.assertEqual(production.handle("GET", "/api/v1/knowledge").status, 503)
        self.assertEqual(
            self.api.handle("GET", "/api/v1/knowledge?limit=101").status, 400
        )
        self.assertEqual(
            self.api.handle(
                "GET", "/api/v1/knowledge/document?path=../secret.md"
            ).status,
            400,
        )
        self.assertEqual(
            self.api.handle("GET", "/api/v1/knowledge/document?path=missing.md").status,
            404,
        )

    def test_malformed_catalog_fails_closed(self) -> None:
        self.catalog_path.write_text("{}", encoding="utf-8")
        response = self.api.handle("GET", "/api/v1/knowledge")
        self.assertEqual(response.status, 503)
        self.assertEqual(
            response.payload["error"]["code"], "catalog_schema_unsupported"
        )

    def test_pagination_filters_and_read_only(self) -> None:
        page = self.api.handle("GET", "/api/v1/knowledge?limit=1&offset=1").payload
        self.assertEqual(len(page["documents"]), 1)
        self.assertEqual(page["summary"]["documents_matched"], 4)
        filtered = self.api.handle(
            "GET", "/api/v1/knowledge?root_area=docs&document_type=guide&status=current"
        ).payload
        self.assertEqual(filtered["summary"]["documents_matched"], 1)
        for target in ("?offset=-1", "?limit=abc", "?q=" + "a" * 121):
            self.assertEqual(
                self.api.handle("GET", "/api/v1/knowledge" + target).status, 400
            )
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertEqual(self.api.handle(method, "/api/v1/knowledge").status, 405)

    def test_reload_excludes_bodies_and_unknown_relationships(self) -> None:
        self.api.handle("GET", "/api/v1/knowledge")
        fixture = json.loads(self.catalog_path.read_text())
        fixture["documents"][0].update(
            {
                "title": "Changed",
                "body": "PRIVATE_BODY_SENTINEL",
                "canonical": "https://example.invalid/PRIVATE_CANONICAL_SENTINEL",
                "aliases": [
                    "https://example.invalid/PRIVATE_ALIAS_SENTINEL",
                    r"\\server\share",
                    "Local Alias",
                ],
                "related": [
                    "missing.md",
                    "../clients/acme/private.md",
                    "../knowledge/two.md",
                ],
            }
        )
        self.catalog_path.write_text(json.dumps(fixture))
        response = self.api.handle("GET", "/api/v1/knowledge/document?path=docs/one.md")
        self.assertEqual(response.payload["document"]["title"], "Changed")
        self.assertEqual(response.payload["document"]["related"], ["knowledge/two.md"])
        self.assertEqual(response.payload["document"]["aliases"], ["Local Alias"])
        self.assertNotIn("PRIVATE_", json.dumps(response.payload))

    def test_duplicate_paths_fail_closed(self) -> None:
        fixture = json.loads(self.catalog_path.read_text())
        fixture["documents"].append(fixture["documents"][0])
        self.catalog_path.write_text(json.dumps(fixture))
        self.assertEqual(self.api.handle("GET", "/api/v1/knowledge").status, 503)

    def test_http_auth_cors_and_read_only_transport(self) -> None:
        class Handler(KnowledgeRequestHandler):
            api = KnowledgeApi(
                self.catalog_path,
                token="test-only-token",
                environment="development",
                allowed_origins=("http://localhost:3300",),
            )

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = HTTPConnection(*server.server_address, timeout=3)
        try:
            cases = (
                ("GET", {}, 401),
                ("GET", {"Authorization": "Bearer test-only-token"}, 200),
                ("GET", {"Origin": "https://untrusted.invalid"}, 403),
                ("POST", {"Authorization": "Bearer test-only-token"}, 405),
                ("OPTIONS", {"Origin": "http://localhost:3300"}, 204),
            )
            for method, headers, expected in cases:
                connection.request(method, "/api/v1/knowledge", headers=headers)
                response = connection.getresponse()
                payload = response.read()
                self.assertEqual(response.status, expected)
                self.assertEqual(response.getheader("Cache-Control"), "no-store")
                self.assertEqual(
                    response.getheader("X-Content-Type-Options"), "nosniff"
                )
                if expected == 204:
                    self.assertEqual(payload, b"")
                    self.assertEqual(
                        response.getheader("Access-Control-Allow-Origin"),
                        "http://localhost:3300",
                    )
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
