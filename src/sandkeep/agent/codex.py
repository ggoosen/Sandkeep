"""CodexDriver — the OpenAI Codex CLI as an AgentDriver (BUILD_SPEC §13).

The second real driver, proving the boundary is genuinely agent-agnostic: the
unmodified boundary suite passes with `--agent codex` selected, because
containment comes from the sandbox, not from which CLI runs inside it.

> **Flags re-verify at build time.** Like the Claude driver (§6), the exact
> Codex CLI invocation must be confirmed with `codex --help` against the
> installed version. The flags below target the documented headless
> (non-interactive) mode: `exec` with full-auto approvals. Because the command
> runs INSIDE the sandbox, full-auto is acceptable here — never on the host.

Codex writes no Sandkeep results contract, so `produces_contract = False`: the
extracted diff is the source of truth and the controller synthesizes a contract
host-side (the same path `sandkeep shell` uses). Its credential is
`OPENAI_API_KEY`; in proxy mode the broker would need an OpenAI upstream, so
`base_url_env` is left empty for now (egress/none modes work today; proxy
support for Codex is a follow-up).
"""

from __future__ import annotations

import shlex

from ..models import Task
from ..sandbox.base import WORKDIR, ExecResult
from .base import AgentDriver, AgentRunResult

AUTH_ERROR_HINT = "auth error from codex — check OPENAI_API_KEY is set and valid"


def _task_prompt(task: Task) -> str:
    return f"Sandkeep task id: {task.id}\n\nTask:\n{task.instruction}"


class CodexDriver(AgentDriver):
    name = "codex"
    secret_env = "OPENAI_API_KEY"
    produces_contract = False  # diff is the truth; contract synthesized host-side
    # Broker-routable in proxy mode (step 14): the broker injects the OpenAI key
    # so the sandbox never holds it. The base-URL var + exact upstream path
    # should be re-verified against the real Codex/OpenAI CLI at build time (§6);
    # OPENAI_BASE_URL is the documented override for the OpenAI SDK.
    base_url_env = "OPENAI_BASE_URL"
    broker_route = {
        "prefix": "/openai",
        "upstream": "https://api.openai.com",
        "auth_header": "Authorization",
        "auth_scheme": "Bearer ",
        "key_env": "OPENAI_API_KEY",
    }

    def install_steps(self) -> list[str]:
        # Kept in sync with the codex per-agent image (asserted by a test).
        return ["npm install -g @openai/codex"]

    def build_command(self, task: Task, *, max_budget_usd: str | None = "5.00") -> str:
        # `codex exec` is the headless mode; --full-auto runs without approval
        # prompts (safe only because this is inside the sandbox). --model picks
        # the tier; --cd anchors the working dir. --max-budget-usd is not a
        # Codex flag, so the wall-clock timeout is the runaway bound here.
        parts = [
            "codex", "exec",
            "--full-auto",
            "--model", task.model,
            "--cd", WORKDIR,
            _task_prompt(task),
        ]
        return f"cd {WORKDIR} && " + shlex.join(parts)

    def build_interactive_command(
        self, task: Task, *, seed: str | None = None, skip_permissions: bool = True
    ) -> list[str]:
        inner = "codex"
        if skip_permissions:
            inner += " --full-auto"
        if seed:
            inner += f" {shlex.quote(seed)}"
        return ["sh", "-lc", f"cd {WORKDIR} && exec {inner}"]

    def parse_result(self, exec_result: ExecResult) -> AgentRunResult:
        # Codex emits human text on stdout, not a JSON contract. Exit code is
        # the outcome signal; surface a tail of stderr as the error detail.
        detail = ""
        if exec_result.exit_code != 0:
            tail = (exec_result.stderr or exec_result.stdout).strip()[-300:]
            detail = tail or f"codex exited {exec_result.exit_code}"
        return AgentRunResult(
            exit_code=exec_result.exit_code,
            timed_out=False,
            stdout=exec_result.stdout,
            stderr=exec_result.stderr,
            output=None,  # no structured output → usage unknown (ledger 0s)
            detail=detail,
        )
