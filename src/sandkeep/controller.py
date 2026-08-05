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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import agent_runner, diff, policy, results, skills
from .agent import get_driver
from .audit import AuditLog, new_trace_id
from .config import Config, resource_path
from .models import TERMINAL_STATES, ResultsContract, Task, TaskState
from .provisioner import ProvisioningError, provision
from .sandbox.base import SandboxError, SandboxHandle, SandboxProvider
from .state_store import StateStore
from .violations import Violation, ViolationKind, archive_sandbox, scan_agent_output


class ControllerError(Exception):
    pass


@dataclasses.dataclass
class SandboxInfo:
    """A live sandbox correlated with its task (for `sandkeep ps`/`gc`)."""

    id: str
    task_id: str | None
    state: str | None  # task state value, or None if no task record
    # classification: orphan (no task), stale (task already terminal),
    # review (alive on purpose, the rollback target), active (running now)
    kind: str

    @property
    def reapable(self) -> bool:
        return self.kind in ("orphan", "stale")


def run_concurrent(
    controller: "Controller",
    specs: list[dict],
    *,
    max_workers: int = 4,
) -> list:
    """Run many tasks in parallel (BUILD_SPEC §12), one sandbox each, on a
    shared Controller. Safe because the StateStore and AuditLog are
    thread-safe and the provider addresses each sandbox independently; the real
    parallelism is the per-task sandboxes/agent runs, not the (microsecond) DB
    writes. ``specs`` are run_task kwargs.

    Returns results in submission order: a Task on completion, or the raised
    Exception for a host-side failure (so one bad task can't sink the batch).
    """
    results: list = [None] * len(specs)

    def worker(spec: dict):
        return controller.run_task(**spec)

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = {pool.submit(worker, spec): i for i, spec in enumerate(specs)}
        for future, i in futures.items():
            try:
                results[i] = future.result()
            except Exception as exc:  # host-side error for this task only
                results[i] = exc
    return results


class Controller:
    def __init__(
        self,
        config: Config,
        store: StateStore,
        audit: AuditLog,
        provider: SandboxProvider,
        *,
        network_denied: bool = False,
        network: str = "egress",
        browser: bool = False,
        pr_gate=None,
    ) -> None:
        self.config = config
        self.store = store
        self.audit = audit
        self.provider = provider
        self.network = network
        self.network_denied = network_denied
        self.browser = browser
        # draft-PR gate (step 16); injectable for tests. Built lazily from
        # config/env on first use when the gate mode calls for it.
        self.pr_gate = pr_gate

    # -- the single-task loop (Phase 1) -----------------------------------

    def run_task(
        self,
        repo_path: str,
        instruction: str,
        *,
        model: str | None = None,
        agent: str | None = None,
        max_budget_usd: float | None = None,
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
            max_budget_usd=(
                max_budget_usd if max_budget_usd is not None else self.config.max_budget_usd
            ),
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
        self._inject_repo_skills(handle, task)

        self.store.update_state(task.id, TaskState.RUNNING, new_trace_id(), "agent dispatched")
        try:
            return self._drive_run(task, handle, driver, started)
        except BaseException as exc:  # noqa: BLE001 — crash recovery, then re-raise
            # Any unexpected failure after RUNNING (a bug, KeyboardInterrupt, the
            # daemon vanishing) must not strand the task at RUNNING with a live
            # sandbox: fail it and tear the sandbox down, then re-raise so the
            # CLI can report it.
            self._fail_if_running(task, handle, f"run aborted: {exc!r}")
            raise

    def _fail_if_running(self, task: Task, handle: SandboxHandle | None, detail: str) -> None:
        current = self.store.get_task(task.id).state
        if current not in TERMINAL_STATES and current is not TaskState.REVIEW:
            try:
                self._fail(task, handle, detail)
            except Exception:  # noqa: BLE001 — never mask the original error
                pass

    def _drive_run(
        self, task: Task, handle: SandboxHandle, driver, started: float
    ) -> Task:
        """The post-RUNNING body of a headless run. Extracted so run_task can
        wrap it in one crash-recovery guard."""
        if driver.produces_contract:
            agent_runner.install_system_prompt(
                self.provider, handle,
                (resource_path("prompts") / "agent_system_prompt.md").read_text(),
            )
        run = agent_runner.run_agent(
            task, self.provider, handle, self.audit,
            trace_id=new_trace_id(), timeout=self.config.task_timeout_seconds,
            max_budget_usd=f"{task.max_budget_usd:.2f}",
        )
        sandbox_seconds = time.monotonic() - started

        in_tok, out_tok = agent_runner.usage_from_output(run.output)

        if run.timed_out:
            # Ledger on every terminal state: a run that burned tokens is
            # accounted for even when it fails (improvement plan, step 5).
            self.store.record_cost(task.id, task.model, in_tok, out_tok, sandbox_seconds)
            self.store.update_state(task.id, TaskState.TIMEOUT, new_trace_id(), run.detail)
            self._roll_back(task, handle, detail=run.detail)
            return self.store.get_task(task.id)

        # The output scan is a HEURISTIC over the agent's transcript. Split it:
        #  - ESCAPE markers (docker.sock, nsenter, /proc/1/root) are specific and
        #    serious — keep archiving as a VIOLATION for forensics.
        #  - NETWORK / FILESYSTEM markers are prone to false positives (an agent
        #    that merely *mentions* "/src … permission denied" on a clean run),
        #    so they are ADVISORY: surfaced as risk flags at the gate, never
        #    archiving a successful run. Real egress/write ground truth comes
        #    from the sandbox boundary and, after the broker lands, from
        #    proxy-reported denials. BUILD_SPEC §8/§10b; improvement plan step 5.
        all_scan = scan_agent_output(run.stdout, run.stderr, network_denied=self.network_denied)
        hard = [v for v in all_scan if v.kind is ViolationKind.ESCAPE]
        scan_flags = [v for v in all_scan if v.kind is not ViolationKind.ESCAPE]
        if hard:
            self.store.record_cost(task.id, task.model, in_tok, out_tok, sandbox_seconds)
            self._violation(task, handle, hard)
            return self.store.get_task(task.id)

        if run.exit_code != 0:
            self.store.record_cost(task.id, task.model, in_tok, out_tok, sandbox_seconds)
            self._fail(task, handle, run.detail or f"agent exited {run.exit_code}")
            return self.store.get_task(task.id)

        if driver.produces_contract:
            # results contract + patch are the only things that cross back
            try:
                raw = self.provider.read_file(handle, agent_runner.RESULTS_JSON_PATH)
                contract = results.parse_results(raw, expected_task_id=task.id)
            except (FileNotFoundError, results.ContractError) as exc:
                self.store.record_cost(task.id, task.model, in_tok, out_tok, sandbox_seconds)
                self._fail(task, handle, f"results contract invalid: {exc}")
                return self.store.get_task(task.id)
            if contract.status != "succeeded":
                self.store.record_cost(task.id, task.model, in_tok, out_tok, sandbox_seconds)
                self._fail(
                    task, handle,
                    f"agent reported status={contract.status}: {contract.summary}",
                )
                return self.store.get_task(task.id)
            self._land_in_review(
                task, handle, raw, contract.summary,
                usage=(in_tok, out_tok), sandbox_seconds=sandbox_seconds,
                scan_flags=scan_flags,
            )
        else:
            # No agent-written contract; the diff is the truth (BUILD_SPEC §13).
            self._land_from_diff(
                task, handle,
                summary=f"{driver.name} run (diff-synthesized contract).",
                usage=(in_tok, out_tok), sandbox_seconds=sandbox_seconds,
                scan_flags=scan_flags,
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
        self._inject_repo_skills(handle, task)

        self.store.update_state(
            task.id, TaskState.RUNNING, new_trace_id(), "interactive session started"
        )
        try:
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
        except BaseException as exc:  # noqa: BLE001 — incl. Ctrl-C in the TTY
            self._fail_if_running(task, handle, f"interactive session aborted: {exc!r}")
            raise
        return self.store.get_task(task.id)

    # -- shared helpers ----------------------------------------------------

    #: where a proxy-mode sandbox reaches the broker (network alias + port).
    BROKER_URL = "http://broker:8080"
    #: where a browser-bridge sandbox reaches the CDP endpoint (step 11).
    BROWSER_CDP_URL = "http://browser:9222"

    def _agent_env(self, driver) -> dict[str, str]:
        """The sandbox environment for a driver.

        In `proxy` mode the sandbox gets NO credential: it points at the broker
        (which injects the key) via the driver's base-URL var and routes all
        other egress through the broker's allowlist proxy — the agent never
        holds the key and can't reach anything off the allowlist. In `egress`/
        `none` mode we forward the driver's secret from the host shell (the
        Phase 0–1 simplification the broker replaces). When the browser bridge
        is on, the agent is told where to reach the CDP endpoint."""
        if self.network == "proxy":
            env = {
                "HTTP_PROXY": self.BROKER_URL,
                "HTTPS_PROXY": self.BROKER_URL,
                "http_proxy": self.BROKER_URL,
                "https_proxy": self.BROKER_URL,
            }
            # Point the agent's base URL at this driver's broker route (step 14),
            # so the broker injects the right key for the right upstream.
            if driver.base_url_env and driver.broker_route:
                env[driver.base_url_env] = self.BROKER_URL + driver.broker_route["prefix"]
        else:
            env = {}
            secret = os.environ.get(driver.secret_env, "")
            if secret:
                env[driver.secret_env] = secret
        if self.browser:
            # the agent connects Playwright/Puppeteer here instead of launching
            # its own browser (connectOverCDP). It never gets a browser of its
            # own — only a capability, proxied and logged like its API calls.
            env["SANDKEEP_BROWSER_CDP"] = self.BROWSER_CDP_URL
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
        scan_flags: list[Violation] | None = None,
    ) -> None:
        """Synthesize a contract from the extracted diff (the diff is the
        truth) and land at REVIEW — or fail if the diff is empty. Shared by
        the interactive path and headless drivers that write no contract."""
        try:
            patch_path = diff.extract_patch(
                self.provider, handle, task, self.config.outputs_dir,
                exec_timeout=self.config.exec_timeout_seconds,
                max_bytes=self.config.max_patch_bytes,
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
            scan_flags=scan_flags,
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
        scan_flags: list[Violation] | None = None,
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

        patch_text = patch_path.read_text()
        # Phase 3: assess the diff and log risk flags so the audit trail records
        # what kind of change is heading to the gate (BUILD_SPEC §14).
        flags = policy.analyze_patch(patch_text)
        if flags:
            self.audit.log(
                "policy_risk_flagged",
                trace_id=new_trace_id(), task_id=task.id,
                flags=[{"category": f.category, "detail": f.detail} for f in flags],
            )
        # Advisory output-scan hits (step 5): persist to a sidecar so the gate
        # surfaces them as risk flags, and audit them — but they never block.
        if scan_flags:
            (self.config.outputs_dir / f"{task.id}.scan.json").write_text(
                json.dumps([{"kind": v.kind.value, "detail": v.detail} for v in scan_flags])
            )
            self.audit.log(
                "output_scan_flagged", trace_id=new_trace_id(), task_id=task.id,
                flags=[{"kind": v.kind.value, "detail": v.detail} for v in scan_flags],
            )
        # Phase 4: capture skills the agent authored — sandkeep metadata read
        # from the sandbox (excluded from the patch), saved to a host sidecar so
        # the gate can show them and accept can register them (BUILD_SPEC §15).
        try:
            authored = skills.read_authored(
                self.provider, handle, timeout=self.config.exec_timeout_seconds
            )
        except skills.SkillError as exc:
            authored = []
            self.audit.log(
                "skill_invalid", trace_id=new_trace_id(), task_id=task.id, error=str(exc)
            )
        if authored:
            sidecar = self.config.outputs_dir / f"{task.id}.skills"
            sidecar.mkdir(parents=True, exist_ok=True)
            for s in authored:
                (sidecar / s.filename).write_text(s.text)
            self.audit.log(
                "skills_authored", trace_id=new_trace_id(), task_id=task.id,
                names=[s.name for s in authored],
            )

        # One transaction for SUCCEEDED → REVIEW so a crash can't strand the
        # task on SUCCEEDED (from which no CLI command can recover it).
        self.store.advance(
            task.id, [TaskState.SUCCEEDED, TaskState.REVIEW], new_trace_id(), summary
        )

    # -- coordination & policy (Phase 3, read-only, host-side) ------------

    def risk_flags(self, task: Task) -> list[policy.RiskFlag]:
        """Risk flags for a task's patch (empty if no patch yet), including an
        advisory contract-mismatch flag when the agent's claimed files_changed
        disagrees with what the patch actually touches."""
        if not task.patch_path or not Path(task.patch_path).exists():
            return []
        patch_text = Path(task.patch_path).read_text()
        flags = policy.analyze_patch(patch_text)
        mismatch = policy.cross_check_files(self._claimed_files(task), patch_text)
        if mismatch:
            flags.append(mismatch)
        # Advisory output-scan hits captured at land time (step 5).
        scan_path = self.config.outputs_dir / f"{task.id}.scan.json"
        if scan_path.exists():
            try:
                for item in json.loads(scan_path.read_text()):
                    flags.append(policy.RiskFlag(
                        f"output-scan/{item['kind']}", item["detail"]
                    ))
            except (json.JSONDecodeError, OSError, KeyError):
                pass
        return flags

    def _claimed_files(self, task: Task) -> list[str]:
        """The files_changed the agent's contract claimed (from the sidecar
        results.json written at land time). Empty if none/unreadable."""
        path = self.config.outputs_dir / f"{task.id}.results.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return []
        claimed = data.get("files_changed")
        return claimed if isinstance(claimed, list) else []

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

    # -- capability authoring (Phase 4) -----------------------------------

    def _inject_repo_skills(self, handle: SandboxHandle, task: Task) -> None:
        """Push this repo's stored skills into the sandbox before the agent
        runs, so it can build on previously-authored capabilities."""
        stored = skills.SkillStore(self.config.home, task.repo_path).list()
        if not stored:
            return
        skills.inject(
            self.provider, handle, stored,
            timeout=self.config.exec_timeout_seconds,
        )
        self.audit.log(
            "skills_injected", trace_id=new_trace_id(), task_id=task.id,
            names=[s.name for s in stored],
        )

    def authored_skills(self, task: Task) -> list[skills.Skill]:
        """Skills a task authored, from the host sidecar captured at land time
        (empty if none)."""
        sidecar = self.config.outputs_dir / f"{task.id}.skills"
        if not sidecar.is_dir():
            return []
        return [
            skills.parse_skill(p.read_text(), source=str(p))
            for p in sorted(sidecar.glob("*.md"))
        ]

    # -- human gate --------------------------------------------------------

    def run_tests(self, task: Task, test_command: str) -> tuple[int, str]:
        """Run a test command INSIDE the task's (still-alive) sandbox, against
        the agent's actual changes — never on the host (golden rule). Returns
        (exit_code, combined output). BUILD_SPEC §14 test-gated merge."""
        if not task.sandbox_id:
            raise ControllerError("no live sandbox for task (already torn down?)")
        handle = SandboxHandle(id=task.sandbox_id, workdir="/work/repo")
        result = self.provider.exec(
            handle, ["sh", "-c", f"cd /work/repo && {test_command}"],
            timeout=self.config.task_timeout_seconds,
        )
        self.audit.log(
            "tests_run", trace_id=new_trace_id(), task_id=task.id,
            command=test_command, exit_code=result.exit_code,
        )
        return result.exit_code, (result.stdout + result.stderr)

    def accept(self, task_id: str, *, test_command: str | None = None) -> str:
        """Apply the patch to a fresh host branch; returns the commit sha. If
        test_command is given, it must pass IN THE SANDBOX first or accept is
        refused (the task stays at REVIEW)."""
        task = self.store.get_task(task_id)
        if task.state is not TaskState.REVIEW:
            raise ControllerError(f"task is {task.state.value}, not review")
        if test_command:
            code, output = self.run_tests(task, test_command)
            if code != 0:
                raise ControllerError(
                    f"test gate failed (exit {code}); not merging — task stays at "
                    f"review. Output (tail):\n{output[-1500:]}"
                )
        branch = f"sandkeep-accepted/{task.id}"
        sha = diff.apply_to_fresh_branch(
            task.repo_path, task.base_ref, branch, Path(task.patch_path),
            f"sandkeep: {task.instruction}\n\nTask: {task.id}",
        )
        detail = f"applied to {branch} @ {sha}"
        if self.config.gate == "draft-pr":
            pr = self._open_draft_pr(task, branch)
            detail += f"; draft PR {pr.url or f'#{pr.number}'}"
        self.store.update_state(task.id, TaskState.MERGED, new_trace_id(), detail)
        # Phase 4: register the skills this task authored, scoped to the repo,
        # so future runs against it inherit the learned capabilities.
        authored = self.authored_skills(task)
        if authored:
            skills.SkillStore(self.config.home, task.repo_path).save_all(authored)
            self.audit.log(
                "skills_registered", trace_id=new_trace_id(), task_id=task.id,
                names=[s.name for s in authored],
            )
        self._destroy_quietly(task)
        return sha

    def _open_draft_pr(self, task: Task, branch: str):
        """Push the accepted branch and open a draft PR (step 16). Uses the
        injected gate if present, else builds one from config/env. Raises
        ControllerError (not a raw PRGateError) so the CLI reports it cleanly."""
        from . import gate as gate_mod

        gw = self.pr_gate
        if gw is None:
            gw = gate_mod.DraftPRGate(
                remote=self.config.git_remote, base=self.config.pr_base,
                token=os.environ.get("GITHUB_TOKEN", ""),
            )
        contract = {}
        rp = self.config.outputs_dir / f"{task.id}.results.json"
        if rp.exists():
            try:
                contract = json.loads(rp.read_text())
            except (json.JSONDecodeError, OSError):
                contract = {}
        body = gate_mod.build_pr_body(
            instruction=task.instruction,
            summary=contract.get("summary", ""),
            files=self._claimed_files(task),
            risk_flags=self.risk_flags(task),
            task_id=task.id,
        )
        try:
            pr = gw.open(
                repo_path=task.repo_path, branch=branch,
                title=f"sandkeep: {task.instruction}"[:72], body=body,
            )
        except gate_mod.PRGateError as exc:
            raise ControllerError(f"draft-PR gate failed: {exc}") from None
        self.audit.log(
            "draft_pr_opened", trace_id=new_trace_id(), task_id=task.id,
            branch=branch, url=pr.url, number=pr.number,
        )
        return pr

    def reject(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if task.state is not TaskState.REVIEW:
            raise ControllerError(f"task is {task.state.value}, not review")
        self.store.update_state(task.id, TaskState.REJECTED, new_trace_id(), "human rejected")
        self._destroy_quietly(task)

    # -- sandbox housekeeping (`sandkeep ps` / `gc`) ----------------------

    def list_sandboxes(self) -> list[SandboxInfo]:
        """Every live sandbox the backend holds, classified against the store:
        orphan (no task), stale (task already terminal), review (alive on
        purpose), active (running now)."""
        by_sandbox = {
            t.sandbox_id: t for t in self.store.list_tasks() if t.sandbox_id
        }
        infos: list[SandboxInfo] = []
        for sid in sorted(self.provider.list_sandbox_ids()):
            task = by_sandbox.get(sid)
            if task is None:
                infos.append(SandboxInfo(sid, None, None, "orphan"))
            elif task.state in TERMINAL_STATES:
                infos.append(SandboxInfo(sid, task.id, task.state.value, "stale"))
            elif task.state is TaskState.REVIEW:
                infos.append(SandboxInfo(sid, task.id, task.state.value, "review"))
            else:
                infos.append(SandboxInfo(sid, task.id, task.state.value, "active"))
        return infos

    def reconcile(self, *, dry_run: bool = False) -> list[Task]:
        """Recover tasks a crashed controller left wedged: any non-terminal,
        non-REVIEW task whose sandbox is gone (or never recorded) can never make
        progress, so drive it FAILED → ROLLED_BACK. Returns the tasks that were
        (or would be) reconciled. This is what unsticks a RUNNING/PROVISIONING/
        SUCCEEDED row after the process that owned it died."""
        try:
            live = set(self.provider.list_sandbox_ids())
        except (SandboxError, NotImplementedError):
            live = set()
        stuck: list[Task] = []
        for task in self.store.list_tasks():
            if task.state in TERMINAL_STATES or task.state is TaskState.REVIEW:
                continue
            if task.sandbox_id and task.sandbox_id in live:
                continue  # sandbox still there — a run may genuinely be in flight
            stuck.append(task)
        if dry_run:
            return stuck
        for task in stuck:
            trace = new_trace_id()
            detail = "reconciled: controller gone, no live sandbox"
            # RUNNING/PROVISIONING/SUCCEEDED all allow → FAILED → ROLLED_BACK
            self.store.update_state(task.id, TaskState.FAILED, trace, detail)
            self.store.update_state(task.id, TaskState.ROLLED_BACK, trace, detail)
            self.audit.log(
                "task_reconciled", trace_id=trace, task_id=task.id,
                from_state=task.state.value, sandbox_id=task.sandbox_id,
            )
        return stuck

    def gc(self, *, include_review: bool = False, dry_run: bool = False) -> list[SandboxInfo]:
        """Reap sandboxes that shouldn't be alive: orphans (no task) and stale
        (task already terminal). With include_review, also reject + reap the
        intentional REVIEW rollback targets. Never touches 'active' sandboxes
        (a run may be in flight). Returns what was (or would be) reaped. Pair
        with reconcile() to also recover crash-wedged task rows — `sandkeep gc`
        runs both."""
        targets = [
            s for s in self.list_sandboxes()
            if s.reapable or (include_review and s.kind == "review")
        ]
        if dry_run:
            return targets
        for s in targets:
            if s.kind == "review" and s.task_id:
                self.reject(s.task_id)  # proper transition + destroy
            else:
                self._destroy_handle(
                    SandboxHandle(id=s.id, workdir="/work/repo"), s.task_id or "gc"
                )
            self.audit.log(
                "sandbox_reaped", trace_id=new_trace_id(),
                task_id=s.task_id, sandbox_id=s.id, kind=s.kind,
            )
        return targets

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
