"""Extract / validate / apply patches (BUILD_SPEC §7, §10).

Only the patch leaves the sandbox; only `accept` ever writes to the host
repo, and then only onto a fresh branch. Host-side git runs through
subprocess here — that is fine (the docker-only rule applies to docker).
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from .models import Task
from .sandbox.base import SandboxHandle, SandboxProvider


class DiffError(Exception):
    pass


# .sandkeep/ holds the contract + agent notes; it is the return channel,
# never part of the change itself
_EXCLUDE_PATHSPEC = ":(exclude).sandkeep"


def extract_patch(
    provider: SandboxProvider,
    handle: SandboxHandle,
    task: Task,
    outputs_dir: Path,
    *,
    exec_timeout: int = 120,
) -> Path:
    """Stage everything inside the sandbox, diff against the pinned base,
    and write the patch to outputs/<task_id>.patch on the host."""
    add = provider.exec(
        handle,
        ["git", "-C", handle.workdir, "add", "-A", "--", ".", _EXCLUDE_PATHSPEC],
        timeout=exec_timeout,
    )
    if add.exit_code != 0:
        raise DiffError(f"git add failed in sandbox: {add.stderr.strip()}")
    diff = provider.exec(
        handle,
        ["git", "-C", handle.workdir, "diff", "--cached", "--patch", "--binary",
         task.base_ref, "--", ".", _EXCLUDE_PATHSPEC],
        timeout=exec_timeout,
    )
    if diff.exit_code != 0:
        raise DiffError(f"git diff failed in sandbox: {diff.stderr.strip()}")

    outputs_dir.mkdir(parents=True, exist_ok=True)
    patch_path = outputs_dir / f"{task.id}.patch"
    patch_path.write_text(diff.stdout)
    return patch_path


def files_in_patch(patch_text: str) -> list[str]:
    """Paths touched by a patch, for cross-checking the contract's
    files_changed claim."""
    files: set[str] = set()
    for match in re.finditer(r"^diff --git a/(.+?) b/(.+)$", patch_text, re.MULTILINE):
        files.add(match.group(1))
        files.add(match.group(2))
    return sorted(files)


def _git(repo: Path | str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def validate_patch(host_repo: Path | str, base_ref: str, patch_path: Path) -> None:
    """git apply --check against a FRESH checkout of the host repo at
    base_ref — never against the user's working tree (BUILD_SPEC §7.3)."""
    if patch_path.stat().st_size == 0:
        raise DiffError("patch is empty — the agent made no changes")
    with tempfile.TemporaryDirectory(prefix="sandkeep-validate-") as tmp:
        clone = _git(".", "clone", "--no-hardlinks", "-q", str(host_repo), tmp)
        if clone.returncode != 0:
            raise DiffError(f"validation clone failed: {clone.stderr.strip()}")
        checkout = _git(tmp, "checkout", "-q", base_ref)
        if checkout.returncode != 0:
            raise DiffError(f"base ref {base_ref} not in host repo: {checkout.stderr.strip()}")
        check = _git(tmp, "apply", "--check", str(patch_path.resolve()))
        if check.returncode != 0:
            raise DiffError(f"patch does not apply cleanly at {base_ref}: {check.stderr.strip()}")


def apply_to_fresh_branch(
    host_repo: Path | str,
    base_ref: str,
    branch: str,
    patch_path: Path,
    commit_message: str,
) -> str:
    """The accept path (BUILD_SPEC §10): fresh branch from base_ref, apply,
    commit, then return the user to where they were. Returns the commit sha.

    Refuses to run if the working tree is dirty — fail loud, never mingle
    the user's uncommitted work with an applied patch."""
    status = _git(host_repo, "status", "--porcelain")
    if status.stdout.strip():
        raise DiffError("host working tree is dirty — commit or stash before accepting")

    original = _git(host_repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    created = _git(host_repo, "checkout", "-q", "-b", branch, base_ref)
    if created.returncode != 0:
        raise DiffError(f"could not create branch {branch}: {created.stderr.strip()}")
    try:
        applied = _git(host_repo, "apply", str(patch_path.resolve()))
        if applied.returncode != 0:
            raise DiffError(f"git apply failed: {applied.stderr.strip()}")
        _git(host_repo, "add", "-A")
        committed = _git(host_repo, "commit", "-q", "-m", commit_message)
        if committed.returncode != 0:
            raise DiffError(f"commit failed: {committed.stderr.strip()}")
        return _git(host_repo, "rev-parse", "HEAD").stdout.strip()
    finally:
        if original and original != "HEAD":
            _git(host_repo, "checkout", "-q", original)
