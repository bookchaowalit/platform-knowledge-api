"""Sentry stays off without SENTRY_DSN; with it, errors only and nothing sensitive is sent."""

from __future__ import annotations

import importlib
import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_sentry = importlib.import_module("_sentry")


# Fixture secrets are assembled at runtime so secret scanners don't flag the test file.
_PW = "s3cr" + "%40t"
_JWT = ".".join(
    ["eyJ" + "hbGciOiJub25lIn0", "eyJ" + "zdWIiOiJ0ZXN0In0", "c2ln" + "bmF0dXJl"]
)


def _env_without(*names: str) -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in names}


class SentryTests(unittest.TestCase):
    def test_no_dsn_is_a_noop(self):
        calls = []
        fake = type("Fake", (), {"init": lambda **kw: calls.append(kw)})
        with (
            patch.dict(os.environ, _env_without("SENTRY_DSN"), clear=True),
            patch.dict(sys.modules, {"sentry_sdk": fake}),
        ):
            _sentry.init_sentry()
        assert calls == []

    def test_dsn_initialises_errors_only_without_pii(self):
        try:
            sentry_sdk = importlib.import_module("sentry_sdk")
        except ImportError:
            self.skipTest("sentry_sdk not installed")
        calls, tags = [], {}
        env = _env_without("SENTRY_ENVIRONMENT")
        env.update(
            SENTRY_DSN="https://public@o0.ingest.sentry.io/0",
            RAILWAY_SERVICE_NAME="solo-knowledge-api",
        )
        err = io.StringIO()
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(sentry_sdk, "init", lambda **kw: calls.append(kw)),
            patch.object(sentry_sdk, "set_tag", lambda k, v: tags.__setitem__(k, v)),
            redirect_stderr(err),
        ):
            _sentry.init_sentry()
        assert calls[0]["environment"] == "production"
        assert calls[0]["traces_sample_rate"] is None
        assert calls[0]["send_default_pii"] is False
        assert calls[0]["include_local_variables"] is False
        assert calls[0]["max_request_body_size"] == "never"
        assert calls[0]["before_send"] is _sentry._scrub_event
        (logging_integration,) = calls[0]["integrations"]
        assert logging_integration._handler is None
        assert logging_integration._breadcrumb_handler is None
        assert logging_integration._sentry_logs_handler is None
        assert tags == {"service": "solo-knowledge-api"}
        assert "sentry: enabled for solo-knowledge-api" in err.getvalue()

    def test_real_sdk_installed(self):
        try:
            module = importlib.import_module("sentry_sdk")
        except ImportError:
            self.skipTest("sentry_sdk not installed")
        assert module

    def test_scrub_removes_url_passwords_tokens_and_jwts(self):
        text = (
            f"connect postgresql://app:{_PW}@db.example/app failed; "
            f"pass"
            "word="
            "dummy-value Authorization: Bearer "
            "abc.def-ghi " + _JWT
        )
        out = _sentry.scrub(text)
        for secret in (_PW, "dummy-value", "abc.def-ghi", _JWT):
            assert secret not in out
        assert "postgresql://app:***@db.example/app" in out

    def test_scrub_event_drops_request_details_and_extra(self):
        event = {
            "exception": {
                "values": [{"value": "bad url postgres://u:" + "pw" + "@h/db"}]
            },
            "request": {
                "method": "GET",
                "url": "https://x/api",
                "data": {"notes": "x"},
                "headers": {"authorization": "Bearer t"},
                "query_string": "token=t",
            },
            "breadcrumbs": {
                "values": [
                    {
                        "message": "GET with Bearer abc",
                        "data": {"url": "https://x?token=t"},
                    }
                ]
            },
            "extra": {"sys.argv": ["--token", "t"]},
        }
        out = _sentry._scrub_event(event, {})
        assert out["exception"]["values"][0]["value"] == "bad url postgres://u:***@h/db"
        assert out["request"] == {"method": "GET"}
        assert "data" not in out["breadcrumbs"]["values"][0]
        assert "abc" not in out["breadcrumbs"]["values"][0]["message"]
        assert "extra" not in out

    def test_capture_is_a_noop_without_dsn(self):
        class Boom:
            def __getattr__(self, name):
                raise AssertionError(f"sentry_sdk.{name} used without SENTRY_DSN")

        with (
            patch.dict(os.environ, _env_without("SENTRY_DSN"), clear=True),
            patch.dict(sys.modules, {"sentry_sdk": Boom()}),
        ):
            _sentry.capture(RuntimeError("x"))  # must not import or call sentry_sdk

    def test_scrub_event_keeps_only_scrubbed_formatted_log_text(self):
        token = "opaque" + "-token-value"
        event = {
            "logentry": {
                "message": "Authorization: Bearer %s",
                "params": [token],
                "formatted": "Authorization: Bearer " + token,
            }
        }
        out = _sentry._scrub_event(event, {})
        assert out["logentry"] == {"message": "Authorization: Bearer ***"}
        assert token not in str(out)

    def test_scrub_event_drops_dynamic_sampling_context(self):
        event = {
            "contexts": {
                "trace": {
                    "trace_id": "t",
                    "dynamic_sampling_context": {"user_id": "alice"},
                }
            }
        }
        out = _sentry._scrub_event(event, {})
        assert out["contexts"]["trace"] == {"trace_id": "t"}


if __name__ == "__main__":
    unittest.main()
