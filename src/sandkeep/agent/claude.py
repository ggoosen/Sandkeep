"""ClaudeDriver — the Claude Code CLI as an AgentDriver (BUILD_SPEC §13).

This is the verbatim home of the logic that used to live inline in
agent_runner.py. Flags verified against claude CLI 2.1.168 (re-verify with
``claude --help`` at build time):

- `--output-format json`, `--allowedTools`, `--append-system-prompt-file`,
  `--model`, and `--dangerously-skip-permissions` all present.
- `--max-turns` has been REMOVED from the CLI. Runaway protection is the
  wall-clock timeout (enforced by the runner) plus `--max-budget-usd`.

`--dangerously-skip-permissions` is acceptable here only because the command
runs INSIDE the sandbox; never use it on the host.

ANTHROPIC_API_KEY reaches the agent via the sandbox environment, never the
command line. TODO(phase-2): host-side secret-injecting proxy.
"""

from __future__ import annotations

import json
import shlex

from ..models import Task
from ..sandbox.base import WORKDIR, ExecResult
from .base import AgentDriver, AgentRunResult

SYSTEM_PROMPT_PATH = "/work/.sandkeep/agent_system_prompt.md"
RESULTS_JSON_PATH = "/work/repo/.sandkeep/results.json"

# Exit codes per BUILD_SPEC §6: 0 success, 1 general error, 2 auth error.
AUTH_ERROR_EXIT = 2
AUTH_ERROR_HINT = "auth error from claude — check ANTHROPIC_API_KEY is set and valid"


def _task_prompt(task: Task) -> str:
    return (
        f"Sandkeep task id: {task.id}\n\n"
        f"Task:\n{task.instruction}\n\n"
        "When done, write the results contract to .sandkeep/results.json "
        f'exactly as your system prompt specifies, with "task_id": "{task.id}".'
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


class ClaudeDriver(AgentDriver):
    name = "claude"
    secret_env = "ANTHROPIC_API_KEY"
    produces_contract = True

    def install_steps(self) -> list[str]:
        # Kept in sync with sandbox_image/Dockerfile (asserted by a test).
        return ["npm install -g @anthropic-ai/claude-code"]

    def build_command(self, task: Task, *, max_budget_usd: str | None = "5.00") -> str:
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
        self, task: Task, *, seed: str | None = None, skip_permissions: bool = True
    ) -> list[str]:
        inner = "claude"
        if skip_permissions:
            inner += " --dangerously-skip-permissions"
        if seed:
            inner += f" {shlex.quote(seed)}"
        return ["sh", "-lc", f"cd {WORKDIR} && exec {inner}"]

    def parse_result(self, exec_result: ExecResult) -> AgentRunResult:
        output: dict | None = None
        try:
            parsed = json.loads(exec_result.stdout)
            if isinstance(parsed, dict):
                output = parsed
        except (json.JSONDecodeError, ValueError):
            pass  # non-JSON output is handled via exit_code/detail

        detail = ""
        if exec_result.exit_code == AUTH_ERROR_EXIT:
            detail = AUTH_ERROR_HINT
        elif exec_result.exit_code != 0:
            detail = _error_detail(output) or f"agent exited {exec_result.exit_code}"

        return AgentRunResult(
            exit_code=exec_result.exit_code,
            timed_out=False,
            stdout=exec_result.stdout,
            stderr=exec_result.stderr,
            output=output,
            detail=detail,
        )
