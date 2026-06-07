# Contributing to Sandkeep

Thanks for looking at Sandkeep. This file covers dev setup, the rules that keep
the design honest, and — the part most people come here for — **how to add a new
sandbox backend** (e.g. a microVM provider for real containment).

## Dev setup

```bash
git clone https://github.com/ggoosen/Sandkeep
cd Sandkeep
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'        # editable install + pytest
pytest -q                      # full suite (docker-backed tests skip if no daemon)
```

You need Python 3.12+ and a Docker daemon for the integration tests. Tests that
require Docker skip cleanly when it's unavailable, so a no-Docker machine still
runs the pure-logic suite.

## The rules that don't bend

Read [`CLAUDE.md`](CLAUDE.md) — it's the governance contract, and a PR that
breaks a golden rule won't merge. The load-bearing ones:

1. **The git worktree lives *inside* the sandbox, never on the host.** Only a
   diff comes back out.
2. **Never writable-bind-mount the host repo or host `.git`.** Host repo is
   mounted **read-only**; the sandbox gets its own independent clone.
3. **Scripts own the filesystem; agents own the code.** The controller (this
   codebase) never executes agent-generated code. Agent code runs *only* inside
   a sandbox.
4. **Every state transition is logged** to the audit log with a trace id.

## Running the tests

```bash
pytest -q                       # everything
pytest tests/test_boundary.py   # the adversarial boundary suite — the contract
```

`tests/test_boundary.py` is special: it plays a hostile agent and proves nothing
reaches the host. **Any sandbox backend must pass it unmodified.** It is the
definition of "this backend is safe enough to ship."

## Extension points

### Adding a sandbox backend (the big one)

Sandkeep talks to sandboxes through one interface, `SandboxProvider`
(`src/sandkeep/sandbox/base.py`). The Docker backend
(`src/sandkeep/sandbox/docker_provider.py`) is the Phase 0–1 reference — a
*mechanics harness, not a security boundary*. Real containment (a microVM
backend) is **Phase 2**, and it drops in right here.

You implement four methods (plus one optional):

| Method | Contract |
|---|---|
| `create(repo_path, env)` | start a sandbox; mount the host repo **read-only** at `/src`; apply network policy; return a `SandboxHandle`. **Never** mount the host `.git` writable or the docker socket. |
| `exec(handle, cmd, timeout)` | run a command inside; return `ExecResult(exit_code, stdout, stderr)`; raise `SandboxExecTimeout` on timeout. |
| `read_file(handle, path)` | read a file from inside (used to pull the contract + skills); raise `FileNotFoundError` if absent. |
| `destroy(handle)` | tear down and discard **all** sandbox state. |
| `exec_interactive(handle, cmd)` *(optional)* | attach to the host TTY for `sandkeep shell`; return exit code. Backends that can't offer a TTY may leave it unimplemented. |

**Acceptance:** point the test `provider` fixture at your backend and run
`pytest tests/test_boundary.py`. Green = your backend honours the boundary. See
the step-by-step build guide, including a concrete **E2B microVM** walkthrough
and how the egress proxy / secret broker slot in:
**[docs/phase-2-implementation.md](docs/phase-2-implementation.md)**.

Keep all backend-specific code inside your provider module — the controller, CLI,
and everything else must stay backend-agnostic (they only see the ABC).

### Adding an agent (Phase 5 seam)

Agents are pluggable behind `AgentDriver` (`src/sandkeep/agent/base.py`); `claude`
is the default. To add one (e.g. Codex): implement a driver (command build,
result parse, install steps, `secret_env`, `produces_contract`), register it in
`src/sandkeep/agent/__init__.py`, and add its CLI to the sandbox image. Verify
its CLI flags against the real tool at build time — same discipline as the Claude
driver. See `BUILD_SPEC.md` §13.

## PR expectations

- **Build to the current phase only.** Phases beyond the current one are roadmap;
  leave `TODO(phase-N)` markers rather than building ahead (golden rule #4).
- **Tests are the definition of done.** New behaviour needs a test; a new backend
  must pass `tests/test_boundary.py` unmodified.
- **Don't fake guarantees.** A config flag or stub that *implies* a security
  property it doesn't enforce is worse than an honest gap — this is a security
  tool. If you can't enforce it yet, mark it `TODO` and say so.
- Type hints, `dataclasses` for models, structured logging via `audit.py`, no bare
  `print` outside the CLI.

By contributing you agree your contributions are licensed under the project's
[PolyForm Noncommercial License](LICENSE).
