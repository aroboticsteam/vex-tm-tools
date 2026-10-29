"""Exercises the real `pywebpush` call (VAPID signing + RFC 8291 payload
encryption) against a throwaway local HTTP "push service", so a bad key
format or payload would fail here rather than in production."""
from __future__ import annotations

import base64
import http.server
import json
import threading

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from backend.core.settings import settings
from backend.modules.notifications import delivery


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.fixture
def vapid_keys(monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    private_raw = key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", b64url(private_raw))
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", b64url(public_raw))
    monkeypatch.setattr(settings, "VAPID_SUBJECT", "mailto:ops@example.org")


@pytest.fixture
def push_service():
    received = []
    status = {"code": 201}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.append((dict(self.headers), body))
            self.send_response(status["code"])
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/push/abc", received, status
    server.shutdown()


def client_subscription_keys():
    key = ec.generate_private_key(ec.SECP256R1())
    p256dh = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64url(p256dh), b64url(b"0123456789abcdef")


def target(endpoint):
    p256dh, auth = client_subscription_keys()
    return delivery.PushTarget(None, endpoint, p256dh, auth, {"title": "Q2 coming up", "body": "1234A is up next"})


def test_successful_push_is_signed_and_encrypted(vapid_keys, push_service):
    endpoint, received, _ = push_service
    assert delivery._send_blocking(target(endpoint)) == "ok"
    headers, body = received[0]
    lowered = {k.lower(): v for k, v in headers.items()}
    assert lowered["authorization"].lower().startswith(("vapid ", "webpush "))
    assert lowered["content-encoding"] == "aes128gcm"
    assert lowered["ttl"] == str(delivery._PUSH_TTL_SECONDS)
    assert b"Q2 coming up" not in body  # payload is encrypted


@pytest.mark.parametrize("code,expected", [(410, "gone"), (404, "gone"), (500, "error"), (429, "error")])
def test_status_codes_map_to_outcomes(vapid_keys, push_service, code, expected):
    endpoint, _, status = push_service
    status["code"] = code
    assert delivery._send_blocking(target(endpoint)) == expected


def test_unreachable_push_service_is_an_error_not_a_crash(vapid_keys):
    assert delivery._send_blocking(target("http://127.0.0.1:9/push/x")) == "error"


def test_generated_key_tool_output_is_loadable():
    """tools/generate_vapid_keys.py's key format must be accepted by pywebpush."""
    import importlib.util
    import io
    from contextlib import redirect_stdout
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "generate_vapid_keys", Path(__file__).parents[3] / "tools" / "generate_vapid_keys.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main()
    env = dict(line.split("=", 1) for line in buf.getvalue().strip().splitlines())
    from py_vapid import Vapid

    Vapid.from_string(env["VAPID_PRIVATE_KEY"])
    assert len(base64.urlsafe_b64decode(env["VAPID_PUBLIC_KEY"] + "==")) == 65
