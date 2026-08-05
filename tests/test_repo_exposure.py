"""Repo read-exposure controls (improvement plan, step 15).

Read-only ≠ unreadable: the agent can read every tracked file in /src. These
test the pre-provision secret scan and the shallow-clone default (host-side).
"""

from __future__ import annotations

from pathlib import Path

from sandkeep.config import Config
from sandkeep.provisioner import build_clone_step, scan_repo_secrets


# -- secret scan ---------------------------------------------------------

def test_scan_finds_committed_secret(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "config.py").write_text('AWS_KEY = "AKIAIOSFODNN7EXAMPLE0"\n')
    (repo / "README.md").write_text("# clean\n")
    findings = scan_repo_secrets(repo)
    assert any("config.py" in f for f in findings)
    assert any("AWS" in f for f in findings)


def test_scan_clean_repo_finds_nothing(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('hello')\n")
    assert scan_repo_secrets(repo) == []


def test_scan_skips_git_dir(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "cfg").write_text('token = "ghp_' + "a" * 30 + '"\n')
    assert scan_repo_secrets(repo) == []  # .git contents aren't part of the tree


def test_scan_skips_binary(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "blob.bin").write_bytes(b"\x00\x01AKIAIOSFODNN7EXAMPLE0\x00")
    assert scan_repo_secrets(repo) == []


def test_scan_is_capped(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(10):
        (repo / f"f{i}.env").write_text('password = "supersecret123"\n')
    assert len(scan_repo_secrets(repo, max_findings=3)) == 3


# -- shallow clone -------------------------------------------------------

def test_shallow_clone_uses_depth_and_file_url():
    step = build_clone_step("/src", "/work/repo", shallow=True)
    assert "--depth=1" in step
    assert any(a.startswith("file://") for a in step)  # local shallow needs file://


def test_full_history_clone_is_plain():
    step = build_clone_step("/src", "/work/repo", shallow=False)
    assert "--depth=1" not in step
    assert "/src" in step


# -- config --------------------------------------------------------------

def test_shallow_is_default_and_scan_on():
    cfg = Config()
    assert cfg.full_history is False
    assert cfg.scan_repo_secrets is True


def test_full_history_env(monkeypatch):
    monkeypatch.setenv("SANDKEEP_FULL_HISTORY", "on")
    assert Config.from_env().full_history is True


def test_scan_secrets_can_be_disabled(monkeypatch):
    monkeypatch.setenv("SANDKEEP_SCAN_SECRETS", "off")
    assert Config.from_env().scan_repo_secrets is False
