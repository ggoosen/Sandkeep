"""Tier 2: sandbox lifecycle — provisioning (BUILD_SPEC §5).

Read-only /src mount → independent clone at /work/repo → task branch.
The clone owning its own writable .git, with /src untouchable, IS the
isolation-critical step; nothing here ever points a writable mount at
the host repo.
"""

from __future__ import annotations

from .audit import AuditLog
from .models import Task, TaskState
from .sandbox.base import SRC_MOUNT, SandboxHandle, SandboxProvider
from .state_store import StateStore


class ProvisioningError(Exception):
    pass


def task_branch(task_id: str) -> str:
    return f"sandkeep/{task_id}"


def provision(
    task: Task,
    provider: SandboxProvider,
    store: StateStore,
    audit: AuditLog,
    env: dict[str, str],
    *,
    trace_id: str,
    exec_timeout: int = 120,
) -> SandboxHandle:
    """Provision a sandbox for `task`. Transitions NEW → PROVISIONING up
    front so any failure can legally move on to FAILED."""
    store.update_state(task.id, TaskState.PROVISIONING, trace_id, "sandbox create + clone")
    handle = provider.create(task.repo_path, env)
    store.update_fields(task.id, sandbox_id=handle.id)
    audit.log("sandbox_created", trace_id=trace_id, task_id=task.id, sandbox_id=handle.id)

    branch = task_branch(task.id)
    steps: list[list[str]] = [
        # /src is owned by a host uid; tell git inside it's ok to read
        ["git", "config", "--global", "--add", "safe.directory", SRC_MOUNT],
        # the independent clone: its own writable .git, host .git untouched.
        # submodules are not initialised, LFS is absent from the image —
        # TODO(phase-2): opt-in submodule/LFS support
        ["git", "clone", "--no-hardlinks", SRC_MOUNT, handle.workdir],
        ["git", "-C", handle.workdir, "checkout", "-b", branch, task.base_ref],
        # drop the origin remote so nothing inside references /src again
        ["git", "-C", handle.workdir, "remote", "remove", "origin"],
        # neutralise hooks inherited from the host repo
        ["git", "-C", handle.workdir, "config", "core.hooksPath", "/dev/null"],
        # identity for the agent's commits inside the sandbox
        ["git", "-C", handle.workdir, "config", "user.name", "sandkeep-agent"],
        ["git", "-C", handle.workdir, "config", "user.email", "agent@sandkeep.local"],
    ]
    for cmd in steps:
        result = provider.exec(handle, cmd, timeout=exec_timeout)
        if result.exit_code != 0:
            raise ProvisioningError(
                f"provisioning step failed ({' '.join(cmd)}): {result.stderr.strip()}"
            )

    # best-effort toolchain pinning (BUILD_SPEC §5.4) — never fatal
    mise = provider.exec(
        handle,
        ["sh", "-c",
         f"cd {handle.workdir} && {{ [ -f mise.toml ] || [ -f .mise.toml ]; }}"
         " && mise install || true"],
        timeout=exec_timeout,
    )
    audit.log(
        "provisioned",
        trace_id=trace_id,
        task_id=task.id,
        sandbox_id=handle.id,
        branch=branch,
        mise_exit=mise.exit_code,
    )
    store.update_fields(task.id, branch=branch)
    return handle
