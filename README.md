# Sandkeep

> Run Claude Code agents in isolated, disposable sandboxes — one per task, with cheap rollback and a human gate before anything merges.

[![CI](https://github.com/<your-org>/sandkeep/actions/workflows/ci.yml/badge.svg)](https://github.com/<your-org>/sandkeep/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sandkeep.svg)](https://pypi.org/project/sandkeep/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

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

## Quickstart

```bash
pip install sandkeep                 # or: uvx sandkeep
sandkeep image build                 # build the sandbox image (Node 22 + claude CLI + git + mise)
export ANTHROPIC_API_KEY=sk-ant-...

sandkeep run --repo /path/to/repo --task "Add input validation to parse_config()"
sandkeep show <task_id>              # review the summary + patch
sandkeep accept <task_id>            # apply to a fresh branch on your repo
# or
sandkeep reject <task_id>            # discard and tear down the sandbox
```

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

## Roadmap

- [x] Phase 0 — boundary proof (Docker mechanics)
- [ ] Phase 1 — single governed task loop
- [ ] Phase 2 — microVM isolation, snapshots, parallelism, secret broker, draft-PR gate
- [ ] Phase 3 — conflict detection, diff risk analysis
- [ ] Phase 4 — per-repo skill authoring

## License

Apache-2.0. See [LICENSE](LICENSE).
