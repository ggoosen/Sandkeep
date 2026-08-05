"""Core data models: Task, TaskState, ResultsContract.

Defined in BUILD_SPEC §2. The legal state-machine transitions (§9) live here
too so the state store can enforce them on every write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TaskState(str, Enum):
    NEW = "new"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    SUCCEEDED = "succeeded"  # agent finished, valid contract + patch
    FAILED = "failed"  # agent errored / invalid output
    TIMEOUT = "timeout"
    VIOLATION = "violation"  # boundary breach attempt detected
    REVIEW = "review"  # awaiting human gate
    MERGED = "merged"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


# BUILD_SPEC §9:
# NEW → PROVISIONING → RUNNING → (SUCCEEDED → REVIEW) → MERGED | REJECTED
#                              ↘ FAILED | TIMEOUT | VIOLATION → ROLLED_BACK
ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.NEW: frozenset({TaskState.PROVISIONING}),
    TaskState.PROVISIONING: frozenset(
        {TaskState.RUNNING, TaskState.FAILED, TaskState.VIOLATION}
    ),
    TaskState.RUNNING: frozenset(
        {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.TIMEOUT, TaskState.VIOLATION}
    ),
    TaskState.SUCCEEDED: frozenset({TaskState.REVIEW, TaskState.FAILED}),
    # REVIEW → RUNNING: `sandkeep revise` re-dispatches the agent into the
    # still-alive sandbox to iterate the diff (improvement plan, step 17).
    TaskState.REVIEW: frozenset({TaskState.MERGED, TaskState.REJECTED, TaskState.RUNNING}),
    TaskState.FAILED: frozenset({TaskState.ROLLED_BACK}),
    TaskState.TIMEOUT: frozenset({TaskState.ROLLED_BACK}),
    TaskState.VIOLATION: frozenset({TaskState.ROLLED_BACK}),
    TaskState.MERGED: frozenset(),
    TaskState.REJECTED: frozenset(),
    TaskState.ROLLED_BACK: frozenset(),
}

TERMINAL_STATES: frozenset[TaskState] = frozenset(
    state for state, nexts in ALLOWED_TRANSITIONS.items() if not nexts
)


@dataclass
class Task:
    id: str  # uuid4
    repo_path: str  # host path, mounted read-only
    instruction: str  # what the agent should do
    base_ref: str = "HEAD"  # diff is computed against this
    branch: str = ""  # task branch inside the sandbox
    state: TaskState = TaskState.NEW
    model: str = "claude-sonnet-4-6"  # task-tier model; alias verified 2026-06 (BUILD_SPEC §2)
    agent: str = "claude"  # which AgentDriver runs inside the sandbox (BUILD_SPEC §13)
    max_budget_usd: float = 5.0  # per-run spend cap handed to the agent CLI
    allowed_tools: list[str] = field(
        default_factory=lambda: ["Read", "Edit", "Write", "Bash"]
    )
    sandbox_id: str = ""
    patch_path: str = ""
    results: "ResultsContract | None" = None


@dataclass
class ResultsContract:
    task_id: str
    status: str  # succeeded | failed | timeout | violation
    summary: str
    files_changed: list[str]
    tests_run: int = 0
    tests_passed: int = 0
    risks: list[str] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    duration_seconds: float = 0.0
