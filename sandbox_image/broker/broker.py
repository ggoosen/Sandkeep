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


def injected_headers(headers: dict[str, str], *, auth_header: str,
                     auth_scheme: str, key: str) -> dict[str, str]:
    """Copy request headers for the upstream call, injecting the auth the
    sandbox never had (per-route: Anthropic uses `x-api-key`, OpenAI uses
    `Authorization: Bearer …`). Drops any client-supplied auth/host so the agent
    can't override or spoof them, and keeps anthropic-version present."""
    drop = {"x-api-key", "authorization", "host", "content-length", "connection",
            "proxy-connection", auth_header.lower()}
    out = {k: v for k, v in headers.items() if k.lower() not in drop}
    out[auth_header] = f"{auth_scheme}{key}" if auth_scheme else key
    if auth_header.lower() == "x-api-key":  # Anthropic requires a version header
        out.setdefault("anthropic-version", headers.get("anthropic-version", "2023-06-01"))
    return out


def match_route(path: str, routes: list[dict]) -> dict | None:
    """The route whose prefix the request path falls under, if any."""
    for route in routes:
        prefix = route["prefix"]
        if path == prefix or path.startswith(prefix + "/"):
            return route
    return None


def method_allowed(method: str, route: dict) -> bool:
    """A route may restrict which HTTP methods it forwards (host+method rule,
    improvement plan step 14). No `methods` list → all methods allowed."""
    methods = route.get("methods")
    if not methods:
        return True
    return method.upper() in {m.upper() for m in methods}


class BrokerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- config injected by the server ------------------------------------
    allowlist: set[str] = set(DEFAULT_ALLOWLIST)  # CONNECT (opaque TLS) hosts
    routes: list[dict] = []                        # reverse-proxied upstreams
    max_req_bytes: int = 10 * 1024 * 1024          # cap on a forwarded body
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

    # -- reverse proxy: match a route, inject its key ---------------------
    def _handle(self):
        route = match_route(self.path, self.routes)
        if route is None:
            self._emit("deny", "-", "no reverse-proxy route for path")
            self.send_error(403, "no route for this path on the sandkeep broker")
            return
        if not method_allowed(self.command, route):
            self._emit("deny", route["upstream"], f"method {self.command} not allowed on route")
            self.send_error(405, "method not allowed on this route")
            return
        return self._proxy_route(route)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_PATCH = _handle

    def _proxy_route(self, route: dict):
        if not route.get("key"):
            self._emit("deny", route["upstream"], "broker has no key for route")
            self.send_error(502, "broker misconfigured: no key for route")
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > self.max_req_bytes:
            # bound exfil-via-request-body; a normal API call is far smaller
            self._emit("violation", route["upstream"],
                       f"request body {length} exceeds cap {self.max_req_bytes}")
            self.send_error(413, "request body exceeds sandkeep broker cap")
            return
        body = self.rfile.read(length) if length else None
        sub = self.path[len(route["prefix"]):] or "/"
        url = route["upstream"].rstrip("/") + sub
        headers = injected_headers(
            dict(self.headers), auth_header=route["auth_header"],
            auth_scheme=route.get("auth_scheme", ""), key=route["key"],
        )
        self._emit("allow", route["upstream"], f"proxy {self.command} {sub}")
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


def load_routes() -> list[dict]:
    """Reverse-proxy routes from env. SANDKEEP_ROUTES is a JSON list of
    {prefix, upstream, auth_header, auth_scheme?, key_env, methods?}; each key
    is read from the broker's OWN environment by key_env (never on the argv).
    Falls back to the single Anthropic route from ANTHROPIC_API_KEY so existing
    deployments keep working."""
    raw = os.environ.get("SANDKEEP_ROUTES", "")
    if raw:
        specs = json.loads(raw)
    else:
        specs = [{
            "prefix": ANTHROPIC_PREFIX,
            "upstream": os.environ.get("ANTHROPIC_UPSTREAM", "https://api.anthropic.com"),
            "auth_header": "x-api-key", "auth_scheme": "",
            "key_env": "ANTHROPIC_API_KEY",
        }]
    routes = []
    for s in specs:
        routes.append({
            "prefix": s["prefix"],
            "upstream": s["upstream"],
            "auth_header": s.get("auth_header", "x-api-key"),
            "auth_scheme": s.get("auth_scheme", ""),
            "methods": s.get("methods"),
            "key": os.environ.get(s.get("key_env", ""), ""),
        })
    return routes


def build_server(port: int | None = None) -> ThreadingHTTPServer:
    allowlist = os.environ.get("SANDKEEP_ALLOWLIST", "")
    BrokerHandler.allowlist = (
        {h.strip() for h in allowlist.split(",") if h.strip()}
        if allowlist else set(DEFAULT_ALLOWLIST)
    )
    BrokerHandler.routes = load_routes()
    BrokerHandler.max_req_bytes = int(
        os.environ.get("SANDKEEP_MAX_REQ_BYTES", str(10 * 1024 * 1024))
    )
    BrokerHandler.log_path = os.environ.get("LOG_PATH", "/var/log/sandkeep-broker.log")
    port = port if port is not None else int(os.environ.get("PORT", "8080"))
    return ThreadingHTTPServer(("0.0.0.0", port), BrokerHandler)


if __name__ == "__main__":
    server = build_server()
    print(json.dumps({"action": "start", "port": server.server_address[1],
                      "routes": [r["prefix"] for r in BrokerHandler.routes],
                      "allowlist": sorted(BrokerHandler.allowlist)}),
          file=sys.stderr, flush=True)
    server.serve_forever()
