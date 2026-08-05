"""Streaming-exec seam (improvement plan, step 25).

The stream-json parsing itself is deferred (needs the real claude CLI to verify
the event shape), but the provider seam it will use is in place and degrades
gracefully — like exec_interactive. This pins that contract.
"""

from __future__ import annotations

import pytest

from sandkeep.sandbox.base import SandboxHandle, SandboxProvider


def test_exec_stream_is_optional_and_degrades():
    class Minimal(SandboxProvider):
        def create(self, repo_path, env): ...
        def exec(self, handle, cmd, timeout): ...
        def read_file(self, handle, path): ...
        def destroy(self, handle): ...

    with pytest.raises(NotImplementedError):
        Minimal().exec_stream(SandboxHandle(id="x", workdir="/w"), ["echo", "hi"], 5)
