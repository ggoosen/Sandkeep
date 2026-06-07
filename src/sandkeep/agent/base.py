"""AgentDriver ABC + shared agent types (BUILD_SPEC §13, Phase 5).

The boundary is agent-agnostic: containment comes from the sandbox, not from
which agent runs inside it. An ``AgentDriver`` is the *only* thing that knows
how to invoke a particular CLI agent (Claude, and future Codex/Aider/…). It
builds command strings on the host; the agent itself still executes solely
inside the sandbox, so the golden rule ("scripts own the filesystem; agents
own the code") is preserved.

A driver must never weaken the boundary — the unmodified boundary suite must
pass regardless of which driver is selected.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..models import Task
from ..sandbox.base import ExecResult


@dataclass
class AgentRunResult:
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str
    output: dict | None  # parsed structured output (e.g. --output-format json), if any
    detail: str = ""


class UnknownAgent(Exception):
    """Raised when an agent name has no registered driver. A host-side
    misconfiguration — it must fail loud before any sandbox is created."""

    def __init__(self, name: str, available: list[str]) -> None:
        self.name = name
        self.available = available
        super().__init__(
            f"unknown agent {name!r}; available: {', '.join(available) or '(none)'}"
        )


class AgentDriver(ABC):
    """How to run one CLI agent inside a sandbox.

    Subclasses set the three class attributes and implement the four methods.
    Everything an agent needs that differs from another agent lives here and
    nowhere else.
    """

    #: short identifier, used for ``--agent`` / config selection
    name: str
    #: host env var holding this agent's credential, forwarded into the sandbox
    secret_env: str
    #: True if the agent writes a results contract (.sandkeep/results.json);
    #: False means the diff is the source of truth (host-side synthesis).
    produces_contract: bool

    @abstractmethod
    def install_steps(self) -> list[str]:
        """Dockerfile ``RUN`` lines that install this agent's CLI into the
        sandbox image (consumed by per-agent image builds)."""

    @abstractmethod
    def build_command(self, task: Task, *, max_budget_usd: str | None = "5.00") -> str:
        """The headless shell line to run inside the sandbox."""

    @abstractmethod
    def build_interactive_command(
        self, task: Task, *, seed: str | None = None, skip_permissions: bool = True
    ) -> list[str]:
        """argv for an interactive (TTY) session inside the sandbox."""

    @abstractmethod
    def parse_result(self, exec_result: ExecResult) -> AgentRunResult:
        """Interpret a finished exec into an AgentRunResult (exit-code meaning,
        error detail, structured output)."""
