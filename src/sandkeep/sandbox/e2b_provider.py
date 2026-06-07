"""E2B microVM SandboxProvider (BUILD_SPEC §16, Phase 2 — the real boundary).

This is the first non-Docker backend: each task runs in an E2B Firecracker
microVM (its own kernel, hardware-virtualized isolation), unlike the Docker
harness which shares the host kernel.

⚠️  VERIFICATION STATUS: this provider is written against the E2B Python SDK's
documented surface but has NOT yet been run through the boundary suite by the
author (no E2B account on the build machine). It is **not proven to contain
anything** until someone runs, with a real key:

    SANDKEEP_TEST_BACKEND=e2b E2B_API_KEY=... pytest tests/test_boundary.py

Green there = verified. Until then, treat this as candidate code. All E2B SDK
calls are isolated in this module, so if the installed SDK version differs the
fixes are local (verify names against your `e2b` version — the SDK has churned).

Design notes specific to E2B (vs Docker):
- **No host bind-mount.** E2B can't mount your filesystem, so `create()` uploads
  the repo into the VM at /src and makes it read-only. NOTE: this means the repo
  bytes leave your machine for E2B's cloud — inherent to any remote sandbox, and
  a real consideration for sensitive code. (Docker keeps the repo on-host; a
  cloud microVM cannot.)
- **Non-root template required.** The read-only-`/src` guarantee relies on the
  template running as a non-root user (like the Docker image's `node`). A
  root template would let writes to /src/.git succeed → boundary suite fails.
- **Stateless addressing.** Like the Docker provider, methods address a sandbox
  by id (reconnect), so a later `sandkeep accept` in a fresh process still works.
- **Network.** E2B sandboxes have internet by default; per-sandbox no-network
  isn't exposed by the basic SDK. So the network-exfil boundary test may report
  uncontained egress until the Phase 2 brokering proxy lands — that's honest, not
  hidden. Filesystem/host/escape boundaries are what the microVM enforces here.
- **Interactive `shell`** is not implemented (no host-TTY bridge yet); it raises
  the base "not supported" error. TODO(phase-2): wire E2B PTY to the host TTY.
"""

from __future__ import annotations

import io
import shlex
import tarfile
from dataclasses import dataclass
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

_INSTALL_HINT = "E2B backend needs the 'e2b' package: pip install 'sandkeep[e2b]'"


@dataclass
class E2BConfig:
    template: str = "sandkeep"  # E2B template name/id (built from sandbox_image/e2b.Dockerfile)
    api_key: str | None = None  # None → E2B SDK reads E2B_API_KEY from the env
    # how long an idle sandbox lives; must outlast provision→review→accept.
    timeout_seconds: int = 3600
    # advisory only — E2B's basic SDK doesn't expose per-sandbox no-network.
    # Kept for parity; real egress control is the Phase 2 brokering proxy.
    network: str = "egress"


def _import_e2b():
    try:
        import e2b  # noqa: F401
        from e2b import Sandbox
    except ImportError as exc:  # pragma: no cover - exercised via the hint test
        raise SandboxError(_INSTALL_HINT) from exc
    # CommandExitException carries exit_code/stdout/stderr for non-zero exits;
    # name/location has moved across SDK versions, so resolve defensively.
    exit_exc = _resolve_command_exit_exception(e2b)
    timeout_exc = _resolve_timeout_exception(e2b)
    return Sandbox, exit_exc, timeout_exc


def _resolve_command_exit_exception(e2b):
    for name in ("CommandExitException", "ProcessExitException"):
        exc = getattr(e2b, name, None)
        if exc is not None:
            return exc
    return type("Never", (Exception,), {})  # nothing to catch → run() never raises on exit


def _resolve_timeout_exception(e2b):
    for name in ("TimeoutException", "SandboxException"):
        exc = getattr(e2b, name, None)
        if exc is not None:
            return exc
    return TimeoutError


def _tar_repo(repo_path: Path) -> bytes:
    """Tar the whole repo (including .git, which the in-VM clone needs)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(str(repo_path), arcname=".")
    return buf.getvalue()


class E2BProvider(SandboxProvider):
    def __init__(self, config: E2BConfig | None = None) -> None:
        self.config = config or E2BConfig()
        self._Sandbox, self._ExitExc, self._TimeoutExc = _import_e2b()
        self._cache: dict[str, object] = {}  # id -> live Sandbox (per process)

    # -- SandboxProvider -------------------------------------------------

    def create(self, repo_path: str, env: dict[str, str]) -> SandboxHandle:
        repo = Path(repo_path).resolve()
        if not repo.is_dir():
            raise SandboxError(f"repo path does not exist: {repo}")
        try:
            sbx = self._Sandbox(
                template=self.config.template,
                api_key=self.config.api_key,
                timeout=self.config.timeout_seconds,
                envs=dict(env),
            )
        except Exception as exc:  # SDK/network/auth failures surface as SandboxError
            raise SandboxError(f"E2B sandbox create failed: {exc}") from exc

        sbx_id = getattr(sbx, "sandbox_id", None) or getattr(sbx, "id")
        self._cache[sbx_id] = sbx

        # Upload the repo and make /src read-only (no host mount on E2B).
        try:
            sbx.files.write("/tmp/_sk_repo.tar", _tar_repo(repo))
        except Exception as exc:
            raise SandboxError(f"E2B repo upload failed: {exc}") from exc
        setup = self.exec(
            SandboxHandle(id=sbx_id, workdir=WORKDIR),
            ["sh", "-c",
             f"mkdir -p {SRC_MOUNT} && tar -xf /tmp/_sk_repo.tar -C {SRC_MOUNT}"
             f" && rm -f /tmp/_sk_repo.tar && chmod -R a-w {SRC_MOUNT}"],
            timeout=120,
        )
        if setup.exit_code != 0:
            self.destroy(SandboxHandle(id=sbx_id, workdir=WORKDIR))
            raise SandboxError(f"E2B /src setup failed: {setup.stderr.strip()}")
        return SandboxHandle(id=sbx_id, workdir=WORKDIR)

    def exec(self, handle: SandboxHandle, cmd: list[str], timeout: int) -> ExecResult:
        sbx = self._connect(handle.id)
        script = shlex.join(cmd)
        try:
            r = sbx.commands.run(script, timeout=timeout)
            return ExecResult(
                exit_code=getattr(r, "exit_code", 0),
                stdout=getattr(r, "stdout", "") or "",
                stderr=getattr(r, "stderr", "") or "",
            )
        except self._ExitExc as e:  # non-zero exit is normal control flow here
            return ExecResult(
                exit_code=getattr(e, "exit_code", 1),
                stdout=getattr(e, "stdout", "") or "",
                stderr=(getattr(e, "stderr", "") or "") or str(e),
            )
        except self._TimeoutExc as e:
            raise SandboxExecTimeout(f"timed out after {timeout}s: {cmd!r}") from e

    def read_file(self, handle: SandboxHandle, path: str) -> str:
        sbx = self._connect(handle.id)
        try:
            return sbx.files.read(path)
        except Exception as exc:
            raise FileNotFoundError(f"{path} in sandbox {handle.id}: {exc}") from exc

    def destroy(self, handle: SandboxHandle) -> None:
        try:
            sbx = self._connect(handle.id)
            sbx.kill()
        except SandboxError:
            return  # already gone / unreachable is fine for teardown
        except Exception as exc:
            raise SandboxError(f"E2B kill failed for {handle.id}: {exc}") from exc
        finally:
            self._cache.pop(handle.id, None)

    # -- internals -------------------------------------------------------

    def _connect(self, sandbox_id: str):
        """Return a live Sandbox for an id — cached in-process, else reconnect
        (so a fresh `sandkeep accept` process can still reach the sandbox)."""
        if sandbox_id in self._cache:
            return self._cache[sandbox_id]
        try:
            sbx = self._Sandbox.connect(sandbox_id, api_key=self.config.api_key)
        except Exception as exc:
            raise SandboxError(f"E2B connect failed for {sandbox_id}: {exc}") from exc
        self._cache[sandbox_id] = sbx
        return sbx
