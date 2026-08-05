"""Sandkeep egress broker (improvement plan, step 1).

A tiny stdlib-only proxy that runs in its OWN container between the sandbox and
the internet, so the untrusted agent never holds the API key and can only reach
an allowlist:

  sandbox (internal network, no egress, no key)
     │  ANTHROPIC_BASE_URL=http://broker:8080/anthropic   (plaintext, in-network)
     │  HTTPS_PROXY=http://broker:8080                     (registries via CONNECT)
     ▼
  broker (this) ── injects x-api-key from ITS OWN env ──▶ https://api.anthropic.com
                └─ CONNECT allowlist ───────────────────▶ pypi.org, registry.npmjs.org, …
                   everything else → 403, logged

Two jobs:
  * reverse-proxy the Anthropic API on /anthropic/*, injecting the key the
    sandbox never sees;
  * forward-proxy (HTTP CONNECT) only allowlisted registry hosts, no injection.

Every decision is emitted as one JSON line to LOG_PATH (and stderr), so the
controller can turn a denied egress attempt into a real VIOLATION signal —
unlike the transcript scanner, this is ground truth.

Config via env: ANTHROPIC_API_KEY, ANTHROPIC_UPSTREAM (default
https://api.anthropic.com), SANDKEEP_ALLOWLIST (comma-separated hosts for
CONNECT), PORT (default 8080), LOG_PATH (default /var/log/sandkeep-broker.log).
Stdlib only — the broker is host-trust-side code; keep its surface minimal.
"""

from __future__ import annotations

import json
import os
import select
import socket
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_ALLOWLIST = (
    "api.anthropic.com",
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
)
ANTHROPIC_PREFIX = "/anthropic"


def host_allowed(host: str, allowlist: set[str]) -> bool:
    """A host is allowed if it exactly matches an allowlist entry or is a
    subdomain of one (so cdn.pypi.org is covered by pypi.org). The port, if
    any, is ignored. Never matches on a bare substring."""
    host = (host or "").split(":", 1)[0].strip().lower().rstrip(".")
    if not host:
        return False
    for entry in allowlist:
        entry = entry.strip().lower().rstrip(".")
        if host == entry or host.endswith("." + entry):
            return True
    return False


def injected_headers(headers: dict[str, str], api_key: str) -> dict[str, str]:
    """Copy request headers for the upstream Anthropic call, injecting auth the
    sandbox never had. Drops any client-supplied auth/host so the agent can't
    override or spoof them, and ensures the API version is present."""
    drop = {"x-api-key", "authorization", "host", "content-length", "connection",
            "proxy-connection", "anthropic-version"}
    out = {k: v for k, v in headers.items() if k.lower() not in drop}
    out["x-api-key"] = api_key
    out["anthropic-version"] = headers.get("anthropic-version", "2023-06-01")
    return out


class BrokerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- config injected by the server ------------------------------------
    allowlist: set[str] = set(DEFAULT_ALLOWLIST)
    api_key: str = ""
    upstream: str = "https://api.anthropic.com"
    log_path: str = "/var/log/sandkeep-broker.log"

    def log_message(self, *args):  # silence the default apache-style logging
        pass

    def _emit(self, action: str, host: str, detail: str = "") -> None:
        line = json.dumps({
            "action": action, "method": self.command,
            "host": host, "path": self.path, "detail": detail,
        })
        try:
            with open(self.log_path, "a") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        print(line, file=sys.stderr, flush=True)

    # -- HTTPS forward proxy (registries): allow only the allowlist -------
    def do_CONNECT(self):
        host = self.path.split(":", 1)[0]
        if not host_allowed(host, self.allowlist):
            self._emit("deny", host, "connect host not in allowlist")
            self.send_error(403, "host not allowed by sandkeep broker")
            return
        self._emit("allow", host, "connect tunnel")
        try:
            addr_host, addr_port = self.path.split(":", 1)
            upstream = socket.create_connection((addr_host, int(addr_port)), timeout=30)
        except OSError as exc:
            self.send_error(502, f"upstream connect failed: {exc}")
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        self._tunnel(self.connection, upstream)

    @staticmethod
    def _tunnel(a: socket.socket, b: socket.socket) -> None:
        a.setblocking(False)
        b.setblocking(False)
        try:
            while True:
                r, _, x = select.select([a, b], [], [a, b], 30)
                if x or not r:
                    break
                for src in r:
                    dst = b if src is a else a
                    data = src.recv(65536)
                    if not data:
                        return
                    dst.sendall(data)
        except OSError:
            return
        finally:
            b.close()

    # -- Anthropic reverse proxy (inject the key) -------------------------
    def _handle(self):
        if self.path.startswith(ANTHROPIC_PREFIX + "/") or self.path == ANTHROPIC_PREFIX:
            return self._proxy_anthropic()
        # A plain (non-CONNECT) request that isn't the Anthropic path: deny.
        self._emit("deny", "-", "non-anthropic plaintext request")
        self.send_error(403, "only the Anthropic API is reverse-proxied")

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_PATCH = _handle

    def _proxy_anthropic(self):
        if not self.api_key:
            self._emit("deny", "api.anthropic.com", "broker has no api key")
            self.send_error(502, "broker misconfigured: no API key")
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        sub = self.path[len(ANTHROPIC_PREFIX):] or "/"
        url = self.upstream.rstrip("/") + sub
        headers = injected_headers(dict(self.headers), self.api_key)
        self._emit("allow", "api.anthropic.com", f"proxy {sub}")
        req = urllib.request.Request(url, data=body, headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                payload = resp.read()
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() in ("transfer-encoding", "connection", "content-length"):
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as exc:  # upstream 4xx/5xx — relay as-is
            payload = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError as exc:
            self.send_error(502, f"upstream error: {exc}")


def build_server(port: int | None = None) -> ThreadingHTTPServer:
    allowlist = os.environ.get("SANDKEEP_ALLOWLIST", "")
    BrokerHandler.allowlist = (
        {h.strip() for h in allowlist.split(",") if h.strip()}
        if allowlist else set(DEFAULT_ALLOWLIST)
    )
    BrokerHandler.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    BrokerHandler.upstream = os.environ.get("ANTHROPIC_UPSTREAM", "https://api.anthropic.com")
    BrokerHandler.log_path = os.environ.get("LOG_PATH", "/var/log/sandkeep-broker.log")
    port = port if port is not None else int(os.environ.get("PORT", "8080"))
    return ThreadingHTTPServer(("0.0.0.0", port), BrokerHandler)


if __name__ == "__main__":
    server = build_server()
    print(json.dumps({"action": "start", "port": server.server_address[1],
                      "allowlist": sorted(BrokerHandler.allowlist)}),
          file=sys.stderr, flush=True)
    server.serve_forever()
