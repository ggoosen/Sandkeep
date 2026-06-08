"""Structured JSON-lines audit logging with trace ids.

Golden rule 5: every state transition is logged here with a trace id.
No bare print anywhere in the controller — this is the only output channel
besides the CLI's user-facing messages.
"""

from __future__ import annotations

import json
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def new_trace_id() -> str:
    return uuid.uuid4().hex


class AuditLog:
    """Append-only JSON-lines log. One event per line, UTC timestamps."""

    def __init__(self, path: Path, *, echo: bool = False) -> None:
        self.path = path
        self.echo = echo  # also mirror to stderr (useful for `sandkeep run`)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # serialize writes from concurrent tasks

    def log(self, event: str, *, trace_id: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "trace_id": trace_id,
            **fields,
        }
        line = json.dumps(record, default=str, ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if self.echo:
            sys.stderr.write(line + "\n")
