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
    envs: list[dict[str, str] | None] = field(default_factory=list)

    def __call__(self, cmd: list[str], timeout: int | None = None, env=None):
        self.calls.append(cmd)
        self.envs.append(env)
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


def test_create_passes_env_without_secret_on_argv(provider, runner, tmp_path):
    """Secrets travel as `--env KEY` with the value in the docker client's
    process env — never in the argv (host ps / /proc/*/cmdline)."""
    provider.create(str(tmp_path), env={"ANTHROPIC_API_KEY": "sk-test-secret"})
    cmd = runner.calls[0]
    assert cmd[cmd.index("--env") + 1] == "ANTHROPIC_API_KEY"
    assert "sk-test-secret" not in " ".join(cmd)
    assert runner.envs[0] is not None
    assert runner.envs[0]["ANTHROPIC_API_KEY"] == "sk-test-secret"


def test_create_without_env_inherits_parent_environment(provider, runner, tmp_path):
    """No secrets to forward → env=None so the docker client keeps its normal
    environment (PATH, DOCKER_HOST, …)."""
    provider.create(str(tmp_path), env={})
    assert runner.envs[0] is None
    assert "--env" not in runner.calls[0]


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


def test_exec_interactive_uses_tty_and_inherits_stdio(provider, monkeypatch):
    """Interactive exec must request a TTY and NOT capture output (it shares
    the host terminal) — it bypasses the injected runner on purpose."""
    import subprocess as sp

    captured = {}

    def fake_run(cmd):  # note: no capture_output kwarg → inherits stdio
        captured["cmd"] = cmd
        return sp.CompletedProcess(cmd, 0)

    monkeypatch.setattr(sp, "run", fake_run)
    handle = SandboxHandle(id="sandkeep-test", workdir="/work/repo")
    code = provider.exec_interactive(handle, ["sh", "-lc", "claude"])
    assert code == 0
    assert captured["cmd"][:5] == ["docker", "exec", "--interactive", "--tty", "sandkeep-test"]


def test_exec_interactive_is_optional_on_base():
    """A backend that doesn't override it reports the capability as absent
    without affecting the core boundary contract."""
    from sandkeep.sandbox.base import SandboxProvider

    class Minimal(SandboxProvider):
        def create(self, repo_path, env):
            ...

        def exec(self, handle, cmd, timeout):
            ...

        def read_file(self, handle, path):
            ...

        def destroy(self, handle):
            ...

    with pytest.raises(NotImplementedError):
        Minimal().exec_interactive(SandboxHandle(id="x", workdir="/w"), ["claude"])
