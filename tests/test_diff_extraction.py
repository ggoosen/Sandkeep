"""Diff extraction acceptance (BUILD_SPEC §7): a run that edits two files
yields a patch that `git apply --check` accepts on a clean host checkout;
files_changed cross-checks; the .sandkeep return channel never leaks into
the patch."""

from __future__ import annotations

from pathlib import Path

import pytest

from sandkeep.config import Config
from sandkeep.diff import (
    DiffError,
    apply_to_fresh_branch,
    extract_patch,
    files_in_patch,
    validate_patch,
)


def _sh(provider, handle, script: str):
    result = provider.exec(handle, ["sh", "-c", script], timeout=60)
    assert result.exit_code == 0, f"{script}: {result.stderr}"
    return result


@pytest.fixture
def edited_sandbox(provider, store, sandbox):
    """Simulate an agent editing two files + writing its contract."""
    task, handle = sandbox
    task.base_ref = store.get_task(task.id).base_ref  # pinned SHA
    _sh(provider, handle, "echo '# validated' >> /work/repo/parse_config.py")
    _sh(provider, handle, "echo 'changed' >> /work/repo/README.md")
    _sh(
        provider,
        handle,
        "mkdir -p /work/repo/.sandkeep && echo '{}' > /work/repo/.sandkeep/results.json",
    )
    _sh(provider, handle, "cd /work/repo && git add -A -- . ':(exclude).sandkeep' "
        "&& git commit -qm 'agent work'")
    return task, handle


def test_patch_applies_cleanly_to_fresh_host_checkout(
    provider, edited_sandbox, host_repo, tmp_path
):
    task, handle = edited_sandbox
    patch = extract_patch(provider, handle, task, tmp_path / "outputs")
    assert patch.exists()
    validate_patch(host_repo, task.base_ref, patch)  # raises on failure


def test_files_changed_matches_patch(provider, edited_sandbox, tmp_path):
    task, handle = edited_sandbox
    patch = extract_patch(provider, handle, task, tmp_path / "outputs")
    assert files_in_patch(patch.read_text()) == ["README.md", "parse_config.py"]


def test_sandkeep_dir_never_leaks_into_patch(provider, edited_sandbox, tmp_path):
    task, handle = edited_sandbox
    patch = extract_patch(provider, handle, task, tmp_path / "outputs")
    assert ".sandkeep" not in patch.read_text()


def test_uncommitted_agent_work_is_still_captured(provider, store, sandbox, tmp_path):
    """The agent may forget to commit; staged-vs-base still catches it."""
    task, handle = sandbox
    task.base_ref = store.get_task(task.id).base_ref
    result = provider.exec(
        handle, ["sh", "-c", "echo uncommitted >> /work/repo/README.md"], timeout=60
    )
    assert result.exit_code == 0
    patch = extract_patch(provider, handle, task, tmp_path / "outputs")
    assert "uncommitted" in patch.read_text()


def test_empty_patch_is_rejected(provider, store, sandbox, tmp_path, host_repo):
    task, handle = sandbox
    task.base_ref = store.get_task(task.id).base_ref
    patch = extract_patch(provider, handle, task, tmp_path / "outputs")
    with pytest.raises(DiffError, match="empty"):
        validate_patch(host_repo, task.base_ref, patch)


def test_apply_to_fresh_branch_lands_commit_and_restores_user(
    provider, edited_sandbox, host_repo, tmp_path
):
    task, handle = edited_sandbox
    patch = extract_patch(provider, handle, task, tmp_path / "outputs")
    import subprocess

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(host_repo), *args], capture_output=True, text=True
        ).stdout.strip()

    sha = apply_to_fresh_branch(
        host_repo, task.base_ref, f"sandkeep-accepted/{task.id}", patch, "sandkeep: test"
    )
    assert sha
    # user back on their original branch, working tree clean
    assert git("rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git("status", "--porcelain") == ""
    # the accepted branch carries exactly the change
    assert "# validated" in git("show", f"sandkeep-accepted/{task.id}:parse_config.py")


def test_apply_refuses_dirty_host_tree(provider, edited_sandbox, host_repo, tmp_path):
    task, handle = edited_sandbox
    patch = extract_patch(provider, handle, task, tmp_path / "outputs")
    (Path(host_repo) / "uncommitted.txt").write_text("wip")
    import subprocess

    subprocess.run(["git", "-C", str(host_repo), "add", "uncommitted.txt"], check=True)
    with pytest.raises(DiffError, match="dirty"):
        apply_to_fresh_branch(
            host_repo, task.base_ref, f"sandkeep-accepted/{task.id}", patch, "msg"
        )


def test_config_outputs_dir_used_for_patches(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDKEEP_HOME", str(tmp_path / "home"))
    cfg = Config.from_env()
    cfg.ensure_dirs()
    assert cfg.outputs_dir.is_dir()
