"""AgentDriver acceptance (BUILD_SPEC §13, Phase 5).

The boundary is agent-agnostic; these tests pin the pluggable seam:
- the default ClaudeDriver is unchanged behind the new interface,
- an unknown agent fails loud on the host before any sandbox exists,
- the registered driver stays in sync with the sandbox image,
- a non-contract driver lands at REVIEW via host-side diff synthesis (docker).
"""

from __future__ import annotations

import json
import shlex
import subprocess
import uuid
from pathlib import Path

import pytest

from sandkeep.agent import (
    AgentDriver,
    UnknownAgent,
    available_agents,
    get_driver,
    register_driver,
)
from sandkeep.agent.base import AgentRunResult
from sandkeep.agent.claude import ClaudeDriver
from sandkeep.config import Config
from sandkeep.controller import Controller
from sandkeep.models import Task, TaskState
from sandkeep.sandbox.base import ExecResult


# -- registry + selection ------------------------------------------------


def test_claude_is_registered_by_default():
    assert "claude" in available_agents()
    assert isinstance(get_driver("claude"), ClaudeDriver)


def test_unknown_agent_fails_loud_with_available_list():
    with pytest.raises(UnknownAgent) as exc:
        get_driver("does-not-exist")
    assert "does-not-exist" in str(exc.value)
    assert "claude" in str(exc.value)


# -- ClaudeDriver is unchanged behind the interface ----------------------


def _task(**over) -> Task:
    defaults = dict(id=uuid.uuid4().hex, repo_path="/tmp/r", instruction="add validation")
    defaults.update(over)
    return Task(**defaults)


def test_claude_driver_headless_command_is_byte_identical():
    """The exact pre-refactor command string, pinned so the seam can't drift."""
    task = _task()
    driver = ClaudeDriver()
    expected_parts = [
        "claude",
        "-p",
        (
            f"Sandkeep task id: {task.id}\n\n"
            f"Task:\n{task.instruction}\n\n"
            "When done, write the results contract to .sandkeep/results.json "
            f'exactly as your system prompt specifies, with "task_id": "{task.id}".'
        ),
        "--output-format", "json",
        "--model", task.model,
        "--allowedTools", "Read,Edit,Write,Bash",
        "--append-system-prompt-file", "/work/.sandkeep/agent_system_prompt.md",
        "--dangerously-skip-permissions",
        "--max-budget-usd", "5.00",
    ]
    expected = "cd /work/repo && " + shlex.join(expected_parts)
    assert driver.build_command(task) == expected


def test_claude_driver_parses_billing_error():
    driver = ClaudeDriver()
    payload = {"is_error": True, "api_error_status": 400, "result": "Credit balance is too low"}
    run = driver.parse_result(ExecResult(1, json.dumps(payload), ""))
    assert run.detail == "Credit balance is too low (api status 400)"


def test_claude_driver_install_steps_match_dockerfile():
    """The driver's declared install steps must actually be in the image, or a
    per-agent build would ship a different sandbox than the unit tests pin."""
    dockerfile = (Path(__file__).parents[1] / "sandbox_image" / "Dockerfile").read_text()
    for step in ClaudeDriver().install_steps():
        assert step in dockerfile, f"install step not in Dockerfile: {step}"


# -- a non-contract driver end to end (docker-backed) --------------------


class _DiffOnlyDriver(AgentDriver):
    """A stub driver that writes no results.json — the diff is the truth.
    It just appends a line to a file inside the sandbox so a diff exists."""

    name = "diffonly-test"
    secret_env = "DIFFONLY_TEST_KEY"
    produces_contract = False

    def install_steps(self):
        return []

    def build_command(self, task, *, max_budget_usd="5.00"):
        return "echo '# diffonly edit' >> /work/repo/parse_config.py"

    def build_interactive_command(self, task, *, seed=None, skip_permissions=True):
        return ["sh", "-lc", "true"]

    def parse_result(self, exec_result):
        return AgentRunResult(
            exit_code=exec_result.exit_code, timed_out=False,
            stdout=exec_result.stdout, stderr=exec_result.stderr, output=None,
        )


@pytest.fixture
def diffonly_driver():
    register_driver(_DiffOnlyDriver())
    yield
    from sandkeep.agent import _DRIVERS

    _DRIVERS.pop("diffonly-test", None)


@pytest.fixture
def controller(tmp_path, monkeypatch, store, audit, provider) -> Controller:
    monkeypatch.setenv("SANDKEEP_HOME", str(tmp_path / "skhome"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return Controller(Config.from_env(), store, audit, provider, network_denied=True)


def _chain(store, task_id):
    return [(r["from_state"], r["to_state"]) for r in store.get_transitions(task_id)]


def test_noncontract_driver_lands_in_review_via_diff_synthesis(
    controller, store, host_repo, diffonly_driver
):
    task = controller.run_task(str(host_repo), "edit something", agent="diffonly-test")
    assert task.state is TaskState.REVIEW
    assert task.agent == "diffonly-test"
    assert _chain(store, task.id) == [
        ("new", "provisioning"),
        ("provisioning", "running"),
        ("running", "succeeded"),
        ("succeeded", "review"),
    ]
    # contract was synthesized host-side from the patch (no results.json existed)
    contract = json.loads((controller.config.outputs_dir / f"{task.id}.results.json").read_text())
    assert contract["files_changed"] == ["parse_config.py"]
    assert "diff-synthesized" in contract["summary"]
    assert Path(task.patch_path).exists()
    controller.reject(task.id)


def test_unknown_agent_creates_no_task_and_no_sandbox(controller, store, host_repo):
    with pytest.raises(UnknownAgent):
        controller.run_task(str(host_repo), "x", agent="nope")
    assert store.list_tasks() == []
