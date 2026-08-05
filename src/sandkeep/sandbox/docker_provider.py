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

import os
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
    # "none"   = no network at all (Phase 0 / boundary suite).
    # "egress" = Docker's default bridge; the agent reaches anything and holds
    #            the key. NOT an allowlist.
    # "proxy"  = the sandbox runs on an --internal (no-egress) network behind
    #            the key-broker sidecar: it never holds the API key and can only
    #            reach the allowlist (improvement plan, step 1).
    network: str = "none"
    memory: str = "2g"
    cpus: str = "2"
    pids_limit: int = 256
    extra_run_args: list[str] = field(default_factory=list)
    # proxy-mode only: the broker image, the host allowlist, and the API key
    # the BROKER (never the sandbox) holds.
    broker_image: str = "sandkeep-broker:latest"
    egress_allowlist: str = "api.anthropic.com,pypi.org,files.pythonhosted.org,registry.npmjs.org"
    broker_api_key: str = ""
    # generalized broker routing (improvement plan, step 14): a JSON route list
    # (SANDKEEP_ROUTES) + the per-route secrets the broker holds, so any driver
    # runs key-broker-protected. Empty → the back-compat Anthropic-only path.
    broker_routes: str = ""
    broker_secrets: dict = field(default_factory=dict)
    # browser bridge (improvement plan, step 11): a headless-Chromium sidecar on
    # the task network exposing a CDP endpoint at http://browser:9222.
    browser: bool = False
    browser_image: str = "sandkeep-browser:latest"
    # Hardening (improvement plan, step 12). seccomp_profile: path to a custom
    # seccomp json passed as `--security-opt seccomp=<path>` ("" leaves Docker's
    # built-in default profile in force). read_only_rootfs: run with a read-only
    # root filesystem + tmpfs for the writable dirs, so the only mutable state is
    # the disposable workspace. Off by default (verify against your image first).
    seccomp_profile: str = ""
    read_only_rootfs: bool = False


# extra_run_args is an operator-supplied escape hatch. These flags would let it
# re-introduce a writable path into the host, escalate privileges, or override
# the network/security posture the provider sets — defeating the "one mount,
# read-only, ever" invariant. Reject them loud rather than splice them in
# (improvement plan, step 12).
_FORBIDDEN_RUN_ARG_PREFIXES = (
    "--privileged",
    "-v", "--volume", "--mount",              # writable mounts into the host
    "--cap-add",                               # re-adds a dropped capability
    "--device",                                # host device access
    "--security-opt",                          # e.g. seccomp=unconfined
    "--userns",                                # e.g. --userns=host
    "--pid", "--ipc", "--uts", "--cgroupns",   # host namespace sharing
    "--network", "--net",                      # override the provider's netns
)


def validate_extra_run_args(args: list[str]) -> None:
    """Reject operator-supplied docker run args that would breach the boundary
    (writable host mounts, privilege escalation, namespace/network overrides).
    Raises SandboxError naming the offending flag."""
    for tok in args:
        head = tok.split("=", 1)[0]
        if head in _FORBIDDEN_RUN_ARG_PREFIXES:
            raise SandboxError(
                f"extra_run_args contains a forbidden flag {tok!r}: it could "
                "breach the sandbox boundary (writable mount / privilege / "
                "namespace override) and is refused"
            )


# Names derived from the sandbox container name so destroy() can find the
# sidecars + network without extra bookkeeping. Kept clear of the "sandkeep-"
# filter used by list_sandbox_ids so a sidecar is never mistaken for a sandbox.
def _broker_name(sandbox_name: str) -> str:
    return "skbroker-" + sandbox_name.removeprefix("sandkeep-")


def _browser_name(sandbox_name: str) -> str:
    return "skbrowser-" + sandbox_name.removeprefix("sandkeep-")


def _network_name(sandbox_name: str) -> str:
    return "sknet-" + sandbox_name.removeprefix("sandkeep-")


# The sandbox reaches its sidecars by network alias on the task network.
BROKER_ALIAS_URL = "http://broker:8080"
BROWSER_CDP_URL = "http://browser:9222"


def _run(
    cmd: list[str],
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)


class DockerProvider(SandboxProvider):
    def __init__(self, config: DockerConfig | None = None, runner=_run) -> None:
        self.config = config or DockerConfig()
        self._run = runner  # injectable for unit tests

    # -- SandboxProvider -------------------------------------------------

    def create(self, repo_path: str, env: dict[str, str]) -> SandboxHandle:
        repo = Path(repo_path).resolve()
        if not repo.is_dir():
            raise SandboxError(f"repo path does not exist: {repo}")
        # Fail loud on a boundary-breaching escape hatch BEFORE any docker call.
        validate_extra_run_args(self.config.extra_run_args)
        name = f"sandkeep-{uuid.uuid4().hex[:12]}"

        # A dedicated per-task network is needed when we run sidecars (the
        # broker in proxy mode, and/or the browser bridge): the default bridge
        # has no DNS aliases, so containers couldn't find `broker`/`browser`.
        needs_network = self.config.network == "proxy" or self.config.browser
        if needs_network:
            network_arg = self._provision_sidecars(name)
        else:
            network_arg = "none" if self.config.network == "none" else "bridge"

        cmd = [
            "docker", "run", "--detach",
            "--name", name,
            "--network", network_arg,
            "--memory", self.config.memory,
            "--cpus", self.config.cpus,
            "--pids-limit", str(self.config.pids_limit),
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            # the one mount: host repo, read-only. Never the docker socket,
            # never a writable path into the host.
            "--volume", f"{repo}:{SRC_MOUNT}:ro",
            *self._hardening_args(),
            *self.config.extra_run_args,
        ]
        # Secrets stay OFF the argv: `--env KEY` (no value) makes the docker
        # client read the value from its own process environment, so it never
        # appears in host `ps`/`/proc/*/cmdline`. In proxy mode `env` carries NO
        # secret at all — only ANTHROPIC_BASE_URL/HTTPS_PROXY pointing at the
        # broker, which holds the key.
        for key in env:
            cmd += ["--env", key]
        cmd += [self.config.image, "sleep", "infinity"]
        run_env = {**os.environ, **env} if env else None
        proc = self._run(cmd, timeout=60, env=run_env)
        if proc.returncode != 0:
            if needs_network:
                self._teardown_sidecars(name)
            raise SandboxError(f"docker run failed: {proc.stderr.decode(errors='replace')}")
        return SandboxHandle(id=name, workdir=WORKDIR)

    def _hardening_args(self) -> list[str]:
        """Extra `docker run` hardening flags (improvement plan, step 12):
        a custom seccomp profile if configured, and an optional read-only root
        filesystem with tmpfs for the dirs the agent legitimately writes."""
        args: list[str] = []
        if self.config.seccomp_profile:
            args += ["--security-opt", f"seccomp={self.config.seccomp_profile}"]
        if self.config.read_only_rootfs:
            # only the disposable workspace + scratch are writable; the root FS
            # (system binaries, the baked image) cannot be modified in-run
            args += [
                "--read-only",
                "--tmpfs", "/work:rw,exec",
                "--tmpfs", "/tmp:rw,exec",
                "--tmpfs", "/home/node:rw,exec",
            ]
        return args

    def _provision_sidecars(self, sandbox_name: str) -> str:
        """Create the per-task network and stand up whichever sidecars the run
        needs (broker in proxy mode, browser if enabled). Returns the network
        the sandbox must join.

        In proxy mode the network is `--internal` (no direct egress): the broker
        straddles it and the default bridge and is the only route out. Otherwise
        (egress + browser) it is an ordinary user-defined network so the browser
        keeps normal egress. Either way containers reach each other by alias."""
        proxy = self.config.network == "proxy"
        net = _network_name(sandbox_name)

        create = ["docker", "network", "create"] + (["--internal"] if proxy else []) + [net]
        made = self._run(create, timeout=60)
        if made.returncode != 0:
            raise SandboxError(
                f"could not create task network: {made.stderr.decode(errors='replace')}"
            )
        try:
            if proxy:
                self._start_broker(net, sandbox_name)
            if self.config.browser:
                self._start_browser(net, sandbox_name, proxy=proxy)
        except SandboxError:
            self._teardown_sidecars(sandbox_name)
            raise
        return net

    def _start_broker(self, net: str, sandbox_name: str) -> None:
        """The egress broker: holds the API key(s), straddles the default bridge
        (real egress) and the task's internal net, answers to alias `broker`.

        Generalized routing (step 14): when broker_routes is set, the broker is
        given SANDKEEP_ROUTES + each route's secret (by its key_env), so any
        driver runs key-broker-protected. Otherwise the back-compat
        Anthropic-only path (broker_api_key → ANTHROPIC_API_KEY) is used."""
        broker = _broker_name(sandbox_name)
        broker_env = {"SANDKEEP_ALLOWLIST": self.config.egress_allowlist}
        env_flags = ["--env", "SANDKEEP_ALLOWLIST"]
        if self.config.broker_routes:
            broker_env["SANDKEEP_ROUTES"] = self.config.broker_routes
            env_flags += ["--env", "SANDKEEP_ROUTES"]
            for key_env, value in self.config.broker_secrets.items():
                broker_env[key_env] = value
                env_flags += ["--env", key_env]
        else:  # back-compat: single Anthropic route from broker_api_key
            broker_env["ANTHROPIC_API_KEY"] = self.config.broker_api_key
            env_flags += ["--env", "ANTHROPIC_API_KEY"]
        up = self._run(
            ["docker", "run", "--detach", "--name", broker,
             "--memory", "512m", "--pids-limit", "128",
             "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
             *env_flags, self.config.broker_image],
            timeout=60, env={**os.environ, **broker_env},
        )
        if up.returncode != 0:
            raise SandboxError(f"broker failed to start: {up.stderr.decode(errors='replace')}")
        conn = self._run(
            ["docker", "network", "connect", "--alias", "broker", net, broker], timeout=60
        )
        if conn.returncode != 0:
            raise SandboxError(
                f"could not attach broker to network: {conn.stderr.decode(errors='replace')}"
            )

    def _start_browser(self, net: str, sandbox_name: str, *, proxy: bool) -> None:
        """The browser bridge: headless Chromium on the task net, answering to
        alias `browser`, exposing only its CDP endpoint. In proxy mode its own
        egress is routed through the broker allowlist so page loads obey the
        same policy as the agent's API calls."""
        browser = _browser_name(sandbox_name)
        cmd = ["docker", "run", "--detach", "--name", browser,
               "--network", net, "--network-alias", "browser",
               "--memory", "1g", "--pids-limit", "256",
               "--security-opt", "no-new-privileges", "--cap-drop", "ALL"]
        browser_env: dict[str, str] = {}
        if proxy:
            # the browser has no direct egress on the internal net; route it
            # through the broker so page fetches are allowlisted + logged
            for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                cmd += ["--env", var]
                browser_env[var] = BROKER_ALIAS_URL
        cmd.append(self.config.browser_image)
        up = self._run(cmd, timeout=60, env={**os.environ, **browser_env} if browser_env else None)
        if up.returncode != 0:
            raise SandboxError(f"browser bridge failed to start: {up.stderr.decode(errors='replace')}")

    def _teardown_sidecars(self, sandbox_name: str) -> None:
        """Best-effort removal of a task's sidecars (broker, browser) + network."""
        self._run(["docker", "rm", "--force", "--volumes",
                   _broker_name(sandbox_name)], timeout=60)
        self._run(["docker", "rm", "--force", "--volumes",
                   _browser_name(sandbox_name)], timeout=60)
        self._run(["docker", "network", "rm", _network_name(sandbox_name)], timeout=60)

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

    def list_sandbox_ids(self) -> list[str]:
        proc = self._run(
            ["docker", "ps", "-a", "--filter", "name=sandkeep-", "--format", "{{.Names}}"],
            timeout=30,
        )
        if proc.returncode != 0:
            raise SandboxError(f"docker ps failed: {proc.stderr.decode(errors='replace')}")
        return [n for n in proc.stdout.decode(errors="replace").split() if n.strip()]

    def destroy(self, handle: SandboxHandle) -> None:
        proc = self._run(["docker", "rm", "--force", "--volumes", handle.id], timeout=60)
        # Tear down any sidecars too (no-op / harmless if this sandbox had none
        # — the broker/browser/network simply won't exist).
        self._teardown_sidecars(handle.id)
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


# Per-agent image templating (BUILD_SPEC §13). The default 'claude' image is
# the static sandbox_image/Dockerfile; other agents render this base with their
# driver's install_steps() injected (root phase, before USER node).
_BASE_DOCKERFILE = """\
# Rendered by sandkeep for agent: {agent}. Mirrors sandbox_image/Dockerfile.
FROM node:22-slim

RUN apt-get update \\
    && apt-get install -y --no-install-recommends git ca-certificates curl \\
    && rm -rf /var/lib/apt/lists/*

{agent_install}

RUN mkdir -p /work && chown -R node:node /work

USER node
ENV HOME=/home/node

RUN curl -fsSL https://mise.run | sh
ENV PATH="/home/node/.local/bin:/home/node/.local/share/mise/shims:${{PATH}}"

COPY --chown=node:node settings.json /home/node/.claude/settings.json

WORKDIR /work
CMD ["sleep", "infinity"]
"""


def render_dockerfile(agent: str, install_steps: list[str]) -> str:
    """The sandbox Dockerfile for an agent: the shared base + the agent's CLI
    install steps as RUN lines (run as root, before the drop to USER node)."""
    install = "\n".join(f"RUN {step}" for step in install_steps) or "# (no agent install steps)"
    return _BASE_DOCKERFILE.format(agent=agent, agent_install=install)


def build_agent_image(context_dir: Path, tag: str, agent: str, install_steps: list[str]) -> None:
    """Render a per-agent Dockerfile and build it, reusing context_dir's other
    assets (settings.json). The static Dockerfile is left untouched."""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        shutil.copy(context_dir / "settings.json", tmp_dir / "settings.json")
        (tmp_dir / "Dockerfile").write_text(render_dockerfile(agent, install_steps))
        proc = subprocess.run(
            ["docker", "build", "--tag", tag, str(tmp_dir)], capture_output=False
        )
    if proc.returncode != 0:
        raise SandboxError(f"docker build failed for agent {agent} (exit {proc.returncode})")
