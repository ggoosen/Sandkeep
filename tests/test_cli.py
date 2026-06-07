"""Phase 1 gate (BUILD_SPEC §11): end to end on a real local repo —
run → REVIEW with valid contract + applicable patch; accept lands a fresh
host branch; reject discards cleanly; ledger + audit complete.

The agent is stubbed (same seam as the controller tests) so the gate runs
deterministic and key-free; test_phase1_e2e_real_agent below runs the real
headless claude when ANTHROPIC_API_KEY is present.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from sandkeep import agent_runner
from sandkeep.agent_runner import AgentRunResult
from sandkeep.cli import main


@pytest.fixture
def cli_env(tmp_path, monkeypatch, sandbox_image):
    home = tmp_path / "skhome"
    monkeypatch.setenv("SANDKEEP_HOME", str(home))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    return home


def _stub_agent(monkeypatch):
    def behaviour(task, provider, handle, audit, *, trace_id, timeout, **kw):
        contract = json.dumps(
            {
                "task_id": task.id,
                "status": "succeeded",
                "summary": "Added input validation.",
                "files_changed": ["parse_config.py"],
            }
        )
        script = (
            "echo '# validated' >> /work/repo/parse_config.py"
            " && mkdir -p /work/repo/.sandkeep"
            f" && echo {shlex.quote(contract)} > /work/repo/.sandkeep/results.json"
        )
        result = provider.exec(handle, ["sh", "-c", script], timeout=60)
        assert result.exit_code == 0, result.stderr
        return AgentRunResult(
            exit_code=0, timed_out=False, stdout="", stderr="",
            output={"usage": {"input_tokens": 10, "output_tokens": 5}}, detail="",
        )

    monkeypatch.setattr(agent_runner, "run_agent", behaviour)


def _run_cli(*argv: str) -> int:
    return main(list(argv))


def _task_id(capsys) -> str:
    out = capsys.readouterr().out
    match = re.search(r"task ([0-9a-f]{32}): ready for review", out)
    assert match, f"no task id in output:\n{out}"
    return match.group(1)


def test_phase1_gate_accept_path(cli_env, host_repo, monkeypatch, capsys):
    _stub_agent(monkeypatch)
    assert _run_cli("run", "--repo", str(host_repo), "--task", "add validation") == 0
    task_id = _task_id(capsys)

    assert _run_cli("status", task_id) == 0
    status_out = capsys.readouterr().out
    assert "review" in status_out
    assert "new → provisioning" in status_out  # full transition chain visible

    assert _run_cli("show", task_id) == 0
    show_out = capsys.readouterr().out
    assert "Added input validation." in show_out
    assert f"{task_id}.patch" in show_out

    assert _run_cli("accept", task_id) == 0
    accept_out = capsys.readouterr().out
    assert f"sandkeep-accepted/{task_id}" in accept_out
    shown = subprocess.run(
        ["git", "-C", str(host_repo), "show", f"sandkeep-accepted/{task_id}:parse_config.py"],
        capture_output=True, text=True,
    )
    assert "# validated" in shown.stdout
    # ledger + audit exist
    audit_log = Path(os.environ["SANDKEEP_HOME"]) / "audit.jsonl"
    assert "state_transition" in audit_log.read_text()
    # accepting twice fails loudly
    assert _run_cli("accept", task_id) == 1


def test_phase1_gate_reject_path(cli_env, host_repo, monkeypatch, capsys):
    _stub_agent(monkeypatch)
    assert _run_cli("run", "--repo", str(host_repo), "--task", "add validation") == 0
    task_id = _task_id(capsys)
    assert _run_cli("reject", task_id) == 0
    branches = subprocess.run(
        ["git", "-C", str(host_repo), "branch"], capture_output=True, text=True
    ).stdout
    assert "sandkeep-accepted" not in branches
    assert _run_cli("reject", task_id) == 1  # already rejected


def test_shell_gate_accept_path(cli_env, host_repo, monkeypatch, capsys):
    """`sandkeep shell` end to end through the real CLI, TTY session stubbed."""
    from sandkeep.sandbox.docker_provider import DockerProvider

    def fake_interactive(self, handle, cmd):
        subprocess.run(
            ["docker", "exec", handle.id, "sh", "-c",
             "echo '# via shell' >> /work/repo/parse_config.py"],
            check=True,
        )
        return 0

    monkeypatch.setattr(DockerProvider, "exec_interactive", fake_interactive)
    assert _run_cli("shell", "--repo", str(host_repo), "--task", "tidy up") == 0
    out = capsys.readouterr().out
    match = re.search(r"task ([0-9a-f]{32}) is ready for review", out)
    assert match, out
    task_id = match.group(1)
    assert _run_cli("accept", task_id) == 0
    shown = subprocess.run(
        ["git", "-C", str(host_repo), "show", f"sandkeep-accepted/{task_id}:parse_config.py"],
        capture_output=True, text=True,
    )
    assert "# via shell" in shown.stdout


def test_shell_requires_api_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SANDKEEP_HOME", str(tmp_path / "skhome"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _run_cli("shell", "--repo", ".") == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_run_requires_api_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SANDKEEP_HOME", str(tmp_path / "skhome"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _run_cli("run", "--repo", ".", "--task", "x") == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_unknown_task_id_errors(cli_env, capsys):
    assert _run_cli("status", "deadbeef") == 1
    assert "no such task" in capsys.readouterr().err


def test_security_banner_shown_on_run(cli_env, host_repo, monkeypatch, capsys):
    _stub_agent(monkeypatch)
    _run_cli("run", "--repo", str(host_repo), "--task", "add validation")
    assert "NOT" in capsys.readouterr().err  # containment honesty, always


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY", "").startswith("sk-ant-"),
    reason="real-agent e2e needs a real ANTHROPIC_API_KEY",
)
def test_phase1_e2e_real_agent(tmp_path, monkeypatch, sandbox_image, host_repo, capsys):
    """The unstubbed Phase 1 gate: a real headless claude run in the sandbox."""
    monkeypatch.setenv("SANDKEEP_HOME", str(tmp_path / "skhome"))
    code = _run_cli(
        "run", "--repo", str(host_repo), "--task",
        "Add input validation to parse_config(): skip blank lines and raise "
        "ValueError on lines without '='. Keep it minimal.",
    )
    assert code == 0, capsys.readouterr().err
    task_id = _task_id(capsys)
    assert _run_cli("accept", task_id) == 0
    shown = subprocess.run(
        ["git", "-C", str(host_repo), "show", f"sandkeep-accepted/{task_id}:parse_config.py"],
        capture_output=True, text=True,
    )
    assert "ValueError" in shown.stdout
