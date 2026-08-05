"""--require-docker acceptance (improvement plan, step 4).

CLAUDE.md says the boundary suite "must pass before anything else", but the
docker-backed tests silently skip when the daemon is absent — so a green run
could mean zero security tests executed. --require-docker turns that skip
into a hard failure; CI always passes it. These meta-tests drive a child
pytest over one boundary test with docker deterministically "unavailable"
(SANDKEEP_TEST_FORCE_NO_DOCKER) so they behave the same with or without a
real daemon on the machine.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
ONE_BOUNDARY_TEST = "tests/test_boundary.py::test_host_secrets_unreachable"


def _run_child_pytest(*extra_args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, SANDKEEP_TEST_FORCE_NO_DOCKER="1")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", ONE_BOUNDARY_TEST, *extra_args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_require_docker_fails_loud_when_daemon_unavailable() -> None:
    proc = _run_child_pytest("--require-docker")
    assert proc.returncode != 0
    out = proc.stdout + proc.stderr
    assert "docker daemon not available" in out
    assert "--require-docker" in out


def test_without_flag_docker_tests_skip_and_summary_warns() -> None:
    proc = _run_child_pytest()
    assert proc.returncode == 0  # skip, not fail — local ergonomics unchanged
    out = proc.stdout + proc.stderr
    assert "SKIPPED" in out and "NOT verified" in out
