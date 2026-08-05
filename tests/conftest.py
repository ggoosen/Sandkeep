from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from sandkeep.audit import AuditLog
from sandkeep.models import Task
from sandkeep.sandbox.docker_provider import DockerConfig, DockerProvider
from sandkeep.state_store import StateStore

IMAGE = "sandkeep-sandbox:latest"

REQUIRE_DOCKER_HELP = (
    "fail (instead of skip) docker-backed tests when the docker daemon is "
    "unavailable — CI passes this so a green run can never silently omit "
    "the boundary suite"
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--require-docker", action="store_true", help=REQUIRE_DOCKER_HELP)


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """One honest line at the end of every session: did the docker-backed
    tests (including the boundary suite) actually run, or were they skipped?"""
    skipped = terminalreporter.stats.get("skipped", [])
    docker_skips = sum(
        1 for r in skipped if "docker daemon not available" in str(getattr(r, "longrepr", ""))
    )
    if docker_skips:
        terminalreporter.write_line(
            f"sandkeep: {docker_skips} docker-backed tests SKIPPED — "
            "containment was NOT verified this run (pass --require-docker to enforce)",
            yellow=True,
        )
    elif terminalreporter.stats.get("passed"):
        terminalreporter.write_line(
            "sandkeep: docker-backed tests ran (or were deselected); "
            "no docker-availability skips this session",
            green=True,
        )


@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def store(tmp_path: Path, audit: AuditLog) -> StateStore:
    s = StateStore(tmp_path / "state.sqlite3", audit=audit)
    yield s
    s.close()


@pytest.fixture
def task() -> Task:
    return Task(
        id=uuid.uuid4().hex,
        repo_path="/tmp/example-repo",
        instruction="example instruction",
    )


# -- docker-backed integration fixtures ----------------------------------


def _docker_ready() -> bool:
    if os.environ.get("SANDKEEP_TEST_FORCE_NO_DOCKER"):
        # deterministic "no docker" for the --require-docker meta-test and
        # for CI jobs that want the pure host-side suite
        return False
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(
            ["docker", "info"], capture_output=True, timeout=30
        ).returncode
        == 0
    )


@pytest.fixture(scope="session")
def sandbox_image(request: pytest.FixtureRequest) -> str:
    """Skip docker-backed tests when docker is unavailable — or FAIL if the
    session was started with --require-docker; build the sandbox image on
    first use if it is missing."""
    required = request.config.getoption("--require-docker")
    if not _docker_ready():
        msg = "docker daemon not available"
        if required:
            pytest.fail(f"{msg} but --require-docker was given: the boundary "
                        "suite cannot be verified", pytrace=False)
        pytest.skip(msg)
    inspect = subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True
    )
    if inspect.returncode != 0:
        build = subprocess.run(
            ["docker", "build", "-t", IMAGE, str(Path(__file__).parents[1] / "sandbox_image")],
            capture_output=True,
        )
        if build.returncode != 0:
            detail = f"could not build {IMAGE}: {build.stderr.decode()[-500:]}"
            if required:
                pytest.fail(detail, pytrace=False)
            pytest.skip(detail)
    return IMAGE


@pytest.fixture
def provider(request):
    """The sandbox backend under test. Defaults to Docker; set
    SANDKEEP_TEST_BACKEND=e2b (with E2B_API_KEY) to run the SAME suite —
    notably tests/test_boundary.py — against the E2B microVM provider. That is
    how a new backend earns its "verified" badge (BUILD_SPEC §16)."""
    if os.environ.get("SANDKEEP_TEST_BACKEND") == "e2b":
        if not os.environ.get("E2B_API_KEY"):
            pytest.skip("SANDKEEP_TEST_BACKEND=e2b but E2B_API_KEY is not set")
        from sandkeep.sandbox.e2b_provider import E2BConfig, E2BProvider

        template = os.environ.get("SANDKEEP_E2B_TEMPLATE", "sandkeep")
        return E2BProvider(E2BConfig(template=template, network="none"))
    # default: Docker — only now do we require the docker image fixture
    image = request.getfixturevalue("sandbox_image")
    return DockerProvider(DockerConfig(image=image, network="none"))


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


@pytest.fixture
def host_repo(tmp_path: Path) -> Path:
    """A small real git repo standing in for the user's repo."""
    repo = tmp_path / "host-repo"
    repo.mkdir()
    subprocess.run(
        ["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True
    )
    _git(repo, "config", "user.name", "Host User")
    _git(repo, "config", "user.email", "host@example.com")
    (repo / "parse_config.py").write_text(
        "def parse_config(raw):\n    return dict(line.split('=', 1) for line in raw.splitlines())\n"
    )
    (repo / "README.md").write_text("# demo\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


@pytest.fixture
def sandbox(provider: DockerProvider, store, audit, host_repo: Path):
    """A provisioned sandbox for a real task; always destroyed afterwards
    (even if the test body raises). provision() now cleans up its own
    container on a provisioning failure, so a failed provision can't leak."""
    from sandkeep.audit import new_trace_id
    from sandkeep.provisioner import provision

    t = Task(id=uuid.uuid4().hex, repo_path=str(host_repo), instruction="test task")
    store.create_task(t, new_trace_id())
    handle = provision(t, provider, store, audit, env={}, trace_id=new_trace_id())
    try:
        yield t, handle
    finally:
        try:
            provider.destroy(handle)
        except Exception:
            pass


@pytest.fixture(scope="session", autouse=True)
def _reap_session_sandboxes():
    """Safety net: remove any sandkeep-* containers CREATED DURING this test
    session (by diffing the set before/after), so a leaked test never litters
    Docker — without touching the user's own live sandboxes."""
    def _ids() -> set[str]:
        if shutil.which("docker") is None:
            return set()
        out = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=sandkeep-", "--format", "{{.Names}}"],
            capture_output=True, text=True,
        )
        return {n for n in out.stdout.split() if n.strip()}

    before = _ids()
    yield
    leaked = _ids() - before
    if leaked:
        subprocess.run(["docker", "rm", "-f", "-v", *leaked], capture_output=True)
