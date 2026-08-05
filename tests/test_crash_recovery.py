"""Crash recovery & reconciliation (improvement plan, step 3).

A controller that dies mid-run must not leave a task wedged in a non-terminal
state with a leaked sandbox and no way to recover it. These tests use a fake
provider (no Docker) to exercise: the try/finally guard around the run loop,
gc/reconcile unsticking a crash-wedged task, the atomic SUCCEEDED→REVIEW hop,
and the CLI's clean error surface.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

from sandkeep import cli
from sandkeep.audit import AuditLog, new_trace_id
from sandkeep.config import Config
from sandkeep.controller import Controller
from sandkeep.models import Task, TaskState
from sandkeep.sandbox.base import ExecResult, SandboxHandle
from sandkeep.state_store import IllegalTransition, StateStore


@dataclass
class FakeProvider:
    """A provider whose sandboxes are just recorded ids; exec/read raise so any
    run that reaches the agent blows up (the point: prove the guard catches it)."""

    live: set = field(default_factory=set)
    exec_raises: bool = False

    def create(self, repo_path, env):
        sid = f"sandkeep-{uuid.uuid4().hex[:8]}"
        self.live.add(sid)
        return SandboxHandle(id=sid, workdir="/work/repo")

    def exec(self, handle, cmd, timeout):
        if self.exec_raises:
            raise RuntimeError("boom in sandbox exec")
        return ExecResult(exit_code=0, stdout="", stderr="")

    def read_file(self, handle, path):
        raise FileNotFoundError(path)

    def destroy(self, handle):
        self.live.discard(handle.id)

    def list_sandbox_ids(self):
        return sorted(self.live)


@pytest.fixture
def wiring(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    store = StateStore(tmp_path / "state.sqlite3", audit=audit)
    cfg = Config()
    cfg.home = tmp_path / "home"
    provider = FakeProvider()
    controller = Controller(cfg, store, audit, provider)
    yield controller, store, provider, cfg
    store.close()


def _provisionable(provider, monkeypatch):
    """Make provision() a no-op-ish success returning a real handle, so tests
    can reach the RUNNING state without the full docker provisioner."""
    import sandkeep.controller as ctrl

    def fake_provision(task, prov, store, audit, env, *, trace_id, exec_timeout, **kw):
        handle = prov.create(task.repo_path, env)
        store.update_fields(task.id, sandbox_id=handle.id, base_ref="HEAD")
        store.update_state(task.id, TaskState.PROVISIONING, trace_id, "prov")
        return handle

    monkeypatch.setattr(ctrl, "provision", fake_provision)


def test_run_guard_fails_task_on_unexpected_exception(wiring, monkeypatch):
    controller, store, provider, cfg = wiring
    _provisionable(provider, monkeypatch)
    provider.exec_raises = True  # agent dispatch will explode

    with pytest.raises(RuntimeError, match="boom"):
        controller.run_task("/repo", "do a thing")

    # exactly one task, and the guard drove it to a terminal state + reaped it
    tasks = store.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].state in (TaskState.FAILED, TaskState.ROLLED_BACK)
    assert provider.live == set()  # sandbox torn down, not leaked


def test_reconcile_unsticks_wedged_running_task(wiring):
    controller, store, provider, cfg = wiring
    # simulate a crash: a RUNNING task whose sandbox no longer exists
    t = Task(id=uuid.uuid4().hex, repo_path="/r", instruction="x")
    store.create_task(t, new_trace_id())
    store.update_state(t.id, TaskState.PROVISIONING, new_trace_id(), "")
    store.update_fields(t.id, sandbox_id="sandkeep-dead")
    store.update_state(t.id, TaskState.RUNNING, new_trace_id(), "")
    # provider.live is empty → sandbox is gone

    preview = controller.reconcile(dry_run=True)
    assert [x.id for x in preview] == [t.id]
    assert store.get_task(t.id).state is TaskState.RUNNING  # dry run: unchanged

    done = controller.reconcile()
    assert [x.id for x in done] == [t.id]
    assert store.get_task(t.id).state is TaskState.ROLLED_BACK


def test_reconcile_leaves_tasks_with_live_sandbox_alone(wiring):
    controller, store, provider, cfg = wiring
    t = Task(id=uuid.uuid4().hex, repo_path="/r", instruction="x")
    store.create_task(t, new_trace_id())
    store.update_state(t.id, TaskState.PROVISIONING, new_trace_id(), "")
    store.update_fields(t.id, sandbox_id="sandkeep-alive")
    store.update_state(t.id, TaskState.RUNNING, new_trace_id(), "")
    provider.live.add("sandkeep-alive")  # a run may genuinely be in flight

    assert controller.reconcile() == []
    assert store.get_task(t.id).state is TaskState.RUNNING


def test_advance_is_atomic_and_records_both_hops(wiring):
    controller, store, provider, cfg = wiring
    t = Task(id=uuid.uuid4().hex, repo_path="/r", instruction="x")
    store.create_task(t, new_trace_id())
    store.update_state(t.id, TaskState.PROVISIONING, new_trace_id(), "")
    store.update_state(t.id, TaskState.RUNNING, new_trace_id(), "")

    store.advance(t.id, [TaskState.SUCCEEDED, TaskState.REVIEW], new_trace_id(), "land")
    assert store.get_task(t.id).state is TaskState.REVIEW
    hops = [(r["from_state"], r["to_state"]) for r in store.get_transitions(t.id)]
    assert ("succeeded", "review") in hops and ("running", "succeeded") in hops


def test_advance_rejects_illegal_hop_without_partial_write(wiring):
    controller, store, provider, cfg = wiring
    t = Task(id=uuid.uuid4().hex, repo_path="/r", instruction="x")
    store.create_task(t, new_trace_id())
    with pytest.raises(IllegalTransition):
        store.advance(t.id, [TaskState.RUNNING], new_trace_id(), "")  # NEW→RUNNING illegal
    assert store.get_task(t.id).state is TaskState.NEW  # nothing moved


def test_cli_reports_bad_network_config_cleanly(monkeypatch, capsys):
    monkeypatch.setenv("SANDKEEP_NETWORK", "banana")
    rc = cli.main(["status", "whatever"])
    assert rc == 1
    assert "error:" in capsys.readouterr().err.lower()


def test_cli_debug_reraises(monkeypatch):
    monkeypatch.setenv("SANDKEEP_NETWORK", "banana")
    with pytest.raises(ValueError):
        cli.main(["--debug", "status", "whatever"])
