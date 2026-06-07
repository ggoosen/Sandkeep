"""Tier 0: the deterministic state machine (BUILD_SPEC §9).

Drives one task through:

    NEW → PROVISIONING → RUNNING → (SUCCEEDED → REVIEW) → MERGED | REJECTED
                                  ↘ FAILED | TIMEOUT | VIOLATION → ROLLED_BACK

The controller never executes agent output; it only moves patches and
contracts. Every transition goes through state_store.update_state with a
fresh trace id. On terminal failure the sandbox is destroyed — or archived,
never silently, on VIOLATION — and never re-dispatched.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
import uuid
from pathlib import Path

from . import agent_runner, diff, policy, results
from .agent import get_driver
from .audit import AuditLog, new_trace_id
from .config import Config, resource_path
from .models import ResultsContract, Task, TaskState
from .provisioner import ProvisioningError, provision
from .sandbox.base import SandboxError, SandboxHandle, SandboxProvider
from .state_store import StateStore
from .violations import Violation, archive_sandbox, scan_agent_output


class ControllerError(Exception):
    pass


class Controller:
    def __init__(
        self,
        config: Config,
        store: StateStore,
        audit: AuditLog,
        provider: SandboxProvider,
        *,
        network_denied: bool = False,
    ) -> None:
        self.config = config
        self.store = store
        self.audit = audit
        self.provider = provider
        self.network_denied = network_denied

    # -- the single-task loop (Phase 1) -----------------------------------

    def run_task(
        self,
        repo_path: str,
        instruction: str,
        *,
        model: str | None = None,
        agent: str | None = None,
        max_turns: int | None = None,
        allowed_tools: list[str] | None = None,
    ) -> Task:
        """Run one governed task; returns the task in REVIEW on success or
        in a terminal failure state otherwise. Never raises for agent
        failures — only for host-side misconfiguration (an unknown agent
        raises UnknownAgent before anything is provisioned)."""
        self.config.ensure_dirs()
        # Resolve the driver BEFORE creating any state or sandbox: an unknown
        # agent is host-side misconfiguration and must fail loud (BUILD_SPEC §13).
        driver = get_driver(agent or self.config.agent)
        task = Task(
            id=uuid.uuid4().hex,
            repo_path=str(Path(repo_path).resolve()),
            instruction=instruction,
            model=model or self.config.model,
            agent=driver.name,
            max_turns=max_turns or self.config.max_turns,
        )
        if allowed_tools:
            task.allowed_tools = allowed_tools
        self.store.create_task(task, new_trace_id())

        handle: SandboxHandle | None = None
        started = time.monotonic()
        try:
            handle = provision(
                task, self.provider, self.store, self.audit, self._agent_env(driver),
                trace_id=new_trace_id(), exec_timeout=self.config.exec_timeout_seconds,
            )
        except (ProvisioningError, SandboxError) as exc:
            self._fail(task, handle, f"provisioning failed: {exc}")
            return self.store.get_task(task.id)
        task.base_ref = self.store.get_task(task.id).base_ref  # pinned SHA

        self.store.update_state(task.id, TaskState.RUNNING, new_trace_id(), "agent dispatched")
        if driver.produces_contract:
            agent_runner.install_system_prompt(
                self.provider, handle,
                (resource_path("prompts") / "agent_system_prompt.md").read_text(),
            )
        run = agent_runner.run_agent(
            task, self.provider, handle, self.audit,
            trace_id=new_trace_id(), timeout=self.config.task_timeout_seconds,
        )
        sandbox_seconds = time.monotonic() - started

        if run.timed_out:
            self.store.update_state(task.id, TaskState.TIMEOUT, new_trace_id(), run.detail)
            self._roll_back(task, handle, detail=run.detail)
            return self.store.get_task(task.id)

        found = scan_agent_output(run.stdout, run.stderr, network_denied=self.network_denied)
        if found:
            self._violation(task, handle, found)
            return self.store.get_task(task.id)

        if run.exit_code != 0:
            self._fail(task, handle, run.detail or f"agent exited {run.exit_code}")
            return self.store.get_task(task.id)

        in_tok, out_tok = agent_runner.usage_from_output(run.output)
        if driver.produces_contract:
            # results contract + patch are the only things that cross back
            try:
                raw = self.provider.read_file(handle, agent_runner.RESULTS_JSON_PATH)
                contract = results.parse_results(raw, expected_task_id=task.id)
            except (FileNotFoundError, results.ContractError) as exc:
                self._fail(task, handle, f"results contract invalid: {exc}")
                return self.store.get_task(task.id)
            if contract.status != "succeeded":
                self._fail(
                    task, handle,
                    f"agent reported status={contract.status}: {contract.summary}",
                )
                return self.store.get_task(task.id)
            self._land_in_review(
                task, handle, raw, contract.summary,
                usage=(in_tok, out_tok), sandbox_seconds=sandbox_seconds,
            )
        else:
            # No agent-written contract; the diff is the truth (BUILD_SPEC §13).
            self._land_from_diff(
                task, handle,
                summary=f"{driver.name} run (diff-synthesized contract).",
                usage=(in_tok, out_tok), sandbox_seconds=sandbox_seconds,
            )
        return self.store.get_task(task.id)

    # -- the interactive session (Phase 1, `sandkeep shell`) --------------

    def run_interactive(
        self,
        repo_path: str,
        *,
        instruction: str = "interactive session",
        model: str | None = None,
        agent: str | None = None,
        seed: str | None = None,
        skip_permissions: bool = True,
    ) -> Task:
        """Provision the same sandbox, drop the user into an interactive
        agent session on the clone, and on exit land at REVIEW with a
        host-synthesized contract — or roll back if nothing changed
        (BUILD_SPEC §10b). Returns the task in its resulting state."""
        self.config.ensure_dirs()
        driver = get_driver(agent or self.config.agent)  # fail loud on unknown
        task = Task(
            id=uuid.uuid4().hex,
            repo_path=str(Path(repo_path).resolve()),
            instruction=instruction,
            model=model or self.config.model,
            agent=driver.name,
        )
        self.store.create_task(task, new_trace_id())

        handle: SandboxHandle | None = None
        started = time.monotonic()
        try:
            handle = provision(
                task, self.provider, self.store, self.audit, self._agent_env(driver),
                trace_id=new_trace_id(), exec_timeout=self.config.exec_timeout_seconds,
            )
        except (ProvisioningError, SandboxError) as exc:
            self._fail(task, handle, f"provisioning failed: {exc}")
            return self.store.get_task(task.id)
        task.base_ref = self.store.get_task(task.id).base_ref

        self.store.update_state(
            task.id, TaskState.RUNNING, new_trace_id(), "interactive session started"
        )
        cmd = agent_runner.build_interactive_command(
            task, seed=seed, skip_permissions=skip_permissions
        )
        exit_code = self.provider.exec_interactive(handle, cmd)
        sandbox_seconds = time.monotonic() - started
        self.audit.log(
            "interactive_session_ended",
            trace_id=new_trace_id(), task_id=task.id, exit_code=exit_code,
        )

        # No agent contract for an interactive run; the diff is the truth.
        self._land_from_diff(
            task, handle,
            summary=f"Interactive {driver.name} session.",
            usage=(0, 0), sandbox_seconds=sandbox_seconds,
            empty_detail="no changes made in the interactive session",
        )
        return self.store.get_task(task.id)

    # -- shared helpers ----------------------------------------------------

    def _agent_env(self, driver) -> dict[str, str]:
        """The sandbox environment for a driver: forward its declared secret
        from the host shell if present (BUILD_SPEC §13 first cut).
        TODO(phase-2): secret-injecting proxy — the agent never holds the key."""
        env: dict[str, str] = {}
        secret = os.environ.get(driver.secret_env, "")
        if secret:
            env[driver.secret_env] = secret
        return env

    def _land_from_diff(
        self,
        task: Task,
        handle: SandboxHandle,
        *,
        summary: str,
        usage: tuple[int, int],
        sandbox_seconds: float,
        empty_detail: str = "no changes produced",
    ) -> None:
        """Synthesize a contract from the extracted diff (the diff is the
        truth) and land at REVIEW — or fail if the diff is empty. Shared by
        the interactive path and headless drivers that write no contract."""
        try:
            patch_path = diff.extract_patch(
                self.provider, handle, task, self.config.outputs_dir,
                exec_timeout=self.config.exec_timeout_seconds,
            )
        except diff.DiffError as exc:
            self._fail(task, handle, f"could not extract diff: {exc}")
            return
        if patch_path.stat().st_size == 0:
            self._fail(task, handle, empty_detail)
            return
        contract = ResultsContract(
            task_id=task.id,
            status="succeeded",
            summary=summary,
            files_changed=diff.files_in_patch(patch_path.read_text()),
        )
        raw = json.dumps(dataclasses.asdict(contract))
        self._land_in_review(
            task, handle, raw, summary,
            usage=usage, sandbox_seconds=sandbox_seconds, patch_path=patch_path,
        )

    def _land_in_review(
        self,
        task: Task,
        handle: SandboxHandle,
        raw: str,
        summary: str,
        *,
        usage: tuple[int, int],
        sandbox_seconds: float,
        patch_path: Path | None = None,
    ) -> None:
        """Validate the patch, record cost + contract, transition
        SUCCEEDED → REVIEW. Shared by the headless and interactive paths.
        The sandbox stays up — it is the rollback target until accept/reject."""
        try:
            if patch_path is None:
                patch_path = diff.extract_patch(
                    self.provider, handle, task, self.config.outputs_dir,
                    exec_timeout=self.config.exec_timeout_seconds,
                )
            diff.validate_patch(task.repo_path, task.base_ref, patch_path)
        except diff.DiffError as exc:
            self._fail(task, handle, f"patch invalid: {exc}")
            return

        self.store.update_fields(task.id, patch_path=str(patch_path))
        self.store.record_cost(task.id, task.model, usage[0], usage[1], sandbox_seconds)
        # contract JSON sits next to the patch for `sandkeep show`
        (self.config.outputs_dir / f"{task.id}.results.json").write_text(raw)

        # Phase 3: assess the diff and log risk flags so the audit trail records
        # what kind of change is heading to the gate (BUILD_SPEC §14).
        flags = policy.analyze_patch(patch_path.read_text())
        if flags:
            self.audit.log(
                "policy_risk_flagged",
                trace_id=new_trace_id(), task_id=task.id,
                flags=[{"category": f.category, "detail": f.detail} for f in flags],
            )

        self.store.update_state(task.id, TaskState.SUCCEEDED, new_trace_id(), summary)
        self.store.update_state(
            task.id, TaskState.REVIEW, new_trace_id(), "awaiting human gate"
        )

    # -- coordination & policy (Phase 3, read-only, host-side) ------------

    def risk_flags(self, task: Task) -> list[policy.RiskFlag]:
        """Risk flags for a task's patch (empty if no patch yet)."""
        if not task.patch_path or not Path(task.patch_path).exists():
            return []
        return policy.analyze_patch(Path(task.patch_path).read_text())

    def conflicts(self, task: Task) -> list[policy.Conflict]:
        """Other tasks awaiting review whose patches touch the same files."""
        if not task.patch_path or not Path(task.patch_path).exists():
            return []
        this_files = diff.files_in_patch(Path(task.patch_path).read_text())
        others: dict[str, list[str]] = {}
        for other in self.store.list_tasks():
            if other.id == task.id or other.state is not TaskState.REVIEW:
                continue
            if other.patch_path and Path(other.patch_path).exists():
                others[other.id] = diff.files_in_patch(
                    Path(other.patch_path).read_text()
                )
        return policy.find_conflicts(this_files, others)

    # -- human gate --------------------------------------------------------

    def accept(self, task_id: str) -> str:
        """Apply the patch to a fresh host branch; returns the commit sha."""
        task = self.store.get_task(task_id)
        if task.state is not TaskState.REVIEW:
            raise ControllerError(f"task is {task.state.value}, not review")
        branch = f"sandkeep-accepted/{task.id}"
        sha = diff.apply_to_fresh_branch(
            task.repo_path, task.base_ref, branch, Path(task.patch_path),
            f"sandkeep: {task.instruction}\n\nTask: {task.id}",
        )
        self.store.update_state(
            task.id, TaskState.MERGED, new_trace_id(), f"applied to {branch} @ {sha}"
        )
        self._destroy_quietly(task)
        return sha

    def reject(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if task.state is not TaskState.REVIEW:
            raise ControllerError(f"task is {task.state.value}, not review")
        self.store.update_state(task.id, TaskState.REJECTED, new_trace_id(), "human rejected")
        self._destroy_quietly(task)

    # -- failure paths -------------------------------------------------------

    def _fail(self, task: Task, handle: SandboxHandle | None, detail: str) -> None:
        self.store.update_state(task.id, TaskState.FAILED, new_trace_id(), detail)
        self._roll_back(task, handle, detail=detail)

    def _violation(self, task: Task, handle: SandboxHandle, found: list[Violation]) -> None:
        trace_id = new_trace_id()
        detail = "; ".join(v.detail for v in found)
        self.store.update_state(task.id, TaskState.VIOLATION, trace_id, detail)
        archive_sandbox(
            self.provider, handle, self.config, self.audit,
            task_id=task.id, trace_id=trace_id, violations=found,
        )
        self.store.update_state(
            task.id, TaskState.ROLLED_BACK, new_trace_id(), "archived after violation"
        )

    def _roll_back(self, task: Task, handle: SandboxHandle | None, *, detail: str) -> None:
        if handle is not None:
            self._destroy_handle(handle, task.id)
        self.store.update_state(
            task.id, TaskState.ROLLED_BACK, new_trace_id(), f"sandbox discarded: {detail}"
        )

    def _destroy_quietly(self, task: Task) -> None:
        if task.sandbox_id:
            self._destroy_handle(
                SandboxHandle(id=task.sandbox_id, workdir="/work/repo"), task.id
            )

    def _destroy_handle(self, handle: SandboxHandle, task_id: str) -> None:
        try:
            self.provider.destroy(handle)
        except SandboxError as exc:
            # already gone is fine; anything else must be visible
            self.audit.log(
                "sandbox_destroy_failed",
                trace_id=new_trace_id(),
                task_id=task_id,
                sandbox_id=handle.id,
                error=str(exc),
            )
