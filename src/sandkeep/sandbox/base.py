"""SandboxProvider ABC (BUILD_SPEC §4).

The controller talks ONLY to this interface. The Docker backend is a
mechanics harness, not a security boundary; Phase 2's microVM backend
implements the same ABC and must pass tests/test_boundary.py unmodified.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


# Part of the provider contract: every backend mounts the host repo
# read-only here and gives the clone its own writable workdir.
SRC_MOUNT = "/src"
WORKDIR = "/work/repo"


class SandboxError(Exception):
    """Provider-level failure (create/exec/read/destroy)."""


class SandboxExecTimeout(SandboxError):
    """A command inside the sandbox exceeded its timeout."""


@dataclass
class SandboxHandle:
    id: str
    workdir: str  # path INSIDE the sandbox, e.g. /work/repo


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


class SandboxProvider(ABC):
    @abstractmethod
    def create(self, repo_path: str, env: dict[str, str]) -> SandboxHandle:
        """Start a sandbox. Mount `repo_path` READ-ONLY at /src.
        Network: deny-by-default except an allowlist (BUILD_SPEC §5)."""

    @abstractmethod
    def exec(self, handle: SandboxHandle, cmd: list[str], timeout: int) -> ExecResult:
        """Run a command inside the sandbox."""

    def exec_interactive(self, handle: SandboxHandle, cmd: list[str]) -> int:
        """Run a command inside the sandbox attached to the host's TTY,
        returning its exit code (BUILD_SPEC §10b, `sandkeep shell`).

        Optional capability: backends that cannot offer an interactive TTY
        may leave this unimplemented. The core boundary contract is
        create/exec/read_file/destroy; this does not extend it, so existing
        backends and tests/test_boundary.py are unaffected."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support interactive sessions"
        )

    def exec_stream(self, handle: SandboxHandle, cmd: list[str], timeout: int):
        """Run a command, yielding output line-by-line as it arrives, then
        returning the final ExecResult (improvement plan, step 25).

        Optional capability for live progress + incremental scanning. Backends
        that only capture one-shot leave this unimplemented; callers fall back
        to exec(). The real consumer — ClaudeDriver parsing
        `--output-format stream-json` — is deferred until the event shape can be
        verified against the installed CLI (§6 discipline); this is the seam it
        will use, kept honest as a hook, not a stubbed guarantee."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support streaming exec"
        )

    @abstractmethod
    def read_file(self, handle: SandboxHandle, path: str) -> str:
        """Read a file from inside the sandbox (used to pull the contract + patch)."""

    @abstractmethod
    def destroy(self, handle: SandboxHandle) -> None:
        """Tear down and discard all sandbox state."""

    def list_sandbox_ids(self) -> list[str]:
        """All sandbox ids this backend currently holds (for `sandkeep ps`/`gc`).

        Optional capability — backends that can't enumerate may leave it
        unimplemented; gc/ps then report that listing isn't supported."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support listing sandboxes"
        )
