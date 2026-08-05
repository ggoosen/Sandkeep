"""Browser bridge wiring (improvement plan, step 11).

Host-side proof (no Docker) that --browser stands the CDP sidecar up on the
task network, injects SANDKEEP_BROWSER_CDP into the sandbox, keeps no secret in
the sandbox, tears the sidecar down, and refuses the unsupported combinations.
The Docker-backed containment proof (CDP reachable, page egress allowlisted) is
tests/test_browser_boundary.py, run in CI.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import pytest

from sandkeep.agent import get_driver
from sandkeep.cli import _resolve_browser, build_parser
from sandkeep.config import Config
from sandkeep.controller import Controller, ControllerError
from sandkeep.sandbox.base import SandboxHandle
from sandkeep.sandbox.docker_provider import (
    DockerConfig,
    DockerProvider,
    _browser_name,
    _network_name,
)


# -- config + flag -------------------------------------------------------

def test_browser_env_flag(monkeypatch):
    monkeypatch.setenv("SANDKEEP_BROWSER", "on")
    assert Config.from_env().browser is True


def test_browser_cli_flag_parses():
    args = build_parser().parse_args(["run", "--repo", "/r", "--task", "t", "--browser"])
    assert args.browser is True


# -- resolve guardrails --------------------------------------------------

def _args(**kw):
    ns = build_parser().parse_args(["run", "--repo", "/r", "--task", "t"])
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_browser_refused_without_network():
    with pytest.raises(ControllerError, match="no-network"):
        _resolve_browser(Config(), _args(browser=True), network="none")


def test_browser_refused_on_e2b():
    cfg = Config()
    cfg.backend = "e2b"
    with pytest.raises(ControllerError, match="e2b"):
        _resolve_browser(cfg, _args(browser=True), network="egress")


def test_browser_off_by_default():
    assert _resolve_browser(Config(), _args(browser=False), network="egress") is False


# -- controller env injection --------------------------------------------

def _controller(*, network: str, browser: bool) -> Controller:
    return Controller(Config(), store=None, audit=None, provider=None,
                      network=network, browser=browser)


def test_cdp_env_injected_when_browser_on(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-egress")
    env = _controller(network="egress", browser=True)._agent_env(get_driver("claude"))
    assert env["SANDKEEP_BROWSER_CDP"] == "http://browser:9222"
    assert env["ANTHROPIC_API_KEY"] == "sk-egress"  # egress mode still forwards key


def test_cdp_env_absent_when_browser_off(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-egress")
    env = _controller(network="egress", browser=False)._agent_env(get_driver("claude"))
    assert "SANDKEEP_BROWSER_CDP" not in env


def test_proxy_plus_browser_keeps_key_out_and_adds_cdp(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    env = _controller(network="proxy", browser=True)._agent_env(get_driver("claude"))
    assert "ANTHROPIC_API_KEY" not in env                    # broker holds it
    assert env["ANTHROPIC_BASE_URL"] == "http://broker:8080/anthropic"
    assert env["SANDKEEP_BROWSER_CDP"] == "http://browser:9222"


# -- provider command sequence -------------------------------------------

@dataclass
class FakeRunner:
    calls: list[list[str]] = field(default_factory=list)
    envs: list = field(default_factory=list)

    def __call__(self, cmd, timeout=None, env=None):
        self.calls.append(cmd)
        self.envs.append(env)
        return subprocess.CompletedProcess(cmd, 0, b"id\n", b"")


def test_egress_browser_builds_sidecar_on_task_network(tmp_path):
    runner = FakeRunner()
    provider = DockerProvider(
        DockerConfig(network="egress", browser=True), runner=runner
    )
    handle = provider.create(str(tmp_path), env={"SANDKEEP_BROWSER_CDP": "http://browser:9222"})

    net = _network_name(handle.id)
    browser = _browser_name(handle.id)
    joined = [" ".join(c) for c in runner.calls]
    # a user-defined (NON-internal) network for egress+browser
    assert any(j == f"docker network create {net}" for j in joined)
    # browser launched on it with the `browser` alias
    browser_cmd = next(c for c in runner.calls if browser in c)
    assert browser_cmd[browser_cmd.index("--network-alias") + 1] == "browser"
    # sandbox joins the same network
    sandbox_cmd = next(c for c in runner.calls
                       if c[:3] == ["docker", "run", "--detach"] and handle.id in c)
    assert sandbox_cmd[sandbox_cmd.index("--network") + 1] == net


def test_proxy_browser_routes_browser_egress_through_broker(tmp_path):
    runner = FakeRunner()
    provider = DockerProvider(
        DockerConfig(network="proxy", browser=True, broker_api_key="sk-broker"),
        runner=runner,
    )
    handle = provider.create(str(tmp_path), env={})
    joined = [" ".join(c) for c in runner.calls]
    # internal network (no direct egress), broker AND browser both stood up
    assert any("network create --internal" in j for j in joined)
    browser_cmd = next(c for c in runner.calls if _browser_name(handle.id) in c)
    # the browser's own egress goes through the broker (proxy env forwarded)
    assert "HTTPS_PROXY" in browser_cmd
    envs = runner.envs[runner.calls.index(browser_cmd)]
    assert envs["HTTPS_PROXY"] == "http://broker:8080"


def test_destroy_tears_down_browser_sidecar():
    runner = FakeRunner()
    provider = DockerProvider(DockerConfig(network="egress", browser=True), runner=runner)
    provider.destroy(SandboxHandle(id="sandkeep-xyz789", workdir="/work/repo"))
    joined = [" ".join(c) for c in runner.calls]
    assert any(_browser_name("sandkeep-xyz789") in j for j in joined)
    assert any(f"network rm {_network_name('sandkeep-xyz789')}" in j for j in joined)
