"""Violation detection + classification (BUILD_SPEC §8).

Detection sources, in priority order:
1. NETWORK  — egress attempts while the policy denies (under --network none
              every outbound attempt fails; the failure text is the signal)
2. FILESYSTEM — write attempts outside /work, or any write to read-only /src
3. ESCAPE   — attempts to reach the host: docker socket, host mounts,
              namespace tricks, privilege escalation

On any violation the controller transitions to VIOLATION, archives the
sandbox (forensics, never silent destroy), and never re-dispatches.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .audit import AuditLog
from .config import Config
from .sandbox.base import ExecResult, SandboxHandle, SandboxProvider


class ViolationKind(str, Enum):
    NETWORK = "network"
    FILESYSTEM = "filesystem"
    ESCAPE = "escape"


@dataclass(frozen=True)
class Violation:
    kind: ViolationKind
    detail: str


# Signals that a command *tried* to leave the boundary. Matching is
# deliberately simple and loud — false negatives are worse than noise here.
_ESCAPE_MARKERS = (
    "/var/run/docker.sock",
    "docker.sock",
    "/proc/1/root",
    "nsenter",
    "/dev/kmsg",
    "--privileged",
)
_NETWORK_FAILURE_MARKERS = (
    "could not resolve host",
    "temporary failure in name resolution",
    "network is unreachable",
    "no route to host",
    "connection refused",
    "connection timed out",
)
_READONLY_SRC_MARKERS = (
    "read-only file system",
    "permission denied",
)


def scan_exec(
    cmd: list[str] | str, result: ExecResult, *, network_denied: bool = True
) -> list[Violation]:
    """Classify one executed command + its outcome into violations."""
    text = cmd if isinstance(cmd, str) else " ".join(cmd)
    err = (result.stderr + "\n" + result.stdout).lower()
    found: list[Violation] = []

    for marker in _ESCAPE_MARKERS:
        if marker in text:
            found.append(Violation(ViolationKind.ESCAPE, f"escape attempt: {marker} in {text!r}"))
            break

    if network_denied and result.exit_code != 0:
        for marker in _NETWORK_FAILURE_MARKERS:
            if marker in err:
                found.append(
                    Violation(ViolationKind.NETWORK, f"egress attempt under deny policy: {text!r}")
                )
                break

    if result.exit_code != 0 and "/src" in (text + err):
        for marker in _READONLY_SRC_MARKERS:
            if marker in err:
                found.append(
                    Violation(
                        ViolationKind.FILESYSTEM, f"write attempt on read-only /src: {text!r}"
                    )
                )
                break

    return found


def archive_sandbox(
    provider: SandboxProvider,
    handle: SandboxHandle,
    config: Config,
    audit: AuditLog,
    *,
    task_id: str,
    trace_id: str,
    violations: list[Violation],
) -> None:
    """Archive a violating sandbox (BUILD_SPEC §8): record what happened,
    then quarantine (SANDKEEP_KEEP_FORENSICS=1 keeps the stopped sandbox)
    or destroy. Never silent, never re-dispatched."""
    config.ensure_dirs()
    record = {
        "task_id": task_id,
        "sandbox_id": handle.id,
        "trace_id": trace_id,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "violations": [{"kind": v.kind.value, "detail": v.detail} for v in violations],
    }
    path = config.archive_dir / f"{task_id}.json"
    path.write_text(json.dumps(record, indent=2))
    keep = os.environ.get("SANDKEEP_KEEP_FORENSICS") == "1"
    if keep and hasattr(provider, "stop"):
        provider.stop(handle)  # quarantined, not destroyed
    else:
        provider.destroy(handle)
    audit.log(
        "sandbox_archived",
        trace_id=trace_id,
        task_id=task_id,
        sandbox_id=handle.id,
        kept_for_forensics=keep,
        violations=[v.detail for v in violations],
        archive_record=str(path),
    )
