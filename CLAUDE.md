# CLAUDE.md — Sandkeep

> Read this fully before writing code. Build strictly to `BUILD_SPEC.md` and, for current work, `docs/improvement-plan.md`. Do not build ahead of what those documents call for.

## What this is

Sandkeep is an orchestrator that runs Claude Code agents in **isolated, disposable sandboxes**, one per task, with cheap rollback and a human gate before anything merges. The whole point is to run untrusted agents on real code without trusting them and to walk back anything that goes wrong for free.

The one architectural invariant: **the git worktree lives *inside* the sandbox, never on the host.** Only a diff comes back out. If you ever find yourself giving the sandbox write access to the host repo or host `.git`, stop — that breaks the entire design.

## Golden rules (do not violate)

1. **Scripts own the filesystem; agents own the code.** The controller (this codebase) is deterministic and runs on the host. It never runs agent-generated code. Agent code only ever executes inside a sandbox.
2. **Never writable-bind-mount the host repo or host `.git`.** Host repo is mounted **read-only**; the sandbox gets its own independent clone.
3. **Only the diff leaves the sandbox.** The return channel is a patch file + the results contract JSON (plus sandkeep-managed skill metadata, human-gated — BUILD_SPEC §15). Nothing else crosses back.
4. **Build to the spec, not past it.** What remains unbuilt is listed under "Deliberate simplifications" below and in `docs/improvement-plan.md`. Do not stub a security feature a config flag can't actually enforce — an unenforced allowlist reads as "exfil is blocked" when it isn't. Leave `TODO(phase-N)` markers.
5. **Every state transition is logged** to the audit log with a trace id. No silent state changes.
6. **Acceptance tests are the definition of done.** A feature is complete only when its acceptance tests in `tests/` pass. Write the test first if it's missing. Any sandbox backend must pass the unmodified boundary suite (`tests/test_boundary.py`).

## Stack (locked — do not substitute)

- **Language:** Python 3.12+. Controller, CLI, state machine.
- **State:** SQLite (stdlib `sqlite3`). No ORM.
- **Sandbox backends:** behind the `SandboxProvider` ABC. Two exist: **Docker** (`sandbox/docker_provider.py`, the default — a *mechanics harness, not a security boundary*) and **E2B** Firecracker microVMs (`sandbox/e2b_provider.py`, `SANDKEEP_BACKEND=e2b`, optional `e2b` dependency). Keep all backend specifics behind the interface.
- **Agents:** pluggable behind the `AgentDriver` ABC (`agent/base.py`); `claude` (headless or interactive, run **inside** the sandbox) is the built-in default. Select with `--agent`/`SANDKEEP_AGENT`.
- **Packaging:** `pyproject.toml`, `uv` or `pip`. CLI entrypoint `sandkeep`. Package lives under `src/sandkeep/`.
- **Tests:** `pytest`.

Do not add a web framework, message queue, or ORM. Keep dependencies minimal — the default Docker path is stdlib-only.

## How to run

```bash
# store API keys once (hidden prompt; ~/.sandkeep/env, 0600; env var wins)
sandkeep auth set                    # ANTHROPIC_API_KEY
sandkeep auth set E2B_API_KEY        # any other named key (E2B backend, other drivers)
sandkeep auth status                 # every stored/active key, masked

# build the sandbox image once (per agent: image build --agent <name>)
sandkeep image build

# run a single UNATTENDED task against a local repo
sandkeep run --repo /path/to/target-repo --task "Add input validation to parse_config()"

# OR: open an INTERACTIVE Claude Code session inside the sandbox (full harness),
# on an independent clone of the read-only repo; on exit it lands at the review gate
sandkeep shell --repo /path/to/target-repo            # optionally: --task "<seed first message>"

# OR: many tasks in parallel, each in its own sandbox, all landing at the gate
sandkeep batch --repo /path/to/target-repo --task "fix A" --task "fix B" --max-parallel 4

# inspect a task
sandkeep status <task_id>
sandkeep show <task_id>          # results contract + patch path + risk flags + conflicts

# optional test gate: run the repo's tests INSIDE the task's sandbox
sandkeep test <task_id> --test-cmd "pytest -q"

# human gate
sandkeep accept <task_id>        # applies the patch to a fresh branch on the host
sandkeep reject <task_id>        # archives, rolls back the sandbox

# housekeeping + per-repo skills
sandkeep ps                      # live sandboxes + task state
sandkeep gc [--dry-run]          # reap orphaned/stale sandboxes
sandkeep skills list --repo /path/to/target-repo
```

Useful flags on `run`/`shell`: `--agent <name>`, `--no-network`, `--browser` (headless-Chromium CDP sidecar the agent drives), `--model <id>`, `--max-budget-usd <n>`; `shell` also takes `--no-skip-permissions`. `SANDKEEP_NETWORK=proxy` runs behind the key broker + egress allowlist.

## How to test

```bash
pytest -q                        # all (docker-backed tests skip if no daemon — the summary line tells you)
pytest -q --require-docker       # CI posture: a missing daemon FAILS instead of skipping
pytest tests/test_boundary.py    # the adversarial boundary suite — must pass before anything else
```

## Conventions

- Type hints everywhere; `dataclasses` for models. Keep functions small and pure where possible.
- All sandbox interaction goes through `SandboxProvider`; no `subprocess` calls to `docker` outside `sandbox/docker_provider.py`.
- All agent invocation goes through the `AgentDriver` seam: drivers in `agent/` build command strings, `agent_runner.py` dispatches. Never construct an agent command line anywhere else.
- Structured logging only (JSON lines) via `audit.py`. No bare `print`.
- Fail loud and early on the host; never auto-retry a sandbox that raised a violation — archive it.

## Deliberate simplifications (not bugs)

- **`ANTHROPIC_API_KEY` is passed into the sandbox env.** Violates "agent never holds the key" on purpose to stay runnable. Being removed by the local key-broker + egress-allowlist sidecar (`docs/improvement-plan.md` step 1); until then `TODO(phase-2)` markers stand.
- **Docker (the default) is not production-grade containment.** The E2B microVM backend exists and has its containment verified by the boundary suite; making a microVM the default (and warm pools/snapshots on top) is still open.
- **Human gate is local patch apply, not a PR.** `TODO(phase-2)`: push diff as a draft PR (needs a GitHub remote — deliberately not stubbed).
- **The output-scanning violation detector is a heuristic**, honest about being evadable; real egress detection arrives with the broker (improvement-plan steps 1 and 5).

Flag these in code with the exact `TODO(phase-N)` markers so they're greppable.

## What "good" looks like for a task run

**Headless (`sandkeep run`):** provision read-only-mounted repo → independent clone on a task branch inside the sandbox → run the headless agent with scoped tools → agent writes the results contract → extract the patch → validate → present to the human gate (risk flags, cross-task conflicts, optional in-sandbox test gate) → on accept, apply to a fresh host branch; on reject/violation, archive and discard the sandbox. Nothing touches the host repo's working tree or `.git` until the human accepts.

**Interactive (`sandkeep shell`):** same provisioning and same review gate, but the user drives a full interactive Claude Code session (TTY) on the clone instead of a captured headless run. No agent-written results contract — the controller synthesizes one host-side from the extracted patch. Output-scanning violation detection (§8) does not apply to a live TTY; containment rests on the sandbox boundary. Empty diff → rolled back as "no changes". See `BUILD_SPEC.md` §10b.
