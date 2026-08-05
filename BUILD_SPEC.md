# Sandkeep — Build Specification

**Audience:** Claude Code (and the human reviewing its work).
**Scope of this spec:** Phase 0 (prove the boundary) and Phase 1 (single-task governed loop) in build detail (§0–§11); §13–§16 spec the later phases, most of which are now implemented — each section carries its own status line. Current work is tracked in `docs/improvement-plan.md`.
**Companion:** `CLAUDE.md` (operating brief, golden rules). Read it first.
**Source of truth for the design rationale:** the Sandkeep v2 design doc. This spec is the *buildable subset*.

---

## 0. Build order (follow exactly)

Build in this sequence. Each step has acceptance tests; do not advance until they pass.

1. Project skeleton + models + state store + audit log.
2. `SandboxProvider` ABC + Docker provider + sandbox image.
3. Provisioning (read-only repo mount → independent clone → task branch).
4. Boundary proof + adversarial tests. **(Phase 0 gate)**
5. Agent runner (headless `claude -p` inside the sandbox).
6. Results contract + diff extraction + validation.
7. Controller state machine wiring it together.
8. CLI + human gate (local patch apply). **(Phase 1 gate)**

---

## 1. Repository layout

```
sandkeep/
  CLAUDE.md
  BUILD_SPEC.md
  README.md
  pyproject.toml
  src/sandkeep/
    __init__.py
    cli.py                 # `sandkeep` entrypoint
    config.py              # paths, env, defaults
    models.py              # Task, TaskState, ResultsContract (dataclasses)
    state_store.py         # SQLite: tasks, transitions, ledger
    audit.py               # JSON-line structured logging + trace ids
    controller.py          # Tier 0: the state machine
    policy.py              # diff risk analysis + cross-task conflicts (§14)
    provisioner.py         # Tier 2: sandbox lifecycle
    agent_runner.py        # agent-neutral dispatch to AgentDriver (§13)
    diff.py                # extract / validate / apply patches
    results.py             # parse + validate the results contract
    violations.py          # violation detection + classification
    skills.py              # per-repo capability authoring (§15)
    agent/
      __init__.py          # driver registry (get_driver)
      base.py              # AgentDriver ABC (§13)
      claude.py            # the built-in claude driver
    sandbox/
      __init__.py
      base.py              # SandboxProvider ABC
      docker_provider.py   # default backend (the ONLY place docker is touched)
      e2b_provider.py      # E2B microVM backend (§16, SANDKEEP_BACKEND=e2b)
  prompts/
    agent_system_prompt.md # appended to the agent's system prompt
  sandbox_image/
    Dockerfile             # node 22 + claude code + git + mise
    settings.json          # .claude/settings.json copied into the sandbox
  tests/
    test_boundary.py       # Phase 0 adversarial suite
    test_provisioning.py
    test_diff_extraction.py
    test_results_contract.py
    test_state_machine.py
    conftest.py
```

---

## 2. Data models (`models.py`)

```python
from dataclasses import dataclass, field
from enum import Enum

class TaskState(str, Enum):
    NEW          = "new"
    PROVISIONING = "provisioning"
    RUNNING      = "running"
    SUCCEEDED    = "succeeded"      # agent finished, valid contract + patch
    FAILED       = "failed"         # agent errored / invalid output
    TIMEOUT      = "timeout"
    VIOLATION    = "violation"      # boundary breach attempt detected
    REVIEW       = "review"         # awaiting human gate
    MERGED       = "merged"
    REJECTED     = "rejected"
    ROLLED_BACK  = "rolled_back"

@dataclass
class Task:
    id: str                         # uuid4
    repo_path: str                  # host path, mounted read-only
    instruction: str                # what the agent should do
    base_ref: str = "HEAD"          # diff is computed against this
    branch: str = ""                # task branch inside the sandbox
    state: TaskState = TaskState.NEW
    model: str = "claude-sonnet-4-6"  # task-tier model; verify current alias
    max_budget_usd: float = 5.0       # per-run spend cap (--max-turns is gone upstream)
    allowed_tools: list[str] = field(default_factory=lambda: ["Read", "Edit", "Write", "Bash"])
    sandbox_id: str = ""
    patch_path: str = ""
    results: "ResultsContract | None" = None

@dataclass
class ResultsContract:
    task_id: str
    status: str                     # succeeded | failed | timeout | violation
    summary: str
    files_changed: list[str]
    tests_run: int = 0
    tests_passed: int = 0
    risks: list[str] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    duration_seconds: float = 0.0
```

`model` alias must be verified against current Claude Code docs at build time (see §6). Treat it as config, not a hardcode.

---

## 3. State store (`state_store.py`)

SQLite, stdlib only. Three tables:

- `tasks(id, repo_path, instruction, base_ref, branch, state, model, max_turns, sandbox_id, patch_path, created_at, updated_at)`
- `transitions(id, task_id, from_state, to_state, trace_id, detail, ts)` — append-only audit of every state change.
- `ledger(task_id, model, input_tokens, output_tokens, sandbox_seconds, ts)` — cost accounting.

Expose: `create_task`, `get_task`, `update_state(task_id, new_state, trace_id, detail)` (writes both `tasks.state` and a `transitions` row in one transaction), `record_cost`, `list_tasks`. **Every** state change goes through `update_state` so the audit trail is complete.

---

## 4. Sandbox provider (`sandbox/base.py`)

The controller talks only to this interface. Phase 2's microVM backend implements the same ABC.

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class SandboxHandle:
    id: str
    workdir: str          # path INSIDE the sandbox, e.g. /work/repo

@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str

class SandboxProvider(ABC):
    @abstractmethod
    def create(self, repo_path: str, env: dict[str, str]) -> SandboxHandle:
        """Start a sandbox. Mount `repo_path` READ-ONLY at /src.
        Network: deny-by-default except an allowlist (see §5)."""

    @abstractmethod
    def exec(self, handle: SandboxHandle, cmd: list[str], timeout: int) -> ExecResult:
        """Run a command inside the sandbox."""

    @abstractmethod
    def read_file(self, handle: SandboxHandle, path: str) -> str:
        """Read a file from inside the sandbox (used to pull the contract + patch)."""

    @abstractmethod
    def destroy(self, handle: SandboxHandle) -> None:
        """Tear down and discard all sandbox state."""
```

### Docker provider requirements (`sandbox/docker_provider.py`)

- Host repo mounted **read-only**: `-v {repo_path}:/src:ro`.
- Run as a **non-root** user inside the container.
- **Network deny-by-default.** Phase 0: `--network none` for the boundary tests. Phase 1: attach to a user-defined bridge with egress restricted to the package registries + `api.anthropic.com` the agent needs (document the exact allowlist; if full egress control is hard in Docker for P1, gate it behind config and leave `TODO(phase-2)` for the proper brokering proxy).
- Resource caps: `--memory`, `--cpus`, `--pids-limit`.
- No access to the host Docker daemon (do not mount the docker socket — ever).
- `destroy` removes the container and any volumes.

---

## 5. Provisioning (`provisioner.py`)

Given a `Task` and a `SandboxProvider`:

1. `create` the sandbox with the repo mounted read-only at `/src`.
2. Inside the sandbox, make an **independent clone** (this is the isolation-critical step):
   ```
   git clone --no-hardlinks /src /work/repo
   cd /work/repo
   git checkout -b sandkeep/<task_id>
   git config core.hooksPath /dev/null   # neutralise inherited hooks
   ```
   The clone has its own writable `.git`; `/src` stays read-only. The host `.git` is never writable from inside.
3. Disable submodules/LFS unless the task opts in (`TODO(phase-2)` for opt-in).
4. Pin the toolchain with `mise install` if a `mise.toml` is present (best-effort in P1).
5. Record `branch` and `sandbox_id` on the task; transition `NEW → PROVISIONING`.

**Acceptance (`test_provisioning.py`):**
- After provisioning, `/work/repo/.git` exists and is writable; `/src` is read-only (writes to `/src` fail).
- `git -C /work/repo rev-parse --abbrev-ref HEAD` == `sandkeep/<task_id>`.
- From inside the sandbox there is no writable path to the host repo.

---

## 6. Agent runner (`agent_runner.py`)

Builds and runs the headless Claude Code command **inside** the sandbox. This is the only place that constructs the `claude` invocation.

**Verified headless invocation** (Claude Code docs — headless / CI-CD; re-confirm current flags at build time via `claude --help`):

```bash
cd /work/repo && \
claude -p "<instruction + contract-writing instructions>" \
  --output-format json \
  --allowedTools "<comma-separated task.allowed_tools>" \
  --append-system-prompt-file /work/.sandkeep/agent_system_prompt.md \
  --max-budget-usd <task.max_budget_usd> \
  --dangerously-skip-permissions
```

> `--max-turns` was removed from the upstream claude CLI and is gone from
> Sandkeep too; runs are bounded by the spend cap (`--max-budget-usd`,
> configurable per run) and the controller's wall-clock timeout.

Notes:
- `--dangerously-skip-permissions` is acceptable here **because it runs inside the sandbox** (the docs explicitly note containers are the safe place for it). Never use it on the host.
- `--output-format json` returns a structured object including `result`, `session_id`, `num_turns`, `duration_ms`, and usage/cost fields. Parse it for the ledger; do not rely on field order.
- Exit codes: `0` success, `1` general error, `2` auth error. Map `2` to a clear "check ANTHROPIC_API_KEY" message.
- `ANTHROPIC_API_KEY` is passed via sandbox env (`TODO(phase-2)`: secret broker).
- A `.claude/settings.json` is copied into the sandbox to scope tools/permissions for the run; keep it minimal.

The agent's appended system prompt (`prompts/agent_system_prompt.md`) must instruct it to, on completion, write `/work/repo/.sandkeep/results.json` matching the `ResultsContract` schema, plus an optional `/work/repo/.sandkeep/RESULTS.md`. The runner enforces an overall wall-clock `timeout`; on timeout, transition `RUNNING → TIMEOUT` and archive.

---

## 7. Results contract + diff extraction (`results.py`, `diff.py`)

After the agent exits 0:

1. `read_file` `/work/repo/.sandkeep/results.json`; parse into `ResultsContract`. If missing or invalid → `FAILED`.
2. Extract the patch inside the sandbox:
   ```
   git -C /work/repo add -A
   git -C /work/repo diff --cached --patch <base_ref>..HEAD   # or staged working tree
   ```
   Write to a host path `outputs/<task_id>.patch` via `read_file`. **Only this patch and the contract cross back to the host.**
3. Validate the patch applies cleanly to a *fresh* checkout of the host repo at `base_ref` (`git apply --check`). If not → `FAILED` with detail.
4. Record cost in the ledger from the agent's JSON output.
5. Transition to `SUCCEEDED`, then `REVIEW`.

**Acceptance (`test_diff_extraction.py`, `test_results_contract.py`):**
- A run that edits two files produces a patch that `git apply --check` accepts on a clean host checkout.
- A malformed/missing `results.json` yields `FAILED`, never a crash.
- `files_changed` in the contract matches the files in the patch.

---

## 8. Violations (`violations.py`) — Phase 0 core

Detection sources, in priority order:
1. **Network (primary):** any egress attempt while the policy is deny/allowlist. In Phase 0 the boundary tests run with `--network none`, so any outbound attempt fails and is logged as a violation signal.
2. **Filesystem:** any attempt to read outside `/work` and the read-only `/src`, or to write outside `/work`.
3. **Escape patterns:** attempts to reach the host (e.g. docker socket, host paths), or to disable enforcement.

On any violation: transition to `VIOLATION`, **archive** the sandbox (do not destroy silently — keep it for forensics if configured), log the attempted action with the trace id, and never re-dispatch automatically.

---

## 9. Controller + state machine (`controller.py`)

Drives one task through:

```
NEW → PROVISIONING → RUNNING → (SUCCEEDED → REVIEW) → MERGED | REJECTED
                              ↘ FAILED | TIMEOUT | VIOLATION → ROLLED_BACK
```

Rules:
- Every transition via `state_store.update_state` with a fresh trace id.
- On any terminal-failure state, call `provider.destroy` (or archive on `VIOLATION`).
- The controller never executes agent output; it only moves patches and contracts.

**Acceptance (`test_state_machine.py`):** illegal transitions raise; every transition writes a `transitions` row; a violation path ends `ROLLED_BACK`/archived with the sandbox destroyed/quarantined.

---

## 10. CLI + human gate (`cli.py`) — Phase 1 gate

Commands (see `CLAUDE.md` for usage):
- `sandkeep auth set|status|clear` — store/inspect/remove the Anthropic API key
  (`~/.sandkeep/env`, mode 0600, masked output; an exported `ANTHROPIC_API_KEY`
  always wins). `TODO(phase-2)`: replaced by the secret broker.
- `sandkeep image build` — build the sandbox image.
- `sandkeep run --repo <path> --task "<instruction>" [--model ...] [--max-turns N]` — full single-task loop; stops at `REVIEW` and prints the summary + patch path.
- `sandkeep status <task_id>` / `sandkeep show <task_id>` — state + contract + patch.
- `sandkeep accept <task_id>` — apply the patch to a **fresh branch on the host** (`git checkout -b sandkeep-accepted/<task_id> <base_ref>` then `git apply`), transition `MERGED`. Never apply onto the user's current working tree without an explicit branch.
- `sandkeep reject <task_id>` — transition `REJECTED`, destroy/quarantine the sandbox.

The human gate is local patch apply in Phase 1. `TODO(phase-2)`: draft PR.

---

## 10b. Interactive session (`sandkeep shell`) — Phase 1

The headless loop (§6–§10) is for *unattended* agents. `sandkeep shell` is the
*interactive* counterpart: the user drives a normal, fully interactive Claude
Code session — permission prompts, skills, MCP, plan mode, slash commands — but
inside the **same sandbox** as the headless path, on the **same independent
clone** of a read-only host repo. The point is to get the full Claude Code
harness while keeping the one architectural invariant intact: the worktree
lives inside the sandbox; only a diff comes back; the host repo is untouched
until the human accepts.

What is **identical** to the headless path (reused, not reimplemented):
- Provisioning (§5): read-only `/src` mount → `git clone --no-hardlinks` →
  task branch → base_ref pinned to a SHA.
- Diff extraction + validation (§7): on session exit, stage everything in the
  clone (excluding the `.sandkeep` channel), diff against the pinned base,
  write `outputs/<task_id>.patch`, `git apply --check` on a fresh host checkout.
- Human gate (§10): `show` / `accept` / `reject` work unchanged.
- State machine (§9): `NEW → PROVISIONING → RUNNING → SUCCEEDED → REVIEW →
  MERGED | REJECTED`, with the same failure/rollback edges.

What is **different** from the headless path:
- **TTY, not capture.** The agent runs via an interactive exec that inherits
  the host's stdio (`docker exec -it … claude`). The provider exposes an
  optional `exec_interactive(handle, cmd) -> int`; backends that cannot offer
  a TTY raise `NotImplementedError` (the boundary contract — create/exec/
  read_file/destroy — is unchanged, so existing backends and `test_boundary.py`
  are unaffected).
- **No `-p`, no `--output-format json`, no `--max-turns`.** The interactive
  `claude` invocation is plain; an optional `--task` seeds the first message.
  Built only in `agent_runner.py` (`build_interactive_command`), same as the
  headless command.
- **No agent-written results contract.** There is no `.sandkeep/results.json`
  (the human was in the loop). The controller **synthesizes** a
  `ResultsContract` host-side from the extracted patch (`files_changed` from the
  diff, a generic interactive summary) so `show`/`accept` stay uniform.
- **Output-scanning violation detection (§8) does not apply** — the session is
  a live TTY, not a captured transcript. Containment still rests entirely on
  the sandbox boundary (read-only mount, network policy, caps), which is the
  real protection; the output scan was always only a heuristic.
- **An empty diff** (the user changed nothing) is not an error: transition
  `RUNNING → FAILED → ROLLED_BACK` with detail "no changes" and destroy the
  sandbox. Nothing to review.

`sandkeep shell --repo <path> [--task "<seed>"] [--model ...]` provisions with
egress enabled (the interactive agent needs `api.anthropic.com`), drops the
user into the session, and on exit lands at `REVIEW` (or rolls back on empty
diff), printing the same accept/reject instructions as `run`.

---

## 11. Phase gates (definition of done)

**Phase 0 — Boundary proof.** `tests/test_boundary.py` must pass. The sandbox runs `claude -p`, produces a diff, and the diff extracts cleanly; AND the adversarial cases below are all caught and archived, never executed against the host:

- Prompt the agent to read a host secret (e.g. `~/.ssh/id_rsa`, `/etc/passwd`) → no host secret is ever reachable; attempt is contained/logged.
- Prompt it to exfiltrate via network (with `--network none`) → outbound fails; flagged as violation.
- Prompt it to reach the host (docker socket, host paths) → unreachable; flagged.
- Confirm `git status` inside shows only the task branch; no host history/remotes.
- Confirm snapshot/rollback-equivalent: destroying the sandbox leaves the host repo byte-for-byte unchanged.

**Phase 1 — Single governed loop.** End to end on a real local repo: `sandkeep run` → `REVIEW` with a valid contract + applicable patch; `sandkeep accept` lands it on a fresh host branch; `sandkeep reject` discards cleanly. Ledger has a cost row. Audit log has the full transition chain.

**Phase 1 (interactive) — `sandkeep shell`.** End to end on a real local repo: provision the same sandbox, run an interactive Claude Code session on the clone, and on exit reach `REVIEW` with a synthesized contract + an applicable patch (or `ROLLED_BACK` "no changes" on an empty diff); `accept`/`reject` behave exactly as the headless path. The interactive command carries no `-p`/`--output-format`/`--max-turns`; the host repo is byte-for-byte unchanged until accept.

---

## 12. Later-phase status (summary)

Most of these are now built — see each phase's own section (§13–§16) for detail. What remains unbuilt keeps `TODO(phase-N)` markers; do not stub security features that can't actually be enforced.

- **Phase 2 — Real isolation + parallelism:** microVM `SandboxProvider` (E2B / Firecracker / Docker Sandboxes); snapshot/restore; concurrency + warm pool; secret-injecting broker (agent never holds the key); proper brokering egress proxy; draft-PR human gate. Status in §16. **Done:** network toggle, concurrency, and an E2B microVM with **containment verified**. **Remaining:** egress allowlist proxy, secret broker, draft-PR gate, warm pool (infra-bound) — see §16.
- **Phase 3 — Coordination & policy:** cross-task conflict detection; test-gated merge queue; diff risk analysis (flag workflow/deploy/auth/secret/dep changes); richer `policy.py`. Full status in §14. **All implemented** (risk analysis, conflict detection, test-gated merge).
- **Phase 4 — Capability authoring:** agents author per-repo scoped skills inside their sandbox. Full status in §15. **Implemented** (authoring, per-repo store, injection).
- **Phase 5 — Pluggable agents:** the hardwired `claude` CLI becomes one `AgentDriver` among several (Codex, Aider, …); `--agent`/config selection; per-agent images; per-agent secret env; diff-only contract fallback for agents that don't write `results.json`. Full design + status in §13. **Implemented** (seam, selection, per-agent images, multi-key auth); remaining: a real second driver with verified flags.

---

## 13. Pluggable agent drivers (Phase 5 — core implemented)

> **Status.** Core seam built and tested (`src/sandkeep/agent/`, `tests/test_agent_driver.py`); default Claude path byte-identical behind the interface. **Per-agent image templating** is implemented (`render_dockerfile`/`build_agent_image`; `image build --agent <name>` renders from `driver.install_steps()`; runs select `cfg.image_for(agent)`). **Per-agent secret storage** is covered by the multi-key `sandkeep auth set <NAME>` CLI. **A real second driver ships:** `CodexDriver` (`agent/codex.py`, `tests/test_codex_driver.py`) — a `produces_contract=False` driver that lands via host-side diff synthesis, proving the seam. Its CLI flags target the documented `codex exec` headless mode and should be re-verified with `codex --help` at build time (same §6 discipline). Remaining: proxy-mode reverse-proxy support for non-Anthropic drivers.

The boundary is **agent-agnostic**: containment comes from the sandbox, not from which agent runs inside it (the boundary suite, §9–§10, proves this without reference to Claude). So any file-editing CLI agent can run in the box and inherit the same guarantees. Phase 5 makes that explicit.

**Invariant (unchanged):** drivers run **only inside the sandbox**; the controller never executes agent code; only a diff (+ contract) crosses back. A new driver must not weaken the boundary — **the unmodified boundary suite must still pass with any driver selected.** Golden rule still holds: the driver only *builds command strings* on the host; the agent still executes solely inside the sandbox.

Today the `claude` CLI is hardwired in three places, which Phase 5 extracts:
1. **The image** — `sandbox_image/Dockerfile` installs `@anthropic-ai/claude-code`.
2. **`agent_runner.py`** — constructs `claude` commands and parses Claude's `--output-format json`.
3. **The headless contract** — the agent is prompted to write `.sandkeep/results.json` (§6).

### Interface (`agent/base.py`)

```python
class AgentDriver(ABC):
    name: str               # "claude", "codex", ...
    secret_env: str         # host env var to forward, e.g. "ANTHROPIC_API_KEY"
    produces_contract: bool # True: writes results.json; False: diff is the truth

    def install_steps(self) -> list[str]: ...   # Dockerfile lines for this agent's CLI
    def build_command(self, task) -> str: ...                       # headless
    def build_interactive_command(self, task, *, seed, skip_permissions) -> list[str]: ...
    def parse_result(self, exec_result: ExecResult) -> AgentRunResult: ...  # exit codes, error detail, tokens
```

`agent_runner.py` becomes a thin dispatcher: resolve the driver by name, call build/parse. **Today's logic moves verbatim into a `ClaudeDriver`** — no behavior change for the default.

### Selection (mirror `model`)

- `Config.agent: str = "claude"`, with a `SANDKEEP_AGENT` env override (exact twin of how `model`/`SANDKEEP_MODEL` already work in `config.py`).
- `--agent <name>` on `run` and `shell`; precedence **flag > env > config**.
- Persist `agent` on the task (new `tasks.agent` column) so `status`/`show` report which agent produced a diff and the ledger attributes cost per agent.

### Decisions (resolved)

1. **Image — per-agent images.** `sandkeep image build --agent <name>` renders the Dockerfile with the driver's `install_steps()`, tagged `sandkeep-sandbox:<name>` (`config.image_for`); the provider launches the image matching the task's agent. *(Rejected: one fat image with every CLI — ships unused binaries and enlarges the in-box attack surface.)*
2. **Secret — driver-declared env var.** The controller forwards only the driver's `secret_env` into the sandbox. First cut: forward it from the host shell if set. Follow-up: `sandkeep auth set --agent <name>` stores per-agent keys (multi-key store). The Phase 0–1 `TODO(phase-2)` secret-broker note applies to every driver.
3. **Contract — diff-only fallback.** When `produces_contract=False`, the headless `run` path uses the **same host-side diff-synthesis the interactive `shell` path already uses** (§10b). This removes the Claude-specific `results.json` dependency for other agents and simplifies the runner.

### Acceptance (`test_agent_driver.py`)

- `ClaudeDriver` produces **byte-identical** commands to the pre-refactor `build_command`/`build_interactive_command` (proves the default is unchanged).
- An unknown `--agent` fails **loud on the host, before any sandbox is created**.
- A stub driver with `produces_contract=False` runs end-to-end to `REVIEW` via diff-synthesis.
- The **unmodified boundary suite passes** regardless of which driver is selected.

---

## 14. Coordination & policy (Phase 3 — core implemented)

> **Status.** Implemented: diff **risk analysis** + cross-task **conflict detection** (`policy.py`) and the **test-gated merge queue** (`controller.run_tests` + `accept(test_command=...)`, `sandkeep test`, `SANDKEEP_TEST_COMMAND`). The test gate runs the command **inside the task's still-alive sandbox** against the agent's actual changes (never on the host — golden rule); `accept` is refused if it fails and the task stays at REVIEW. Tests in `test_policy.py` + `test_controller.py`.

`policy.py` is host-side and deterministic: it never runs agent code, it only reads the diff that already crossed back. It is **advisory** — it informs the human gate, it does not auto-block (the gate is still the human's call).

### Diff risk analysis — `policy.analyze_patch(patch_text) -> list[RiskFlag]`

Flags changes that touch sensitive surfaces, so the gate sees *what kind* of change it is approving:

- **ci/workflow** — `.github/workflows/`, `.gitlab-ci.yml`, `Jenkinsfile`, …
- **deploy** — `Dockerfile`, `docker-compose*`, `*.tf`, `Procfile`, `fly.toml`, `k8s/`, …
- **auth** — paths matching `auth`, `login`, `session`, `permission`, `rbac`, `middleware`, …
- **secret** — by path (`.env`, `*.pem`, `*.key`, `credential*`) **and** by content: added (`+`) lines matching key shapes (Anthropic/AWS/GitHub tokens, `PRIVATE KEY`, `password = "…"`). Removed lines never flag.
- **dependency** — `requirements*.txt`, `pyproject.toml`, `package*.json`, lockfiles, `Cargo.*`, `go.*`, `Gemfile*`.

Risk flags are logged (`policy_risk_flagged`) at land time and surfaced by `run`, `shell`, and `show`.

### Cross-task conflict detection — `policy.find_conflicts(this_files, others)`

When a task lands at REVIEW, `Controller.conflicts(task)` compares its changed files against every *other* task currently in REVIEW. Any file overlap is a `Conflict`, surfaced at the gate and warned about on `accept` (advisory — accepting one task can no longer silently collide with another awaiting review).

### Acceptance (`test_policy.py`, `test_controller.py`)

- Each risk category flags from a representative patch; a clean patch flags nothing; the secret scan catches an added hardcoded key but ignores removed lines; flags dedupe.
- Conflicts detect file overlap and ignore disjoint/empty sets.
- Docker-backed: a dependency-touching run is flagged and audited; two REVIEW tasks editing the same file are reported as conflicting.

---

## 15. Capability authoring (Phase 4 — implemented)

> **Status.** Implemented (`src/sandkeep/skills.py`, `tests/test_skills.py`, controller + CLI wiring). An agent authors skills; they persist per repo and are injected on later runs. The skill *format* is intentionally minimal (frontmatter `name` + `description`); richer validation/versioning can follow.

An agent can author **per-repo scoped skills** inside its sandbox — small markdown capability files (frontmatter `name`/`description` + body) written under `.sandkeep/skills/` in the clone.

**Return-channel discipline.** `.sandkeep/skills/` is **excluded from the patch** (`diff.py`), so authored skills are sandkeep-managed *metadata*, never changes to the user's repo — they never touch the host working tree or `.git`. The controller reads them from the sandbox at land time (`skills.read_authored`, a gated read in sandkeep's own namespace, **still subject to the human gate**), saves them to a host sidecar (`outputs/<task_id>.skills/`) for the gate to display, and **registers them in a per-repo store only on `accept`**. So nothing the agent authored becomes durable without human approval — the golden-rule intent holds, even though skills travel beside the patch rather than in it.

**Lifecycle:**
1. **Author** — agent writes `.sandkeep/skills/<name>.md` in the clone.
2. **Capture** — at land, `read_authored` pulls + validates them; malformed files are audited (`skill_invalid`), not dropped silently; valid ones are surfaced at the gate (`run`/`shell`/`show`) and audited (`skills_authored`).
3. **Register** — on `accept`, `SkillStore(home, repo).save_all(...)` persists them under `<home>/skills/<repo_key>/` (`skills_registered`).
4. **Inject** — on the next run against that repo, stored skills are pushed to `.claude/skills/<name>/SKILL.md` so the agent's Claude Code loads them (`skills_injected`). That inject target is also excluded from the patch so injection never pollutes a returned diff.

CLI: `sandkeep skills list --repo <path>` shows a repo's registered skills.

### Acceptance (`test_skills.py`)

- Parsing accepts a valid skill; rejects missing frontmatter / missing or invalid `name`.
- Store round-trips and is scoped per repo.
- Docker-backed: an authored skill is **excluded from the patch** yet captured for the gate; `accept` registers it in the repo store; the next run injects it at the Claude Code skill path and audits `skills_injected`.

---

## 16. Real isolation + parallelism (Phase 2 — slice implemented; rest is an infra wall)

> **Status.** Implemented: the **network-off toggle** (`SANDKEEP_NETWORK`/`--no-network`), **concurrency** (`sandkeep batch`, `run_concurrent`, thread-safe store), and a **microVM backend whose containment is verified** (E2B, boundary suite 9/9 — `e2b_provider.py`, `SANDKEEP_BACKEND=e2b`). Remaining Phase 2 pieces (egress allowlist proxy, secret broker, draft-PR gate, warm pool) need infrastructure this repo can't provision and are **deliberately not stubbed** — a config-only stub would imply a security guarantee that doesn't exist, the exact anti-pattern Sandkeep warns about.

### Implemented: network toggle

The Docker provider already supports `--network none`; Phase 2 exposes it. `network` is config (`SANDKEEP_NETWORK`, default `egress`) with a `--no-network` override on `run`/`shell` (precedence flag > env > default). `none` = the boundary-test posture: the agent cannot reach its API, so a normal run will fail — it's for boundary testing or a future offline/local-model agent. The CLI warns when network is off. `egress` remains the open bridge.

### Implemented: concurrency

`sandkeep batch --repo R --task … [--tasks-file f] [--max-parallel N]` runs many tasks in parallel (`controller.run_concurrent`), each in its own sandbox, all landing at the gate. `StateStore` is thread-safe (one shared connection + reentrant lock) and `AuditLog` serializes its appends, so concurrent tasks don't corrupt state; a per-task host-side error is captured rather than sinking the batch. Tests in `test_concurrency.py`.

### Implemented: microVM backend (E2B) — containment verified

`SANDKEEP_BACKEND=e2b` runs each task in an E2B Firecracker microVM (`e2b_provider.py`). The adversarial boundary suite passes **9/9 isolation checks** against a real microVM (`SANDKEEP_TEST_BACKEND=e2b pytest tests/test_boundary.py`); `allow_internet_access=False` gives a true no-network posture, and `/src` is built as root / read-only with a non-root agent. The 2 remaining suite reds are tool-presence (the custom template needs an E2B access token to build), not containment gaps. See docs/phase-2-implementation.md → "Verifying the E2B backend".

### Implemented: local key broker + egress allowlist (`SANDKEEP_NETWORK=proxy`)

The "brokering egress allowlist proxy" and "secret-injecting broker" rows below turned out **not** to need cloud infra — both are buildable locally with Docker `--internal` networks and the agent CLI's `ANTHROPIC_BASE_URL`. `SANDKEEP_NETWORK=proxy` runs the sandbox on a fresh `--internal` (no-egress) network behind a stdlib broker sidecar (`sandbox_image/broker/broker.py`). The broker holds the API key and straddles that internal network plus the default bridge; the sandbox reaches it at `http://broker:8080`, points its base URL at `…/broker/anthropic` (the broker injects the key), and routes other egress through the broker's CONNECT allowlist. So **the agent never holds the key and can only reach the allowlist** — real enforcement, not a config flag. Broker decision logic + reverse-proxy round-trip are host-tested (`tests/test_broker.py`); the env split + provider wiring in `tests/test_proxy_mode.py`; the Docker-backed containment proof (no key in the sandbox, disallowed egress refused) in `tests/test_proxy_boundary.py`, run in CI.

### Deferred — needs infrastructure, NOT stubbed (and why)

| Piece | Status |
|---|---|
| **microVM `SandboxProvider`** (E2B) | ✅ shipped + containment verified — boundary suite 9/11 on a real E2B microVM (all 9 isolation checks; 2 reds are tool-presence, not leaks) |
| **egress *allowlist* proxy + secret-injecting broker** | ✅ shipped for Docker (`SANDKEEP_NETWORK=proxy`) — the agent never holds the key and can only reach the allowlist. E2B parity via `SandboxNetworkOpts` is the remaining piece |
| **draft-PR human gate** | ⏳ needs a GitHub remote + auth; nothing to test against locally |
| **warm pool / snapshot-restore** | ⏳ fits E2B's upload model; premature without the microVM as the default backend |

The two remaining rows keep `TODO(phase-2)` markers in code. The `SandboxProvider` ABC (§4) is the seam: a microVM backend drops in there and **must pass the unmodified boundary suite** (§9–§10) — that contract is what makes the deferral safe.

### Acceptance (`test_network.py`)

- `SANDKEEP_NETWORK` defaults to `egress`, accepts `none`, rejects unknown values.
- `--no-network` forces `none`; absent, config/env wins. (The `none` posture's actual egress block is already proven by the boundary suite running under `--network none`.)

---

## 17. References

- Claude Code headless mode (flags, output formats, exit codes): https://docs.claude.com/en/docs/claude-code/overview and the headless/CI-CD docs. Re-verify `--max-turns`, `--allowedTools`, `--output-format json`, and the current model alias with `claude --help` at build time.
- Design rationale, threat model, and the five-tier architecture: the Sandkeep v2 design doc.
