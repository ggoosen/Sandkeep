"""Docker backend hardening (improvement plan, step 12).

extra_run_args is an operator escape hatch; these tests pin that
boundary-breaching flags are refused before any docker call, and that the
optional seccomp / read-only-rootfs hardening reaches the run command.
Host-side only (fake runner) — no daemon needed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import pytest

from sandkeep.sandbox import SandboxError
from sandkeep.sandbox.docker_provider import (
    DockerConfig,
    DockerProvider,
    validate_extra_run_args,
)


@dataclass
class FakeRunner:
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, cmd, timeout=None, env=None):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"id\n", b"")


@pytest.mark.parametrize(
    "bad",
    [
        ["--privileged"],
        ["-v", "/etc:/etc"],
        ["--volume=/root:/root"],
        ["--mount", "type=bind,src=/,dst=/host"],
        ["--cap-add", "SYS_ADMIN"],
        ["--device", "/dev/sda"],
        ["--security-opt", "seccomp=unconfined"],
        ["--userns=host"],
        ["--pid=host"],
        ["--network=host"],
    ],
)
def test_forbidden_extra_run_args_rejected(bad):
    with pytest.raises(SandboxError, match="forbidden"):
        validate_extra_run_args(bad)


def test_innocuous_extra_run_args_allowed():
    validate_extra_run_args(["--label", "team=sandkeep", "--hostname", "box"])  # no raise


def test_create_refuses_forbidden_args_before_docker_call(tmp_path):
    runner = FakeRunner()
    provider = DockerProvider(
        DockerConfig(extra_run_args=["-v", "/etc:/etc:rw"]), runner=runner
    )
    with pytest.raises(SandboxError, match="forbidden"):
        provider.create(str(tmp_path), env={})
    assert runner.calls == []  # nothing ran — fail loud, pre-docker


def test_seccomp_profile_reaches_run_command(tmp_path):
    runner = FakeRunner()
    provider = DockerProvider(
        DockerConfig(seccomp_profile="/opt/sk/seccomp.json"), runner=runner
    )
    provider.create(str(tmp_path), env={})
    joined = " ".join(runner.calls[0])
    assert "seccomp=/opt/sk/seccomp.json" in joined


def test_read_only_rootfs_adds_tmpfs_workspace(tmp_path):
    runner = FakeRunner()
    provider = DockerProvider(DockerConfig(read_only_rootfs=True), runner=runner)
    provider.create(str(tmp_path), env={})
    cmd = runner.calls[0]
    assert "--read-only" in cmd
    tmpfs = [cmd[i + 1] for i, t in enumerate(cmd) if t == "--tmpfs"]
    assert any(t.startswith("/work") for t in tmpfs)
    assert any(t.startswith("/home/node") for t in tmpfs)


def test_hardening_off_by_default_keeps_prior_flags(tmp_path):
    runner = FakeRunner()
    provider = DockerProvider(DockerConfig(), runner=runner)
    provider.create(str(tmp_path), env={})
    cmd = runner.calls[0]
    assert "--read-only" not in cmd            # opt-in only
    assert "seccomp=" not in " ".join(cmd)     # Docker default profile in force
    assert "--cap-drop" in cmd and "no-new-privileges" in " ".join(cmd)
