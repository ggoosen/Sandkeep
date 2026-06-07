# Sandkeep

> Run Claude Code agents in isolated, disposable sandboxes — one per task, with cheap rollback and a human gate before anything merges.

[![CI](https://github.com/ggoosen/Sandkeep/actions/workflows/ci.yml/badge.svg)](https://github.com/ggoosen/Sandkeep/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sandkeep.svg)](https://pypi.org/project/sandkeep/)
[![License](https://img.shields.io/badge/license-PolyForm%20Noncommercial-blue.svg)](LICENSE)

## ⚠️ Security status — read before using

Sandkeep is **alpha**. The current sandbox backend is **Docker, which is NOT a
security boundary** — it is a mechanics harness. A determined or compromised
agent can escape a container. **Do not point Sandkeep at code or agents you
genuinely do not trust yet.** Real containment (a microVM backend) is planned and
will be announced explicitly when it ships. See [SECURITY.md](SECURITY.md).

## What it is

Sandkeep orchestrates headless Claude Code agents so each task runs in its own
disposable sandbox. The git worktree lives **inside** the sandbox; only a diff
comes back out; a human approves before anything touches your real repo. The
point is to run agents on real code without trusting them, and to walk back
anything that goes wrong for free.

## What it is not

- **Not an agent framework.** You bring the agent (Claude Code); Sandkeep governs where it runs.
- **Not production-grade isolation yet.** See the security status above.
- **Not autonomous merge.** A human gates every change, by design.

## Sandkeep or Cordon? — pick by how much you trust the run

Sandkeep has a lighter-weight sibling, **[Cordon](#cordon--the-native-sibling)**,
that enforces the *same* isolate → review → gate workflow using only Claude
Code's native features (worktrees, sandbox, hooks, a governing `CLAUDE.md`) — no
Docker. They sit at two points on one **trust dial**:

```
 TRUST HIGH ───────────────────────────────────► TRUST LOW
        CORDON (native discipline)        SANDKEEP (real containment)
   "I trust the agent — keep its work     "I don't trust the agent or the code —
    isolated, reversible, reviewed"        keep it away from my machine"
```

| Use **Cordon** when… | Use **Sandkeep** when… |
|---|---|
| Your devs, your code, you want every session isolated/reviewable by default | The agent **or the code** is untrusted (3rd-party, a fork, a sketchy dep) |
| You want full native speed and the complete Claude Code harness | You need a boundary you can point to in a security review |
| No container runtime available | The run is **unattended** (CI, batch, a fleet) |
| "Keep me safe from mistakes" | "Keep it away from my machine" |

They share one vocabulary — isolate → review → gate, `accept`/`reject`, an audit
trail — so a developer learns it once and slides along the dial as trust
changes. **Cordon is the everyday driver; Sandkeep is the vault you escalate to.**

## Quickstart

```bash
pip install sandkeep                 # or: uvx sandkeep
sandkeep image build                 # build the sandbox image (Node 22 + claude CLI + git + mise)
sandkeep auth set                    # store your Anthropic API key (hidden prompt, 0600)
                                     # (or: export ANTHROPIC_API_KEY=sk-ant-... — env always wins)

# unattended: hand it one task, get back a reviewable diff
sandkeep run --repo /path/to/repo --task "Add input validation to parse_config()"

# OR interactive: a full Claude Code session (chat, skills, MCP, plan mode)
# inside the sandbox, on a throwaway clone — exits at the same review gate
sandkeep shell --repo /path/to/repo

sandkeep show <task_id>              # review summary + patch + risk flags + conflicts
sandkeep accept <task_id>            # apply to a fresh branch on your repo
# or
sandkeep reject <task_id>            # discard and tear down the sandbox

sandkeep skills list --repo /path/to/repo   # skills the agent authored for this repo
```

Useful flags on `run`/`shell`:

| Flag | Effect |
|---|---|
| `--agent <name>` | pick which agent runs in the sandbox (default `claude`; also `SANDKEEP_AGENT`) |
| `--no-network` | run with **no network at all** (agent can't reach its API; for boundary testing / offline agents; also `SANDKEEP_NETWORK=none`) |
| `--no-skip-permissions` | (`shell`) restore Claude Code permission prompts |
| `--model <id>` | override the task-tier model |

Two ways to run the agent: **`run`** (headless, fire-and-forget — good for
unattended/CI) and **`shell`** (interactive — the full harness on a disposable
clone). Both end at the same human gate; nothing touches your real repo until
you `accept`.

**Permissions are skipped by default.** Both `run` and `shell` launch Claude
Code with `--dangerously-skip-permissions`, so the agent never stops to ask you
to approve a tool call. This is deliberate: the session runs **inside the
sandbox** on a throwaway clone, so the permission prompt would only add friction
without adding a boundary — the containment is the boundary. To restore the
prompts in an interactive session, run `sandkeep shell --no-skip-permissions`.
(This flag is host-dangerous and is only ever used *inside* the sandbox — never
on your machine.)

See [`examples/quickstart.md`](examples/quickstart.md) for an end-to-end first run on a throwaway repo.

## How it works

1. **Provision** — your repo is mounted **read-only**; the sandbox makes its own independent clone on a task branch.
2. **Run** — a headless Claude Code agent works only inside the sandbox, with a scoped tool set.
3. **Extract** — only a patch + a structured results contract leave the sandbox.
4. **Gate** — you review; on accept, Sandkeep applies the patch to a **fresh branch** on your repo. Nothing touches your working tree or `.git` until you say so.

## What the gate shows you

Every run lands at the human gate with more than just a diff:

- **Diff risk flags** — changes touching sensitive surfaces are called out by category: `ci/workflow`, `deploy`, `auth`, `secret`, `dependency`. The secret check scans both file paths *and* added lines (Anthropic/AWS/GitHub tokens, private keys, hardcoded credentials). You see *what kind* of change you're approving, not just a file list.
- **Cross-task conflicts** — if another task awaiting review touches the same files, the gate says so (and `accept` warns), so approving one can't silently collide with another in flight.

It's advisory — the human still decides. Nothing auto-blocks, nothing auto-merges.

## Pluggable agents

The boundary is **agent-agnostic** — containment comes from the sandbox, not from which agent runs inside it. Agents live behind a single `AgentDriver` interface (`claude` is the built-in default), selectable with `--agent` / `SANDKEEP_AGENT`. Adding another CLI agent (e.g. Codex) is a small, contained change: register a driver and teach the image to install its CLI. An unknown agent fails loud on the host before any sandbox is created.

## Capability authoring (per-repo skills)

An agent can **author skills** while it works — small markdown capability files written under `.sandkeep/skills/`. These are sandkeep-managed metadata: they're **excluded from the patch** (they never land in your repo's working tree or `.git`), surfaced at the gate, and registered to a **per-repo store only when you `accept`**. On later runs against the same repo, stored skills are injected so the agent builds on what earlier runs learned. Inspect them with `sandkeep skills list --repo <path>`. Nothing the agent authored becomes durable without your gate.

## Extending it

Sandkeep talks to sandboxes through a single `SandboxProvider` interface. Adding a
new backend (microVM, remote sandbox service) is the main extension point — and the
contract is strict: **any backend must pass the unmodified boundary test suite**
(`tests/test_boundary.py`). See [CONTRIBUTING.md](CONTRIBUTING.md).

## Requirements

- Python 3.12+
- Docker (for the current sandbox backend)
- An Anthropic API key (`ANTHROPIC_API_KEY`)

## Platform support

The controller is pure-stdlib Python and the sandbox is always a Linux
container, so the host only needs Python, git, and a Docker daemon:

| Platform | Status |
|---|---|
| **macOS** (Intel & Apple Silicon) | ✅ **Tested** — full suite, incl. the boundary tests, runs green |
| **Linux** | ✅ Expected to work (native Docker; CI target) |
| **Windows — WSL2** | ✅ Recommended path on Windows; effectively the Linux case |
| **Windows — native** (PowerShell + Docker Desktop) | ⚠️ Untested. Likely works; known risk spots: drive-letter volume-mount syntax, TTY behaviour of `sandkeep shell`, and CRLF (`core.autocrlf`) rejecting sandbox-generated patches on `accept`. Issues welcome. |

Note for corporate users: Docker Desktop itself requires a paid subscription
above Docker's company-size thresholds (macOS and Windows). That's a Docker
constraint, not a Sandkeep one; the planned microVM backend (Phase 2) removes
the Docker dependency.

## Roadmap

- [x] Phase 0 — boundary proof (Docker mechanics)
- [x] Phase 1 — single governed task loop (`run` + interactive `shell`, human gate)
- [~] Phase 2 — real isolation + parallelism. **Done:** network-off toggle (`--no-network`). **Pending infra:** microVM backend, brokering egress-allowlist proxy, secret broker, draft-PR gate (need KVM/cloud/GitHub; deliberately not stubbed)
- [x] Phase 3 — diff risk analysis + cross-task conflict detection
- [x] Phase 4 — per-repo skill authoring
- [x] Phase 5 — pluggable agents (`--agent`, `AgentDriver` seam)

The build details for each phase live in [`BUILD_SPEC.md`](BUILD_SPEC.md) (§13–§16).

## Cordon — the native sibling

**Cordon** is the high-trust end of the dial: the same isolate → review → gate
discipline, enforced with Claude Code's own primitives instead of a container.
A governing `CLAUDE.md` shepherds the session, hooks block the escape hatches, the
Bash sandbox + worktree isolate the work, and review skills (`/cordon-review`,
`/cordon-accept`) gate the merge — all so every session in a Cordon project is
isolated, reversible, and reviewable **by default, with zero infrastructure**.

It's containment against *accidents and misbehavior*, not against an adversary —
when you genuinely don't trust the code, that's Sandkeep's job, and the two are
designed to hand off to each other.

The full build spec lives here: **[docs/native-harness-build-spec.md](docs/native-harness-build-spec.md)**.
*(Cordon ships from its own repo — link TBD once published.)*

## License

**Source-available** under the [PolyForm Noncommercial License 1.0.0](LICENSE) —
*not* an OSI open-source license. You may use, modify, and redistribute Sandkeep
freely for any **noncommercial** purpose (personal, research, education,
non-profit, evaluation).

**Commercial use of any kind requires a paid license.** If you want to use
Sandkeep in or for a commercial product, service, or business, contact
**info@elusivecoffee.com.au** to arrange one.

See [NOTICE](NOTICE) for attribution requirements.
