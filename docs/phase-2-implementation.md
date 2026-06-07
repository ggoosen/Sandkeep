# Phase 2 implementation guide — real isolation + parallelism

This is the build guide for the deferred Phase 2 work: the pieces that turn
Sandkeep from an *alpha mechanics harness* into a boundary you can run untrusted
code behind. They're deferred because they need infrastructure a dev laptop
doesn't have (KVM/cloud for a microVM, a real proxy, a GitHub remote) — **not**
because the design is unsettled. Each one is specified here well enough to pick
up.

See `BUILD_SPEC.md` §16 for the status table and the *why-not-stubbed* rationale.
The non-negotiable rule for all of it: a new backend **must pass
`tests/test_boundary.py` unmodified** (and you should *add* tests for the new
guarantees). Don't ship a config flag that implies a guarantee it can't enforce.

---

## 0. The contract you're implementing against

Everything talks to sandboxes through `SandboxProvider`
(`src/sandkeep/sandbox/base.py`):

```python
class SandboxProvider(ABC):
    def create(self, repo_path: str, env: dict[str, str]) -> SandboxHandle: ...
    def exec(self, handle, cmd: list[str], timeout: int) -> ExecResult: ...
    def read_file(self, handle, path: str) -> str: ...
    def destroy(self, handle) -> None: ...
    def exec_interactive(self, handle, cmd: list[str]) -> int: ...   # optional (TTY)
```

Invariants every backend must hold (the Docker provider is the reference):

- Host repo present **read-only** at `/src`; the agent works on an independent
  clone at `/work/repo` (the provisioner makes the clone — you just provide the
  read-only `/src` and a writable `/work`).
- No writable path to the host filesystem or host `.git`. No host control-plane
  socket (docker/k8s/hypervisor) reachable from inside.
- Non-root inside; least privilege.
- `destroy` discards **all** state — that's what makes `reject` free.

`SandboxHandle(id, workdir)` is whatever your backend needs to address the
sandbox later (`workdir` is `/work/repo`).

### Acceptance gate for any backend

`tests/test_boundary.py` plays a hostile agent and asserts each of these is
contained:

- host secrets unreachable; `/src/.git` not writable; `git push` to host fails
- network exfil fails (under the no-network posture) and is flagged
- no docker/host socket; privilege-escalation paths closed; non-root
- git inside is task-scoped (no host remotes/history)
- `destroy` after a hostile run leaves the host byte-for-byte unchanged
- a VIOLATION is archived (quarantined), never silent

To run it against your backend, point the `provider` fixture in
`tests/conftest.py` at your provider (or add a parametrized fixture), then:

```bash
pytest tests/test_boundary.py
```

---

## 1. microVM `SandboxProvider` — the real boundary

**What it gives:** Docker shares the host kernel, so a container-escape exploit
reaches the host. A microVM gives each task its own kernel + hardware-virtualized
isolation — the jump from "contains accidents and prompt injection" to "contains
a determined adversary."

**Recommended path: E2B** (managed Firecracker microVMs — no infra to run
yourself). Firecracker-direct or Cloud Hypervisor are the self-hosted
alternatives if you have bare metal with `/dev/kvm`.

### Sketch: `src/sandkeep/sandbox/e2b_provider.py`

```python
from e2b import Sandbox          # pip install e2b
from .base import SandboxProvider, SandboxHandle, ExecResult, SandboxError, SandboxExecTimeout, SRC_MOUNT, WORKDIR

class E2BProvider(SandboxProvider):
    def __init__(self, config): self.config = config
    _sandboxes: dict[str, Sandbox] = {}   # id -> live handle

    def create(self, repo_path, env):
        sbx = Sandbox(template=self.config.template, env_vars=env)
        # E2B has no host bind-mount: UPLOAD the repo read-only instead of mounting.
        # Upload the working tree to /src and chmod it read-only; the provisioner
        # then clones /src -> /work/repo inside the VM (same as Docker).
        _upload_readonly(sbx, repo_path, SRC_MOUNT)
        self._sandboxes[sbx.id] = sbx
        return SandboxHandle(id=sbx.id, workdir=WORKDIR)

    def exec(self, handle, cmd, timeout):
        sbx = self._sandboxes[handle.id]
        try:
            p = sbx.commands.run(_join(cmd), timeout=timeout)
        except TimeoutError as e:
            raise SandboxExecTimeout(str(e)) from e
        return ExecResult(p.exit_code, p.stdout, p.stderr)

    def read_file(self, handle, path):
        try:    return self._sandboxes[handle.id].files.read(path)
        except Exception as e:  raise FileNotFoundError(path) from e

    def destroy(self, handle):
        self._sandboxes.pop(handle.id).kill()
```

### The points that matter (and where boundary tests will catch you)

- **Read-only `/src`.** E2B has no host bind-mount, which is *good* — there's no
  host path to leak. Upload the repo into the VM and make `/src` read-only so the
  `git push`/`/src/.git` boundary tests still pass. The host repo never leaves
  your machine except as the bytes you upload.
- **Non-root + dropped caps.** Bake into the E2B template (the VM image), same
  role the Dockerfile plays today.
- **`destroy` = `kill`.** Verify the "host untouched after hostile run" test —
  trivially true for a microVM (nothing was ever mounted), but assert it.
- **`exec_interactive`** maps to E2B's PTY API for `sandkeep shell`; if you skip
  it, `shell` raises a clear "not supported" — that's allowed.

**Definition of done:** `pytest tests/test_boundary.py` green with the `provider`
fixture pointed at `E2BProvider`; `sandkeep run` works end to end against it.

---

## 2. Brokering egress-allowlist proxy — close the exfil hole

**What it gives:** Today network is all-or-nothing (`--no-network` = off, else an
open bridge). An agent that needs `api.anthropic.com` can therefore also reach
the whole internet → it could exfiltrate the repo. A proxy on the host allows
*only* an allowlist (the API + package registries) and blocks everything else.

**Design:**
- Run a forward proxy on the host (e.g. a small mitmproxy/`tinyproxy`-style
  process, or an egress filter) with an allowlist: `api.anthropic.com`, your
  package registries, nothing else.
- Start the sandbox with **no direct network**; route it at the proxy via
  `HTTP(S)_PROXY` env (and block non-proxied egress at the VM/network layer).
- Add config: `egress_allowlist: list[str]`, and a `network="proxy"` mode
  alongside `none`/`egress`.

**Verify it's real:** extend `tests/test_boundary.py` — under `network="proxy"`,
a connection to `api.anthropic.com` succeeds but a connection to an
arbitrary host (e.g. `example.com`) **fails and is flagged**. Until that test is
green, the allowlist is not enforced — so don't add the config until it is.

---

## 3. Secret-injecting broker — the agent never holds the key

**What it gives:** Today `ANTHROPIC_API_KEY` is passed into the sandbox env (a
flagged Phase 0–1 shortcut — `TODO(phase-2)` in `controller.py`,
`agent_runner.py`, `agent/claude.py`, `config.py`). The untrusted agent can read
it. The broker keeps the key on the host and injects it into outbound API
requests at the proxy, so the agent's calls work but it never sees the credential.

**Design (builds on #2):**
- The key lives only on the host, given to the proxy — never in the sandbox env.
- The proxy intercepts requests to `api.anthropic.com` and injects the
  `x-api-key` / `Authorization` header.
- Remove the `_agent_env` secret-forwarding in `controller.py` for proxied
  backends; the `driver.secret_env` mechanism then only feeds the host-side proxy.

**Verify:** a boundary test asserting `printenv` / `cat`-ing common key locations
inside the sandbox reveals **no** Anthropic key, while a real `sandkeep run` still
succeeds through the proxy.

---

## 4. Draft-PR human gate — review in GitHub

**What it gives:** `accept` currently applies the diff to a fresh local branch
(`diff.apply_to_fresh_branch`). The draft-PR variant pushes the agent's work as a
GitHub draft PR so review happens with CI + comments.

**Design:**
- New gate mode (config): keep local-apply as default; add `gate="draft-pr"`.
- On `accept`: push the `sandkeep-accepted/<task_id>` branch and open a draft PR
  via the `gh` CLI or the GitHub API; record the PR URL in the audit log + task.
- Needs a configured remote + auth; fail loud on the host if absent (same pattern
  as the unknown-agent check).

**Verify:** integration test against a throwaway remote (or a mocked `gh`),
asserting a draft PR is opened and the task records its URL.

---

## 5. Concurrency + warm pool / snapshots — speed & scale

**What it gives:** one-task-at-a-time → a fleet of agents in parallel, with a
pool of pre-provisioned sandboxes for near-instant starts; snapshots for cheap
fork/restore.

**Design notes:** the controller is currently synchronous and single-task. Add a
scheduler that runs N tasks concurrently, each with its own sandbox + its own
state-machine instance; the SQLite store already keys everything by task id, so
parallel tasks don't collide there — but you'll want a connection-per-thread and
to audit pool checkout/return. Snapshots map cleanly onto microVM
snapshot/restore (another reason this follows #1, not precedes it).

**Verify:** a test running several tasks concurrently to REVIEW with isolated
patches; assert no cross-task state bleed in the store/ledger.

---

## Suggested order

1. **microVM backend (#1)** — the headline; everything else is better on top of it.
2. **egress proxy (#2)** then **secret broker (#3)** — they pair; #3 needs #2.
3. **draft-PR gate (#4)** — independent, low-risk, do anytime.
4. **concurrency/snapshots (#5)** — last; payoff is coupled to the microVM.

Each lands behind config with the old behaviour as the default, so nothing
regresses for existing users, and each ships only when its verifying test is
green.
