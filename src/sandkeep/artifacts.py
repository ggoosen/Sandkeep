"""Gated artifact return path (improvement plan, step 24).

An agent (or the browser bridge) can produce files worth reviewing — a
screenshot, a rendered report, a coverage summary — that aren't code changes.
The agent writes them under ``.sandkeep/artifacts/`` in the clone. Like authored
skills, that path is **excluded from the patch** (see `diff.py`), so artifacts
are sandkeep-managed metadata, never changes to the user's repo. The controller
reads them at land time (a gated channel), enforces a content-type allowlist +
size cap so only reviewable, bounded files can leave the sandbox, surfaces them
at the human gate, and persists them to a host sidecar. Nothing auto-lands.

This module is host-side and deterministic; it never executes agent code.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

from .sandbox.base import SandboxHandle, SandboxProvider

# Where the agent writes artifacts inside the clone (excluded from the patch).
ARTIFACT_DIR = ".sandkeep/artifacts"

# Only reviewable, well-understood types may leave the sandbox. This is the
# control that keeps a "capability" from becoming an arbitrary exfil channel.
ALLOWED_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",   # images (e.g. screenshots)
    ".txt", ".md", ".log", ".json", ".csv", ".html",    # text/report
})
DEFAULT_MAX_ARTIFACT_BYTES = 5 * 1024 * 1024  # per artifact


class ArtifactError(Exception):
    pass


@dataclass(frozen=True)
class Artifact:
    name: str        # basename, e.g. "login-page.png"
    data: bytes      # raw content
    skipped_reason: str = ""  # non-empty if captured-but-rejected (shown at gate)

    @property
    def size(self) -> int:
        return len(self.data)


def _classify(name: str, size: int, max_bytes: int) -> str:
    """Empty string = accepted; otherwise the reason it was rejected."""
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return f"type {ext or '(none)'} not in the artifact allowlist"
    if size > max_bytes:
        return f"{size} bytes exceeds the {max_bytes}-byte artifact cap"
    return ""


def read_artifacts(
    provider: SandboxProvider, handle: SandboxHandle, *,
    timeout: int = 60, max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
) -> list[Artifact]:
    """Artifacts the agent wrote under ARTIFACT_DIR. Disallowed-type or oversize
    files are captured as *rejected* (surfaced at the gate with a reason) rather
    than silently dropped, so the human sees what the agent tried to return."""
    listing = provider.exec(
        handle,
        ["sh", "-c", f"ls -1 /work/repo/{ARTIFACT_DIR} 2>/dev/null || true"],
        timeout=timeout,
    )
    names = [n.strip() for n in listing.stdout.splitlines() if n.strip()]
    out: list[Artifact] = []
    for name in sorted(names):
        # base64 transport so binary artifacts (PNGs) survive the text channel
        b64 = provider.exec(
            handle,
            ["sh", "-c", f"base64 -w0 /work/repo/{ARTIFACT_DIR}/{name} 2>/dev/null || true"],
            timeout=timeout,
        )
        try:
            data = base64.b64decode(b64.stdout.strip()) if b64.stdout.strip() else b""
        except (ValueError, base64.binascii.Error):
            out.append(Artifact(name=name, data=b"", skipped_reason="undecodable"))
            continue
        reason = _classify(name, len(data), max_bytes)
        out.append(Artifact(name=name, data=(b"" if reason else data), skipped_reason=reason))
    return out


def save_artifacts(artifacts: list[Artifact], sidecar_dir: Path) -> list[Artifact]:
    """Write accepted artifacts to a host sidecar for the gate to show. Rejected
    ones are not written (only their reason is surfaced). Returns the accepted."""
    accepted = [a for a in artifacts if not a.skipped_reason]
    if accepted:
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        for a in accepted:
            (sidecar_dir / a.name).write_bytes(a.data)
    return accepted
