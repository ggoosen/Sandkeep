"""Concurrency (BUILD_SPEC §12): many tasks in parallel, one sandbox each.

Docker-backed with the agent stubbed. Proves isolated sandboxes, no shared
state corruption (each task its own SQLite connection over WAL), and a complete
per-task ledger/audit.
"""

from __future__ import annotations

import json
import shlex

import pytest

from sandkeep import agent_runner
from sandkeep.agent_runner import AgentRunResult
from sandkeep.audit import AuditLog
from sandkeep.config import Config
from sandkeep.controller import Controller, run_concurrent
from sandkeep.models import TaskState
from sandkeep.state_store import StateStore


def _do_work(task, provider, handle):
    contract = json.dumps({
        "task_id": task.id, "status": "succeeded",
        "summary": "edit", "files_changed": ["parse_config.py"],
    })
    script = (
        "echo '# c' >> /work/repo/parse_config.py"
        " && mkdir -p /work/repo/.sandkeep"
        f" && echo {shlex.quote(contract)} > /work/repo/.sandkeep/results.json"
    )
    assert provider.exec(handle, ["sh", "-c", script], timeout=60).exit_code == 0
    return AgentRunResult(
        exit_code=0, timed_out=False, stdout="", stderr="",
        output={"usage": {"input_tokens": 1, "output_tokens": 1}}, detail="",
    )


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDKEEP_HOME", str(tmp_path / "skhome"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return Config.from_env()


def test_run_concurrent_isolates_sandboxes_and_state(
    cfg, provider, tmp_path, host_repo, monkeypatch
):
    monkeypatch.setattr(
        agent_runner, "run_agent",
        lambda task, prov, handle, audit, **kw: _do_work(task, prov, handle),
    )
    audit = AuditLog(cfg.audit_log_path)
    # one shared controller; the thread-safe store + audit make this safe
    controller = Controller(
        cfg, StateStore(cfg.db_path, audit=audit), audit, provider, network_denied=True,
    )

    specs = [dict(repo_path=str(host_repo), instruction=f"task {i}") for i in range(4)]
    results = run_concurrent(controller, specs, max_workers=4)

    # all reached REVIEW, none raised
    assert all(not isinstance(r, Exception) for r in results)
    assert all(r.state is TaskState.REVIEW for r in results)
    # distinct tasks and distinct sandboxes
    assert len({r.id for r in results}) == 4
    assert len({r.sandbox_id for r in results}) == 4

    # state survived concurrent writers: 4 tasks, each with a ledger row
    check = StateStore(cfg.db_path)
    try:
        assert len(check.list_tasks()) == 4
        for r in results:
            assert len(check.get_ledger(r.id)) == 1
    finally:
        check.close()

    # cleanup sandboxes
    for r in results:
        controller.reject(r.id)


def test_run_concurrent_isolates_failures(cfg, provider, host_repo, monkeypatch):
    """A host-side failure in one task is captured, not propagated to siblings."""
    monkeypatch.setattr(
        agent_runner, "run_agent",
        lambda task, prov, handle, audit, **kw: _do_work(task, prov, handle),
    )
    audit = AuditLog(cfg.audit_log_path)
    controller = Controller(
        cfg, StateStore(cfg.db_path, audit=audit), audit, provider, network_denied=True,
    )
    specs = [
        dict(repo_path=str(host_repo), instruction="good"),
        dict(repo_path=str(host_repo), instruction="bad", agent="does-not-exist"),
    ]
    results = run_concurrent(controller, specs, max_workers=2)
    assert results[0].state is TaskState.REVIEW
    assert isinstance(results[1], Exception)  # UnknownAgent captured per-task
    controller.reject(results[0].id)
