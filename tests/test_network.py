"""Network mode config + resolution (BUILD_SPEC §16, Phase 2 slice).

The honest, buildable part of Phase 2: a toggle to run the sandbox with no
network at all. The egress *allowlist* proxy and secret broker are NOT here —
they need the brokering proxy / microVM and would be false security as config
stubs (see §16).
"""

from __future__ import annotations

import argparse

import pytest

from sandkeep.cli import _resolve_network
from sandkeep.config import DEFAULT_NETWORK, Config


def test_default_network_is_egress(monkeypatch):
    monkeypatch.delenv("SANDKEEP_NETWORK", raising=False)
    assert Config.from_env().network == "egress" == DEFAULT_NETWORK


def test_env_sets_network_none(monkeypatch):
    monkeypatch.setenv("SANDKEEP_NETWORK", "none")
    assert Config.from_env().network == "none"


def test_env_rejects_unknown_network(monkeypatch):
    monkeypatch.setenv("SANDKEEP_NETWORK", "wide-open")
    with pytest.raises(ValueError):
        Config.from_env()


def _args(**kw):
    return argparse.Namespace(**kw)


def test_resolve_flag_forces_none(monkeypatch):
    monkeypatch.delenv("SANDKEEP_NETWORK", raising=False)
    cfg = Config.from_env()  # egress
    assert _resolve_network(cfg, _args(no_network=True)) == "none"


def test_resolve_falls_back_to_config(monkeypatch):
    monkeypatch.setenv("SANDKEEP_NETWORK", "none")
    cfg = Config.from_env()
    # no flag → config/env wins
    assert _resolve_network(cfg, _args(no_network=False)) == "none"


def test_resolve_flag_absent_uses_egress(monkeypatch):
    monkeypatch.delenv("SANDKEEP_NETWORK", raising=False)
    cfg = Config.from_env()
    assert _resolve_network(cfg, _args()) == "egress"
