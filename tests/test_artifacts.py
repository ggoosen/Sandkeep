"""Gated artifact return path (improvement plan, step 24).

Artifacts widen what leaves the sandbox, so they're type/size-gated and
human-shown, never auto-landed. These test the classification + capture logic
with a fake provider (base64 transport) — no Docker.
"""

from __future__ import annotations

import base64
from pathlib import Path

from sandkeep.artifacts import (
    ALLOWED_EXTENSIONS,
    Artifact,
    read_artifacts,
    save_artifacts,
)
from sandkeep.sandbox.base import ExecResult, SandboxHandle


class FakeProvider:
    """Serves a fake .sandkeep/artifacts listing + base64 file contents."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files

    def exec(self, handle, cmd, timeout):
        import re

        script = cmd[-1]
        if script.startswith("ls -1"):
            return ExecResult(0, "\n".join(self.files) + "\n", "")
        # base64 -w0 /work/repo/.sandkeep/artifacts/<name> 2>/dev/null || true
        m = re.search(r"/artifacts/(\S+?)\s", script)
        name = m.group(1) if m else ""
        data = self.files.get(name, b"")
        return ExecResult(0, base64.b64encode(data).decode(), "")


def _read(files, **kw):
    return read_artifacts(FakeProvider(files), SandboxHandle(id="x", workdir="/work/repo"), **kw)


def test_accepts_allowlisted_image():
    arts = _read({"shot.png": b"\x89PNG\r\n\x1a\nbinary-bytes\x00\x01"})
    assert len(arts) == 1
    a = arts[0]
    assert a.name == "shot.png" and not a.skipped_reason
    assert a.data.startswith(b"\x89PNG")  # binary survived base64 transport


def test_rejects_disallowed_type_but_keeps_reason():
    arts = _read({"evil.sh": b"#!/bin/sh\nrm -rf /\n"})
    assert arts[0].skipped_reason  # rejected
    assert arts[0].data == b""     # content not captured
    assert "allowlist" in arts[0].skipped_reason


def test_rejects_oversize():
    arts = _read({"big.txt": b"x" * 100}, max_bytes=10)
    assert "exceeds" in arts[0].skipped_reason


def test_png_is_allowlisted():
    assert ".png" in ALLOWED_EXTENSIONS and ".sh" not in ALLOWED_EXTENSIONS


def test_save_writes_only_accepted(tmp_path: Path):
    arts = [
        Artifact(name="ok.txt", data=b"hello"),
        Artifact(name="bad.exe", data=b"", skipped_reason="type .exe not allowed"),
    ]
    accepted = save_artifacts(arts, tmp_path / "arts")
    assert [a.name for a in accepted] == ["ok.txt"]
    assert (tmp_path / "arts" / "ok.txt").read_bytes() == b"hello"
    assert not (tmp_path / "arts" / "bad.exe").exists()
