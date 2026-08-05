"""Proxy network mode wiring (improvement plan, step 1).

Host-side proof that proxy mode keeps the key out of the sandbox and stands the
broker up correctly. The end-to-end containment proof (curl denied, env has no
key) is the Docker-backed boundary test; here we pin the wiring reachable
without a daemon: config validation, the controller's env split, and the
DockerProvider command sequence via the fake runner.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import pytest

from sandkeep.agent import get_driver
from sandkeep.config import Config
from sandkeep.controller import Controller
from sandkeep.sandbox.docker_provider import (
    DockerConfig,
    DockerProvider,
    _broker_name,
    _network_name,
)


# -- config --------------------------------------------------------------

def test_proxy_is_a_valid_network_mode(monkeypatch):
    monkeypatch.setenv("SANDKEEP_NETWORK", "proxy")
    assert Config.from_env().network == "proxy"


def test_unknown_network_still_rejected(monkeypatch):
    monkeypatch.setenv("SANDKEEP_NETWORK", "sideways")
    with pytest.raises(ValueError):
        Config.from_env()


# -- controller env split ------------------------------------------------

def _controller(network: str) -> Controller:
    return Controller(Config(), store=None, audit=None, provider=None, network=network)


def test_proxy_env_has_no_key_and_points_at_broker(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-reach-sandbox")
    env = _controller("proxy")._agent_env(get_driver("claude"))
    assert "ANTHROPIC_API_KEY" not in env               # the key never enters
    assert env["ANTHROPIC_BASE_URL"] == "http://broker:8080/anthropic"
    assert env["HTTPS_PROXY"] == "http://broker:8080"
    assert "sk-should-not-reach-sandbox" not in "".join(env.values())


def test_egress_mode_still_forwards_the_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-egress")
    env = _controller("egress")._agent_env(get_driver("claude"))
    assert env["ANTHROPIC_API_KEY"] == "sk-egress"


def test_codex_proxy_points_at_openai_route(monkeypatch):
    """A non-Anthropic driver runs broker-protected: base URL points at its own
    /openai route and no OpenAI key enters the sandbox (step 14)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-should-not-leak")
    env = _controller("proxy")._agent_env(get_driver("codex"))
    assert env["OPENAI_BASE_URL"] == "http://broker:8080/openai"
    assert "OPENAI_API_KEY" not in env
    assert "sk-openai-should-not-leak" not in "".join(env.values())


# -- provider command sequence -------------------------------------------

@dataclass
class FakeRunner:
    calls: list[list[str]] = field(default_factory=list)
    envs: list = field(default_factory=list)

    def __call__(self, cmd, timeout=None, env=None):
        self.calls.append(cmd)
        self.envs.append(env)
        return subprocess.CompletedProcess(cmd, 0, b"id\n", b"")


def test_proxy_create_builds_broker_network_and_keyless_sandbox(tmp_path):
    runner = FakeRunner()
    provider = DockerProvider(
        DockerConfig(network="proxy", broker_api_key="sk-broker",
                     egress_allowlist="api.anthropic.com"),
        runner=runner,
    )
    handle = provider.create(str(tmp_path), env={
        "ANTHROPIC_BASE_URL": "http://broker:8080/anthropic",
        "HTTPS_PROXY": "http://broker:8080",
    })

    joined = [" ".join(c) for c in runner.calls]
    # an internal network was created, the broker launched, attached, then the
    # sandbox launched on that internal network
    assert any("network create --internal" in j for j in joined)
    assert any("network connect --alias broker" in j for j in joined)

    net = _network_name(handle.id)
    broker = _broker_name(handle.id)
    sandbox_cmd = next(c for c in runner.calls
                       if c[:3] == ["docker", "run", "--detach"] and handle.id in c)
    # sandbox sits on the internal network, NOT the default bridge
    assert sandbox_cmd[sandbox_cmd.index("--network") + 1] == net
    # the key is never in the sandbox argv, and the sandbox env carries no key
    assert "sk-broker" not in " ".join(sandbox_cmd)
    assert "ANTHROPIC_API_KEY" not in sandbox_cmd

    # the broker DID receive the key — via its own process env, not the argv
    broker_cmd = next(c for c in runner.calls
                      if c[:3] == ["docker", "run", "--detach"] and broker in c)
    broker_env = runner.envs[runner.calls.index(broker_cmd)]
    assert broker_env["ANTHROPIC_API_KEY"] == "sk-broker"
    assert "sk-broker" not in " ".join(broker_cmd)


def test_destroy_tears_down_broker_and_network():
    runner = FakeRunner()
    provider = DockerProvider(DockerConfig(network="proxy"), runner=runner)
    from sandkeep.sandbox.base import SandboxHandle

    provider.destroy(SandboxHandle(id="sandkeep-abc123", workdir="/work/repo"))
    joined = [" ".join(c) for c in runner.calls]
    assert any("rm --force --volumes sandkeep-abc123" in j for j in joined)
    assert any(_broker_name("sandkeep-abc123") in j for j in joined)
    assert any(f"network rm {_network_name('sandkeep-abc123')}" in j for j in joined)
