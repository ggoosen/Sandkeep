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
export ANTHROPIC_API_KEY=sk-ant-...

# unattended: hand it one task, get back a reviewable diff
sandkeep run --repo /path/to/repo --task "Add input validation to parse_config()"

# OR interactive: a full Claude Code session (chat, skills, MCP, plan mode)
# inside the sandbox, on a throwaway clone — exits at the same review gate
sandkeep shell --repo /path/to/repo

sandkeep show <task_id>              # review the summary + patch
sandkeep accept <task_id>            # apply to a fresh branch on your repo
# or
sandkeep reject <task_id>            # discard and tear down the sandbox
```

Two ways to run the agent: **`run`** (headless, fire-and-forget — good for
unattended/CI) and **`shell`** (interactive — the full harness on a disposable
clone). Both end at the same human gate; nothing touches your real repo until
you `accept`.

See [`examples/quickstart.md`](examples/quickstart.md) for an end-to-end first run on a throwaway repo.

## How it works

1. **Provision** — your repo is mounted **read-only**; the sandbox makes its own independent clone on a task branch.
2. **Run** — a headless Claude Code agent works only inside the sandbox, with a scoped tool set.
3. **Extract** — only a patch + a structured results contract leave the sandbox.
4. **Gate** — you review; on accept, Sandkeep applies the patch to a **fresh branch** on your repo. Nothing touches your working tree or `.git` until you say so.

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
- [ ] Phase 1 — single governed task loop
- [ ] Phase 2 — microVM isolation, snapshots, parallelism, secret broker, draft-PR gate
- [ ] Phase 3 — conflict detection, diff risk analysis
- [ ] Phase 4 — per-repo skill authoring

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
