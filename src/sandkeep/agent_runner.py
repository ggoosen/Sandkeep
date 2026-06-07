"""Builds + runs the headless claude command inside the sandbox.

This is the ONLY place a `claude` invocation is constructed (CLAUDE.md
conventions). Flags verified against claude CLI 2.1.168 at build time
(BUILD_SPEC §6 requires re-verification):

- `--output-format json`, `--allowedTools`, `--append-system-prompt-file`,
  `--model`, and `--dangerously-skip-permissions` all present.
- `--max-turns` has been REMOVED from the CLI. Runaway protection is the
  wall-clock timeout enforced here plus `--max-budget-usd` (present since
  2.x). Task.max_turns is retained in the model for forward compatibility.

`--dangerously-skip-permissions` is acceptable here only because the command
runs INSIDE the sandbox; never use it on the host.

ANTHROPIC_API_KEY reaches the agent via the sandbox environment set at
create() time — it never appears in the command line.
TODO(phase-2): host-side secret-injecting proxy so the agent never holds it.
"""

from __future__ import annotations

import base64
import json
import shlex
from dataclasses import dataclass

from .audit import AuditLog
from .models import Task
from .sandbox.base import (
    WORKDIR,
    ExecResult,
    SandboxExecTimeout,
    SandboxHandle,
    SandboxProvider,
)

SYSTEM_PROMPT_PATH = "/work/.sandkeep/agent_system_prompt.md"
RESULTS_JSON_PATH = "/work/repo/.sandkeep/results.json"

# Exit codes per BUILD_SPEC §6: 0 success, 1 general error, 2 auth error.
AUTH_ERROR_EXIT = 2
AUTH_ERROR_HINT = "auth error from claude — check ANTHROPIC_API_KEY is set and valid"


@dataclass
class AgentRunResult:
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str
    output: dict | None  # parsed --output-format json payload, if any
    detail: str = ""


def _task_prompt(task: Task) -> str:
    return (
        f"Sandkeep task id: {task.id}\n\n"
        f"Task:\n{task.instruction}\n\n"
        "When done, write the results contract to .sandkeep/results.json "
        f'exactly as your system prompt specifies, with "task_id": "{task.id}".'
    )


def build_command(task: Task, *, max_budget_usd: str | None = "5.00") -> str:
    """The exact shell line run inside the sandbox. Kept separate so tests
    can pin it without docker."""
    parts = [
        "claude",
        "-p", _task_prompt(task),
        "--output-format", "json",
        "--model", task.model,
        "--allowedTools", ",".join(task.allowed_tools),
        "--append-system-prompt-file", SYSTEM_PROMPT_PATH,
        "--dangerously-skip-permissions",
    ]
    if max_budget_usd:
        parts += ["--max-budget-usd", max_budget_usd]
    return f"cd {WORKDIR} && " + shlex.join(parts)


def build_interactive_command(
    task: Task, *, seed: str | None = None, skip_permissions: bool = True
) -> list[str]:
    """The argv for an interactive `sandkeep shell` session (BUILD_SPEC §10b).

    Plain interactive claude — NO -p, NO --output-format, NO --max-turns. The
    user is in the loop, so there is no headless contract. An optional seed
    becomes the first message. Returned as argv (not a shell string) because
    it is handed to `docker exec -it`, not a shell.

    `--dangerously-skip-permissions` is on by default, matching the headless
    path: both run INSIDE the sandbox, so the permission prompts add friction
    without adding a boundary. The opt-out (`--no-skip-permissions` on the CLI)
    restores prompts for users who want them. Never use this flag on the host.
    """
    inner = "claude"
    if skip_permissions:
        inner += " --dangerously-skip-permissions"
    if seed:
        inner += f" {shlex.quote(seed)}"
    return ["sh", "-lc", f"cd {WORKDIR} && exec {inner}"]


def install_system_prompt(
    provider: SandboxProvider, handle: SandboxHandle, content: str, *, timeout: int = 30
) -> None:
    """Place the appended system prompt inside the sandbox (base64 transport
    so arbitrary markdown survives the shell)."""
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
    """Run the headless agent to completion inside the sandbox, enforcing the
    overall wall-clock timeout (BUILD_SPEC §6)."""
    command = build_command(task, max_budget_usd=max_budget_usd)
    audit.log(
        "agent_started",
        trace_id=trace_id,
        task_id=task.id,
        sandbox_id=handle.id,
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

    output: dict | None = None
    try:
        parsed = json.loads(result.stdout)
        if isinstance(parsed, dict):
            output = parsed
    except (json.JSONDecodeError, ValueError):
        pass  # non-JSON output is handled by the caller via exit_code/detail

    detail = ""
    if result.exit_code == AUTH_ERROR_EXIT:
        detail = AUTH_ERROR_HINT
    elif result.exit_code != 0:
        detail = _error_detail(output) or f"agent exited {result.exit_code}"

    audit.log(
        "agent_finished",
        trace_id=trace_id,
        task_id=task.id,
        exit_code=result.exit_code,
        num_turns=(output or {}).get("num_turns"),
        duration_ms=(output or {}).get("duration_ms"),
        detail=detail,
    )
    return AgentRunResult(
        exit_code=result.exit_code,
        timed_out=False,
        stdout=result.stdout,
        stderr=result.stderr,
        output=output,
        detail=detail,
    )


def _error_detail(output: dict | None) -> str:
    """Surface claude's own error message from --output-format json instead of
    a bare exit code. The CLI puts a human message in `result` and, on API
    failures, an `api_error_status` (e.g. 400 'Credit balance is too low')."""
    if not output:
        return ""
    message = str(output.get("result") or "").strip()
    api_status = output.get("api_error_status")
    if message and api_status:
        return f"{message} (api status {api_status})"
    if message:
        return message
    if api_status:
        return f"agent API error (status {api_status})"
    return ""


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
