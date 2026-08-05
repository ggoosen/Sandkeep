"""GeminiDriver — the Google Gemini CLI as an AgentDriver (improvement plan,
step 21).

A third real driver, further evidence the boundary is agent-agnostic: the
unmodified boundary suite passes with `--agent gemini` selected.

> **Flags re-verify at build time** (§6 discipline). These target the Gemini
> CLI's documented non-interactive mode: `--prompt` with `--yolo` (auto-approve
> tool use — acceptable only INSIDE the sandbox, never on the host). Confirm with
> `gemini --help` against the installed version.

Gemini writes no Sandkeep contract, so `produces_contract = False`: the diff is
the source of truth (host-side synthesis). Its credential is `GEMINI_API_KEY`;
broker-routable in proxy mode via `GOOGLE_GEMINI_BASE_URL` so the key stays with
the broker.
"""

from __future__ import annotations

import shlex

from ..models import Task
from ..sandbox.base import WORKDIR, ExecResult
from .base import AgentDriver, AgentRunResult


def _task_prompt(task: Task) -> str:
    return f"Sandkeep task id: {task.id}\n\nTask:\n{task.instruction}"


class GeminiDriver(AgentDriver):
    name = "gemini"
    secret_env = "GEMINI_API_KEY"
    produces_contract = False
    base_url_env = "GOOGLE_GEMINI_BASE_URL"
    broker_route = {
        "prefix": "/gemini",
        "upstream": "https://generativelanguage.googleapis.com",
        # Gemini authenticates with an API key; the header form is verified at
        # build time. x-goog-api-key is the documented header for the REST API.
        "auth_header": "x-goog-api-key",
        "auth_scheme": "",
        "key_env": "GEMINI_API_KEY",
    }

    def install_steps(self) -> list[str]:
        return ["npm install -g @google/gemini-cli"]

    def build_command(self, task: Task, *, max_budget_usd: str | None = "5.00") -> str:
        # `--prompt` is the non-interactive mode; `--yolo` auto-approves tool
        # use (safe only inside the sandbox). No budget flag → the wall-clock
        # timeout is the runaway bound.
        parts = ["gemini", "--yolo", "--model", task.model, "--prompt", _task_prompt(task)]
        return f"cd {WORKDIR} && " + shlex.join(parts)

    def build_interactive_command(
        self, task: Task, *, seed: str | None = None, skip_permissions: bool = True
    ) -> list[str]:
        inner = "gemini"
        if skip_permissions:
            inner += " --yolo"
        if seed:
            inner += f" --prompt {shlex.quote(seed)}"
        return ["sh", "-lc", f"cd {WORKDIR} && exec {inner}"]

    def parse_result(self, exec_result: ExecResult) -> AgentRunResult:
        detail = ""
        if exec_result.exit_code != 0:
            detail = (exec_result.stderr or exec_result.stdout).strip()[-300:] \
                or f"gemini exited {exec_result.exit_code}"
        return AgentRunResult(
            exit_code=exec_result.exit_code, timed_out=False,
            stdout=exec_result.stdout, stderr=exec_result.stderr,
            output=None, detail=detail,
        )
