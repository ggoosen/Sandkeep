"""E2B feature-parity guardrails (improvement plan, step 19).

Full parity (key broker + browser bridge on the microVM) is blocked on the
basic E2B SDK — it exposes no inbound tunnel or per-host allowlist, and an
in-VM broker would put the key back within the agent's reach. Until then the
one thing that MUST hold is: a caller who asked for proxy/browser protection on
E2B never silently gets an unprotected run. These host-side tests pin the loud
refusals (no E2B account needed).
"""

from __future__ import annotations

import argparse

import pytest

from sandkeep.cli import _resolve_browser, _resolve_network
from sandkeep.config import Config
from sandkeep.controller import ControllerError


def _args(**kw):
    ns = argparse.Namespace(no_network=False, browser=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_proxy_on_e2b_refused_loud(monkeypatch):
    cfg = Config()
    cfg.backend = "e2b"
    cfg.network = "proxy"
    with pytest.raises(ControllerError, match="not supported on the e2b"):
        _resolve_network(cfg, _args())


def test_proxy_on_docker_is_fine():
    cfg = Config()
    cfg.backend = "docker"
    cfg.network = "proxy"
    assert _resolve_network(cfg, _args()) == "proxy"


def test_browser_on_e2b_refused_loud():
    cfg = Config()
    cfg.backend = "e2b"
    with pytest.raises(ControllerError, match="e2b"):
        _resolve_browser(cfg, _args(browser=True), network="egress")


def test_e2b_provider_create_refuses_proxy_without_sdk(monkeypatch):
    """Defense in depth: even called directly, the provider refuses proxy —
    proven without the e2b package by faking the SDK import."""
    import builtins

    real_import = builtins.__import__
    fake_sandbox = type("S", (), {})

    def fake_import(name, *a, **k):
        if name == "e2b" or name.startswith("e2b"):
            mod = type("e2b", (), {"Sandbox": fake_sandbox})
            return mod
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    from sandkeep.sandbox.base import SandboxError
    from sandkeep.sandbox.e2b_provider import E2BConfig, E2BProvider

    provider = E2BProvider(E2BConfig(network="proxy"))
    with pytest.raises(SandboxError, match="not supported on the E2B"):
        provider.create(".", env={})
