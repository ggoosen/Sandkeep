"""Egress broker logic (improvement plan, step 1).

The broker is the security core of proxy mode: it holds the API key the sandbox
never sees, injects it on Anthropic calls, and enforces the CONNECT allowlist.
These tests exercise that decision logic directly (pure functions) plus a
localhost round-trip of the reverse-proxy and a denied request — no Docker.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_BROKER = Path(__file__).parents[1] / "sandbox_image" / "broker" / "broker.py"


def _load_broker():
    spec = importlib.util.spec_from_file_location("sandkeep_broker", _BROKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


broker = _load_broker()


# -- allowlist decision --------------------------------------------------

@pytest.mark.parametrize(
    "host,allowed",
    [
        ("api.anthropic.com", True),
        ("API.ANTHROPIC.COM", True),
        ("api.anthropic.com:443", True),
        ("files.pythonhosted.org", True),
        ("cdn.pypi.org", True),          # subdomain of an allowed entry
        ("pypi.org.evil.com", False),    # not a subdomain — suffix trick
        ("evil.com", False),
        ("notanthropic.com", False),     # bare-substring must not match
        ("", False),
    ],
)
def test_host_allowed(host, allowed):
    allowlist = {"api.anthropic.com", "pypi.org", "files.pythonhosted.org"}
    assert broker.host_allowed(host, allowlist) is allowed


# -- key injection -------------------------------------------------------

def test_injected_headers_adds_key_and_drops_client_auth():
    headers = {"x-api-key": "client-supplied", "Authorization": "Bearer x",
               "Host": "broker", "Content-Type": "application/json"}
    out = broker.injected_headers(headers, "real-secret")
    assert out["x-api-key"] == "real-secret"          # broker's key wins
    assert "Authorization" not in out and "authorization" not in out
    assert "Host" not in out
    assert out["anthropic-version"]                    # ensured present
    assert out["Content-Type"] == "application/json"   # innocuous header kept


# -- localhost round-trip ------------------------------------------------

class _FakeUpstream(BaseHTTPRequestHandler):
    received: dict = {}

    def do_POST(self):
        _FakeUpstream.received = {
            "path": self.path, "x-api-key": self.headers.get("x-api-key"),
        }
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def broker_server(upstream, monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-broker-secret")
    monkeypatch.setenv("ANTHROPIC_UPSTREAM", upstream)
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "broker.log"))
    monkeypatch.setenv("SANDKEEP_ALLOWLIST", "api.anthropic.com")
    srv = broker.build_server(port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path / "broker.log"
    srv.shutdown()


def test_reverse_proxy_injects_key(broker_server):
    base, _log = broker_server
    req = urllib.request.Request(
        base + "/anthropic/v1/messages", data=b"{}",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
        assert json.loads(resp.read())["ok"] is True
    # the sandbox sent NO key; the broker injected its own on the way out
    assert _FakeUpstream.received["x-api-key"] == "sk-broker-secret"
    assert _FakeUpstream.received["path"] == "/v1/messages"


def test_non_anthropic_request_denied_and_logged(broker_server):
    base, log = broker_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(base + "/evil", timeout=10)
    assert exc.value.code == 403
    assert "deny" in log.read_text()
