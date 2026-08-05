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

def test_injected_headers_anthropic_style():
    headers = {"x-api-key": "client-supplied", "Authorization": "Bearer x",
               "Host": "broker", "Content-Type": "application/json"}
    out = broker.injected_headers(headers, auth_header="x-api-key",
                                  auth_scheme="", key="real-secret")
    assert out["x-api-key"] == "real-secret"          # broker's key wins
    assert "Authorization" not in out and "authorization" not in out
    assert "Host" not in out
    assert out["anthropic-version"]                    # ensured present
    assert out["Content-Type"] == "application/json"   # innocuous header kept


def test_injected_headers_openai_bearer_style():
    out = broker.injected_headers(
        {"Authorization": "Bearer client"}, auth_header="Authorization",
        auth_scheme="Bearer ", key="sk-openai",
    )
    assert out["Authorization"] == "Bearer sk-openai"  # broker's key, scheme applied


# -- route + method matching ---------------------------------------------

def test_match_route_by_prefix():
    routes = [{"prefix": "/anthropic"}, {"prefix": "/openai"}]
    assert broker.match_route("/openai/v1/chat", routes)["prefix"] == "/openai"
    assert broker.match_route("/anthropic", routes)["prefix"] == "/anthropic"
    assert broker.match_route("/evil", routes) is None
    assert broker.match_route("/anthropicX", routes) is None  # not a prefix boundary


def test_method_allowed_rule():
    assert broker.method_allowed("POST", {"methods": ["POST"]})
    assert not broker.method_allowed("DELETE", {"methods": ["POST", "GET"]})
    assert broker.method_allowed("DELETE", {})  # no restriction → allowed


# -- localhost round-trip ------------------------------------------------

class _FakeUpstream(BaseHTTPRequestHandler):
    received: dict = {}

    def do_POST(self):
        _FakeUpstream.received = {
            "path": self.path, "x-api-key": self.headers.get("x-api-key"),
            "authorization": self.headers.get("Authorization"),
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


@pytest.fixture
def routed_broker(upstream, monkeypatch, tmp_path):
    """A broker with a generalized OpenAI-style route: Bearer auth, POST-only,
    tiny body cap (improvement plan, step 14)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "broker.log"))
    monkeypatch.setenv("SANDKEEP_MAX_REQ_BYTES", "64")
    monkeypatch.setenv("SANDKEEP_ROUTES", json.dumps([{
        "prefix": "/openai", "upstream": upstream,
        "auth_header": "Authorization", "auth_scheme": "Bearer ",
        "key_env": "OPENAI_API_KEY", "methods": ["POST"],
    }]))
    srv = broker.build_server(port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path / "broker.log"
    srv.shutdown()


def test_generalized_route_injects_bearer_key(routed_broker):
    base, _log = routed_broker
    req = urllib.request.Request(base + "/openai/v1/chat/completions", data=b"{}",
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
    assert _FakeUpstream.received["authorization"] == "Bearer sk-openai-secret"
    assert _FakeUpstream.received["path"] == "/v1/chat/completions"


def test_route_method_rule_denies_get(routed_broker):
    base, log = routed_broker
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(base + "/openai/v1/models", timeout=10)  # GET
    assert exc.value.code == 405
    assert "not allowed" in log.read_text()


def test_request_body_cap_enforced(routed_broker):
    base, log = routed_broker
    big = b"x" * 200  # over the 64-byte cap
    req = urllib.request.Request(base + "/openai/v1/chat", data=big, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 413
    assert "violation" in log.read_text()  # logged as a hard egress signal
