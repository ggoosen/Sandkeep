"""Agent runner unit tests: command construction is pinned here so no other
module can drift into building claude invocations differently."""

from __future__ import annotations

import json
import shlex
import uuid
from dataclasses import dataclass, field

import pytest

from sandkeep.agent_runner import (
    AUTH_ERROR_HINT,
    SYSTEM_PROMPT_PATH,
    build_command,
    build_interactive_command,
    install_system_prompt,
    run_agent,
    usage_from_output,
)
from sandkeep.models import Task
from sandkeep.sandbox.base import ExecResult, SandboxExecTimeout, SandboxHandle


@dataclass
class FakeSandbox:
    """In-memory SandboxProvider double recording exec calls."""

    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    raise_timeout: bool = False
    calls: list[list[str]] = field(default_factory=list)

    def exec(self, handle, cmd, timeout):
        self.calls.append(cmd)
        if self.raise_timeout:
            raise SandboxExecTimeout("boom")
        return ExecResult(self.exit_code, self.stdout, self.stderr)


@pytest.fixture
def task() -> Task:
    return Task(id=uuid.uuid4().hex, repo_path="/tmp/r", instruction="add validation")


@pytest.fixture
def handle() -> SandboxHandle:
    return SandboxHandle(id="sandkeep-test", workdir="/work/repo")


def test_command_shape(task):
    cmd = build_command(task)
    argv = shlex.split(cmd.split("&&", 1)[1])
    assert cmd.startswith("cd /work/repo && ")
    assert argv[0] == "claude"
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == task.model
    assert argv[argv.index("--allowedTools") + 1] == "Read,Edit,Write,Bash"
    assert argv[argv.index("--append-system-prompt-file") + 1] == SYSTEM_PROMPT_PATH
    assert "--dangerously-skip-permissions" in argv
    # --max-turns was removed from the CLI (verified 2.1.168); never emit it
    assert "--max-turns" not in argv
    assert "--max-budget-usd" in argv


def test_interactive_command_is_plain_claude(task):
    cmd = build_interactive_command(task)
    assert cmd[:2] == ["sh", "-lc"]
    script = cmd[2]
    assert script.startswith("cd /work/repo && exec claude")
    # interactive: none of the headless flags belong here
    for flag in ("-p", "--output-format", "--max-turns", "--max-budget-usd"):
        assert flag not in script


def test_interactive_command_seeds_first_message(task):
    cmd = build_interactive_command(task, seed="fix the bug")
    assert "claude 'fix the bug'" in cmd[2]


def test_interactive_command_quotes_seed_safely(task):
    cmd = build_interactive_command(task, seed="rm -rf / ; echo $HOME")
    # the seed must be a single shell-quoted argument, not interpretable
    assert "'rm -rf / ; echo $HOME'" in cmd[2]


def test_prompt_carries_task_id_and_instruction(task):
    cmd = build_command(task)
    assert task.id in cmd
    assert "add validation" in cmd


def test_command_never_contains_api_key(task, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-supersecret")
    assert "sk-ant-supersecret" not in build_command(task)


def test_run_agent_parses_json_and_audits(task, handle, audit):
    payload = {
        "result": "done",
        "num_turns": 3,
        "duration_ms": 1234,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }
    sandbox = FakeSandbox(stdout=json.dumps(payload))
    result = run_agent(task, sandbox, handle, audit, trace_id="t1", timeout=60)
    assert result.exit_code == 0
    assert not result.timed_out
    assert result.output == payload
    events = audit.path.read_text()
    assert "agent_started" in events and "agent_finished" in events


def test_run_agent_maps_auth_error(task, handle, audit):
    sandbox = FakeSandbox(exit_code=2, stdout="", stderr="auth")
    result = run_agent(task, sandbox, handle, audit, trace_id="t1", timeout=60)
    assert result.detail == AUTH_ERROR_HINT


def test_run_agent_timeout(task, handle, audit):
    sandbox = FakeSandbox(raise_timeout=True)
    result = run_agent(task, sandbox, handle, audit, trace_id="t1", timeout=5)
    assert result.timed_out
    assert "timeout" in result.detail


def test_run_agent_surfaces_claude_error_message(task, handle, audit):
    """A nonzero exit with parseable JSON should report claude's own reason
    (e.g. billing), not a bare 'agent exited 1'."""
    payload = {
        "is_error": True,
        "api_error_status": 400,
        "result": "Credit balance is too low",
    }
    sandbox = FakeSandbox(exit_code=1, stdout=json.dumps(payload))
    result = run_agent(task, sandbox, handle, audit, trace_id="t1", timeout=60)
    assert result.detail == "Credit balance is too low (api status 400)"


def test_run_agent_tolerates_non_json_output(task, handle, audit):
    sandbox = FakeSandbox(exit_code=1, stdout="not json at all")
    result = run_agent(task, sandbox, handle, audit, trace_id="t1", timeout=60)
    assert result.output is None
    assert result.detail == "agent exited 1"


def test_install_system_prompt_base64_roundtrip(handle):
    sandbox = FakeSandbox()
    content = "# prompt\nwith 'quotes' and $vars and\nnewlines"
    install_system_prompt(sandbox, handle, content)
    script = sandbox.calls[0][2]
    assert SYSTEM_PROMPT_PATH in script
    import base64

    encoded = script.split("echo ", 1)[1].split(" |", 1)[0]
    assert base64.b64decode(encoded).decode() == content


def test_usage_extraction_is_defensive():
    assert usage_from_output(None) == (0, 0)
    assert usage_from_output({}) == (0, 0)
    assert usage_from_output(
        {"usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 90}}
    ) == (100, 5)
