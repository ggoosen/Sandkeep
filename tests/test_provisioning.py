"""Provisioning acceptance (BUILD_SPEC §5): independent writable clone on
the task branch; /src read-only; no writable path back to the host repo."""

from __future__ import annotations

import hashlib
from pathlib import Path

from sandkeep.models import TaskState
from sandkeep.provisioner import task_branch


def _sh(provider, handle, script: str):
    return provider.exec(handle, ["sh", "-c", script], timeout=60)


def _tree_digest(root: Path) -> str:
    """Byte-level digest of every file under root (incl. .git)."""
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            h.update(str(path.relative_to(root)).encode())
            h.update(path.read_bytes())
    return h.hexdigest()


def test_clone_has_its_own_writable_git(provider, sandbox):
    _, handle = sandbox
    assert _sh(provider, handle, "test -d /work/repo/.git").exit_code == 0
    assert _sh(provider, handle, "touch /work/repo/.git/sandkeep-probe").exit_code == 0
    assert _sh(provider, handle, "echo x >> /work/repo/parse_config.py").exit_code == 0


def test_src_mount_is_read_only(provider, sandbox):
    _, handle = sandbox
    assert _sh(provider, handle, "touch /src/probe 2>/dev/null").exit_code != 0
    assert _sh(provider, handle, "touch /src/.git/probe 2>/dev/null").exit_code != 0
    assert _sh(provider, handle, "rm /src/README.md 2>/dev/null").exit_code != 0


def test_checked_out_on_task_branch(provider, sandbox):
    task, handle = sandbox
    result = provider.exec(
        handle, ["git", "-C", handle.workdir, "rev-parse", "--abbrev-ref", "HEAD"], timeout=60
    )
    assert result.stdout.strip() == task_branch(task.id)


def test_no_remotes_and_hooks_neutralised(provider, sandbox):
    _, handle = sandbox
    remotes = provider.exec(handle, ["git", "-C", handle.workdir, "remote"], timeout=60)
    assert remotes.stdout.strip() == ""
    hooks = provider.exec(
        handle, ["git", "-C", handle.workdir, "config", "core.hooksPath"], timeout=60
    )
    assert hooks.stdout.strip() == "/dev/null"


def test_host_repo_byte_identical_after_provision_and_writes(
    provider, store, audit, host_repo
):
    """No writable path to the host: provision, write hard inside, destroy —
    the host repo must be byte-for-byte unchanged."""
    import uuid

    from sandkeep.audit import new_trace_id
    from sandkeep.models import Task
    from sandkeep.provisioner import provision

    before = _tree_digest(host_repo)
    t = Task(id=uuid.uuid4().hex, repo_path=str(host_repo), instruction="probe")
    store.create_task(t, new_trace_id())
    handle = provision(t, provider, store, audit, env={}, trace_id=new_trace_id())
    try:
        _sh(provider, handle, "echo tampered > /work/repo/parse_config.py")
        _sh(provider, handle, "cd /work/repo && git add -A && git commit -qm tamper")
        _sh(provider, handle, "touch /src/owned 2>/dev/null")
    finally:
        provider.destroy(handle)
    assert _tree_digest(host_repo) == before


def test_provision_records_state_branch_and_sandbox_id(store, sandbox):
    task, handle = sandbox
    stored = store.get_task(task.id)
    assert stored.state is TaskState.PROVISIONING
    assert stored.branch == task_branch(task.id)
    assert stored.sandbox_id == handle.id
    assert len(store.get_transitions(task.id)) == 1
