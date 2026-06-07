# Sandkeep — Build Specification (Phases 0–1)

**Audience:** Claude Code (and the human reviewing its work).
**Scope of this spec:** Phase 0 (prove the boundary) and Phase 1 (single-task governed loop) in build detail. Phases 2–4 are roadmap stubs at the end — **do not build them yet.**
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
    policy.py              # Tier 1: scope grant (minimal in P0/P1)
    provisioner.py         # Tier 2: sandbox lifecycle
    agent_runner.py        # builds + runs the headless claude command
    diff.py                # extract / validate / apply patches
    results.py             # parse + validate the results contract
    violations.py          # violation detection + classification
    sandbox/
      __init__.py
      base.py              # SandboxProvider ABC
      docker_provider.py   # Phase 0 backend (the ONLY place docker is touched)
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
    max_turns: int = 8
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
  --max-turns <task.max_turns> \
  --allowedTools "<comma-separated task.allowed_tools>" \
  --append-system-prompt-file /work/.sandkeep/agent_system_prompt.md \
  --dangerously-skip-permissions
```

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

## 12. Roadmap stubs — DO NOT BUILD YET

Leave `TODO(phase-N)` markers; do not implement until the phase is started.

- **Phase 2 — Real isolation + parallelism:** microVM `SandboxProvider` (E2B / Firecracker / Docker Sandboxes); snapshot/restore; concurrency + warm pool; secret-injecting broker (agent never holds the key); proper brokering egress proxy; draft-PR human gate.
- **Phase 3 — Coordination & policy:** cross-task conflict detection; test-gated merge queue; diff risk analysis (flag workflow/deploy/auth/secret/dep changes); richer `policy.py`. Full status in §14. **Risk analysis + conflict detection implemented**; test-gated merge queue deferred (see §14).
- **Phase 4 — Capability authoring:** agents author per-repo scoped skills inside their sandbox.
- **Phase 5 — Pluggable agents:** the hardwired `claude` CLI becomes one `AgentDriver` among several (Codex, Aider, …); `--agent`/config selection; per-agent images; per-agent secret env; diff-only contract fallback for agents that don't write `results.json`. Full design + status in §13. **Core seam implemented**; two pieces deferred (see §13).

---

## 13. Pluggable agent drivers (Phase 5 — core implemented)

> **Status.** Core seam built and tested (`src/sandkeep/agent/`, `tests/test_agent_driver.py`). The default Claude path is byte-identical behind the new interface; the full suite incl. the boundary suite passes. **Deferred** (clearly bounded, not faked): (a) per-agent **image templating** — `image build --agent <name>` errors for non-default agents until the Dockerfile is rendered from `driver.install_steps()`; (b) per-agent **secret storage** — `auth set --agent <name>`; the first cut forwards `driver.secret_env` from the host shell. Shipping a real second driver (Codex/Aider) needs its CLI flags verified at build time, same discipline as §6.

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

1. **Image — per-agent images.** `sandkeep image build --agent <name>` renders the Dockerfile with the driver's `install_steps()`, tagged `sandkeep-img:<name>`; the provider launches the image matching the task's agent. *(Rejected: one fat image with every CLI — ships unused binaries and enlarges the in-box attack surface.)*
2. **Secret — driver-declared env var.** The controller forwards only the driver's `secret_env` into the sandbox. First cut: forward it from the host shell if set. Follow-up: `sandkeep auth set --agent <name>` stores per-agent keys (multi-key store). The Phase 0–1 `TODO(phase-2)` secret-broker note applies to every driver.
3. **Contract — diff-only fallback.** When `produces_contract=False`, the headless `run` path uses the **same host-side diff-synthesis the interactive `shell` path already uses** (§10b). This removes the Claude-specific `results.json` dependency for other agents and simplifies the runner.

### Acceptance (`test_agent_driver.py`)

- `ClaudeDriver` produces **byte-identical** commands to the pre-refactor `build_command`/`build_interactive_command` (proves the default is unchanged).
- An unknown `--agent` fails **loud on the host, before any sandbox is created**.
- A stub driver with `produces_contract=False` runs end-to-end to `REVIEW` via diff-synthesis.
- The **unmodified boundary suite passes** regardless of which driver is selected.

---

## 14. Coordination & policy (Phase 3 — core implemented)

> **Status.** Implemented: diff **risk analysis** and cross-task **conflict detection** (`src/sandkeep/policy.py`, `tests/test_policy.py`, controller + CLI wiring). **Deferred:** the **test-gated merge queue** — running the target repo's tests against a patch — because tests must run on agent-touched code, which the golden rule forbids on the host. It therefore needs a *sandboxed* test run (re-provision → apply patch → run a configured test command inside → gate on exit). That reuses Phase 1 provisioning but adds a per-repo test-command config; built when prioritized.

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

## 15. References

- Claude Code headless mode (flags, output formats, exit codes): https://docs.claude.com/en/docs/claude-code/overview and the headless/CI-CD docs. Re-verify `--max-turns`, `--allowedTools`, `--output-format json`, and the current model alias with `claude --help` at build time.
- Design rationale, threat model, and the five-tier architecture: the Sandkeep v2 design doc.
