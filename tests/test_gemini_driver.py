"""GeminiDriver acceptance (improvement plan, step 21).

A third real driver, all broker-routable in proxy mode (step 14). Host-side;
the boundary-suite-passes guarantee is inherited (the suite never names an
agent) and diff-synthesis end-to-end is covered generically.
"""

from __future__ import annotations

import uuid

from sandkeep.agent import available_agents, get_driver
from sandkeep.agent.gemini import GeminiDriver
from sandkeep.config import Config
from sandkeep.controller import Controller
from sandkeep.models import Task
from sandkeep.sandbox.base import ExecResult


def _task(**over):
    d = dict(id=uuid.uuid4().hex, repo_path="/r", instruction="do a thing")
    d.update(over)
    return Task(**d)


def test_three_drivers_registered():
    agents = available_agents()
    assert {"claude", "codex", "gemini"} <= set(agents)


def test_gemini_is_diff_only_and_broker_routable():
    d = GeminiDriver()
    assert d.produces_contract is False
    assert d.secret_env == "GEMINI_API_KEY"
    assert d.broker_route and d.broker_route["prefix"] == "/gemini"


def test_gemini_headless_command():
    cmd = get_driver("gemini").build_command(_task())
    assert cmd.startswith("cd /work/repo && ")
    assert "gemini" in cmd and "--yolo" in cmd and "--prompt" in cmd


def test_gemini_proxy_env_keeps_key_out(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-should-not-leak")
    ctrl = Controller(Config(), store=None, audit=None, provider=None, network="proxy")
    env = ctrl._agent_env(get_driver("gemini"))
    assert env["GOOGLE_GEMINI_BASE_URL"] == "http://broker:8080/gemini"
    assert "GEMINI_API_KEY" not in env
    assert "gk-should-not-leak" not in "".join(env.values())


def test_gemini_per_agent_image_tag():
    assert Config().image_for("gemini") == "sandkeep-sandbox:gemini"


def test_gemini_parse_result_error_tail():
    d = GeminiDriver()
    bad = d.parse_result(ExecResult(1, "", "quota exhausted for project"))
    assert bad.exit_code == 1 and "quota" in bad.detail
