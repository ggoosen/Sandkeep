"""CI + release wiring (improvement plan, step 22).

The workflows can't be *run* here (that needs a real PR / tag push — infra-bound),
but these guard that they exist and carry the guarantees the plan requires: the
boundary suite runs enforced in CI, and release uses a Trusted Publisher (no
long-lived token). Lightweight content checks (no YAML dep).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
WF = ROOT / ".github" / "workflows"


def test_ci_workflow_enforces_boundary_suite():
    ci = (WF / "ci.yml").read_text()
    assert "--require-docker" in ci                 # boundary suite fails, not skips
    assert "sandkeep-broker" in ci and "sandkeep-browser" in ci  # sidecar images built


def test_release_workflow_uses_trusted_publisher():
    rel = (WF / "release.yml").read_text()
    assert "id-token: write" in rel                 # OIDC for Trusted Publisher
    assert "pypa/gh-action-pypi-publish" in rel
    assert "password:" not in rel                   # no long-lived token in the repo
    assert "tags:" in rel                            # tag-triggered release


def test_package_metadata_is_release_ready():
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'name = "sandkeep"' in pyproject
    assert "[project.scripts]" in pyproject and "sandkeep = " in pyproject
    assert "requires-python = \">=3.12\"" in pyproject
