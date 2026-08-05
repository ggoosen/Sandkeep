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
| **E — Capability bridges** | 11 | browser (CDP) bridge — a capability without a hole | 📋 spec'd |

Milestone E is a **post-review follow-up** (from the SandVault comparison); it is
specified below, not yet built.

Dependencies: 4 first (everything after lands gated); 5 benefits from 3's error paths;
1 supersedes part of 5's detection story; 7 last (touches image templating + runner,
which 5 also touches).

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

## Out of scope (unchanged from BUILD_SPEC §16)

Draft-PR gate and warm pool remain deferred — they genuinely need a GitHub remote and
a snapshot-capable backend respectively. The broker work above *removes* the other two
rows ("egress allowlist proxy", "secret broker") from that table.
