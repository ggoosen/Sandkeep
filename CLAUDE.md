# CLAUDE.md — Sandkeep

> Read this fully before writing code. Build strictly to `BUILD_SPEC.md`. Do not build ahead of the current phase.

## What this is

Sandkeep is an orchestrator that runs Claude Code agents in **isolated, disposable sandboxes**, one per task, with cheap rollback and a human gate before anything merges. The whole point is to run untrusted agents on real code without trusting them and to walk back anything that goes wrong for free.

The one architectural invariant: **the git worktree lives *inside* the sandbox, never on the host.** Only a diff comes back out. If you ever find yourself giving the sandbox write access to the host repo or host `.git`, stop — that breaks the entire design.

## Golden rules (do not violate)

1. **Scripts own the filesystem; agents own the code.** The controller (this codebase) is deterministic and runs on the host. It never runs agent-generated code. Agent code only ever executes inside a sandbox.
2. **Never writable-bind-mount the host repo or host `.git`.** Host repo is mounted **read-only**; the sandbox gets its own independent clone.
3. **Only the diff leaves the sandbox.** The return channel is a patch file + the results contract JSON. Nothing else crosses back.
4. **Build to the current phase only.** Phases 2–4 are roadmap. Do not implement microVMs, parallelism, PR integration, or a secret broker until their phase. Leave `TODO(phase-N)` markers instead.
5. **Every state transition is logged** to the audit log with a trace id. No silent state changes.
6. **Acceptance tests are the definition of done.** A phase is complete only when its acceptance tests in `tests/` pass. Write the test first if it's missing.

## Stack (locked — do not substitute)

- **Language:** Python 3.12+. Controller, CLI, state machine.
- **State:** SQLite (stdlib `sqlite3`). No ORM.
- **Sandbox backend (Phase 0–1):** Docker, behind a `SandboxProvider` ABC. This is a *mechanics harness, not a security boundary* — a real microVM provider replaces it in Phase 2. Keep all Docker specifics behind the interface.
- **Agent driver:** the `claude` CLI in headless mode, run **inside** the sandbox.
- **Packaging:** `pyproject.toml`, `uv` or `pip`. CLI entrypoint `sandkeep`. Package lives under `src/sandkeep/`.
- **Tests:** `pytest`.

Do not add a web framework, message queue, or cloud SDK in Phase 0–1. Keep dependencies minimal.

## How to run

```bash
# store the API key once (hidden prompt; ~/.sandkeep/env, 0600; env var wins)
sandkeep auth set

# build the sandbox image once
sandkeep image build

# run a single UNATTENDED task against a local repo
sandkeep run --repo /path/to/target-repo --task "Add input validation to parse_config()"

# OR: open an INTERACTIVE Claude Code session inside the sandbox (full harness),
# on an independent clone of the read-only repo; on exit it lands at the review gate
sandkeep shell --repo /path/to/target-repo            # optionally: --task "<seed first message>"

# inspect a task
sandkeep status <task_id>
sandkeep show <task_id>          # prints the results contract + patch path

# human gate
sandkeep accept <task_id>        # applies the patch to a fresh branch on the host
sandkeep reject <task_id>        # archives, rolls back the sandbox
```

## How to test

```bash
pytest -q                      # all
pytest tests/test_boundary.py  # the Phase 0 adversarial boundary suite — must pass before anything else
```

## Conventions

- Type hints everywhere; `dataclasses` for models. Keep functions small and pure where possible.
- All sandbox interaction goes through `SandboxProvider`; no `subprocess` calls to `docker` outside `sandbox/docker_provider.py`.
- All agent invocation goes through `agent_runner.py`; never construct `claude` command strings elsewhere.
- Structured logging only (JSON lines) via `audit.py`. No bare `print`.
- Fail loud and early on the host; never auto-retry a sandbox that raised a violation — archive it.

## Known, deliberate Phase 0–1 simplifications (not bugs)

- **`ANTHROPIC_API_KEY` is passed into the sandbox env.** This violates the "agent never holds the key" principle on purpose, to keep Phase 0–1 runnable. `TODO(phase-2)`: replace with a host-side secret-injecting proxy.
- **Docker, not a microVM.** Containment is *not* production-grade yet. `TODO(phase-2)`: implement a microVM `SandboxProvider`.
- **Human gate is local patch apply, not a PR.** `TODO(phase-2)`: push diff as a draft PR.
- **No parallelism.** One task at a time. `TODO(phase-2)`: concurrency + warm pool.

Flag these in code with the exact `TODO(phase-N)` markers so they're greppable.

## What "good" looks like for a task run

**Headless (`sandkeep run`):** provision read-only-mounted repo → independent clone on a task branch inside the sandbox → run the headless agent with scoped tools → agent writes the results contract → extract the patch → validate → present to the human gate → on accept, apply to a fresh host branch; on reject/violation, archive and discard the sandbox. Nothing touches the host repo's working tree or `.git` until the human accepts.

**Interactive (`sandkeep shell`):** same provisioning and same review gate, but the user drives a full interactive Claude Code session (TTY) on the clone instead of a captured headless run. No agent-written results contract — the controller synthesizes one host-side from the extracted patch. Output-scanning violation detection (§8) does not apply to a live TTY; containment rests on the sandbox boundary. Empty diff → rolled back as "no changes". See `BUILD_SPEC.md` §10b.
