"""Concurrency edge cases (improvement plan, step 26).

Blind spots the round-1 review named: concurrent accept of two tasks, and
accept racing gc/reconcile. Host-side — a real host git repo for the accept
path, a no-op fake provider (the accept work that matters is host-side git),
so these run without Docker.
"""

from __future__ import annotations

import subprocess
import threading
import uuid
from pathlib import Path

import pytest

from sandkeep.audit import AuditLog, new_trace_id
from sandkeep.config import Config
from sandkeep.controller import Controller
from sandkeep.models import Task, TaskState
from sandkeep.state_store import StateStore


class NoopProvider:
    """Accept only needs host-side git; the sandbox is already 'gone'."""
    def destroy(self, handle): ...
    def list_sandbox_ids(self): return []


@pytest.fixture
def host_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.name", "T")
    git("config", "user.email", "t@e.com")
    (repo / "a.txt").write_text("base\n")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def wiring(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    store = StateStore(tmp_path / "state.sqlite3", audit=audit)
    cfg = Config()
    cfg.home = tmp_path / "home"
    cfg.ensure_dirs()
    ctrl = Controller(cfg, store, audit, NoopProvider())
    yield ctrl, store, cfg
    store.close()


def _review_task(ctrl, store, cfg, host_repo, name, filename) -> Task:
    """Create a task already parked at REVIEW with a real, applicable patch that
    edits `filename` (distinct files → no real apply conflict)."""
    base = subprocess.run(["git", "-C", str(host_repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    t = Task(id=uuid.uuid4().hex, repo_path=str(host_repo), instruction=name, base_ref=base)
    store.create_task(t, new_trace_id())
    store.update_state(t.id, TaskState.PROVISIONING, new_trace_id(), "")
    store.update_state(t.id, TaskState.RUNNING, new_trace_id(), "")
    store.advance(t.id, [TaskState.SUCCEEDED, TaskState.REVIEW], new_trace_id(), "")
    patch = cfg.outputs_dir / f"{t.id}.patch"
    patch.write_text(
        f"diff --git a/{filename} b/{filename}\n"
        "new file mode 100644\n--- /dev/null\n"
        f"+++ b/{filename}\n@@ -0,0 +1 @@\n+{name}\n"
    )
    store.update_fields(t.id, patch_path=str(patch))
    return store.get_task(t.id)


def test_concurrent_accept_of_two_tasks(wiring, host_repo):
    ctrl, store, cfg = wiring
    t1 = _review_task(ctrl, store, cfg, host_repo, "one", "one.txt")
    t2 = _review_task(ctrl, store, cfg, host_repo, "two", "two.txt")

    errors = []

    def acc(tid):
        try:
            ctrl.accept(tid)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=acc, args=(t.id,)) for t in (t1, t2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert not errors, errors
    assert store.get_task(t1.id).state is TaskState.MERGED
    assert store.get_task(t2.id).state is TaskState.MERGED
    # both landed on their own branches, state store consistent
    branches = subprocess.run(["git", "-C", str(host_repo), "branch", "--format=%(refname:short)"],
                              capture_output=True, text=True).stdout
    assert f"sandkeep-accepted/{t1.id}" in branches
    assert f"sandkeep-accepted/{t2.id}" in branches


def test_reconcile_never_touches_review_tasks(wiring, host_repo):
    """A REVIEW task (alive on purpose) must survive reconcile — otherwise
    accept could race a reconcile that rolled it back underneath."""
    ctrl, store, cfg = wiring
    t = _review_task(ctrl, store, cfg, host_repo, "keep", "keep.txt")
    reconciled = ctrl.reconcile()
    assert t.id not in [x.id for x in reconciled]
    assert store.get_task(t.id).state is TaskState.REVIEW


def test_accept_after_reconcile_still_works(wiring, host_repo):
    ctrl, store, cfg = wiring
    t = _review_task(ctrl, store, cfg, host_repo, "x", "x.txt")
    ctrl.reconcile()                       # no-op for REVIEW
    ctrl.accept(t.id)                      # still acceptable
    assert store.get_task(t.id).state is TaskState.MERGED
