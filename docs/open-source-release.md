# Open-Source Release Guide

How to take the orchestrator from a working prototype to a credible public GitHub library. Read alongside `BUILD_SPEC.md` and `CLAUDE.md`. Replace the placeholder name `PROJECT` throughout.

> **Not legal advice.** Licensing and trademark notes below are informational; confirm anything binding with a professional.

---

## 0. The two blockers to clear first

### Name — decided: **Sandkeep**
The original codename "Burrow" was dropped (LinkedIn's established Apache-2.0 Kafka project owns that name). Short common words and the `molt*` family were also checked and found taken/crowded. **Sandkeep** (sandbox + *keep*, a fortified enclosure) was chosen for availability and fit. The install/CLI surface is `pip install sandkeep` and `sandkeep run`.

Before relying on it, do the final availability check and then **reserve it immediately** (see §5 → "Register & reserve the name on PyPI"):
- **PyPI:** open `https://pypi.org/project/sandkeep/` — a 404 means free. Also check normalised variants (`sand-keep`, `sand_keep`) since PyPI treats hyphen/underscore/case as equivalent.
- **GitHub:** `https://github.com/<your-org>/sandkeep` repo + org free.
- **npm:** only if a JS companion is ever planned.

### The safety-honesty obligation (non-negotiable)
This tool's premise is running untrusted agents safely, but the Phase 0/1 Docker backend is **not** a security boundary. Publishing without saying so, prominently, risks someone trusting it for real containment. Every user-facing surface (README top, SECURITY.md, CLI banner on first run) must state the current containment status plainly. This is baked into the templates below — keep it there.

---

## 1. License

**Decided: PolyForm Noncommercial License 1.0.0 (source-available).** The owner
chose to retain the right to be paid whenever Sandkeep is commercialised, so the
permissive route (Apache-2.0/MIT, which grant free commercial use) was *not*
taken. Under PolyForm Noncommercial, anyone may use, modify, and redistribute
Sandkeep for any **noncommercial** purpose free of charge; **commercial use of
any kind requires a paid license** from the licensor. Unlike the Business Source
License, it does **not** convert to open source on a change date — the
commercial-licensing right does not expire.

> Earlier drafts of this guide recommended Apache-2.0 for adoption. That advice is
> superseded by the decision above; it's retained here only as context.

**Critical labelling rule:** this is **source-available, NOT "open source"** in
the OSI sense. Do not call it open source anywhere (README, PyPI, marketing) — the
community pushes back hard on mislabelling, and it's simply inaccurate. "Source-
available" / "free for noncommercial use" is correct.

Action (done in-repo): `LICENSE` holds the PolyForm Noncommercial 1.0.0 text
verbatim with a `Required Notice:` copyright line; `NOTICE` carries the copyright
and the commercial-licensing contact; `pyproject.toml` sets
`license = "LicenseRef-PolyForm-Noncommercial-1.0.0"` with the
`License :: Free for non-commercial use` classifier (the OSI-Apache classifier was
removed); the README states the model and the commercial-contact email.

> **Not legal advice.** PolyForm is a well-used, plain-language template, but if
> real money will ride on enforcement, have a lawyer confirm the wording and your
> commercial-licensing terms.

---

## 2. Repository manifest

```
PROJECT/
  README.md                  # §3 — the adoption-critical file
  LICENSE                    # PolyForm Noncommercial 1.0.0, verbatim (§1)
  NOTICE
  SECURITY.md                # §4 — threat model + disclosure
  CONTRIBUTING.md            # §6 — incl. the provider extension point
  CODE_OF_CONDUCT.md         # Contributor Covenant, standard text
  CHANGELOG.md               # Keep a Changelog format, SemVer
  pyproject.toml             # §5 — packaging + entrypoint
  CLAUDE.md                  # operating brief (also helps contributors)
  src/PROJECT/...            # the package (see BUILD_SPEC §1)
  tests/...                  # incl. the Phase 0 boundary suite
  examples/
    quickstart.md            # a run that works in < 5 minutes
  docs/
    design.md                # the v2 design doc
    build-spec.md            # BUILD_SPEC.md
  .github/
    workflows/
      ci.yml                 # §5 — lint + test + boundary suite
      release.yml            # §5 — tag-driven PyPI publish (OIDC)
    ISSUE_TEMPLATE/
      bug_report.md
      feature_request.md
    PULL_REQUEST_TEMPLATE.md
```

---

## 3. README skeleton (paste, then fill)

````markdown
# PROJECT

> Run Claude Code agents in isolated, disposable sandboxes — one per task, with cheap rollback and a human gate before anything merges.

[![CI](https://github.com/USER/PROJECT/actions/workflows/ci.yml/badge.svg)](…)
[![PyPI](https://img.shields.io/pypi/v/PROJECT.svg)](…)
[![License](https://img.shields.io/badge/license-PolyForm%20Noncommercial-blue.svg)](LICENSE)

## ⚠️ Security status — read before using

PROJECT is **alpha**. The current sandbox backend is **Docker, which is NOT a
security boundary.** It is a mechanics harness. Do **not** point this at code or
agents you actually do not trust yet — a determined or compromised agent can
escape a container. Real containment (microVM) lands in a later release and will
be announced explicitly. See [SECURITY.md](SECURITY.md).

## What it is

PROJECT orchestrates headless Claude Code agents so each task runs in its own
disposable sandbox. The git worktree lives **inside** the sandbox; only a diff
comes back out; a human approves before it touches your real repo.

## What it is not

- Not an agent framework — you bring the agent (Claude Code).
- Not (yet) production-grade isolation — see the security status above.
- Not autonomous merge — a human gates every change by design.

## Quickstart

```bash
pip install PROJECT          # or: uvx PROJECT
PROJECT image build
PROJECT run --repo /path/to/repo --task "Add input validation to parse_config()"
PROJECT show <task_id>       # review the summary + patch
PROJECT accept <task_id>     # apply to a fresh branch on your repo
```

## How it works

[one diagram + 3-sentence flow: provision read-only clone → run scoped agent →
extract diff → human gate → apply to fresh branch]

## Extending it

PROJECT talks to sandboxes through a `SandboxProvider` interface. Adding a new
backend (microVM, remote sandbox service) is the main extension point — see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Roadmap

- [x] Phase 0 — boundary proof (Docker mechanics)
- [ ] Phase 1 — single governed task loop
- [ ] Phase 2 — microVM isolation, snapshots, parallelism, secret broker, draft-PR gate
- [ ] Phase 3 — conflict detection, diff risk analysis
- [ ] Phase 4 — per-repo skill authoring

## License

PolyForm Noncommercial 1.0.0 (source-available; commercial use requires a paid license — see §1).
````

---

## 4. SECURITY.md (paste)

````markdown
# Security Policy

## Current containment status

PROJECT runs AI agents that execute arbitrary code. Containment depends entirely
on the active `SandboxProvider`:

| Backend | Status | Contains untrusted code? |
|---|---|---|
| Docker (default, alpha) | mechanics harness | **No.** Containers are not a security boundary. |
| microVM | planned | Yes (when shipped) |

**Until a microVM backend ships, do not run agents or code you do not trust.**
The Docker backend proves the orchestration mechanics; it does not guarantee the
host is safe from a hostile agent.

## What is and isn't protected (Docker backend)

- The host repo is mounted **read-only**; the agent works in an independent clone.
- Only a diff is returned; the host working tree/`.git` are not modified until you accept.
- However: container escape, kernel exploits, and egress depend on Docker config
  and are **not** hardened. Treat the host as reachable by a determined agent.

## Reporting a vulnerability

Email <security@…> (or use GitHub private vulnerability reporting). Please do not
open public issues for security bugs. We aim to acknowledge within N business days.
````

---

## 5. Packaging, CI, and release

### Register & reserve the name on PyPI (do this now, before the code is public)

There is **no "reserve a name" button** on PyPI. A name is claimed only when a
distribution is actually uploaded under it. A *pending* trusted publisher does
**not** hold the name — PyPI's docs are explicit that if someone else uploads that
name first, your pending publisher is invalidated. So to truly reserve `sandkeep`,
you must publish something. Two ways:

**Path A — claim it today with a placeholder (recommended for reservation speed):**
1. Create a PyPI account and **enable 2FA** (mandatory; store recovery codes in a password manager). Do the same on TestPyPI.
2. Confirm `https://pypi.org/project/sandkeep/` is a 404 (free).
3. Build and upload a minimal `0.0.1` placeholder to grab the name:
   ```bash
   python -m pip install build twine
   python -m build                      # uses the pyproject below
   python -m twine upload dist/*        # authenticate with a scoped API token
   ```
   The name is now yours. (You can yank `0.0.1` later; the name stays reserved.)
4. Then switch to **trusted publishing** for all real releases (Path B) so you never store long-lived tokens again.

**Path B — pending trusted publisher (no token, claims on first publish):**
1. Account + 2FA as above.
2. On PyPI → your account → **Publishing** → **Add a pending publisher**. Enter:
   - PyPI project name: `sandkeep` (must match `name` in `pyproject.toml`, normalised).
   - Owner/repo: `<your-org>/sandkeep`.
   - Workflow filename: `release.yml`.
   - Environment (recommended): `pypi`.
3. The name is claimed the **first time** the workflow publishes (see `release.yml` below). Until then it is *not* reserved — so if speed matters, do Path A first, then add the publisher.

Rehearse against **TestPyPI** first (same flow, different host) before the real upload.

### `pyproject.toml` essentials
- `[project]` with `name = "PROJECT"`, `requires-python = ">=3.12"`, a short description, `license = "LicenseRef-PolyForm-Noncommercial-1.0.0"` (§1), classifiers (Development Status :: 3 - Alpha).
- `[project.scripts]` → `PROJECT = "PROJECT.cli:main"` (the CLI entrypoint).
- Minimal deps (stdlib-first per BUILD_SPEC). Dev extras: `pytest`, `ruff`.

### `.github/workflows/ci.yml`
```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest          # has Docker, needed for the boundary suite
    strategy:
      matrix:
        python-version: ["3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "${{ matrix.python-version }}" }
      - run: pip install -e ".[dev]"
      - run: ruff check .
      - run: pytest -q                 # includes tests/test_boundary.py
```
The boundary suite running in CI is the single most reassuring signal to adopters that the isolation claims hold — keep it green and visible.

### `.github/workflows/release.yml` — tag-driven publish via PyPI Trusted Publishing (OIDC, no stored tokens)
```yaml
name: Release
on:
  push:
    tags: ["v*"]
permissions:
  id-token: write                     # required for trusted publishing (OIDC)
jobs:
  publish:
    runs-on: ubuntu-latest
    environment:
      name: pypi                       # must match the publisher's environment
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install build
      - run: python -m build
      - uses: pypa/gh-action-pypi-publish@release/v1
```
Set up the Trusted Publisher (or pending publisher) on PyPI **before** the first tag — see "Register & reserve the name" above. The publisher's repo, `release.yml` filename, and `pypi` environment must match this workflow exactly, or the OIDC exchange is rejected. Note PyPI does not accept reusable workflows as the trusted workflow — keep the publish steps in this file directly.

### Versioning
SemVer, starting `0.1.0` to signal pre-stable. Tag `vX.Y.Z` triggers the release. Maintain `CHANGELOG.md` (Keep a Changelog). Cut a GitHub Release per tag with notes.

---

## 6. CONTRIBUTING — make the extension point the on-ramp

The community hook is the `SandboxProvider` interface: the most valuable external contributions will be new backends (microVM, E2B, remote services). Document this explicitly.

````markdown
## Adding a sandbox backend

Implement the `SandboxProvider` ABC (`src/PROJECT/sandbox/base.py`):
`create` (mount repo read-only, deny-by-default network), `exec`, `read_file`,
`destroy`. Your backend MUST pass `tests/test_boundary.py` unmodified — that suite
is the contract every backend honours. A backend that can't pass the adversarial
boundary tests is not mergeable.

Open an issue describing the backend before a large PR. Keep all backend-specific
code inside your provider module; the controller must stay backend-agnostic.
````

Also include: dev setup, `ruff` + `pytest` expectations, conventional-ish commit/PR norms, and the `TODO(phase-N)` convention so contributors don't build ahead of the roadmap.

---

## 7. Pre-launch checklist (gate before `v0.1.0` is public)

- [ ] Named **sandkeep**; GitHub org/repo created.
- [ ] **PyPI name `sandkeep` reserved** by uploading a placeholder (Path A) *or* a verified pending publisher plus a confirmed first publish (Path B). 2FA enabled on PyPI; recovery codes stored.
- [ ] Trusted Publisher configured (repo + `release.yml` + `pypi` environment match the workflow).
- [ ] Release flow rehearsed against TestPyPI.
- [ ] `LICENSE` (PolyForm Noncommercial 1.0.0) + `NOTICE` present; SPDX headers added.
- [ ] README security callout present and accurate to the shipped backend.
- [ ] SECURITY.md containment table matches reality; disclosure channel live.
- [ ] Phase 0 boundary suite passes in CI on a clean runner.
- [ ] Quickstart in `examples/` actually works on a fresh machine in < 5 min.
- [ ] No secrets in repo/history; `ANTHROPIC_API_KEY` only ever via env (and the `TODO(phase-2)` broker marker is present).
- [ ] CI green; release workflow tested against TestPyPI first.
- [ ] CHANGELOG seeded; v0.1.0 tagged.
- [ ] Design doc + build spec in `docs/`.

---

## 8. After launch — keep it adoptable

- Answer the first issues fast; early responsiveness sets the project's reputation.
- Label `good first issue` on small, well-scoped tasks (new providers, docs).
- Be ruthless about the security framing in every release note — never let a version quietly imply stronger isolation than it has.
- When the microVM backend lands, that's the headline release: it's the moment the project becomes safe for the use case it advertises.
