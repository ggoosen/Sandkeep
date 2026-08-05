"""Fleet-level budgets (improvement plan, step 18).

Committed-spend accounting (sum of per-run --max-budget-usd) bounds worst-case
without a live price table. Tests: the daily-cap refusal, the batch total-budget
cutoff, and the ledger accounting basis. Host-side (a dummy provider that's
never reached, since the checks are pre-provision).
"""

from __future__ import annotations

import uuid

import pytest

from sandkeep.audit import AuditLog, new_trace_id
from sandkeep.config import Config
from sandkeep.controller import Controller, ControllerError, run_concurrent
from sandkeep.models import Task
from sandkeep.state_store import StateStore


@pytest.fixture
def store(tmp_path):
    audit = AuditLog(tmp_path / "a.jsonl")
    s = StateStore(tmp_path / "s.sqlite3", audit=audit)
    yield s
    s.close()


def _task(store, budget, ts_offset_created=None):
    t = Task(id=uuid.uuid4().hex, repo_path="/r", instruction="x", max_budget_usd=budget)
    store.create_task(t, new_trace_id())
    return t


# -- ledger accounting ---------------------------------------------------

def test_committed_budget_since_sums_recent_tasks(store):
    _task(store, 5.0)
    _task(store, 2.5)
    # everything is "recent" relative to a far-past cutoff
    assert store.committed_budget_since("2000-01-01T00:00:00") == 7.5
    # nothing is recent relative to a far-future cutoff
    assert store.committed_budget_since("2999-01-01T00:00:00") == 0.0


# -- daily cap -----------------------------------------------------------

def test_daily_budget_refuses_over_cap(store, tmp_path):
    cfg = Config()
    cfg.home = tmp_path
    cfg.daily_budget_usd = 10.0
    ctrl = Controller(cfg, store, AuditLog(tmp_path / "a2.jsonl"), provider=None)
    # already committed $8 in the last 24h
    _task(store, 8.0)
    # a $5 run would push to $13 > $10 → refused before any provisioning
    with pytest.raises(ControllerError, match="daily budget"):
        ctrl._enforce_daily_budget(5.0)
    # a $2 run fits ($10 exactly is allowed; over is not)
    ctrl._enforce_daily_budget(2.0)  # no raise


def test_no_daily_cap_allows_anything(store, tmp_path):
    cfg = Config()
    cfg.home = tmp_path
    cfg.daily_budget_usd = None
    ctrl = Controller(cfg, store, AuditLog(tmp_path / "a3.jsonl"), provider=None)
    ctrl._enforce_daily_budget(1000.0)  # no raise


def test_bad_daily_budget_env_rejected(monkeypatch):
    monkeypatch.setenv("SANDKEEP_DAILY_BUDGET_USD", "lots")
    with pytest.raises(ValueError):
        Config.from_env()


# -- batch total budget --------------------------------------------------

class _FakeController:
    """Just enough for run_concurrent: a config with a default budget and a
    run_task that records dispatch without doing real work."""

    def __init__(self, default_budget=5.0):
        self.config = type("C", (), {"max_budget_usd": default_budget})()
        self.dispatched = []

    def run_task(self, **spec):
        self.dispatched.append(spec["instruction"])
        return spec["instruction"]  # stand-in for a Task


def test_batch_total_budget_stops_dispatching():
    ctrl = _FakeController(default_budget=5.0)
    specs = [dict(repo_path="/r", instruction=f"t{i}") for i in range(5)]
    # total $12 with $5/run → only 2 fit ($10); the 3rd would hit $15
    results = run_concurrent(ctrl, specs, max_workers=2, total_budget_usd=12.0)
    assert len(ctrl.dispatched) == 2
    skipped = [r for r in results if isinstance(r, ControllerError)]
    assert len(skipped) == 3
    assert "batch budget" in str(skipped[0])


def test_batch_without_budget_runs_all():
    ctrl = _FakeController()
    specs = [dict(repo_path="/r", instruction=f"t{i}") for i in range(4)]
    run_concurrent(ctrl, specs, max_workers=2)
    assert len(ctrl.dispatched) == 4
