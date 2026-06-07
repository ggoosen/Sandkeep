"""Agent driver registry (BUILD_SPEC §13, Phase 5).

Resolve an agent name to its ``AgentDriver``. ``claude`` is registered by
default; further drivers (Codex, Aider, …) register here once their CLI flags
are verified. Selection precedence (flag > env > config default) is handled by
the caller; this module only maps a name to a driver and fails loud on an
unknown one.
"""

from __future__ import annotations

from .base import AgentDriver, AgentRunResult, UnknownAgent
from .claude import ClaudeDriver

_DRIVERS: dict[str, AgentDriver] = {}


def register_driver(driver: AgentDriver) -> None:
    _DRIVERS[driver.name] = driver


def get_driver(name: str) -> AgentDriver:
    try:
        return _DRIVERS[name]
    except KeyError:
        raise UnknownAgent(name, available_agents()) from None


def available_agents() -> list[str]:
    return sorted(_DRIVERS)


register_driver(ClaudeDriver())

__all__ = [
    "AgentDriver",
    "AgentRunResult",
    "UnknownAgent",
    "get_driver",
    "register_driver",
    "available_agents",
]
