"""E2B provider — CI-safe unit tests (no E2B account needed).

These pin the wiring (config selection, optional-dependency handling, tar
packaging). The REAL acceptance — that the microVM actually contains a hostile
agent — is the boundary suite run against E2B, which needs a key:

    SANDKEEP_TEST_BACKEND=e2b E2B_API_KEY=... pytest tests/test_boundary.py

Until that is green, the E2B backend is candidate code, not verified (§16).
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from sandkeep.config import Config


# -- config / backend selection ------------------------------------------


def test_backend_defaults_to_docker(monkeypatch):
    monkeypatch.delenv("SANDKEEP_BACKEND", raising=False)
    assert Config.from_env().backend == "docker"


def test_backend_env_selects_e2b(monkeypatch):
    monkeypatch.setenv("SANDKEEP_BACKEND", "e2b")
    cfg = Config.from_env()
    assert cfg.backend == "e2b"
    assert cfg.e2b_template == "sandkeep"


def test_backend_rejects_unknown(monkeypatch):
    monkeypatch.setenv("SANDKEEP_BACKEND", "qemu")
    with pytest.raises(ValueError):
        Config.from_env()


def test_e2b_template_env_override(monkeypatch):
    monkeypatch.setenv("SANDKEEP_BACKEND", "e2b")
    monkeypatch.setenv("SANDKEEP_E2B_TEMPLATE", "my-template")
    assert Config.from_env().e2b_template == "my-template"


# -- optional-dependency handling ----------------------------------------


def test_provider_raises_clear_error_without_e2b_package(monkeypatch):
    """If the 'e2b' package isn't installed, instantiating fails with the
    install hint — not an opaque ImportError deep in a run."""
    import builtins

    from sandkeep.sandbox.base import SandboxError

    real_import = builtins.__import__

    def no_e2b(name, *args, **kwargs):
        if name == "e2b" or name.startswith("e2b."):
            raise ImportError("no e2b")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_e2b)
    from sandkeep.sandbox.e2b_provider import E2BProvider

    with pytest.raises(SandboxError) as exc:
        E2BProvider()
    assert "pip install" in str(exc.value)


# -- repo packaging ------------------------------------------------------


def test_tar_repo_includes_git_and_files(tmp_path):
    from sandkeep.sandbox.e2b_provider import _tar_repo

    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text("[core]\n")
    (repo / "app.py").write_text("print('hi')\n")

    data = _tar_repo(repo)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names = set(tar.getnames())
    assert "./app.py" in names
    assert "./.git/config" in names  # .git must travel so the in-VM clone works
