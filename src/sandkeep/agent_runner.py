"""Runs the agent inside the sandbox by dispatching to its AgentDriver.

Phase 5 (BUILD_SPEC §13) made the agent pluggable: this module no longer
constructs any single agent's command line — it resolves `task.agent` to an
`AgentDriver` and delegates command construction + result parsing to it. The
Claude-specific logic now lives in `agent/claude.py`.

What stays here is the agent-neutral plumbing: dispatch, the wall-clock
timeout, audit events, the contract-prompt installer, and usage extraction.
The public names below are kept stable so existing call sites and tests that
import from `agent_runner` keep working.

ANTHROPIC_API_KEY (and any driver's secret) reaches the agent via the sandbox
environment, never the command line. TODO(phase-2): host-side secret-injecting
proxy so the agent never holds it.
"""

from __future__ import annotations

import base64

from .agent import AgentRunResult, get_driver
from .agent.claude import (
    AUTH_ERROR_EXIT,
    AUTH_ERROR_HINT,
    RESULTS_JSON_PATH,
    SYSTEM_PROMPT_PATH,
)
from .audit import AuditLog
from .models import Task
from .sandbox.base import (
    ExecResult,
    SandboxExecTimeout,
    SandboxHandle,
    SandboxProvider,
)

__all__ = [
    "AgentRunResult",
    "AUTH_ERROR_EXIT",
    "AUTH_ERROR_HINT",
    "RESULTS_JSON_PATH",
    "SYSTEM_PROMPT_PATH",
    "build_command",
    "build_interactive_command",
    "install_system_prompt",
    "run_agent",
    "usage_from_output",
]


def build_command(task: Task, *, max_budget_usd: str | None = "5.00") -> str:
    """The exact shell line run inside the sandbox for `task.agent`. Kept as a
    module function so tests can pin it without docker."""
    return get_driver(task.agent).build_command(task, max_budget_usd=max_budget_usd)


def build_interactive_command(
    task: Task, *, seed: str | None = None, skip_permissions: bool = True
) -> list[str]:
    """argv for an interactive `sandkeep shell` session (BUILD_SPEC §10b),
    built by `task.agent`'s driver.

    `--dangerously-skip-permissions` is on by default, matching the headless
    path: both run INSIDE the sandbox, so the prompts add friction without
    adding a boundary. The opt-out (`--no-skip-permissions` on the CLI)
    restores prompts. Never use this flag on the host."""
    return get_driver(task.agent).build_interactive_command(
        task, seed=seed, skip_permissions=skip_permissions
    )


def install_system_prompt(
    provider: SandboxProvider, handle: SandboxHandle, content: str, *, timeout: int = 30
) -> None:
    """Place the appended system prompt inside the sandbox (base64 transport
    so arbitrary markdown survives the shell). Only relevant for drivers that
    produce a results contract."""
    encoded = base64.b64encode(content.encode()).decode()
    result = provider.exec(
        handle,
        ["sh", "-c",
         f"mkdir -p /work/.sandkeep && echo {encoded} | base64 -d > {SYSTEM_PROMPT_PATH}"],
        timeout=timeout,
    )
    if result.exit_code != 0:
        raise RuntimeError(f"failed to install system prompt: {result.stderr.strip()}")


def run_agent(
    task: Task,
    provider: SandboxProvider,
    handle: SandboxHandle,
    audit: AuditLog,
    *,
    trace_id: str,
    timeout: int,
    max_budget_usd: str | None = "5.00",
) -> AgentRunResult:
    """Run the agent to completion inside the sandbox, enforcing the overall
    wall-clock timeout (BUILD_SPEC §6). Command construction and result parsing
    are delegated to `task.agent`'s driver."""
    driver = get_driver(task.agent)
    command = driver.build_command(task, max_budget_usd=max_budget_usd)
    audit.log(
        "agent_started",
        trace_id=trace_id,
        task_id=task.id,
        sandbox_id=handle.id,
        agent=driver.name,
        model=task.model,
        allowed_tools=task.allowed_tools,
        timeout=timeout,
    )
    try:
        result: ExecResult = provider.exec(handle, ["sh", "-c", command], timeout=timeout)
    except SandboxExecTimeout:
        audit.log("agent_timeout", trace_id=trace_id, task_id=task.id, timeout=timeout)
        return AgentRunResult(
            exit_code=-1, timed_out=True, stdout="", stderr="",
            output=None, detail=f"wall-clock timeout after {timeout}s",
        )

    run = driver.parse_result(result)
    audit.log(
        "agent_finished",
        trace_id=trace_id,
        task_id=task.id,
        exit_code=run.exit_code,
        num_turns=(run.output or {}).get("num_turns"),
        duration_ms=(run.output or {}).get("duration_ms"),
        detail=run.detail,
    )
    return run


def usage_from_output(output: dict | None) -> tuple[int, int]:
    """(input_tokens, output_tokens) from the agent's JSON result, defensively
    (BUILD_SPEC §6: do not rely on field order or presence)."""
    if not output:
        return 0, 0
    usage = output.get("usage") or {}
    in_tok = usage.get("input_tokens") or 0
    out_tok = usage.get("output_tokens") or 0
    # cache reads are still input cost; count them if present
    in_tok += usage.get("cache_read_input_tokens") or 0
    in_tok += usage.get("cache_creation_input_tokens") or 0
    return int(in_tok), int(out_tok)
