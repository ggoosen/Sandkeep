"""Configurable per-run spend cap (improvement plan, step 9).

Precedence mirrors model/agent: flag > SANDKEEP_MAX_BUDGET_USD > default.
The value is persisted on the task so `status`/`show` report the budget the
run actually had.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from sandkeep.agent import get_driver
from sandkeep.cli import build_parser
from sandkeep.config import DEFAULT_MAX_BUDGET_USD, Config
from sandkeep.models import Task
from sandkeep.state_store import StateStore


def test_default_budget_is_five_usd():
    assert Config().max_budget_usd == DEFAULT_MAX_BUDGET_USD == 5.0


def test_env_override_parses(monkeypatch):
    monkeypatch.setenv("SANDKEEP_MAX_BUDGET_USD", "2.5")
    assert Config.from_env().max_budget_usd == 2.5


@pytest.mark.parametrize("bad", ["abc", "", "-1", "0"])
def test_env_override_rejects_bad_values(monkeypatch, bad):
    monkeypatch.setenv("SANDKEEP_MAX_BUDGET_USD", bad)
    with pytest.raises(ValueError):
        Config.from_env()


def test_run_flag_parses():
    args = build_parser().parse_args(
        ["run", "--repo", "/r", "--task", "t", "--max-budget-usd", "1.25"]
    )
    assert args.max_budget_usd == 1.25


def test_batch_flag_parses():
    args = build_parser().parse_args(
        ["batch", "--repo", "/r", "--task", "t", "--max-budget-usd", "0.5"]
    )
    assert args.max_budget_usd == 0.5


def test_claude_command_reflects_budget():
    task = Task(id="tid", repo_path="/r", instruction="do a thing")
    cmd = get_driver("claude").build_command(task, max_budget_usd="1.25")
    assert "--max-budget-usd 1.25" in cmd


def test_store_round_trips_budget(store):
    from sandkeep.audit import new_trace_id

    t = Task(
        id=uuid.uuid4().hex, repo_path="/r", instruction="x", max_budget_usd=2.75
    )
    store.create_task(t, new_trace_id())
    assert store.get_task(t.id).max_budget_usd == 2.75


def test_migration_adds_budget_column_to_old_db(tmp_path: Path):
    """A DB created before the column existed opens cleanly and reads the
    default for old rows (additive, idempotent migration)."""
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, repo_path TEXT NOT NULL, instruction TEXT NOT NULL,
            base_ref TEXT NOT NULL, branch TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL, model TEXT NOT NULL,
            agent TEXT NOT NULL DEFAULT 'claude', max_turns INTEGER NOT NULL,
            sandbox_id TEXT NOT NULL DEFAULT '', patch_path TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO tasks VALUES ('old-task', '/r', 'x', 'HEAD', '', 'new',
                                  'm', 'claude', 8, '', '', 't0', 't0');
        """
    )
    conn.commit()
    conn.close()

    s = StateStore(db)
    try:
        assert s.get_task("old-task").max_budget_usd == 5.0
    finally:
        s.close()
