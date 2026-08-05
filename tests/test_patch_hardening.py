"""Adversarial patch handling (improvement plan, step 2).

`git apply` on the host is the one channel where agent-controlled bytes touch
the user's repo. These tests feed hand-crafted hostile patches straight to the
host-side validate/apply path — no sandbox, no Docker — and assert every one is
rejected BEFORE anything is written to the host. They also pin the quoting-proof
path parser that feeds the risk-flag and conflict logic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sandkeep import policy
from sandkeep.diff import (
    DiffError,
    UnsafePatch,
    apply_to_fresh_branch,
    assert_safe_patch,
    extract_patch,  # noqa: F401  (kept importable for parity)
    files_in_patch,
    validate_patch,
)


@pytest.fixture
def host_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*a: str):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.name", "T")
    git("config", "user.email", "t@example.com")
    (repo / "keep.txt").write_text("hello\n")
    git("add", "-A")
    git("commit", "-q", "-m", "initial")
    return repo


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body)
    return p


# -- path parser (feeds policy + conflicts) ------------------------------

def test_files_in_patch_handles_quoted_paths():
    patch = (
        'diff --git "a/has space.py" "b/has space.py"\n'
        "--- \"a/has space.py\"\n"
        "+++ \"b/has space.py\"\n"
        "@@ -0,0 +1 @@\n+x\n"
    )
    assert files_in_patch(patch) == ["has space.py"]


def test_files_in_patch_ignores_dev_null():
    patch = (
        "diff --git a/new.py b/new.py\n"
        "new file mode 100644\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x\n"
    )
    assert files_in_patch(patch) == ["new.py"]


# -- unsafe-path rejection (unit) ----------------------------------------

@pytest.mark.parametrize(
    "target",
    [
        "../escape.txt",
        "a/../../escape.txt",
        "/etc/cron.d/evil",
        ".git/hooks/post-checkout",
        ".git/config",
        "sub/../../../../tmp/x",
    ],
)
def test_assert_safe_patch_rejects_escapes(tmp_path, target):
    patch = _write(
        tmp_path,
        "p.patch",
        f"diff --git a/{target} b/{target}\n--- /dev/null\n+++ b/{target}\n@@ -0,0 +1 @@\n+pwn\n",
    )
    with pytest.raises(UnsafePatch):
        assert_safe_patch(patch)


def test_assert_safe_patch_allows_ordinary_paths(tmp_path):
    patch = _write(
        tmp_path,
        "ok.patch",
        "diff --git a/src/mod.py b/src/mod.py\n--- a/src/mod.py\n+++ b/src/mod.py\n@@ -1 +1 @@\n-x\n+y\n",
    )
    assert_safe_patch(patch)  # no raise


# -- end-to-end against a real host repo ---------------------------------

def test_git_hooks_patch_never_applied(host_repo, tmp_path):
    """A patch creating .git/hooks/post-checkout must be refused before any
    host write — the file must not exist afterward."""
    patch = _write(
        tmp_path,
        "hook.patch",
        "diff --git a/.git/hooks/post-checkout b/.git/hooks/post-checkout\n"
        "new file mode 100755\n--- /dev/null\n+++ b/.git/hooks/post-checkout\n"
        "@@ -0,0 +1,2 @@\n+#!/bin/sh\n+touch /tmp/pwned\n",
    )
    with pytest.raises(UnsafePatch):
        validate_patch(host_repo, "HEAD", patch)
    with pytest.raises(UnsafePatch):
        apply_to_fresh_branch(host_repo, "HEAD", "sandkeep-accepted/x", patch, "m")
    assert not (host_repo / ".git" / "hooks" / "post-checkout").exists()


def test_traversal_patch_never_escapes_repo(host_repo, tmp_path):
    escape = tmp_path / "escape.txt"
    patch = _write(
        tmp_path,
        "esc.patch",
        "diff --git a/../escape.txt b/../escape.txt\n"
        "new file mode 100644\n--- /dev/null\n+++ b/../escape.txt\n@@ -0,0 +1 @@\n+pwn\n",
    )
    with pytest.raises(UnsafePatch):
        apply_to_fresh_branch(host_repo, "HEAD", "sandkeep-accepted/x", patch, "m")
    assert not escape.exists()


def test_oversize_patch_rejected_at_extraction(tmp_path):
    """extract_patch enforces max_bytes — a diff over the cap raises before it
    is ever written to the host outputs dir."""
    from dataclasses import dataclass

    from sandkeep import diff as diffmod
    from sandkeep.models import Task
    from sandkeep.sandbox.base import ExecResult, SandboxHandle

    big_diff = "diff --git a/x b/x\n" + "+y\n" * 5000

    @dataclass
    class FakeProvider:
        def exec(self, handle, cmd, timeout):
            if "add" in cmd:
                return ExecResult(exit_code=0, stdout="", stderr="")
            return ExecResult(exit_code=0, stdout=big_diff, stderr="")

    task = Task(id="t", repo_path="/r", instruction="x", base_ref="HEAD")
    handle = SandboxHandle(id="fake", workdir="/work/repo")
    with pytest.raises(diffmod.DiffError, match="exceeds"):
        diffmod.extract_patch(
            FakeProvider(), handle, task, tmp_path / "out", max_bytes=1024
        )
    assert not (tmp_path / "out" / "t.patch").exists()


def test_binary_hunk_is_flagged(host_repo):
    patch = (
        "diff --git a/logo.png b/logo.png\n"
        "new file mode 100644\nindex 0000000..1111111\n"
        "GIT binary patch\nliteral 8\nzcmZQ\n"
    )
    flags = policy.analyze_patch(patch)
    assert any(f.category == "binary" for f in flags)


def test_claude_config_change_is_flagged():
    patch = (
        "diff --git a/.claude/settings.json b/.claude/settings.json\n"
        "--- a/.claude/settings.json\n+++ b/.claude/settings.json\n"
        "@@ -1 +1 @@\n-{}\n+{\"hooks\": {}}\n"
    )
    flags = policy.analyze_patch(patch)
    assert any(f.category == "agent-config" for f in flags)


def test_contract_mismatch_flag():
    patch = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/secret.py b/secret.py\n--- a/secret.py\n+++ b/secret.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    # agent claimed only a.py; secret.py is under-reported
    flag = policy.cross_check_files(["a.py"], patch)
    assert flag is not None and flag.category == "contract-mismatch"
    assert "secret.py" in flag.detail
    # honest report → no flag
    assert policy.cross_check_files(["a.py", "secret.py"], patch) is None
