"""Docker SandboxProvider (BUILD_SPEC §4) — Phase 0–1 backend.

This is the ONLY module allowed to shell out to `docker` (CLAUDE.md
conventions). It is a mechanics harness, not a security boundary;
TODO(phase-2): microVM provider behind the same ABC.

Hard requirements honoured here:
- host repo mounted READ-ONLY at /src (never the host .git writable)
- non-root user inside the container
- network deny-by-default (`--network none`); Phase 1 egress is gated
  behind config and documented as NOT a proper allowlist —
  TODO(phase-2): brokering egress proxy
- resource caps (--memory/--cpus/--pids-limit)
- the docker socket is never mounted, ever
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .base import (
    SRC_MOUNT,
    WORKDIR,
    ExecResult,
    SandboxError,
    SandboxExecTimeout,
    SandboxHandle,
    SandboxProvider,
)


@dataclass
class DockerConfig:
    image: str = "sandkeep-sandbox:latest"
    # "none" = no network at all (Phase 0 / boundary suite).
    # "egress" = Docker's default bridge so the agent can reach
    # api.anthropic.com. This is NOT a real allowlist; full egress control
    # is gated here on purpose. TODO(phase-2): secret-injecting brokering proxy.
    network: str = "none"
    memory: str = "2g"
    cpus: str = "2"
    pids_limit: int = 256
    extra_run_args: list[str] = field(default_factory=list)


def _run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


class DockerProvider(SandboxProvider):
    def __init__(self, config: DockerConfig | None = None, runner=_run) -> None:
        self.config = config or DockerConfig()
        self._run = runner  # injectable for unit tests

    # -- SandboxProvider -------------------------------------------------

    def create(self, repo_path: str, env: dict[str, str]) -> SandboxHandle:
        repo = Path(repo_path).resolve()
        if not repo.is_dir():
            raise SandboxError(f"repo path does not exist: {repo}")
        name = f"sandkeep-{uuid.uuid4().hex[:12]}"
        cmd = [
            "docker", "run", "--detach",
            "--name", name,
            "--network", "none" if self.config.network == "none" else "bridge",
            "--memory", self.config.memory,
            "--cpus", self.config.cpus,
            "--pids-limit", str(self.config.pids_limit),
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            # the one mount: host repo, read-only. Never the docker socket,
            # never a writable path into the host.
            "--volume", f"{repo}:{SRC_MOUNT}:ro",
            *self.config.extra_run_args,
        ]
        for key, value in env.items():
            cmd += ["--env", f"{key}={value}"]
        cmd += [self.config.image, "sleep", "infinity"]
        proc = self._run(cmd, timeout=60)
        if proc.returncode != 0:
            raise SandboxError(f"docker run failed: {proc.stderr.decode(errors='replace')}")
        return SandboxHandle(id=name, workdir=WORKDIR)

    def exec(self, handle: SandboxHandle, cmd: list[str], timeout: int) -> ExecResult:
        full = ["docker", "exec", handle.id, *cmd]
        try:
            proc = self._run(full, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise SandboxExecTimeout(f"timed out after {timeout}s: {cmd!r}") from exc
        return ExecResult(
            exit_code=proc.returncode,
            stdout=proc.stdout.decode(errors="replace"),
            stderr=proc.stderr.decode(errors="replace"),
        )

    def exec_interactive(self, handle: SandboxHandle, cmd: list[str]) -> int:
        """`docker exec -it` inheriting the host TTY (BUILD_SPEC §10b).

        Deliberately does NOT go through self._run / capture_output: an
        interactive session must share the real terminal. Returns the
        command's exit code."""
        full = ["docker", "exec", "--interactive", "--tty", handle.id, *cmd]
        # stdin/stdout/stderr inherited from the parent process → live TTY
        return subprocess.run(full).returncode

    def read_file(self, handle: SandboxHandle, path: str) -> str:
        result = self.exec(handle, ["cat", path], timeout=30)
        if result.exit_code != 0:
            raise FileNotFoundError(f"{path} in sandbox {handle.id}: {result.stderr.strip()}")
        return result.stdout

    def destroy(self, handle: SandboxHandle) -> None:
        proc = self._run(["docker", "rm", "--force", "--volumes", handle.id], timeout=60)
        if proc.returncode != 0:
            raise SandboxError(
                f"docker rm failed for {handle.id}: {proc.stderr.decode(errors='replace')}"
            )

    # -- extras (still docker-only, still inside this module) -------------

    def stop(self, handle: SandboxHandle) -> None:
        """Stop without removing — used when archiving a VIOLATION sandbox
        for forensics (BUILD_SPEC §8)."""
        proc = self._run(["docker", "stop", handle.id], timeout=60)
        if proc.returncode != 0:
            raise SandboxError(
                f"docker stop failed for {handle.id}: {proc.stderr.decode(errors='replace')}"
            )


def build_image(context_dir: Path, tag: str) -> None:
    """`sandkeep image build` lands here so docker stays in this module."""
    proc = subprocess.run(
        ["docker", "build", "--tag", tag, str(context_dir)],
        capture_output=False,  # stream build output to the terminal
    )
    if proc.returncode != 0:
        raise SandboxError(f"docker build failed (exit {proc.returncode})")
