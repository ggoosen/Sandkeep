"""Advisory output scan + ledger-on-failure (improvement plan, step 5).

Host-side wiring tests (no Docker): a failed run still writes a ledger row,
and output-scan hits captured at land time surface as advisory risk flags at
the gate. The end-to-end proof that a transcript marker no longer archives a
clean run as VIOLATION is the Docker-backed test in test_controller.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

import pytest

from sandkeep import agent_runner
from sandkeep.agent_runner import AgentRunResult
from sandkeep.audit import AuditLog, new_trace_id
from sandkeep.config import Config
from sandkeep.controller import Controller
from sandkeep.models import Task, TaskState
from sandkeep.sandbox.base import ExecResult, SandboxHandle


@dataclass
class FakeProvider:
    live: set = field(default_factory=set)
    read_raises: bool = True

    def create(self, repo_path, env):
        sid = f"sandkeep-{uuid.uuid4().hex[:8]}"
        self.live.add(sid)
        return SandboxHandle(id=sid, workdir="/work/repo")

    def exec(self, handle, cmd, timeout):
        return ExecResult(exit_code=0, stdout="", stderr="")

    def read_file(self, handle, path):
        if self.read_raises:
            raise FileNotFoundError(path)  # no contract → run fails after agent
        return "{}"

    def destroy(self, handle):
        self.live.discard(handle.id)

    def list_sandbox_ids(self):
        return sorted(self.live)


@pytest.fixture
def wiring(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    from sandkeep.state_store import StateStore

    store = StateStore(tmp_path / "state.sqlite3", audit=audit)
    cfg = Config()
    cfg.home = tmp_path / "home"
    cfg.ensure_dirs()
    controller = Controller(cfg, store, audit, FakeProvider())
    yield controller, store, cfg
    store.close()


def _fake_provision(monkeypatch):
    import sandkeep.controller as ctrl

    def fake(task, prov, store, audit, env, *, trace_id, exec_timeout, **kw):
        handle = prov.create(task.repo_path, env)
        store.update_fields(task.id, sandbox_id=handle.id, base_ref="HEAD")
        store.update_state(task.id, TaskState.PROVISIONING, trace_id, "prov")
        return handle

    monkeypatch.setattr(ctrl, "provision", fake)


def test_failed_run_still_records_ledger(wiring, monkeypatch):
    controller, store, cfg = wiring
    _fake_provision(monkeypatch)

    def fake_run_agent(task, provider, handle, audit, *, trace_id, timeout, **kw):
        # agent exits 0 but writes no contract → run fails at read_file;
        # it still burned tokens, which must land in the ledger.
        return AgentRunResult(
            exit_code=0, timed_out=False, stdout="", stderr="",
            output={"usage": {"input_tokens": 321, "output_tokens": 12}}, detail="",
        )

    monkeypatch.setattr(agent_runner, "run_agent", fake_run_agent)

    task = controller.run_task("/repo", "do a thing")
    assert task.state is TaskState.ROLLED_BACK  # failed → rolled back
    ledger = store.get_ledger(task.id)
    assert len(ledger) == 1
    assert ledger[0]["input_tokens"] == 321
    assert ledger[0]["output_tokens"] == 12


def test_risk_flags_surface_output_scan_sidecar(wiring):
    controller, store, cfg = wiring
    # a task parked at REVIEW with a patch and an advisory scan sidecar
    t = Task(id=uuid.uuid4().hex, repo_path="/r", instruction="x")
    store.create_task(t, new_trace_id())
    patch = cfg.outputs_dir / f"{t.id}.patch"
    patch.write_text(
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    store.update_fields(t.id, patch_path=str(patch))
    (cfg.outputs_dir / f"{t.id}.scan.json").write_text(
        json.dumps([{"kind": "filesystem", "detail": "mention of /src permission denied"}])
    )
    flags = controller.risk_flags(store.get_task(t.id))
    assert any(f.category.startswith("output-scan/") for f in flags)
