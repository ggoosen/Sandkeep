"""SQLite state store: tasks, transitions, ledger (BUILD_SPEC §3).

Stdlib sqlite3 only, no ORM. Every state change goes through
``update_state`` so the transitions table is a complete audit trail;
illegal transitions raise rather than write.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .audit import AuditLog
from .models import ALLOWED_TRANSITIONS, Task, TaskState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    repo_path   TEXT NOT NULL,
    instruction TEXT NOT NULL,
    base_ref    TEXT NOT NULL,
    branch      TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL,
    model       TEXT NOT NULL,
    agent       TEXT NOT NULL DEFAULT 'claude',
    max_turns   INTEGER NOT NULL,
    max_budget_usd REAL NOT NULL DEFAULT 5.0,
    sandbox_id  TEXT NOT NULL DEFAULT '',
    patch_path  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transitions (
    id         TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL REFERENCES tasks(id),
    from_state TEXT NOT NULL,
    to_state   TEXT NOT NULL,
    trace_id   TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    ts         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger (
    task_id         TEXT NOT NULL REFERENCES tasks(id),
    model           TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    sandbox_seconds REAL NOT NULL DEFAULT 0,
    ts              TEXT NOT NULL
);
"""


class IllegalTransition(Exception):
    """Raised when a state change is not permitted by ALLOWED_TRANSITIONS."""


class TaskNotFound(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, db_path: Path | str, audit: AuditLog | None = None) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # Concurrency (BUILD_SPEC §12): one connection shared across worker
        # threads, every access serialized by a reentrant lock (RLock because
        # update_state/list_tasks call get_task). This is simpler and more
        # robust than many connections contending for the file. WAL +
        # busy_timeout still help if a second process opens the same db.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._audit = audit

    def _migrate(self) -> None:
        """Additive, idempotent migrations for DBs created before a column
        existed (the schema's CREATE IF NOT EXISTS won't alter an old table)."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(tasks)")}
        if "agent" not in cols:  # added in Phase 5 (BUILD_SPEC §13)
            with self._conn:
                self._conn.execute(
                    "ALTER TABLE tasks ADD COLUMN agent TEXT NOT NULL DEFAULT 'claude'"
                )
        if "max_budget_usd" not in cols:  # improvement plan, step 9
            with self._conn:
                self._conn.execute(
                    "ALTER TABLE tasks ADD COLUMN max_budget_usd REAL NOT NULL DEFAULT 5.0"
                )

    def close(self) -> None:
        self._conn.close()

    # -- tasks ----------------------------------------------------------

    def create_task(self, task: Task, trace_id: str) -> None:
        with self._lock:
            now = _now()
            with self._conn:
                self._conn.execute(
                    "INSERT INTO tasks (id, repo_path, instruction, base_ref, branch,"
                    " state, model, agent, max_turns, max_budget_usd, sandbox_id,"
                    " patch_path, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task.id,
                        task.repo_path,
                        task.instruction,
                        task.base_ref,
                        task.branch,
                        task.state.value,
                        task.model,
                        task.agent,
                        # max_turns is vestigial: the upstream claude CLI
                        # removed the flag. The column stays (old DBs declare
                        # it NOT NULL) but nothing reads it anymore.
                        8,
                        task.max_budget_usd,
                        task.sandbox_id,
                        task.patch_path,
                        now,
                        now,
                    ),
                )
        if self._audit:
            self._audit.log(
                "task_created", trace_id=trace_id, task_id=task.id, state=task.state.value
            )

    def get_task(self, task_id: str) -> Task:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        return Task(
            id=row["id"],
            repo_path=row["repo_path"],
            instruction=row["instruction"],
            base_ref=row["base_ref"],
            branch=row["branch"],
            state=TaskState(row["state"]),
            model=row["model"],
            agent=row["agent"],
            max_budget_usd=row["max_budget_usd"],
            sandbox_id=row["sandbox_id"],
            patch_path=row["patch_path"],
        )

    def list_tasks(self) -> list[Task]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM tasks ORDER BY created_at"
            ).fetchall()
            return [self.get_task(row["id"]) for row in rows]

    def update_fields(self, task_id: str, **fields: str) -> None:
        """Update mutable non-state columns (branch, sandbox_id, patch_path)."""
        allowed = {"branch", "sandbox_id", "patch_path", "base_ref"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"not updatable here: {sorted(bad)}")
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE tasks SET {sets}, updated_at = ? WHERE id = ?",
                (*fields.values(), _now(), task_id),
            )

    # -- state machine ---------------------------------------------------

    def update_state(
        self, task_id: str, new_state: TaskState, trace_id: str, detail: str = ""
    ) -> None:
        """Transition a task, writing tasks.state and a transitions row in one
        transaction. Raises IllegalTransition on a move §9 does not allow.
        The read-check-write is atomic under the store lock."""
        with self._lock:
            task = self.get_task(task_id)
            if new_state not in ALLOWED_TRANSITIONS[task.state]:
                raise IllegalTransition(
                    f"{task.state.value} → {new_state.value} is not a legal transition"
                )
            now = _now()
            with self._conn:
                self._conn.execute(
                    "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
                    (new_state.value, now, task_id),
                )
                self._conn.execute(
                    "INSERT INTO transitions (id, task_id, from_state, to_state, trace_id, detail, ts)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (uuid.uuid4().hex, task_id, task.state.value, new_state.value, trace_id, detail, now),
                )
            from_state = task.state.value
        if self._audit:
            self._audit.log(
                "state_transition",
                trace_id=trace_id,
                task_id=task_id,
                from_state=from_state,
                to_state=new_state.value,
                detail=detail,
            )

    def get_transitions(self, task_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM transitions WHERE task_id = ? ORDER BY ts", (task_id,)
            ).fetchall()

    # -- ledger -----------------------------------------------------------

    def record_cost(
        self,
        task_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        sandbox_seconds: float,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO ledger (task_id, model, input_tokens, output_tokens,"
                " sandbox_seconds, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, model, input_tokens, output_tokens, sandbox_seconds, _now()),
            )

    def get_ledger(self, task_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM ledger WHERE task_id = ? ORDER BY ts", (task_id,)
            ).fetchall()
