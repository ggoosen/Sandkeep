"""Unit tests for DockerProvider command construction (no real docker).

The adversarial proof that the *running* container honours these flags is
tests/test_boundary.py; this suite pins the invariants at the call site:
read-only /src mount, --network none, caps, and never the docker socket.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import pytest

from sandkeep.sandbox import SandboxExecTimeout, SandboxHandle
from sandkeep.sandbox.docker_provider import DockerConfig, DockerProvider


@dataclass
class FakeRunner:
    returncode: int = 0
    stdout: bytes = b"container-id\n"
    stderr: bytes = b""
    raise_timeout: bool = False
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, cmd: list[str], timeout: int | None = None):
        self.calls.append(cmd)
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(cmd, timeout or 0)
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def provider(runner: FakeRunner) -> DockerProvider:
    return DockerProvider(DockerConfig(), runner=runner)


def test_create_mounts_repo_read_only(provider, runner, tmp_path):
    provider.create(str(tmp_path), env={})
    cmd = runner.calls[0]
    mount = cmd[cmd.index("--volume") + 1]
    assert mount == f"{tmp_path.resolve()}:/src:ro"
    assert ":rw" not in " ".join(cmd)


def test_create_denies_network_by_default(provider, runner, tmp_path):
    provider.create(str(tmp_path), env={})
    cmd = runner.calls[0]
    assert cmd[cmd.index("--network") + 1] == "none"


def test_create_applies_resource_caps_and_hardening(provider, runner, tmp_path):
    provider.create(str(tmp_path), env={})
    joined = " ".join(runner.calls[0])
    assert "--memory" in joined
    assert "--cpus" in joined
    assert "--pids-limit" in joined
    assert "--cap-drop ALL" in joined
    assert "no-new-privileges" in joined


def test_create_never_mounts_docker_socket(provider, runner, tmp_path):
    provider.create(str(tmp_path), env={"ANTHROPIC_API_KEY": "sk-test"})
    assert "docker.sock" not in " ".join(runner.calls[0])


def test_create_passes_env(provider, runner, tmp_path):
    provider.create(str(tmp_path), env={"ANTHROPIC_API_KEY": "sk-test"})
    cmd = runner.calls[0]
    assert "ANTHROPIC_API_KEY=sk-test" in cmd[cmd.index("--env") + 1]


def test_create_rejects_missing_repo(provider, tmp_path):
    from sandkeep.sandbox import SandboxError

    with pytest.raises(SandboxError):
        provider.create(str(tmp_path / "nope"), env={})


def test_exec_timeout_raises(runner, tmp_path):
    runner.raise_timeout = True
    provider = DockerProvider(DockerConfig(), runner=runner)
    handle = SandboxHandle(id="sandkeep-test", workdir="/work/repo")
    with pytest.raises(SandboxExecTimeout):
        provider.exec(handle, ["sleep", "999"], timeout=1)


def test_read_file_missing_raises(provider, runner):
    runner.returncode = 1
    runner.stderr = b"cat: /nope: No such file or directory"
    handle = SandboxHandle(id="sandkeep-test", workdir="/work/repo")
    with pytest.raises(FileNotFoundError):
        provider.read_file(handle, "/nope")


def test_destroy_removes_container_and_volumes(provider, runner):
    handle = SandboxHandle(id="sandkeep-test", workdir="/work/repo")
    provider.destroy(handle)
    cmd = runner.calls[0]
    assert cmd[:2] == ["docker", "rm"]
    assert "--force" in cmd and "--volumes" in cmd
