"""Tier 2: sandbox lifecycle — provisioning (BUILD_SPEC §5).

Read-only /src mount → independent clone at /work/repo → task branch.
The clone owning its own writable .git, with /src untouchable, IS the
isolation-critical step; nothing here ever points a writable mount at
the host repo.
"""

from __future__ import annotations

from pathlib import Path

from . import policy
from .audit import AuditLog
from .models import Task, TaskState
from .sandbox.base import SRC_MOUNT, SandboxHandle, SandboxProvider
from .state_store import StateStore


class ProvisioningError(Exception):
    pass


def task_branch(task_id: str) -> str:
    return f"sandkeep/{task_id}"


# Read-only ≠ unreadable: the agent can read every tracked file in /src. This
# scans what it would expose so the human is told before a run (step 15). Cheap
# and advisory — never blocks; the human decides.
_SCAN_MAX_FILE_BYTES = 1_000_000
_SCAN_MAX_FINDINGS = 50


def scan_repo_secrets(repo_path: str | Path, *, max_findings: int = _SCAN_MAX_FINDINGS) -> list[str]:
    """Secret-shaped content the agent would be able to read from the repo.
    Returns 'path: label' strings (capped). Skips .git, large, and binary
    files. Best-effort — unreadable files are ignored."""
    root = Path(repo_path)
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if len(findings) >= max_findings:
            break
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            if path.stat().st_size > _SCAN_MAX_FILE_BYTES:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw:  # binary
            continue
        for label in policy.find_secrets_in_text(raw.decode("utf-8", "replace")):
            findings.append(f"{path.relative_to(root)}: {label}")
            if len(findings) >= max_findings:
                break
    return findings


def build_clone_step(src_mount: str, workdir: str, *, shallow: bool) -> list[str]:
    """The git clone argv (step 15). A shallow clone limits the deep history the
    *clone* carries — using a file:// URL because git ignores --depth for plain
    local-path clones. (Note: /src itself stays fully readable; a truly
    history-stripped /src is a deeper follow-up.)"""
    if shallow:
        return ["git", "clone", "--depth=1", "--no-hardlinks",
                f"file://{src_mount}", workdir]
    return ["git", "clone", "--no-hardlinks", src_mount, workdir]


def provision(
    task: Task,
    provider: SandboxProvider,
    store: StateStore,
    audit: AuditLog,
    env: dict[str, str],
    *,
    trace_id: str,
    exec_timeout: int = 120,
    shallow: bool = True,
    scan_secrets: bool = True,
) -> SandboxHandle:
    """Provision a sandbox for `task`. Transitions NEW → PROVISIONING up
    front so any failure can legally move on to FAILED."""
    store.update_state(task.id, TaskState.PROVISIONING, trace_id, "sandbox create + clone")
    if scan_secrets:
        exposed = scan_repo_secrets(task.repo_path)
        if exposed:
            audit.log(
                "repo_secret_exposure", trace_id=trace_id, task_id=task.id,
                count=len(exposed), findings=exposed[:20],
            )
    handle = provider.create(task.repo_path, env)
    store.update_fields(task.id, sandbox_id=handle.id)
    audit.log("sandbox_created", trace_id=trace_id, task_id=task.id, sandbox_id=handle.id)
    # Once the container exists, ANY later failure must not leak it — clean up
    # the sandbox we just created, then re-raise for the caller to handle.
    try:
        return _provision_clone(task, provider, handle, audit, trace_id=trace_id,
                                exec_timeout=exec_timeout, store=store, shallow=shallow)
    except BaseException:
        try:
            provider.destroy(handle)
        except Exception:
            pass  # best-effort; the original error is what matters
        raise


def _provision_clone(
    task: Task,
    provider: SandboxProvider,
    handle: SandboxHandle,
    audit: AuditLog,
    *,
    trace_id: str,
    exec_timeout: int,
    store: StateStore,
    shallow: bool = True,
) -> SandboxHandle:
    branch = task_branch(task.id)
    steps: list[list[str]] = [
        # /src is owned by a host/root uid the agent doesn't match; tell git
        # inside it's ok to read it. Both the worktree and its .git are checked
        # (the microVM backend roots /src, which trips git's dubious-ownership
        # guard on /src/.git); harmless where ownership already matches (Docker).
        ["git", "config", "--global", "--add", "safe.directory", SRC_MOUNT],
        ["git", "config", "--global", "--add", "safe.directory", f"{SRC_MOUNT}/.git"],
        # the independent clone: its own writable .git, host .git untouched.
        # shallow by default (step 15) so the clone carries no deep history.
        # submodules are not initialised, LFS is absent from the image —
        # TODO(phase-2): opt-in submodule/LFS support
        build_clone_step(SRC_MOUNT, handle.workdir, shallow=shallow),
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

    # pin base_ref to a concrete SHA: "HEAD" would drift once the agent
    # commits, and the diff (§7) and host apply (§10) must share one base
    rev = provider.exec(
        handle, ["git", "-C", handle.workdir, "rev-parse", "HEAD"], timeout=exec_timeout
    )
    if rev.exit_code != 0:
        raise ProvisioningError(f"could not resolve base ref: {rev.stderr.strip()}")
    base_sha = rev.stdout.strip()
    store.update_fields(task.id, base_ref=base_sha)
    task.base_ref = base_sha

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
