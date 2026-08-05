"""Extract / validate / apply patches (BUILD_SPEC §7, §10).

Only the patch leaves the sandbox; only `accept` ever writes to the host
repo, and then only onto a fresh branch. Host-side git runs through
subprocess here — that is fine (the docker-only rule applies to docker).
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from .models import Task
from .sandbox.base import SandboxHandle, SandboxProvider


class DiffError(Exception):
    pass


class UnsafePatch(DiffError):
    """A returned patch targets a path that must never be written on accept
    (absolute, parent-escaping, or inside .git). Fail loud — never apply."""


# Default cap on the size of a returned patch (improvement plan, step 2). A
# multi-GB or opaque-blob patch is a DoS / review-defeating vector; the human
# gate can't meaningfully review one anyway. Overridable via Config.
DEFAULT_MAX_PATCH_BYTES = 5 * 1024 * 1024

# .sandkeep/ holds the contract + agent notes + authored skills; it is the
# return/metadata channel, never part of the change itself. All of .claude/ is
# sandkeep-managed too: it is where stored skills are injected (Phase 4), and an
# agent-authored .claude/settings.json (hooks/permissions) would otherwise land
# on the host and execute in the user's future local Claude sessions. So the
# whole directory is kept out of the returned patch.
# TODO(phase-4): let a repo opt in to managing its own .claude/ when it isn't a
# sandkeep injection target.
_EXCLUDE_PATHSPECS = [":(exclude).sandkeep", ":(exclude).claude"]


def extract_patch(
    provider: SandboxProvider,
    handle: SandboxHandle,
    task: Task,
    outputs_dir: Path,
    *,
    exec_timeout: int = 120,
    max_bytes: int = DEFAULT_MAX_PATCH_BYTES,
) -> Path:
    """Stage everything inside the sandbox, diff against the pinned base,
    and write the patch to outputs/<task_id>.patch on the host."""
    add = provider.exec(
        handle,
        ["git", "-C", handle.workdir, "add", "-A", "--", ".", *_EXCLUDE_PATHSPECS],
        timeout=exec_timeout,
    )
    if add.exit_code != 0:
        raise DiffError(f"git add failed in sandbox: {add.stderr.strip()}")
    diff = provider.exec(
        handle,
        ["git", "-C", handle.workdir, "diff", "--cached", "--patch", "--binary",
         task.base_ref, "--", ".", *_EXCLUDE_PATHSPECS],
        timeout=exec_timeout,
    )
    if diff.exit_code != 0:
        raise DiffError(f"git diff failed in sandbox: {diff.stderr.strip()}")

    if len(diff.stdout.encode("utf-8", "surrogatepass")) > max_bytes:
        raise DiffError(
            f"patch exceeds the {max_bytes}-byte limit — refusing to bring back "
            "an unreviewably large diff (raise Config.max_patch_bytes to override)"
        )

    outputs_dir.mkdir(parents=True, exist_ok=True)
    patch_path = outputs_dir / f"{task.id}.patch"
    patch_path.write_text(diff.stdout)
    return patch_path


def _unquote_git_path(path: str) -> str:
    r"""Git quotes paths with special chars in a C-style string
    ("a b\tc"); undo that so path-safety checks see the real path."""
    path = path.strip()
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        try:
            return path[1:-1].encode("latin-1", "backslashreplace").decode(
                "unicode_escape"
            )
        except (UnicodeDecodeError, ValueError):
            return path[1:-1]
    return path


def _strip_prefix(path: str) -> str:
    """Drop the a/ or b/ prefix git puts on diff paths (/dev/null stays)."""
    if path == "/dev/null":
        return path
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def files_in_patch(patch_text: str) -> list[str]:
    """Paths touched by a patch. Parses the per-file `+++`/`---` headers
    (quoting-proof, unlike matching the `diff --git` line, whose two paths are
    ambiguous when a name contains spaces) so it feeds policy risk flags and
    conflict detection the real target paths."""
    files: set[str] = set()
    for line in patch_text.splitlines():
        if line.startswith(("--- ", "+++ ")):
            raw = _unquote_git_path(line[4:])
            path = _strip_prefix(raw)
            if path and path != "/dev/null":
                files.add(path)
        elif line.startswith(("rename from ", "rename to ",
                              "copy from ", "copy to ")):
            path = _unquote_git_path(line.split(" ", 2)[2])
            if path:
                files.add(path)
    return sorted(files)


def _is_unsafe_path(path: str) -> bool:
    """True if applying a hunk to `path` could escape the repo root: an
    absolute path, a parent-directory escape, or anything inside .git/."""
    if not path or path == "/dev/null":
        return False
    if path.startswith("/") or (len(path) > 1 and path[1] == ":"):  # /etc, C:\
        return True
    parts = PurePosixPath(path).parts
    if ".." in parts:
        return True
    if parts and parts[0] == ".git":
        return True
    return False


def assert_safe_patch(patch_path: Path) -> None:
    """Reject a patch that targets any path git apply must never write on the
    host (absolute, .., .git/). git apply refuses most of these itself, but we
    fail loud *before* touching the host rather than relying on that."""
    text = patch_path.read_text(errors="replace")
    for path in files_in_patch(text):
        if _is_unsafe_path(path):
            raise UnsafePatch(f"patch targets an unsafe path: {path!r}")


def _git(repo: Path | str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def validate_patch(host_repo: Path | str, base_ref: str, patch_path: Path) -> None:
    """git apply --check against a FRESH checkout of the host repo at
    base_ref — never against the user's working tree (BUILD_SPEC §7.3).
    Path-safety is asserted first, so an unsafe patch is rejected loudly even
    on a git that would otherwise apply it."""
    if patch_path.stat().st_size == 0:
        raise DiffError("patch is empty — the agent made no changes")
    assert_safe_patch(patch_path)
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
    """The accept path (BUILD_SPEC §10): create `branch` from base_ref with the
    patch applied, without ever touching the user's working tree. Returns the
    commit sha.

    Done in a throwaway `git worktree` checked out at base_ref: the patch is
    applied and committed there, then the branch is published back to the main
    repo and the worktree removed. So a crash mid-accept can only leave a
    disposable worktree — the user's checkout, index, and current branch are
    never mutated. Path-safety is asserted before anything is applied."""
    assert_safe_patch(patch_path)
    host_repo = Path(host_repo).resolve()

    exists = _git(host_repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if exists.returncode == 0:
        raise DiffError(f"branch {branch} already exists — accept was already run")

    with tempfile.TemporaryDirectory(prefix="sandkeep-accept-") as tmp:
        wt = str(Path(tmp) / "wt")
        added = _git(host_repo, "worktree", "add", "--detach", "-q", wt, base_ref)
        if added.returncode != 0:
            raise DiffError(f"could not create accept worktree: {added.stderr.strip()}")
        try:
            applied = _git(wt, "apply", str(patch_path.resolve()))
            if applied.returncode != 0:
                raise DiffError(f"git apply failed: {applied.stderr.strip()}")
            _git(wt, "add", "-A")
            committed = _git(
                wt, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", commit_message
            )
            if committed.returncode != 0:
                raise DiffError(f"commit failed: {committed.stderr.strip()}")
            sha = _git(wt, "rev-parse", "HEAD").stdout.strip()
            published = _git(host_repo, "branch", branch, sha)
            if published.returncode != 0:
                raise DiffError(f"could not create branch {branch}: {published.stderr.strip()}")
            return sha
        finally:
            _git(host_repo, "worktree", "remove", "--force", wt)
