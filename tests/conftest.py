from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from sandkeep.audit import AuditLog
from sandkeep.models import Task
from sandkeep.state_store import StateStore


@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def store(tmp_path: Path, audit: AuditLog) -> StateStore:
    s = StateStore(tmp_path / "state.sqlite3", audit=audit)
    yield s
    s.close()


@pytest.fixture
def task() -> Task:
    return Task(
        id=uuid.uuid4().hex,
        repo_path="/tmp/example-repo",
        instruction="example instruction",
    )
