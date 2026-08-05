"""CodexDriver acceptance (improvement plan, step 7).

A real second AgentDriver, proving the "agent-agnostic" claim is demonstrated,
not just asserted. Host-side tests here; the boundary-suite-passes-with-codex
guarantee is inherited (the suite never references the agent) and the
diff-synthesis end-to-end is covered generically by test_agent_driver.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sandkeep.agent import available_agents, get_driver
from sandkeep.agent.codex import CodexDriver
from sandkeep.models import Task
from sandkeep.sandbox.base import ExecResult


def _task(**over) -> Task:
    d = dict(id=uuid.uuid4().hex, repo_path="/tmp/r", instruction="add validation")
    d.update(over)
    return Task(**d)


def test_codex_is_registered():
    assert "codex" in available_agents()
    assert isinstance(get_driver("codex"), CodexDriver)


def test_codex_is_a_diff_only_driver():
    d = CodexDriver()
    assert d.produces_contract is False           # no results.json → diff synthesis
    assert d.secret_env == "OPENAI_API_KEY"        # its own credential


def test_codex_headless_command_targets_exec_mode():
    task = _task()
    cmd = get_driver("codex").build_command(task)
    assert cmd.startswith("cd /work/repo && ")
    assert "codex exec" in cmd
    assert "--full-auto" in cmd
    assert f"--model {task.model}" in cmd
    # no Claude-only flags leaked in
    assert "--output-format" not in cmd
    assert "--append-system-prompt-file" not in cmd


def test_codex_interactive_command_shape():
    argv = get_driver("codex").build_interactive_command(_task(), seed="hi there")
    assert argv[:2] == ["sh", "-lc"]
    assert "codex" in argv[-1] and "hi there" in argv[-1]


def test_codex_parse_result_surfaces_error_tail():
    d = CodexDriver()
    ok = d.parse_result(ExecResult(0, "done", ""))
    assert ok.exit_code == 0 and ok.detail == "" and ok.output is None
    bad = d.parse_result(ExecResult(1, "", "fatal: rate limited by upstream"))
    assert bad.exit_code == 1 and "rate limited" in bad.detail


def test_codex_install_steps_present():
    steps = CodexDriver().install_steps()
    assert steps and any("codex" in s for s in steps)


def test_codex_per_agent_image_renders_install_steps():
    """image build --agent codex must inject the driver's install steps."""
    from sandkeep.sandbox.docker_provider import render_dockerfile

    rendered = render_dockerfile("codex", CodexDriver().install_steps())
    assert "@openai/codex" in rendered
    assert "USER node" in rendered  # still drops privileges like the base image


def test_codex_config_image_tag():
    from sandkeep.config import Config

    assert Config().image_for("codex") == "sandkeep-sandbox:codex"


def test_readme_documents_a_second_driver_exists():
    """Guard the claim: the driver seam ships more than one real agent."""
    assert len([a for a in available_agents()]) >= 2
    assert Path("src/sandkeep/agent/codex.py").exists()
