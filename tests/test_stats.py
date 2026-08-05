"""Observability — sandkeep stats (improvement plan, step 23).

Aggregate cost + outcomes from the ledger/tasks tables. Host-side (SQLite only).
"""

from __future__ import annotations

import uuid

import pytest

from sandkeep.audit import AuditLog, new_trace_id
from sandkeep.models import Task, TaskState
from sandkeep.state_store import StateStore


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "s.sqlite3", audit=AuditLog(tmp_path / "a.jsonl"))
    yield s
    s.close()


def _mk(store, *, agent="claude", model="claude-sonnet-4-6", to_state=None):
    t = Task(id=uuid.uuid4().hex, repo_path="/r", instruction="x", agent=agent, model=model)
    store.create_task(t, new_trace_id())
    if to_state:
        # walk a minimal legal path to the desired state
        path = {
            TaskState.REVIEW: [TaskState.PROVISIONING, TaskState.RUNNING,
                               TaskState.SUCCEEDED, TaskState.REVIEW],
            TaskState.ROLLED_BACK: [TaskState.PROVISIONING, TaskState.FAILED,
                                    TaskState.ROLLED_BACK],
        }[to_state]
        for st in path:
            store.update_state(t.id, st, new_trace_id(), "")
    return t


def test_cost_by_model_aggregates(store):
    t1 = _mk(store, agent="claude")
    t2 = _mk(store, agent="codex", model="gpt-5")
    store.record_cost(t1.id, "claude-sonnet-4-6", 100, 40, 3.0)
    store.record_cost(t1.id, "claude-sonnet-4-6", 50, 10, 1.0)
    store.record_cost(t2.id, "gpt-5", 200, 80, 5.0)

    rows = {(r["model"], r["agent"]): r for r in store.cost_by_model()}
    claude = rows[("claude-sonnet-4-6", "claude")]
    assert claude["runs"] == 2
    assert claude["input_tokens"] == 150 and claude["output_tokens"] == 50
    assert rows[("gpt-5", "codex")]["input_tokens"] == 200


def test_task_outcomes_counts_states(store):
    _mk(store, to_state=TaskState.REVIEW)
    _mk(store, to_state=TaskState.REVIEW)
    _mk(store, to_state=TaskState.ROLLED_BACK)
    outcomes = store.task_outcomes()
    assert outcomes["review"] == 2
    assert outcomes["rolled_back"] == 1


def test_empty_store_reports_nothing(store):
    assert store.cost_by_model() == []
    assert store.task_outcomes() == {}


def test_stats_command_smoke(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SANDKEEP_HOME", str(tmp_path))
    from sandkeep import cli

    rc = cli.main(["stats"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "tasks:" in out
