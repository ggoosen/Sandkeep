"""Controller acceptance (BUILD_SPEC §9) against a live sandbox with the
agent stubbed: full transition chains, rollback on every failure mode,
violation → archive (quarantine, never silent), human gate both ways."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from sandkeep import agent_runner
from sandkeep.agent_runner import AgentRunResult
from sandkeep.config import Config
from sandkeep.controller import Controller, ControllerError
from sandkeep.models import TaskState


def _container_exists(name: str) -> bool:
    return (
        subprocess.run(["docker", "inspect", name], capture_output=True).returncode == 0
    )


def _chain(store, task_id) -> list[tuple[str, str]]:
    return [(r["from_state"], r["to_state"]) for r in store.get_transitions(task_id)]


@pytest.fixture
def controller(tmp_path, monkeypatch, store, audit, provider) -> Controller:
    monkeypatch.setenv("SANDKEEP_HOME", str(tmp_path / "skhome"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return Controller(Config.from_env(), store, audit, provider, network_denied=True)


def _stub_agent(monkeypatch, behaviour):
    """Replace the real headless claude run with `behaviour(task, provider,
    handle)` returning an AgentRunResult."""

    def fake_run_agent(task, provider, handle, audit, *, trace_id, timeout, **kw):
        return behaviour(task, provider, handle)

    monkeypatch.setattr(agent_runner, "run_agent", fake_run_agent)


def _ok_result(**over) -> AgentRunResult:
    defaults = dict(
        exit_code=0, timed_out=False, stdout="", stderr="",
        output={"usage": {"input_tokens": 100, "output_tokens": 40}}, detail="",
    )
    defaults.update(over)
    return AgentRunResult(**defaults)


def _do_work(task, provider, handle, *, write_contract=True) -> AgentRunResult:
    """Simulate a well-behaved agent: edit a file, write the contract."""
    contract = json.dumps(
        {
            "task_id": task.id,
            "status": "succeeded",
            "summary": "Added validation.",
            "files_changed": ["parse_config.py"],
            "tests_run": 1,
            "tests_passed": 1,
        }
    )
    script = "echo '# validated' >> /work/repo/parse_config.py"
    if write_contract:
        script += (
            " && mkdir -p /work/repo/.sandkeep"
            f" && echo {shlex.quote(contract)} > /work/repo/.sandkeep/results.json"
        )
    result = provider.exec(handle, ["sh", "-c", script], timeout=60)
    assert result.exit_code == 0, result.stderr
    return _ok_result()


def test_happy_path_ends_in_review_with_full_audit_chain(
    controller, store, host_repo, monkeypatch
):
    _stub_agent(monkeypatch, _do_work)
    task = controller.run_task(str(host_repo), "add validation")
    assert task.state is TaskState.REVIEW
    assert _chain(store, task.id) == [
        ("new", "provisioning"),
        ("provisioning", "running"),
        ("running", "succeeded"),
        ("succeeded", "review"),
    ]
    assert Path(task.patch_path).exists()
    ledger = store.get_ledger(task.id)
    assert len(ledger) == 1 and ledger[0]["input_tokens"] == 100
    # sandbox still up: it is the rollback target until the human decides
    assert _container_exists(task.sandbox_id)
    controller.reject(task.id)  # cleanup


def test_accept_lands_fresh_branch_and_destroys_sandbox(
    controller, store, host_repo, monkeypatch
):
    _stub_agent(monkeypatch, _do_work)
    task = controller.run_task(str(host_repo), "add validation")
    sha = controller.accept(task.id)
    assert store.get_task(task.id).state is TaskState.MERGED
    shown = subprocess.run(
        ["git", "-C", str(host_repo), "show", f"sandkeep-accepted/{task.id}:parse_config.py"],
        capture_output=True, text=True,
    )
    assert "# validated" in shown.stdout
    assert sha
    assert not _container_exists(task.sandbox_id)


def test_reject_destroys_sandbox_and_leaves_host_untouched(
    controller, store, host_repo, monkeypatch
):
    _stub_agent(monkeypatch, _do_work)
    task = controller.run_task(str(host_repo), "add validation")
    controller.reject(task.id)
    assert store.get_task(task.id).state is TaskState.REJECTED
    assert not _container_exists(task.sandbox_id)
    branches = subprocess.run(
        ["git", "-C", str(host_repo), "branch"], capture_output=True, text=True
    ).stdout
    assert "sandkeep-accepted" not in branches


def test_missing_contract_fails_and_rolls_back(controller, store, host_repo, monkeypatch):
    _stub_agent(monkeypatch, lambda t, p, h: _do_work(t, p, h, write_contract=False))
    task = controller.run_task(str(host_repo), "add validation")
    assert task.state is TaskState.ROLLED_BACK
    assert ("running", "failed") in _chain(store, task.id)
    assert not _container_exists(task.sandbox_id)


def test_agent_error_exit_fails_and_rolls_back(controller, store, host_repo, monkeypatch):
    _stub_agent(monkeypatch, lambda t, p, h: _ok_result(exit_code=1, detail="agent exited 1"))
    task = controller.run_task(str(host_repo), "add validation")
    assert task.state is TaskState.ROLLED_BACK
    assert ("running", "failed") in _chain(store, task.id)


def test_timeout_path(controller, store, host_repo, monkeypatch):
    _stub_agent(
        monkeypatch,
        lambda t, p, h: AgentRunResult(
            exit_code=-1, timed_out=True, stdout="", stderr="", output=None, detail="timeout"
        ),
    )
    task = controller.run_task(str(host_repo), "add validation")
    assert task.state is TaskState.ROLLED_BACK
    assert ("running", "timeout") in _chain(store, task.id)
    assert not _container_exists(task.sandbox_id)


def test_violation_is_archived_never_silent(controller, store, host_repo, monkeypatch):
    _stub_agent(
        monkeypatch,
        lambda t, p, h: _ok_result(stdout="trying /var/run/docker.sock now"),
    )
    task = controller.run_task(str(host_repo), "exfiltrate")
    assert task.state is TaskState.ROLLED_BACK
    assert ("running", "violation") in _chain(store, task.id)
    record = controller.config.archive_dir / f"{task.id}.json"
    assert record.exists()
    assert "docker.sock" in record.read_text()
    assert not _container_exists(task.sandbox_id)


def test_gate_rejects_wrong_state(controller, store, host_repo, monkeypatch):
    _stub_agent(monkeypatch, lambda t, p, h: _ok_result(exit_code=1, detail="x"))
    task = controller.run_task(str(host_repo), "add validation")
    with pytest.raises(ControllerError):
        controller.accept(task.id)
    with pytest.raises(ControllerError):
        controller.reject(task.id)


# -- Phase 3: coordination & policy surfaced at the gate -----------------


def _do_risky_work(task, provider, handle) -> AgentRunResult:
    """A well-behaved agent that happens to touch a dependency manifest."""
    contract = json.dumps({
        "task_id": task.id, "status": "succeeded",
        "summary": "Bumped a dep.", "files_changed": ["requirements.txt"],
    })
    script = (
        "echo 'requests==2.31.0' >> /work/repo/requirements.txt"
        " && mkdir -p /work/repo/.sandkeep"
        f" && echo {shlex.quote(contract)} > /work/repo/.sandkeep/results.json"
    )
    assert provider.exec(handle, ["sh", "-c", script], timeout=60).exit_code == 0
    return _ok_result()


def test_risk_flags_surface_and_are_audited(controller, store, host_repo, monkeypatch):
    _stub_agent(monkeypatch, _do_risky_work)
    task = controller.run_task(str(host_repo), "bump deps")
    assert task.state is TaskState.REVIEW
    flags = controller.risk_flags(task)
    assert any(f.category == "dependency" for f in flags)
    assert "policy_risk_flagged" in controller.audit.path.read_text()
    controller.reject(task.id)


def test_conflicts_detected_between_two_review_tasks(
    controller, store, host_repo, monkeypatch
):
    _stub_agent(monkeypatch, _do_work)  # both edit parse_config.py
    first = controller.run_task(str(host_repo), "first")
    second = controller.run_task(str(host_repo), "second")
    assert first.state is TaskState.REVIEW and second.state is TaskState.REVIEW
    conflicts = controller.conflicts(second)
    assert any(c.other_task_id == first.id for c in conflicts)
    assert any("parse_config.py" in c.files for c in conflicts)
    controller.reject(first.id)
    controller.reject(second.id)


# -- Phase 3: test-gated merge -------------------------------------------


def test_run_tests_executes_in_sandbox(controller, store, host_repo, monkeypatch):
    _stub_agent(monkeypatch, _do_work)
    task = controller.run_task(str(host_repo), "add validation")
    # the agent's change is present in the sandbox working tree
    code, _ = controller.run_tests(task, "test -f parse_config.py")
    assert code == 0
    code, _ = controller.run_tests(task, "exit 7")
    assert code == 7
    controller.reject(task.id)


def test_accept_passing_test_gate_merges(controller, store, host_repo, monkeypatch):
    _stub_agent(monkeypatch, _do_work)
    task = controller.run_task(str(host_repo), "add validation")
    sha = controller.accept(task.id, test_command="grep -q validated parse_config.py")
    assert sha
    assert store.get_task(task.id).state is TaskState.MERGED


def test_accept_failing_test_gate_refuses_and_stays_in_review(
    controller, store, host_repo, monkeypatch
):
    _stub_agent(monkeypatch, _do_work)
    task = controller.run_task(str(host_repo), "add validation")
    with pytest.raises(ControllerError, match="test gate failed"):
        controller.accept(task.id, test_command="exit 1")
    # not merged, still reviewable, sandbox still alive as the rollback target
    assert store.get_task(task.id).state is TaskState.REVIEW
    assert _container_exists(task.sandbox_id)
    assert "tests_run" in controller.audit.path.read_text()
    controller.reject(task.id)
