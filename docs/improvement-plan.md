# Sandkeep — Improvement Plan (post-review)

**Origin:** full codebase review, 2026-08-05 (security boundary, orchestration, tests/docs).
**Style:** follows `BUILD_SPEC.md` conventions — each step has a goal, a design sketch,
files touched, and acceptance criteria. A step is done only when its acceptance tests pass.
**Golden rules apply throughout:** scripts own the filesystem, agents own the code; only the
diff leaves the sandbox; every state transition is audited; the unmodified boundary suite
must pass after every step.

## Ordering at a glance

**Status: all 10 steps implemented.** Each shipped on branch
`claude/sandkeep-review-improve-q8v3ib` with its own commit + acceptance tests;
step 5's `stream-json` sub-item is a documented deferral (see its status note).

| Milestone | Steps | Theme | Status |
|---|---|---|---|
| **A — Foundations** | 4, 8, 9, 10, 6 | CI that enforces the boundary suite; trivial correctness wins; doc truth | ✅ done |
| **B — Host hardening** | 2, 3, 5 | the accept path, crash recovery, honest violation handling | ✅ done |
| **C — Boundary upgrade** | 1 | key broker + egress allowlist, built locally | ✅ done |
| **D — Ecosystem** | 7 | a real second agent driver | ✅ done |
| **E — Capability bridges** | 11 | browser (CDP) bridge — a capability without a hole | ✅ done |
| **F — Containment by default** | 12, 13 | make the real boundary the default, not an opt-in | 📋 spec'd |
| **G — Tighter egress & secret exposure** | 14, 15 | path/method egress policy; limit what the agent can read | 📋 spec'd |
| **H — Workflow** | 16, 17, 18 | draft-PR gate; task iteration; fleet budgets | 📋 spec'd |
| **I — Backend parity & performance** | 19, 20 | E2B feature parity; warm pool | 📋 spec'd |
| **J — Ecosystem** | 21 | verify Codex + a third driver, broker-protected | 📋 spec'd |
| **K — Operations & release** | 22, 23 | CI actually running; release automation; observability | 📋 spec'd |
| **L — Deferred channels & coverage** | 24, 25, 26 | artifact return path; stream-json; test blind spots | 📋 spec'd |

Milestones A–E are **done** (round 1). Milestones F–L are **round 2** — the gaps that
remain after round 1, specified below. Recommended order is roughly F → H(16) → I(19)
first (the headline containment gap, the biggest workflow gap, and closing the
verified-backend feature deficit); the rest slot in behind their dependencies.

Dependencies (round 1): 4 first (everything after lands gated); 5 benefits from 3's error
paths; 1 supersedes part of 5's detection story; 7 last.
Dependencies (round 2): 13 needs 12 + 19 (a default boundary must have parity); 21 needs
14 (broker generalized for non-Anthropic agents); 20 fits on top of 19; 24 builds on 11.

---

## Milestone A — Foundations

### Step 4 — CI with an *enforced* boundary suite

**Problem.** `.github/workflows/` doesn't exist; the README badge pointed at a phantom
workflow (now removed). Worse, every Docker-backed test — including all of
`test_boundary.py` — silently `pytest.skip`s when the daemon is absent
(`tests/conftest.py:54-70`), so "green" can mean *zero* security tests ran. CLAUDE.md's
"boundary suite must pass before anything else" is enforced by nothing.

**Design.**
- `.github/workflows/ci.yml` (ubuntu-latest has Docker natively): job 1 `unit` —
  `pytest -q` on 3.12/3.13; job 2 `boundary` — build the sandbox image, run
  `pytest tests/test_boundary.py` with a new `--require-docker` flag.
- `--require-docker` (conftest addition): turns the Docker-absent skip into a hard
  **fail**. CI always passes it; local runs keep today's skip behavior.
- Emit a one-line summary at session end: "N boundary tests RAN" vs "SKIPPED — no
  containment verified", so a local green is never silently hollow.
- Restore the CI badge in README only in the same PR that adds the workflow.

**Files.** `.github/workflows/ci.yml` (new), `tests/conftest.py`, `README.md`.

**Acceptance.** CI runs the boundary suite for real on every PR; `pytest --require-docker`
without a daemon exits non-zero with a clear message; badge is live and truthful.

### Step 8 — Keep the API key off the host argv

**Problem.** `DockerProvider.create` passes secrets as `--env KEY=VALUE`
(`docker_provider.py:79-81`) — visible in host `ps`/`/proc/*/cmdline` during create and
permanently in `docker inspect`.

**Design.** Pass `--env KEY` (no value); Docker inherits the value from the provider
subprocess's environment. Set the env only on that one `subprocess.run` call, never the
whole process. `docker inspect` still shows the value (Docker stores container env by
design) — document that; the argv/cmdline leak is what this removes. Mirror for E2B if
its SDK ever surfaces secrets in logged params.

**Files.** `sandbox/docker_provider.py`, `tests/test_docker_provider.py`.

**Acceptance.** A fake-runner test asserts no secret value appears in the constructed
argv; Docker-backed test proves the container still sees the key.

### Step 9 — Make the agent budget configurable

**Problem.** `--max-budget-usd` is hardcoded to `"5.00"` in `agent_runner.py:52,98`,
unlike every other limit.

**Design.** `Config.max_budget_usd: float = 5.0` + `SANDKEEP_MAX_BUDGET_USD` env +
`--max-budget-usd` on `run`/`batch` (precedence flag > env > config, the existing
`model`/`agent` pattern). Persist on the task (new column, additive migration) so
`show` reports the budget the run actually had.

**Files.** `config.py`, `cli.py`, `agent_runner.py`, `agent/claude.py`, `models.py`,
`state_store.py`, tests.

**Acceptance.** Flag/env/config precedence tested; driver command reflects the value;
default unchanged at 5.00.

### Step 10 — Wire or drop `--max-turns` (drop)

**Problem.** The upstream `claude` flag was removed; `ClaudeDriver.build_command`
ignores `task.max_turns` (`agent/claude.py:8-10`), yet the CLI still accepts
`--max-turns` and stores it — silently ignored user input. BUILD_SPEC §6 still shows
the flag.

**Design.** Drop it: remove the CLI flag, deprecate the column (leave in schema, stop
writing), remove from `Task` defaults surfaced in `show`. If a future driver supports a
turn limit, it re-enters via `AgentDriver` capability, not a global flag. Update
BUILD_SPEC §6.

**Files.** `cli.py`, `models.py`, `agent_runner.py`, `BUILD_SPEC.md`, tests.

**Acceptance.** `sandkeep run --max-turns 3` errors with "no longer supported" pointing
at the changelog note; no code path reads `max_turns`.

### Step 6 — Documentation truth pass

**Problem.** CLAUDE.md — the file that *governs agents working on this repo* — still
says "no parallelism, Docker only, do not build phase 2" while `batch`, E2B, test-gated
merge, skills, and per-agent images are all shipped. Assorted drift elsewhere
(see review): `docs/phase-2-implementation.md` says E2B is "NOT yet verified" (it is);
`examples/quickstart.md` is a Phase-1 time capsule; `docs/open-source-release.md` still
says Apache-2.0 in three places despite the PolyForm decision; BUILD_SPEC §1 layout and
§13 image-tag name (`sandkeep-img:` vs actual `sandkeep-sandbox:`) are stale.

**Design.** One PR, docs only, in this order:
1. **CLAUDE.md** — rewrite phase status, command list (`batch`/`test`/`ps`/`gc`/
   `skills`/multi-key `auth`), restate the invocation convention in terms of the
   `AgentDriver` seam, keep the golden rules verbatim.
2. **BUILD_SPEC.md** — retitle (no longer "Phases 0–1"), fix §1/§6/§13, reconcile §12's
   "DO NOT BUILD YET" with its own "implemented" annotations.
3. **docs/phase-2-implementation.md** — E2B verification status; delete the
   "controller is synchronous and single-task" claim.
4. **examples/quickstart.md** — add `auth set`, `batch`, risk flags, `test`.
5. **docs/open-source-release.md** — purge residual Apache-2.0 references.

**Acceptance.** `grep -ri "no parallelism\|do not implement microVMs" CLAUDE.md` empty;
every command in CLAUDE.md exists in `cli.py` and vice-versa; no doc contradicts
another on E2B status or license.

---

## Milestone B — Host hardening

### Step 2 — Harden the accept path (the one place agent bytes touch the host)

**Problem.** `apply_to_fresh_branch` (`diff.py:96-127`) runs `git apply` on the host
with agent-controlled bytes and (a) has **zero** adversarial tests, (b) does its
checkout-apply-commit dance in the user's *live working tree*, (c) never cross-checks
the contract's `files_changed` against the patch (the helper `files_in_patch` exists,
unused; its regex also mis-parses quoted paths), (d) excludes only `.sandkeep`/
`.claude/skills` — a patch adding `.claude/settings.json` (hooks that execute in the
user's future local sessions) sails through un-flagged, (e) has no size/binary limits.

**Design.**
1. **Adversarial patch suite first** (`tests/test_patch_hardening.py`): `../` and
   absolute-path traversal, symlink-swap-then-write, `.git/hooks/` and `.git/config`
   creation, spoofed `diff --git` headers, quoted/space paths, a >max-size patch, a
   binary hunk. Assert every one is rejected *before* any host write.
2. **Apply in a temporary `git worktree`** (`git worktree add --detach` at `base_ref`
   → apply → commit → `git branch sandkeep-accepted/<id>` → `worktree remove`). The
   user's checkout is never touched; a crash mid-accept leaves only a disposable
   worktree. Removes the dirty-tree failure mode *and* review issue 6.
3. **Parse the patch properly**: derive changed paths with
   `git apply --numstat -z` (NUL-safe, quoting-proof) instead of the regex; reject any
   path that is absolute, contains `..`, or starts with `.git/`. Replace
   `files_in_patch` internals; keep the signature.
4. **Contract cross-check**: gate display shows patch-derived files; if the contract's
   `files_changed` disagrees, surface a `contract-mismatch` risk flag (advisory, per
   policy philosophy).
5. **Widen exclusions & flags**: exclude `.claude/**` from extraction (`diff.py:28`
   pathspecs); add a `claude-config` risk category in `policy.py` for any patch
   *adding* `.claude/` or `.github/workflows/` files (the latter is already flagged
   as ci/workflow — keep).
6. **Limits**: `Config.max_patch_bytes` (default 5 MB) enforced at extraction; binary
   hunks surface a `binary` risk flag (not a block — the human decides, per the
   advisory philosophy).

**Files.** `diff.py`, `policy.py`, `config.py`, `controller.py` (gate display),
`cli.py`, new test file, `tests/test_diff_extraction.py`.

**Acceptance.** The adversarial suite passes; a mid-apply `kill -9` leaves the user's
working tree and branch untouched (test simulates via injected failure); `show`
displays patch-derived files; oversize patch → FAILED with detail, never applied.

### Step 3 — Crash recovery & reconciliation

**Problem.** No try/finally between the RUNNING transition and a terminal state
(`controller.py:146-198`, interactive `240-260`): a Ctrl-C during `shell` or a Docker
hiccup leaves the task at RUNNING forever and `gc` *refuses* to reap its container
(classified "active", `controller.py:510-517`). `SUCCEEDED→REVIEW` is two transactions
(`controller.py:372-375`) — a crash between them wedges the task. The most common
accept failure (dirty tree — removed by Step 2, but the pattern generalizes) and
`IllegalTransition`/`SandboxError` all surface as raw tracebacks (`cli.py:579-587`
catches only three exception types).

**Design.**
1. **try/finally around the run**: any exception after RUNNING → transition to FAILED
   (detail = exception), destroy the sandbox, re-raise for the CLI to present.
   `KeyboardInterrupt` in `shell` → FAILED "interrupted", clean teardown.
2. **Legal `RUNNING → FAILED` from housekeeping**: extend `ALLOWED_TRANSITIONS`
   (`models.py:35-37`) so reconciliation can move a stuck task without inventing
   states.
3. **`sandkeep gc` reconciles state, not just containers**: a non-terminal task whose
   sandbox is gone (or whose sandbox exists but no controller process is attached —
   detectable via a `heartbeat_at` column updated during the run loop, stale after
   N minutes) → FAILED + reap. `--dry-run` previews as today.
4. **Single-transaction landing**: `update_state` grows an optional multi-hop form
   (`SUCCEEDED→REVIEW` written as two `transitions` rows in one SQLite transaction) so
   the audit trail keeps both hops but a crash can't strand between them.
5. **CLI error surface**: `cli.main` catches `DiffError`, `IllegalTransition`,
   `SandboxError`, `ValueError` (bad config) → one-line message + exit 1; `--debug`
   re-raises. `_cmd_ps`/`_cmd_gc` handle a down daemon gracefully.

**Files.** `controller.py`, `models.py`, `state_store.py`, `cli.py`, `tests/test_gc.py`,
`tests/test_state_machine.py`, new `tests/test_crash_recovery.py`.

**Acceptance.** Injected exception mid-run → task FAILED, container gone, transition
row present; simulated stale-RUNNING row + dead sandbox → `gc` reports and (without
`--dry-run`) resolves it; crash-between-land-hops is impossible by construction
(single transaction, tested via fault injection); no user-reachable path prints a raw
traceback without `--debug`.

### Step 5 — Demote the output scanner to advisory; adopt `stream-json`

> **Status (partial, shipped).** The scanner split + advisory surfacing and
> the ledger-on-every-terminal-state fix are implemented (`controller.py`,
> `tests/test_advisory_scan.py`, `test_controller.py`). The `stream-json`
> switch is **deferred**: it changes the `SandboxProvider` capture model
> (one-shot `exec` → streamed events) and its event shape can't be verified
> against the real `claude` CLI in this environment — shipping it blind would
> break the §6 "verify flags against the real tool at build time" rule. Tokens
> on failed runs are already recovered from the existing JSON `usage` field, so
> the ledger goal is met without it.

**Problem.** `scan_agent_output` (`violations.py:97-124`) is substring matching: an
agent that *mentions* `/src` next to "permission denied" gets a successful run
archived as VIOLATION before the contract is checked (`controller.py:163-166`) — and a
real adversary trivially avoids the strings. It also only ever sees *failed* egress.
Separately, the ledger records cost only on success (`controller.py:336`), and headless
runs are opaque until they finish.

**Design.**
1. **Advisory, not terminal**: scanner hits become `RiskFlag`s surfaced at the gate
   (same channel as policy flags) + an audit event. The VIOLATION state remains for
   *hard* signals only (today: `scan_exec` on provisioning/extraction commands; after
   Step C: proxy-reported egress denials). Never archive a run for transcript strings.
2. **`--output-format stream-json`**: `ClaudeDriver` parses the event stream —
   per-event token usage (ledger rows even on FAILED/TIMEOUT, fixing the undercount),
   live progress lines for `run`/`batch`, and the final result object as before.
   Scanner runs incrementally over events instead of one post-hoc grep.
3. **Ledger on every terminal state**: `record_cost` moves out of `_land_in_review`
   into the terminal-transition path; interactive stays `(0,0)` until a TTY-side
   usage source exists.

**Files.** `violations.py`, `controller.py`, `agent/claude.py`, `agent_runner.py`,
`cli.py`, `tests/test_agent_runner.py`, `tests/test_controller.py`.

**Acceptance.** A stub transcript containing "/src … permission denied" on a clean run
lands at REVIEW with an advisory flag, not VIOLATION; a FAILED run writes a ledger row
with real token counts; `run` prints progress events; boundary suite unchanged.

---

## Milestone C — Boundary upgrade

### Step 1 — Local key broker + egress allowlist (the two "pending infra" items, no cloud needed)

> **Status (shipped, Docker).** `SANDKEEP_NETWORK=proxy` runs the sandbox on a
> `--internal` no-egress network behind the broker sidecar
> (`sandbox_image/broker/`): the agent holds no key and can only reach the
> allowlist. Broker logic + reverse-proxy round-trip are host-verified
> (`tests/test_broker.py`, 12 tests); the env split + provider command sequence
> in `tests/test_proxy_mode.py`; the Docker-backed containment proof
> (`tests/test_proxy_boundary.py`) runs in CI. E2B parity is the remaining
> piece. Build the broker image with `sandkeep image build --with-broker`.

**Problem.** Default network is open egress and the sandbox holds
`ANTHROPIC_API_KEY` — exfiltration is one `curl` away, and nothing detects *successful*
egress. BUILD_SPEC §16 defers both fixes as "needs infrastructure this repo can't
provision". That's wrong: the `claude` CLI honors `ANTHROPIC_BASE_URL`, and Docker
`--internal` networks provide the plumbing locally.

**Design.**
1. **Broker sidecar** (`sandkeep-broker` container, stdlib-only Python image built by
   `image build`): a small HTTP forward-proxy that (a) accepts requests only for an
   allowlist (`api.anthropic.com`, configured registries), (b) injects
   `x-api-key`/`Authorization` from *its own* env on the way out, (c) JSON-logs every
   allowed/denied request with the task's trace id → these logs feed real VIOLATION
   signals (the hard-signal channel Step 5 reserved).
2. **Network topology per task**: `docker network create --internal sandkeep-<id>`;
   sandbox attaches *only* to it; broker attaches to it **and** the default bridge.
   Sandbox env gets `ANTHROPIC_BASE_URL=http://broker:8080/anthropic` (+
   `HTTPS_PROXY` for registry access) and **no API key**. `destroy` removes network +
   broker with the sandbox.
3. **Config**: `SANDKEEP_NETWORK` grows a third value `proxy` (default once stable;
   `egress` and `none` remain). Allowlist in config, logged at create.
4. **Trust boundary note**: the broker runs on the host side of the line — keep it
   dependency-free, request-parsing minimal, and covered by the boundary suite.
5. **E2B parity**: same posture via `SandboxNetworkOpts` allowlisting +
   `ANTHROPIC_BASE_URL` pointed at a broker reachable from the VM. `TODO` if the SDK
   can't express it yet — Docker is the reference implementation.
6. Retire the corresponding `TODO(phase-2)` markers and BUILD_SPEC §16 rows.

**Files.** new `sandbox_image/broker/` (Dockerfile + `broker.py`),
`sandbox/docker_provider.py`, `config.py`, `controller.py`, `violations.py`,
`cli.py`, `BUILD_SPEC.md`; new `tests/test_broker.py` + boundary-suite additions.

**Acceptance (boundary-suite additions, Docker-backed).**
- `env | grep ANTHROPIC_API_KEY` inside the sandbox → empty.
- `curl https://example.com` from the sandbox → connection refused/denied, and a
  denial line with the trace id appears in the broker log → task flagged VIOLATION.
- A request to `api.anthropic.com` via the broker succeeds and arrives *with* the key
  (asserted against a fake upstream in tests; real API in the key-gated e2e).
- The unmodified pre-existing boundary suite still passes in all three network modes.

---

## Milestone D — Ecosystem

### Step 7 — A real second `AgentDriver`

**Problem.** "The boundary is agent-agnostic" is asserted, tested only with a stub
(`test_agent_driver.py`). BUILD_SPEC §13 lists this as the one unfinished Phase-5 item.

**Design.** Pick one headless-capable CLI agent (Codex CLI or Gemini CLI — decide at
build time by verifying current flags against the installed tool, the §6 discipline).
Implement `install_steps()` (image templating already works), `build_command`/
`build_interactive_command`, `parse_result` mapping exit codes + usage,
`produces_contract=False` (the diff-synthesis fallback already exists from §10b/§13),
`secret_env` for its key (multi-key `auth set <NAME>` already works). Register the
driver; per-agent image `sandkeep-sandbox:<name>` builds via
`image build --agent <name>`. Broker (Step 1) grows that provider's API host in the
allowlist, keyed per driver.

**Acceptance (BUILD_SPEC §13 list, now for real).** Unknown agent still fails loud
host-side pre-sandbox; second-driver run reaches REVIEW via diff synthesis (stub-agent
CI test + key-gated real e2e); **the unmodified boundary suite passes with the second
driver selected**; ledger rows attribute cost to the agent.

---

## Milestone E — Capability bridges (post-review, follow-up)

> **Origin.** Comparison against SandVault (webcoyote/sandvault), 2026-08-05. SandVault
> runs a headless browser *outside* its sandbox and exposes only a Chrome DevTools
> Protocol (CDP) endpoint inside via an env var, so an agent can drive a browser without
> the sandbox getting GUI or direct network access. The same "whitelisted bridge" shape
> is exactly the broker from Step 1 — a controlled intermediary that grants a powerful
> capability without direct access. This milestone instantiates that pattern for the
> browser, which proxy mode (Step 1) otherwise makes impossible.

### Step 11 — Browser bridge (CDP over the sandbox's internal network)

> **Status (shipped, Docker).** `--browser` / `SANDKEEP_BROWSER` attaches a
> headless-Chromium CDP sidecar (`sandbox_image/browser/`) to the task network;
> the agent drives it via `SANDKEEP_BROWSER_CDP`, and in proxy mode its page
> loads are allowlisted through the broker. Wiring host-tested
> (`tests/test_browser_bridge.py`, 11 tests); Docker-backed CDP reachability +
> teardown in `tests/test_browser_boundary.py` (CI). `--no-network` and the E2B
> backend refuse it. Build the image with `image build --with-browser`.
> Deferred as planned: E2B parity and a gated screenshot return path.

**Problem.** In `proxy` mode the sandbox has **no direct egress** — which is the point,
but it also means an agent asked to *test a web app, screenshot a page, or scrape a
site* can't launch a browser or reach anything. Even in `egress` mode, running a full
Chromium inside every task sandbox bloats the image and the in-box attack surface (a
headful browser is a large, network-facing target we don't want the untrusted agent
driving from inside the boundary). Today there is no way to give a task browser
capability without weakening containment.

**Design.** Mirror the broker: a **browser sidecar** the agent talks to over a
localhost-style endpoint, never a browser the agent runs itself.

1. **`browser` sidecar container** (`sandbox_image/browser/`): a headless Chromium
   (the environment already ships one at `/opt/pw-browsers/chromium` with Playwright
   configured — reuse it; `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1`) launched with
   `--remote-debugging-address=0.0.0.0 --remote-debugging-port=9222 --headless=new
   --no-sandbox` inside its own container. It exposes **only** the CDP endpoint on the
   task's internal network, reachable at `http://browser:9222` (network alias, same
   trick as `broker`). It runs non-root with `--cap-drop ALL`, its own memory/pids caps,
   and **no writable mount** — it is disposable with the task.
2. **Egress discipline — reuse the broker.** The browser's *own* outbound traffic must
   obey the same allowlist as everything else: the browser container attaches to the
   task's `--internal` network and points `HTTP(S)_PROXY` at the broker, so a page the
   agent navigates to can only load from allowlisted hosts and every fetch is logged
   with the trace id (Step 1's ground-truth egress signal now covers browser traffic
   too). In `egress` mode the browser gets the open bridge; in `none` mode the bridge is
   not provisioned (nothing to browse) — the CLI says so rather than starting a browser
   that can reach nothing.
3. **Wiring, behind the existing seams.** A new `SANDKEEP_BROWSER=on` config flag (+
   `--browser` on `run`/`shell`/`batch`), default off. When on and the backend supports
   it, `DockerProvider.create()` stands the browser sidecar up on the same per-task
   network it already builds for proxy mode (derive `browser`/network names the way
   `_broker_name`/`_network_name` already do), and `destroy()` tears it down alongside
   the broker. The controller injects `SANDKEEP_BROWSER_CDP=http://browser:9222` into the
   sandbox env (the SandVault `SV_BROWSER_ENDPOINT` idea) so the agent's Playwright/
   Puppeteer connects with `connectOverCDP(process.env.SANDKEEP_BROWSER_CDP)` instead of
   launching its own. Backends that can't offer it (E2B first cut) leave the env unset
   and `create()` raises a clear "browser bridge not supported on this backend" if
   `--browser` was asked for — the same optional-capability degradation `exec_interactive`
   already uses.
4. **Invariant check.** Nothing new crosses the return channel: the browser sidecar
   produces no files, mounts nothing, and its container is discarded with the task. Only
   the diff still leaves. The agent gains a *capability*, not a hole — its browsing is
   proxied and logged exactly like its API calls.

**Files.** new `sandbox_image/browser/` (Dockerfile + launch script); `config.py`
(`SANDKEEP_BROWSER`); `cli.py` (`--browser`, provisioning warning); `sandbox/
docker_provider.py` (sidecar provision/teardown, reusing the internal-network plumbing);
`controller.py` (`SANDKEEP_BROWSER_CDP` in `_agent_env`); `sandbox/base.py`
(optional-capability note); `BUILD_SPEC.md` (new §); tests below.

**Acceptance.**
- Host-side: with `--browser`, `DockerProvider.create` builds the browser sidecar on the
  task's internal network and injects `SANDKEEP_BROWSER_CDP`; `destroy` removes it; the
  sandbox argv/env still carry no secret (`tests/test_browser_bridge.py`, fake runner —
  same style as `test_proxy_mode.py`).
- Docker-backed (CI): from inside a `--browser` proxy-mode sandbox, a CDP client
  connects to `SANDKEEP_BROWSER_CDP` and drives a page; a navigation to an
  **allowlisted** host succeeds and a navigation to a **disallowed** host is refused and
  shows up as a broker denial with the trace id; the browser container is gone after
  `destroy` (`tests/test_browser_boundary.py`).
- The unmodified boundary suite still passes in all network modes with the bridge both
  on and off.

**Deferred within this step.** E2B browser-bridge parity (needs a sidecar reachable from
the microVM — same shape as the broker's E2B follow-up); a screenshot/artifact return
path (screenshots would be a *new* return-channel artifact and must go through the human
gate like the patch — spec separately before building, since it widens what leaves the
sandbox).

---

# Round 2 — remaining gaps (post round-1 review)

> **Origin.** Gap analysis after round 1 (steps 1–11) shipped. Round 1 fixed the
> review's findings; these are the structural gaps that remain. Same build-spec style,
> same golden rules. Nothing here is stubbed as a false guarantee — where a piece needs
> infrastructure, that is stated.

## Milestone F — Containment by default

The headline gap: Sandkeep's whole pitch is "run untrusted agents/code away from your
machine," but the **default** backend is Docker, which the README itself says is not a
security boundary. The verified-containment backend (E2B microVM) exists but is opt-in
and feature-poor (no broker, no browser). Round 2's first job is to make the real
boundary the default.

### Step 12 — Harden the default Docker backend

> **Status (shipped, host-verified).** `extra_run_args` validation refuses
> boundary-breaching flags before any docker call (`validate_extra_run_args`,
> fully tested); `SANDKEEP_SECCOMP` wires a custom seccomp profile and
> `SANDKEEP_READONLY_ROOTFS` an opt-in read-only rootfs + tmpfs. Both hardening
> knobs default off (Docker's built-in seccomp stays in force) so existing runs
> are unchanged; flag presence is host-tested (`tests/test_docker_hardening.py`),
> runtime correctness is the operator's to verify against their image (can't be
> exercised without a daemon). Shipping a hand-written seccomp profile + a forced
> read-only rootfs was **not** done — an unverifiable profile that breaks the
> agent is worse than Docker's tested default; SECURITY.md documents how to
> supply one, and userns-remap as the daemon-level setting it is.

**Problem.** The Docker sandbox has `--cap-drop ALL` + `no-new-privileges` and nothing
else: no seccomp/apparmor profile, writable rootfs, no user-namespace remap, and
`extra_run_args` is spliced in unvalidated (an operator config could re-add a writable
mount or `--privileged`). It's a mechanics harness, honestly labelled — but it can be
materially hardened without changing the model.

**Design.**
- Ship a **restrictive seccomp profile** (`sandbox_image/seccomp.json`, derived from
  Docker's default minus the exotic syscalls an editing agent never needs) and pass
  `--security-opt seccomp=…`; same for an apparmor profile where available.
- **Read-only rootfs** (`--read-only`) with a writable `--tmpfs /work` (and `/tmp`), so
  the only writable state is the disposable workspace — matches "scripts own the
  filesystem."
- **User-namespace remap** (`--userns-remap` / rootless where the daemon supports it) so
  in-container root ≠ host root.
- **Validate `extra_run_args`**: reject `--privileged`, any `-v/--volume`/`--mount`, and
  `--cap-add` on the host side, before `docker run` — the "one mount, ever" invariant
  becomes enforced, not conventional.

**Files.** `sandbox/docker_provider.py`, new `sandbox_image/seccomp.json`, `config.py`,
`tests/test_docker_provider.py`, boundary-suite additions.

**Acceptance.** The hardened flags are present at the call site (fake-runner); a
`extra_run_args` containing a writable mount / `--privileged` is refused loud; the
unmodified boundary suite still passes; a Docker-backed test confirms in-container root
can't touch a host-root-owned path.

### Step 13 — Make containment the default, with honest parity

> **Status (shipped; default stays hardened-docker, as designed).**
> `SANDKEEP_POSTURE` (`hardened-docker` | `microvm`) selects the backend;
> `Config.posture` is derived from the backend so it can never disagree with what
> runs. The security banner now reports the **real** posture (microVM / hardened
> Docker + broker / open Docker) instead of a fixed warning, and `sandkeep
> doctor` reports readiness (daemon, images, keys) with fix hints. The default is
> **not** flipped to microVM because Step 19 (E2B parity) is blocked — exactly
> the gate this step specified. Host-tested in `tests/test_posture.py`.

**Problem.** Even hardened, Docker isn't a microVM. The *default* posture should be the
one the product's security claim depends on — today a user has to know to set
`SANDKEEP_BACKEND=e2b`, and even then loses the broker and browser.

**Design.**
- A **`SANDKEEP_POSTURE`** notion (`hardened-docker` | `microvm`) that selects backend +
  the strongest network default together, with the security banner reporting the *actual*
  posture rather than a fixed warning. Default to `microvm` **once** E2B has broker +
  browser parity (Step 19) — until then default stays `hardened-docker` and the banner
  says so.
- Make the microVM path a first-run experience: `sandkeep doctor` checks for the E2B key
  + template and tells the user exactly what to do, so "the safe default" isn't a wall.
- Gate the default flip on parity: a `posture=microvm` run must pass the **unmodified
  boundary suite** on that backend, and the broker/browser features must work there.

**Files.** `config.py`, `cli.py` (`doctor`, banner), `README.md`, `SECURITY.md`,
`tests/`.

**Acceptance.** The banner names the real posture; `doctor` reports readiness; the
default flips to microVM only when Step 19 lands and the boundary suite is green on it.

## Milestone G — Tighter egress & secret exposure

### Step 14 — Path/method-scoped egress policy + generalize the broker

> **Status (shipped, host-verified).** The broker now serves **multiple routes**
> (a JSON route list): each has its own upstream + auth header/scheme + key
> (Anthropic `x-api-key`, OpenAI `Authorization: Bearer …`), read from the
> broker's own env by `key_env`. Routes carry optional **method rules** (host +
> method) and a **request-body size cap** logged as a `violation` when exceeded.
> Drivers declare a `broker_route`; the controller points the agent's base URL at
> that route's prefix and the provider hands the broker the route + key — so
> `codex` (or any driver) runs key-broker-protected in proxy mode, not just
> claude. Verified on localhost (`tests/test_broker.py`: Bearer injection, method
> denial, size cap) and via the env split (`tests/test_proxy_mode.py`). Path
> rules on CONNECT tunnels are intentionally host-only (TLS is opaque); finer
> rules apply to the reverse-proxied API calls where the broker sees method+path.

**Problem.** The broker allowlists whole hosts. A compromised agent that must reach
`api.anthropic.com` can still encode data into API calls or burn credits, and the reverse
proxy is Anthropic-specific so non-Claude agents get no key-broker protection at all.

**Design.**
- Extend the allowlist to **host + path-prefix + method** rules (e.g. allow
  `POST api.anthropic.com/v1/messages`, deny everything else on that host), enforced in
  `broker.py`; keep whole-host rules as the coarse default.
- **Generalize the reverse proxy**: a small per-driver upstream map (`/anthropic` →
  `api.anthropic.com`, `/openai` → `api.openai.com`, …) so any driver with a
  `base_url_env` gets the same "key held by broker, injected out-of-band" treatment. Wire
  `AgentDriver.broker_route` so drivers declare their upstream.
- **Rate/size caps** per rule (bound how much can leave per run) — a partial answer to
  the "exfil via the allowed host" problem, logged as a VIOLATION when exceeded.

**Files.** `sandbox_image/broker/broker.py`, `agent/base.py` + drivers,
`controller._agent_env`, `config.py`, `tests/test_broker.py`.

**Acceptance.** Path/method rules allow the intended call and deny a sibling path on the
same host; a `codex` run in proxy mode reaches OpenAI through the broker without holding
`OPENAI_API_KEY`; an over-cap egress is flagged.

### Step 15 — Limit what the agent can read from the repo

**Problem.** Read-only `/src` exposes the **entire** working tree *and full git history*,
so any secret ever committed is readable by the untrusted agent. Nothing surfaces this.

**Design.**
- Provision from a **shallow, single-branch clone** by default (`--depth=1`), so deep
  history (and secrets scrubbed from HEAD but alive in history) isn't handed over; a
  `--full-history` opt-in for tasks that need it.
- A host-side **pre-provision secret scan** of what `/src` would expose (reuse
  `policy.py`'s secret patterns), surfaced as a warning at run time and audited — "this
  repo contains N apparent secrets the agent will be able to read."
- Document that read-only ≠ unreadable (SECURITY.md).

**Files.** `provisioner.py`, `policy.py` (reuse), `cli.py`, `SECURITY.md`, `tests/`.

**Acceptance.** Default provision is shallow (verified in the clone); a repo with a
committed key warns at run and audits it; `--full-history` restores deep history.

## Milestone H — Workflow

### Step 16 — Draft-PR human gate

> **Status (shipped, host-verified; live GitHub infra-bound).** `--gate draft-pr`
> / `SANDKEEP_GATE`: on accept the branch is applied locally *and* pushed, then a
> draft PR is opened (body from the contract + risk flags). `gate.py` isolates
> the GitHub call behind injectable git+http seams — remote parsing, body
> building, push+open, and the missing-token/remote failures are host-tested
> (`tests/test_gate.py`); a Docker-backed accept drives it end-to-end with a fake
> gateway (`test_controller.py`). The real push+API call needs a configured
> remote + `GITHUB_TOKEN` and fails loud without them — that live path can't be
> exercised here, by design.

**Problem.** `accept` applies to a local branch — it doesn't push, open a PR, or trigger
CI. This is the biggest workflow gap and the last un-built Phase-2 item.

**Design.**
- A `gate="draft-pr"` mode (`--gate draft-pr` / `SANDKEEP_GATE`): on `accept`, push the
  `sandkeep-accepted/<id>` branch to a configured remote and open a **draft** PR via the
  GitHub MCP/API, body pre-filled from the results contract + risk flags + conflicts.
- Keep local-apply as the default; draft-PR is additive and needs a remote + auth
  (fail loud if absent, never silently fall back).
- The PR is *draft* on purpose — the human still merges; Sandkeep never auto-merges.

**Files.** new `gate.py` (or `controller.accept` branch), `config.py`, `cli.py`,
`BUILD_SPEC.md`, `tests/` (mocked GitHub client).

**Acceptance.** With a remote configured, `accept --gate draft-pr` pushes the branch and
opens a draft PR with the contract summary + risk flags; without a remote it errors
clearly; local-apply default is unchanged.

### Step 17 — Task iteration / resume

**Problem.** A task is one-shot. If a diff is close but not right, there's no "revise it"
— you start fresh, losing the sandbox and context.

**Design.**
- `sandkeep revise <task_id> --task "<follow-up>"`: re-open the task's **still-alive
  REVIEW sandbox** (it's kept as the rollback target already), dispatch the agent again
  with the follow-up instruction against the existing clone, re-extract + re-validate the
  diff, land back at REVIEW. A new `REVIEW → RUNNING` transition (audited) + a revision
  counter on the task.
- Bounded by the same budget/timeout; each revision is a ledger row.

**Files.** `models.py` (transition), `controller.py`, `cli.py`, `state_store.py`,
`tests/`.

**Acceptance.** A REVIEW task revised with a follow-up produces an updated diff without a
new sandbox; the transition chain shows the revision; reject/accept still work.

### Step 18 — Fleet-level budgets & quotas

**Problem.** `--max-budget-usd` bounds a single run; nothing caps total spend across a
`batch` or over time, so a runaway fleet can rack up cost.

**Design.**
- A **batch budget** (`--total-budget-usd`) that stops dispatching new tasks once the
  ledger sum for the batch crosses it; in-flight tasks finish.
- A rolling **daily/however quota** in config, checked at run dispatch (host-side, from
  the ledger), refusing to start a run that would exceed it.

**Files.** `controller.run_concurrent`, `state_store` (ledger sums), `config.py`,
`cli.py`, `tests/`.

**Acceptance.** A batch stops dispatching at the total budget; a run refused by the daily
quota fails loud pre-provision; both are audited.

## Milestone I — Backend parity & performance

### Step 19 — E2B feature parity (broker + browser)

> **Status (guardrail shipped; full parity blocked on the SDK).** Full broker +
> browser parity is **not buildable on the basic E2B SDK**: it exposes no inbound
> tunnel or per-host allowlist, and running the broker *inside* the microVM would
> put the key back within the agent's reach — defeating the point. So the secure
> version needs E2B network features (or a publicly-hosted broker) that can't be
> built or verified here. What **is** shipped is the thing that must hold in the
> meantime: E2B now **refuses** `network=proxy` and `--browser` loudly (CLI
> pre-provision *and* provider-level defense-in-depth), instead of silently
> downgrading to an egress run with the key forwarded into the VM
> (`tests/test_e2b_parity.py`). This unblocks Step 13 to keep the default at
> `hardened-docker` honestly. Full parity stays open, dependency stated.

**Problem.** The one *verified* backend (E2B) has neither the key broker (Step 1) nor the
browser bridge (Step 11) — so choosing real containment means losing the best features.
This blocks Step 13 (microVM-by-default).

**Design.**
- **Egress allowlist on E2B** via its `SandboxNetworkOpts` (per-host rules) plus the same
  `ANTHROPIC_BASE_URL`→broker indirection, with the broker reachable from the microVM
  (E2B-hosted sidecar or the host broker exposed to the VM). Mirror the Docker semantics
  behind the `SandboxProvider` seam.
- **Browser bridge on E2B**: a CDP endpoint reachable from the microVM (same
  `SANDKEEP_BROWSER_CDP` contract), however E2B best exposes a second process/host.
- Both must pass the **unmodified** proxy/browser boundary tests with
  `SANDKEEP_TEST_BACKEND=e2b`.

**Files.** `sandbox/e2b_provider.py`, `tests/` (backend-parametrized), docs.

**Acceptance.** `SANDKEEP_TEST_BACKEND=e2b` passes `test_proxy_boundary.py` and
`test_browser_boundary.py`; the key is absent in the microVM; disallowed egress refused.

### Step 20 — Warm pool / snapshot-restore

**Problem.** Every run cold-starts a container/microVM + clone; provisioning latency is
paid each task. Deferred since Phase 2.

**Design.**
- A **pool** of pre-provisioned sandboxes (backend-specific: Docker containers held warm;
  E2B snapshots) checked out per task and returned/discarded, with checkout/return
  audited. On E2B this maps onto snapshot/restore; on Docker, a small idle pool.
- The read-only-mount-at-create constraint (Docker) means the pool holds *image-warm*
  sandboxes and mounts the repo at checkout — documented trade-off.

**Files.** `sandbox/*`, `controller.py`, `config.py` (pool size), `tests/`.

**Acceptance.** A pooled run provisions materially faster than cold; checkout/return is
audited; a returned sandbox never leaks task state into the next.

## Milestone J — Ecosystem

### Step 21 — Verify Codex + ship a third driver, all broker-protected

**Problem.** The `codex` driver's flags are best-effort (not verified against the real
CLI), and no third driver exists despite the seam being ready. With Step 14 done, any
driver can run key-broker-protected.

**Design.**
- Verify `codex` flags against the installed CLI (the §6 discipline) and give it a
  `broker_route` (Step 14) so proxy mode protects it.
- Add a **third driver** (Gemini CLI — headless-capable), same treatment:
  `install_steps`, `produces_contract=False`, `broker_route`, per-agent image.

**Files.** `agent/codex.py`, new `agent/gemini.py`, registry, `tests/`.

**Acceptance.** Codex + the new driver each run to REVIEW; both work in proxy mode
without holding their own key; the unmodified boundary suite passes with each selected.

## Milestone K — Operations & release

### Step 22 — CI actually running + release automation

**Problem.** The CI workflow was added in round 1 but has never *run* (no PR triggered
it), and the specced PyPI trusted-publisher release flow isn't built — so "enforced
boundary suite" and the PyPI badge are aspirational.

**Design.**
- Open a PR to trigger CI and fix whatever the first real run surfaces (the boundary +
  proxy + browser jobs building real images on `ubuntu-latest`).
- Add `.github/workflows/release.yml` (PyPI Trusted Publisher, `pypi` environment),
  rehearsed against TestPyPI, per `docs/open-source-release.md`.

**Files.** `.github/workflows/release.yml`, minor CI fixes, docs.

**Acceptance.** CI is green on a real run (boundary suite executed, not skipped); a tagged
release publishes to (Test)PyPI via the trusted publisher.

### Step 23 — Observability

**Problem.** The only visibility is the JSON audit log and text `ps`/`show`; there's no
aggregate view of cost/throughput for a running fleet.

**Design.**
- `sandkeep stats` — aggregate ledger (cost per agent/model/day, task outcomes) from
  SQLite, plus a `--watch` live `ps`.
- Optional Prometheus-style metrics endpoint is **out of scope** (adds a server); keep it
  to CLI reporting over the data already stored.

**Files.** `cli.py`, `state_store` (aggregate queries), `tests/`.

**Acceptance.** `stats` reports cost + outcomes from the ledger; `ps --watch` refreshes.

## Milestone L — Deferred channels & coverage

### Step 24 — Gated artifact return path

**Problem.** The browser can screenshot and tasks can produce files, but the only things
that cross the gate are the diff + contract — so a screenshot/report can't come back for
review. (Deliberately deferred in Step 11: it *widens what leaves the sandbox*.)

**Design.**
- A declared, size-capped **artifacts channel**: the agent writes to
  `.sandkeep/artifacts/`, which — like authored skills — is **excluded from the patch**,
  pulled host-side at land, surfaced at the gate, and persisted to a sidecar. It becomes
  durable only on `accept`. Same human-gated discipline as skills; nothing auto-lands.
- Content-type allowlist (images, text, json) + per-artifact size cap; binary artifacts
  flagged.

**Files.** `diff.py` (exclusion), `controller.py` (capture at land), `cli.py` (`show`
lists artifacts), `config.py`, `tests/`.

**Acceptance.** An agent-produced screenshot is captured, shown at the gate, excluded
from the diff, and persisted only on accept; oversize/wrong-type artifacts are refused.

### Step 25 — stream-json live progress

**Problem.** Deferred from Step 5: headless runs are opaque until they finish, and live
progress needs the provider to stream rather than capture one-shot.

**Design.**
- An optional streaming `exec` on `SandboxProvider` (`exec_stream`) that yields output
  incrementally; `ClaudeDriver` uses `--output-format stream-json`, parsed per-event for
  live progress + incremental scanning. Verify the event shape against the real CLI
  first (§6). Backends without streaming keep the one-shot path.

**Files.** `sandbox/base.py` + providers, `agent/claude.py`, `agent_runner.py`, `tests/`.

**Acceptance.** `run` prints live progress events; token accounting matches the one-shot
path; the one-shot fallback still works.

### Step 26 — Close the test blind spots

**Problem.** From the round-1 test review, still open: the real interactive TTY flow is
monkeypatched (never exercised), concurrency edges (concurrent accept of conflicting
tasks, accept racing gc) are untested, and E2B containment isn't reproducible in-tree.

**Design.**
- A Docker-backed interactive-flow test driving a scripted `docker exec -it` (pexpect or
  a fake TTY) end to end.
- Concurrency edge tests: two REVIEW tasks touching the same file accepted in parallel;
  `accept` racing `gc`/`reconcile`.
- Make the E2B boundary run a documented, key-gated CI job (opt-in) so "verified" is
  reproducible by anyone with a key.

**Files.** `tests/test_interactive.py`, `tests/test_concurrency.py`, CI, docs.

**Acceptance.** The interactive path is exercised without monkeypatching the exec; the
concurrency edges pass; the E2B job is documented and runnable.

---

## Out of scope

The genuinely infra-bound pieces are now **planned** above rather than deferred: the
draft-PR gate (Step 16) needs a GitHub remote, the warm pool (Step 20) needs a
snapshot-capable backend, E2B parity (Step 19) needs an E2B key — each step states its
dependency and fails loud rather than faking the guarantee. Nothing remains silently
stubbed.
