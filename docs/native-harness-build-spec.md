# Build Spec — Claude-Code-Native Sandkeep Harness ("Cordon")

> A build document for a **new, separate repo**. This is the *native* sibling of
> Sandkeep: instead of a Python controller driving Docker, it forces the same
> isolated → review → gate discipline using only Claude Code's own primitives
> (worktrees, sandbox, permission rules, hooks, skills). Someone clones this
> repo's output into a project and **every Claude Code session there is pushed
> into the controlled flow automatically**.
>
> `Cordon` is a working title (a cordon forces everything through one controlled
> boundary). Rename freely.

---

## 0. Read this first — what you are and aren't building

**The relationship to Sandkeep.** Sandkeep and Cordon sit at two points on one
axis: *how much you trust the agent and the code*.

| | Sandkeep (Docker/microVM) | Cordon (this doc — native) |
|---|---|---|
| Engine | external Python controller + container | Claude Code's own worktree + sandbox + hooks |
| Isolation boundary | independent clone in a container; host repo mounted read-only | OS sandbox (Seatbelt/bubblewrap) + git worktree on the host |
| Protects against | accidents **and** most malicious behavior | **accidents and misbehavior, not a determined malicious escape** |
| Infra needed | Docker (later microVM) | none — pure Claude Code config |
| Rollback | destroy the container | delete the worktree/branch |
| Review gate | patch → `accept`/`reject` | diff in the worktree → merge-or-discard |
| Best for | "I don't trust this agent/code" | "I trust the agent, I just want disciplined, reversible, reviewable work" |

**The honest security line (put this in the new repo's README, prominently).**
Claude Code's `/sandbox` is OS-level sandboxing of the **Bash tool** — real, but
Anthropic explicitly does **not** position it as a trust boundary against
hostile code. There is no separate kernel. Cordon's job is to make the *safe,
reversible, reviewable* workflow the **path of least resistance** (ideally the
only one) — not to contain an adversary. For that, point users at Sandkeep.

**What "force Claude to use this method" really means.** Governance here has two
faces that must be designed together: a **soft layer that shapes intent** (the
agent *wants* to follow the method and shepherds the user through it) and a
**hard layer that enforces it** (the agent *can't* break the method even if it
tries). Get both; neither alone is enough.

**The soft layer — governance (shepherding).** One lever, and it's the one this
update adds as first-class:

0. **`CLAUDE.md`** — auto-loaded into Claude's context every session. It states
   the rules of the house, makes Claude *narrate* the methodology to the user
   (explain the sandbox, why something was blocked, what the next step is), and
   makes Claude **refuse-and-redirect** to the sanctioned skills *before* a hard
   rule ever fires. This governs the **agent**; it shepherds the **user** only
   *through* the agent. It is **soft and probabilistic** — the model usually
   follows it and occasionally drifts, which is exactly why the hard layer sits
   underneath. Full spec in §7b.

**The hard layer — enforcement.** Four levers, strongest last:

1. **Defaults** — the project ships settings that put every session in the right
   mode (sandbox on, permissions scoped, the gate skill front-and-centre).
2. **Deny rules** — `permissions.deny` blocks the escape hatches (writing to
   `.git`/`.claude`, `git push`, `rm -rf`, reading `.env`) *before* the
   permission prompt. These merge across scopes and can't be loosened downward.
3. **Hooks** — a `PreToolUse` hook actively **blocks** any tool call that
   violates the boundary (write outside the worktree, network egress), and a
   `SessionStart` hook **refuses to start** a session that isn't isolated.
4. **Managed settings** — if (and only if) the user deploys the admin-level
   `managed-settings.json`, the above become unoverridable. Without admin
   deployment, a determined *user* can still edit project settings — that's
   fine; Cordon governs the **agent**, and assumes a cooperative human.

**Why both.** `CLAUDE.md` makes the right path the one Claude *chooses*; the hard
layer makes the wrong path the one Claude *can't take*. The soft layer is what
makes Cordon feel like shepherding rather than bumping into fences — without it,
a user hits silent walls; with it, Claude explains the house and walks them
through it. Never rely on the soft layer for safety, though: a security-relevant
rule must always also exist in the hard layer.

> ⚠️ **Verify the exact API surface at build time.** Hook event names, the
> precise `permissionDecision` JSON, and worktree settings keys evolve. Before
> implementing each component below, confirm against `claude --help`, the live
> docs (URLs in §11), and a 5-line spike. Treat every schema in this doc as
> "correct as of CLI 2.1.168, June 2026; re-verify." Where a feature might not
> exist as described, this doc says so and gives a fallback.

---

## 1. Architecture — one governance layer over five enforced guarantees

Cordon delivers the same conceptual guarantees as Sandkeep, mapped onto native
primitives:

| Guarantee | Native mechanism |
|---|---|
| **G0. The agent understands the method and shepherds the user through it** | `CLAUDE.md` governance brain (auto-loaded; narrates, refuses-and-redirects) — §7b |
| **G1. Work is isolated from the main branch/tree** | `--worktree` → a throwaway branch + worktree under `.claude/worktrees/` |
| **G2. The agent can't touch the host outside its workspace** | `sandbox` key (FS write confined to worktree; network allowlisted) + `PreToolUse` boundary hook |
| **G3. The dangerous escape hatches are closed** | `permissions.deny` rules (push/reset, `.git`/`.claude` writes, secret reads) |
| **G4. Nothing merges without human review** | the **gate skill** (`/cordon-review`) + a `Stop`/manual diff presentation; merge is a deliberate human act |
| **G5. Everything is logged** | `PreToolUse`/`PostToolUse` hooks append JSON-lines to an audit file |

The session lifecycle Cordon enforces:

```
open Claude Code in a Cordon project
  └─ CLAUDE.md loads: Claude now knows the house rules and will narrate them (G0)
       └─ SessionStart hook: am I in a worktree + sandbox? if not → refuse / auto-enter
            └─ work happens; Claude proposes the right path, the PreToolUse hook filters every Bash/Edit/Write
                 └─ changes accumulate ONLY in the worktree branch (G1)
                      └─ /cordon-review: show the diff, run checks, summarise (G4)
                           └─ human decides: /cordon-accept (merge to a fresh branch) | /cordon-discard (delete worktree)
```

The two layers in the same picture: **G0 (CLAUDE.md)** makes Claude *choose* this
path and explain it; **G1–G5 (hooks/settings)** make it the *only* path that
actually works. When Claude follows the governance, the user is shepherded
smoothly; when it drifts, the hard layer catches it and Claude — per its
CLAUDE.md — explains the block and redirects.

---

## 2. Repository layout (the new repo)

This repo is **both** a copy-me template *and* an installable plugin. Two
delivery modes from one source (§8).

```
cordon/
  README.md                      # what it is + the security-honesty callout
  LICENSE                        # Apache-2.0 or MIT
  docs/
    design.md                    # this spec, refined
    enforcement-model.md         # the four levers + their honest limits
  .claude-plugin/
    plugin.json                  # plugin manifest (§8)
    marketplace.json             # optional: for distribution
  template/                      # what gets copied into a target project
    CLAUDE.md                    # GOVERNANCE BRAIN: house rules + narration; posture-stamped (§7b)
    governance/
      CLAUDE.guide.md            # "guide" posture — light, explains and recommends
      CLAUDE.enforce.md          # "enforce" posture — strict, refuses-and-redirects (default)
    settings.json                # defaults: sandbox, permissions, hooks, statusline (§3)
    hooks/
      boundary.sh                # PreToolUse: block out-of-bounds tool calls (§4)
      enforce-isolation.sh       # SessionStart: require worktree+sandbox (§5)
      audit.sh                   # Pre/PostToolUse: append JSON-lines audit (§6)
      lib.sh                     # shared: parse stdin json, locate worktree root
    skills/
      cordon-review/SKILL.md     # the human gate: show diff + checks + summary (§7)
      cordon-accept/SKILL.md     # merge worktree branch → fresh review branch
      cordon-discard/SKILL.md    # delete the worktree + branch
      cordon-status/SKILL.md     # where am I, what's changed, what's blocked
    statusline/
      statusline.sh              # shows: worktree name · sandbox on/off · #changes
  skills/                        # SAME skills, exposed at plugin scope (/cordon:review …)
    cordon-review/SKILL.md       # (symlink or duplicate of template/skills/*)
    ...
  hooks/
    hooks.json                   # plugin-level hook registration (§8)
  install.sh                     # copy template/ into a target repo's .claude/ + wire .gitignore
  managed-settings.example.json  # the admin-deployed, unoverridable variant (§9)
  tests/
    test-boundary.sh             # adversarial: prove the hook blocks escapes (§10)
    test-enforcement.sh          # prove SessionStart refusal + deny rules
    fixtures/                    # throwaway git repos for tests
```

> Skills/hooks are duplicated (or symlinked) between `template/` (project-scoped
> install) and the plugin root (`skills/`, `hooks/`) so the **same logic** ships
> both ways. Keep one source of truth in `template/` and generate the plugin
> copies in `install.sh`/CI to avoid drift.

---

## 3. The settings file (`template/settings.json`) — the defaults lever

This is the heart of the "bells and whistles". Shipped as the project's
`.claude/settings.json` (committed, team-shared). Verify every key at build time.

```json
{
  "$schema": "https://json.schemastore.org/claude-code-settings.json",

  "permissions": {
    "defaultMode": "default",
    "deny": [
      "Bash(git push:*)",
      "Bash(git reset --hard:*)",
      "Bash(git checkout main:*)",
      "Bash(git checkout master:*)",
      "Bash(rm -rf:*)",
      "Edit(.git/**)",
      "Edit(.claude/**)",
      "Write(.git/**)",
      "Write(.claude/**)",
      "Read(.env)",
      "Read(.env.*)",
      "Read(**/*.pem)",
      "Read(**/id_rsa*)"
    ],
    "ask": [
      "Bash(git commit:*)"
    ],
    "additionalDirectories": []
  },

  "sandbox": {
    "enabled": true,
    "filesystem": {
      "allowWrite": ["."],
      "denyWrite": ["../", "~", ".git", ".claude"]
    },
    "network": {
      "allowedDomains": ["api.anthropic.com"]
    }
  },

  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          { "type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/enforce-isolation.sh" }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Bash|Edit|Write",
        "hooks": [
          { "type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/boundary.sh" }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          { "type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/audit.sh" }
        ]
      }
    ]
  },

  "statusLine": {
    "type": "command",
    "command": "$CLAUDE_PROJECT_DIR/.claude/statusline/statusline.sh"
  }
}
```

**Design notes & gotchas (verify each):**
- `permissions.deny` patterns use Claude Code's permission-rule syntax
  (`Tool(pattern)`). Confirm the exact glob form (`:*` vs `*`) against the
  permissions docs — it has varied. Test each deny rule with `test-enforcement.sh`.
- The `sandbox` key is **Linux/WSL2 first**; on macOS it relies on `sandbox-exec`
  (Seatbelt), which Apple has marked deprecated. **Confirm current macOS support.**
  If sandbox is unavailable on the user's OS, Cordon must degrade loudly: the
  statusline shows `sandbox: OFF` and the boundary hook becomes the primary
  guard, not a backstop. Never let "sandbox silently off" read as "protected".
- `$CLAUDE_PROJECT_DIR` is the documented way to reference project-root paths in
  hook commands; verify the exact variable name. Fallback: absolute paths
  written by `install.sh`.
- `defaultMode`: keep `default` (prompt) for a template — `acceptEdits` is
  tempting but undercuts the gate. The isolation, not auto-accept, is the point.

---

## 4. The boundary hook (`PreToolUse`) — the active-enforcement lever

A `PreToolUse` hook receives the pending tool call on stdin and can **deny** it.
This is the runtime guard that backs up the static deny rules and the sandbox.

**Contract (verify against hooks docs):**
- stdin is JSON: `{ "hook_event_name": "PreToolUse", "tool_name": "...",
  "tool_input": { ... }, "cwd": "...", "session_id": "..." }`.
- To **block**: either `exit 2` with a human reason on stderr (simple), **or**
  emit JSON on stdout and `exit 0`:
  ```json
  { "hookSpecificOutput": {
      "hookEventName": "PreToolUse",
      "permissionDecision": "deny",
      "permissionDecisionReason": "Cordon: write outside the worktree is not allowed" } }
  ```
  Prefer the JSON form (integrates with the permission system, cleaner reason
  surfaced to the model). Keep `exit 2` as the fallback if JSON isn't honored on
  your CLI version. **Spike both before committing to one.**
- `exit 0` with no output ⇒ allow (fall through to normal permission flow).

**`template/hooks/boundary.sh` (reference implementation):**

```bash
#!/usr/bin/env bash
# PreToolUse boundary guard. Reads the pending tool call on stdin; denies any
# action that would leave the worktree, reach the network, or touch protected
# paths. Allows everything else (normal permission flow still applies).
set -euo pipefail
source "$(dirname "$0")/lib.sh"

input="$(cat)"
tool="$(jq -r '.tool_name' <<<"$input")"
wt_root="$(cordon_worktree_root)"   # the active .claude/worktrees/<name> path

deny() {  # $1 = reason
  jq -n --arg r "$1" '{hookSpecificOutput:{hookEventName:"PreToolUse",
    permissionDecision:"deny", permissionDecisionReason:("Cordon: " + $r)}}'
  exit 0
}

case "$tool" in
  Edit|Write)
    path="$(jq -r '.tool_input.file_path // .tool_input.path // empty' <<<"$input")"
    abs="$(cordon_abspath "$path")"
    case "$abs" in
      "$wt_root"/*) : ;;                         # inside the worktree → ok
      *) deny "edits must stay inside the worktree ($wt_root)";;
    esac
    case "$abs" in
      *"/.git/"*|*"/.claude/"*) deny "writing to .git/.claude is not allowed";;
    esac
    ;;
  Bash)
    cmd="$(jq -r '.tool_input.command' <<<"$input")"
    # escape hatches the static deny rules might miss in compound commands
    case "$cmd" in
      *"git push"*)            deny "git push is blocked; use /cordon-accept to land work";;
      *"git reset --hard"*)    deny "hard reset is blocked inside a Cordon session";;
      *"curl "*|*"wget "*|*"nc "*) deny "raw network egress is blocked; rely on tools/MCP";;
      *"/dev/tcp/"*)           deny "network egress via /dev/tcp is blocked";;
      *"sudo "*)               deny "sudo is not available in a Cordon session";;
    esac
    ;;
esac
exit 0   # allow
```

**`template/hooks/lib.sh` (shared helpers):**

```bash
#!/usr/bin/env bash
# Shared helpers for Cordon hooks.

cordon_worktree_root() {
  # The worktree that the session is running in. If the cwd is under
  # .claude/worktrees/<name>, return that; else return the git toplevel.
  local top; top="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  case "$PWD" in
    *"/.claude/worktrees/"*)
      printf '%s' "${PWD%%/.claude/worktrees/*}/.claude/worktrees/${PWD#*/.claude/worktrees/}" \
        | sed 's#\(/.claude/worktrees/[^/]*\).*#\1#' ;;
    *) printf '%s' "$top" ;;
  esac
}

cordon_abspath() {  # $1 = possibly-relative path
  local p="$1"
  case "$p" in
    /*) printf '%s' "$p" ;;
    *)  printf '%s/%s' "$PWD" "$p" ;;
  esac
}
```

> The path math is the fiddly part — write `test-boundary.sh` (§10) *first* and
> drive this hook against it. The hook is security-adjacent; an off-by-one in the
> prefix check is the whole ballgame.

---

## 5. The isolation gate (`SessionStart`) — the "refuse to start unsafe" lever

A `SessionStart` hook that `exit 2`s **stops session startup** and shows its
stderr to the user. This is how you *force* the worktree (no native
"always-worktree" setting exists; the desktop app auto-creates worktrees, but the
CLI does not — confirm current behavior).

**Two policies — ship both, choose via an env/config flag:**

- **Strict (refuse):** if not in a worktree, print how to relaunch and `exit 2`.
- **Guided (auto-enter):** if not in a worktree, print the exact
  `claude --worktree <name>` command and `exit 2` — Claude Code can't re-exec
  itself into a worktree from a hook, so "auto" here means *instruct + refuse*,
  not *silently move*. Be honest in the message about why.

**`template/hooks/enforce-isolation.sh`:**

```bash
#!/usr/bin/env bash
# SessionStart: refuse to run a Cordon project outside an isolated worktree.
set -euo pipefail

if [[ "$PWD" != *"/.claude/worktrees/"* ]]; then
  cat >&2 <<'MSG'
┌─ Cordon ───────────────────────────────────────────────────────────
│ This project requires an isolated worktree session.
│ Your changes must land in a throwaway branch, not the main checkout.
│
│   Relaunch with:   claude --worktree
│
│ (clean worktrees are auto-deleted on exit; nothing is lost)
└────────────────────────────────────────────────────────────────────
MSG
  exit 2     # stops startup
fi

# In a worktree: warn loudly if the sandbox isn't actually active.
# (Surface real state; never let "off" look like "on".)
echo "Cordon: isolated session active in $(basename "$PWD")" >&2
exit 0
```

> **Verify:** that `SessionStart` + `exit 2` actually halts startup on your CLI
> version, and the exact `matcher` value (`"startup"` vs others). If `exit 2`
> doesn't halt, fall back to a blocking `additionalContext` message + a deny-all
> posture until the user relaunches. Document whichever is true.

---

## 6. Audit (`PostToolUse`) — the logging lever (G5)

Append one JSON line per tool use to `.claude/cordon-audit.jsonl` (gitignored).
Mirrors Sandkeep's audit log so the two harnesses produce comparable trails.

**`template/hooks/audit.sh`:**

```bash
#!/usr/bin/env bash
set -euo pipefail
input="$(cat)"
ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
log="${CLAUDE_PROJECT_DIR:-$PWD}/.claude/cordon-audit.jsonl"
jq -c --arg ts "$ts" '{ts:$ts, event:.hook_event_name, tool:.tool_name,
  input:.tool_input, session:.session_id}' <<<"$input" >> "$log" || true
exit 0
```

---

## 7. The skills — the human gate workflow (G4)

Skills are the user-facing verbs. Location: `.claude/skills/<name>/SKILL.md`
(project) and `skills/<name>/SKILL.md` (plugin → `/cordon:<name>`).

**`template/skills/cordon-review/SKILL.md`:**

```markdown
---
name: cordon-review
description: Review the work done in this isolated session before it lands — show the diff against the base branch, run the project's checks, and summarise risks. Use before accepting any change.
user-invocable: true
allowed-tools: Bash(git diff:*) Bash(git status:*) Bash(git log:*) Bash(npm test:*) Bash(pytest:*)
---

# Cordon — review gate

You are gating work done in an isolated worktree before a human decides to land it.

## Current state
- Branch: !`git branch --show-current`
- Base:   !`git merge-base HEAD origin/HEAD 2>/dev/null || echo HEAD`
- Files changed:
!`git diff --stat $(git merge-base HEAD origin/HEAD 2>/dev/null || echo HEAD)..`

## Do this
1. Show the full diff against the base. Walk the human through it, grouped by file.
2. Run the project's test command if one exists; report pass/fail with output.
3. Flag anything risky: new deps, deletions, auth/secret/config/workflow changes,
   anything touching more than it should for the stated task.
4. End with a one-paragraph verdict and the exact next step:
   `/cordon-accept` to land on a fresh branch, or `/cordon-discard` to throw it away.

Do NOT merge or push. This skill only informs the human decision.
```

**`template/skills/cordon-accept/SKILL.md`:**

```markdown
---
name: cordon-accept
description: Land the reviewed work from this worktree onto a fresh, named review branch off the base — never onto main, never via push. Use only after /cordon-review.
user-invocable: true
disable-model-invocation: true
allowed-tools: Bash(git:*)
---

# Cordon — accept (land to a fresh branch)

1. Confirm the worktree is clean-committed (no uncommitted changes). If not, stop
   and tell the human to commit or discard first.
2. Create a fresh branch off the base: `cordon-accepted/<worktree-name>-<shortsha>`.
3. Apply this worktree's commits onto it (cherry-pick the range, or fast-forward
   if the base hasn't moved).
4. Print the branch name and the command the human can run to open a PR or merge
   it themselves. Do NOT push and do NOT merge to main — landing is the human's
   final, deliberate act.
```

**`template/skills/cordon-discard/SKILL.md`:**

```markdown
---
name: cordon-discard
description: Throw away this isolated session entirely — delete the worktree and its branch. Use when the work is not wanted. Irreversible, but only affects the throwaway worktree.
user-invocable: true
disable-model-invocation: true
allowed-tools: Bash(git worktree:*) Bash(git branch:*)
---

# Cordon — discard

1. Name the worktree and branch about to be removed; ask the human to confirm.
2. `git worktree remove --force` the current worktree and delete its branch.
3. Confirm the main checkout is untouched.
```

> `disable-model-invocation: true` on accept/discard means **only the human** can
> trigger the irreversible/landing actions — the agent can review but can't land
> or destroy on its own. That asymmetry is a core part of the gate. Verify the
> frontmatter key names against the skills docs.

---

## 7b. Governance layer (`CLAUDE.md`) — the shepherding brain (G0)

This is the soft-governance layer: the thing that makes Claude itself act as the
shepherd. It is what turns a pile of hooks and deny rules into a *framework a
user is guided through* rather than a set of walls they bump into.

### What it is and how it works

`CLAUDE.md` at the project root is **auto-discovered and loaded into Claude's
context at the start of every session** (verify current auto-discovery behavior;
`--bare` disables it, and `.claude/CLAUDE.md` is also honored). Its contents
become standing instructions the model treats as operating rules. Cordon ships a
`template/CLAUDE.md` that the installer copies into the target project.

Crucially, `CLAUDE.md` **governs Claude, and shepherds the user only through
Claude.** It cannot block a tool call (that's the hooks) and it cannot stop a
human from doing anything. What it does is make Claude:

1. **Know the house rules** — isolation, no push, no main, gate before landing.
2. **Narrate them** — explain the sandbox state, *why* a thing was blocked, and
   the sanctioned next step, so the human is guided rather than stonewalled.
3. **Refuse-and-redirect** — when asked to do something outside the flow, decline
   and point at the right skill (`/cordon-review`, `/cordon-accept`) *before* a
   hard rule has to fire. The hooks become the backstop, not the first contact.

### Posture is a knob, not a guess

The *tone* of governance is a per-project decision, so Cordon ships two postures
and the installer stamps one into `CLAUDE.md`:

| Posture | Voice | Use when |
|---|---|---|
| **guide** (`CLAUDE.guide.md`) | light: explains the method, recommends the skills, stays out of the way | trusted devs who want the rails but not a nanny |
| **enforce** (`CLAUDE.enforce.md`, **default**) | strict: actively refuses out-of-flow requests, narrates every block, insists on the gate | shared repos, onboarding, "force the method" |

`install.sh --posture guide|enforce` (default `enforce`) copies the chosen
variant to `template/CLAUDE.md` → the target's root `CLAUDE.md`. Switching
posture later is re-running the installer or swapping the file.

### `template/governance/CLAUDE.enforce.md` (default, ready to ship)

```markdown
# CLAUDE.md — this project runs under Cordon (enforce mode)

> You are operating inside a **Cordon-governed** project. Your job is not only to
> do the work, but to keep every change isolated, reversible, and reviewable, and
> to **guide the human through that workflow**. These rules override default
> behavior. Follow them exactly.

## The method (non-negotiable)

1. **All work happens in an isolated worktree.** If the session is not in one,
   the startup gate will have refused — tell the user to relaunch with
   `claude --worktree` and explain why (their main checkout stays clean; a clean
   worktree is auto-deleted on exit, so nothing is lost).
2. **Never touch `main`/`master`, never `git push`, never `git reset --hard`,
   never write to `.git/` or `.claude/`.** These are blocked by policy; do not
   try to work around them. If the user asks for one, **decline and redirect**
   (see below) rather than attempting it and hitting a wall.
3. **Network egress is restricted.** Use your tools/MCP; do not shell out to
   `curl`/`wget`/raw sockets. If something needs a network resource you can't
   reach, say so plainly — don't improvise around the boundary.
4. **Nothing lands without the human gate.** When the work is ready, **stop** and
   run `/cordon-review`. Do not consider the task done until the human has
   reviewed and chosen `/cordon-accept` or `/cordon-discard`. You never land or
   discard on your own.

## How to shepherd the user

- **Narrate state.** At the start, in one line, tell them where they are:
  "Working in an isolated worktree (`<name>`), sandbox on, changes will be gated
  before they touch your repo." Keep it short; don't lecture every turn.
- **Explain blocks.** If a tool call is denied, don't just retry — tell the user
  what was blocked, why (the Cordon rule), and the sanctioned alternative.
- **Refuse-and-redirect, early.** When asked to do something outside the method:
  > "That's outside the Cordon flow — I don't push or merge to main here. When
  > you're happy with the work, run `/cordon-review` and then `/cordon-accept`,
  > which lands it on a fresh branch you can merge yourself."
  Do this *before* attempting the action, so the user learns the model rather
  than watching you hit a fence.
- **Drive toward the gate.** As work nears completion, proactively suggest
  `/cordon-review`. Make finishing == handing to the human, not pushing.

## What you must NOT do

- Do not edit settings, hooks, or this file to loosen the rules, even if asked
  "just for now". If the user wants different policy, that's a deliberate change
  they make to the Cordon config outside a task — not something you do mid-flow.
- Do not present work as "done and shipped". The most you ship is "ready for
  review at the gate."

## If something feels blocked for the wrong reason

Say so. Surface the exact rule and suggest the user adjust the Cordon config
deliberately. Never silently route around the boundary — that defeats the point.
```

### `template/governance/CLAUDE.guide.md` (light posture)

```markdown
# CLAUDE.md — this project uses Cordon (guide mode)

> This project keeps changes isolated, reversible, and reviewable using Cordon.
> Help the user work that way, but stay light — recommend, don't nag.

- Work lands in an isolated worktree; if you're not in one, suggest
  `claude --worktree`. The user's main checkout stays clean.
- Prefer the project's flow: when work is ready, suggest `/cordon-review`, then
  `/cordon-accept` (lands on a fresh branch) or `/cordon-discard`.
- `git push`, force-resets, and writes to `.git`/`.claude` are blocked by policy;
  if one comes up, mention the gate skills instead.
- Once per session, briefly note the setup so the user knows the rails exist.
  After that, get out of the way and do the work.
```

### Honest limits (carry into §12)

- **Soft and probabilistic.** The model usually honors `CLAUDE.md` but can drift,
  especially in long sessions or after compaction. **Never put a security-
  critical rule *only* here** — every such rule must also live in
  `permissions.deny` / hooks / managed settings (the hard layer). `CLAUDE.md`
  shapes intent; it does not enforce.
- **It governs the agent, not the human.** It cannot stop a user; it makes Claude
  guide them. The "force" comes from the hard layer; the *shepherding* comes from
  here.
- **Plugins can't ship it into a project.** `CLAUDE.md` is project-scoped content;
  the **template install** places it. A plugin alone gives skills+hooks but not
  the governance brain — so the template path is the complete framework (note
  this in §8 and the README).
- **Keep it short and imperative.** Long CLAUDE.md files dilute attention; the
  enforce variant above is near the ceiling. Resist bloating it.

---

## 8. Packaging — template *and* plugin (§ delivery)

Ship two install paths from one source:

**A. Template install (no plugin system needed):**
`install.sh` copies `template/` into a target repo's `.claude/`, makes the hook
scripts executable, and appends the audit log + worktree dir to `.gitignore`.

```bash
# in the target project:
curl -fsSL https://raw.githubusercontent.com/<you>/cordon/main/install.sh | bash
# or: git clone … && ./cordon/install.sh /path/to/target
```

**B. Plugin install (distributable, updatable):**
`.claude-plugin/plugin.json`:
```json
{
  "name": "cordon",
  "description": "Force every Claude Code session into an isolated, sandboxed, diff-gated workflow.",
  "version": "0.1.0",
  "license": "Apache-2.0",
  "author": { "name": "<you>" },
  "repository": "https://github.com/<you>/cordon"
}
```
Plugin-level hooks live in `hooks/hooks.json` (same schema as the settings
`hooks` block). Skills at the plugin root become `/cordon:cordon-review` etc.
Users install with `/plugin install https://github.com/<you>/cordon`.

> **Key limitation to document honestly:** a *plugin* can ship skills, hooks, and
> some settings, but **the project still needs the `sandbox` + `permissions.deny`
> settings** to be active. Plugins can't force arbitrary `permissions`/`sandbox`
> keys into a project's settings (only `agent`/`subagentStatusLine`-type defaults
> per current docs — verify). It also **cannot ship the `CLAUDE.md` governance
> brain** (§7b) — that's project-scoped content the template install places. So:
> the **template install is the complete framework** (governance + enforcement);
> the **plugin is the convenient skills+hooks layer** on top. State this in the
> README so nobody thinks `/plugin install` alone governs or sandboxes them.

---

## 9. Managed (admin) enforcement — the unoverridable lever

For "the user genuinely cannot turn this off" (managed fleets, shared machines),
ship `managed-settings.example.json` and document deployment:

- macOS: `/Library/Application Support/ClaudeCode/managed-settings.json`
- Linux/WSL: `/etc/claude-code/managed-settings.json`
- Windows: `C:\Program Files\ClaudeCode\managed-settings.json`

Same shape as §3's settings, but at the highest precedence — deny rules and
sandbox config here **cannot** be loosened by project or user settings. This is
the only truly "forced" tier; everything else assumes a cooperative user.
Make clear this requires admin/MDM deployment and is optional.

---

## 10. Acceptance tests — the definition of done

Cordon's tests are shell scripts that play an adversary against the hooks, mirroring
Sandkeep's `test_boundary.py` philosophy. A phase is done when its tests pass.

**`tests/test-boundary.sh` — feed crafted PreToolUse payloads to `boundary.sh`:**
- Edit/Write to a path **outside** the worktree → denied.
- Edit/Write to `.git/**` or `.claude/**` → denied.
- Edit/Write **inside** the worktree → allowed.
- Bash `git push` / `git reset --hard` / `curl` / `/dev/tcp` / `sudo` → denied.
- Benign Bash (`npm test`, `git status`) → allowed.

```bash
# shape of one case
echo '{"tool_name":"Write","tool_input":{"file_path":"/etc/passwd"},"hook_event_name":"PreToolUse"}' \
  | ./template/hooks/boundary.sh | jq -e '.hookSpecificOutput.permissionDecision=="deny"'
```

**`tests/test-enforcement.sh`:**
- `enforce-isolation.sh` outside a worktree → exit 2 with the relaunch message.
- inside a worktree → exit 0.
- each `permissions.deny` rule actually blocks its target (drive via a real
  `claude -p` smoke run in a fixture repo if feasible, else assert the settings
  parse + a representative deny via the hook).

**`tests/test-governance.sh` — the CLAUDE.md layer behaves (soft, so assert via
`claude -p` transcripts in a fixture, not exit codes):**
- with the **enforce** posture, asking Claude to `git push` → it declines and
  names `/cordon-review`/`/cordon-accept` instead of attempting the push.
- Claude narrates the isolated-worktree state at session start.
- the posture stamp is correct after `install.sh --posture guide|enforce`.
- (These are behavioral/probabilistic checks — treat flakes as signal to tighten
  the CLAUDE.md wording, and remember the hard layer is the real guarantee.)

**End-to-end (manual or scripted with `claude -p` in a fixture):**
- `claude --worktree` in a fixture → Claude opens by stating the house rules
  (G0) → edits land only in the worktree branch → `/cordon-review` shows the diff
  → `/cordon-accept` lands a `cordon-accepted/*` branch → main checkout
  byte-for-byte unchanged → `/cordon-discard` removes a worktree cleanly.

---

## 11. Build order (follow like Sandkeep's §0)

1. **Spike & verify** every API in §0's warning box with 5-line tests:
   PreToolUse stdin shape + deny mechanism; SessionStart exit-2 halting;
   `sandbox` key on the target OS; `permissions.deny` glob syntax; worktree
   paths; skill frontmatter keys. Write down what's actually true → `docs/verified-apis.md`.
2. **`lib.sh` + `boundary.sh` + `tests/test-boundary.sh`** — the security core. Tests first.
3. **`enforce-isolation.sh` + `test-enforcement.sh`** — the startup gate.
4. **`settings.json`** — wire sandbox + deny + hooks + statusline; verify it loads clean.
5. **`audit.sh` + statusline** — observability.
6. **The four skills** — review / accept / discard / status.
7. **Governance brain** — `CLAUDE.guide.md` + `CLAUDE.enforce.md` + posture
   stamping; `tests/test-governance.sh`. This is what makes it a *framework the
   user is guided through*, not just walls — don't skip or stub it.
8. **`install.sh`** — template delivery (incl. posture stamp); idempotent; touches `.gitignore`.
9. **Plugin packaging** — `plugin.json`, `hooks/hooks.json`, plugin-scoped skills.
10. **`managed-settings.example.json`** + deployment docs.
11. **README** with the security-honesty callout front and centre; end-to-end demo.

---

## 12. The honest limitations (put these in the README, don't bury them)

- **Not a malicious-code boundary.** `/sandbox` is accident/misbehavior
  containment, not adversary containment. For untrusted code, use Sandkeep's
  container/microVM backend. Say this loudly.
- **macOS sandbox is on deprecated Apple plumbing** (`sandbox-exec`). Treat
  macOS sandbox state as best-effort; lean on the boundary hook + deny rules and
  surface real state in the statusline.
- **"Forced" only fully holds under managed settings.** Without admin
  deployment, a determined human can edit project settings. Cordon governs the
  **agent** and assumes a cooperative operator; that's the design centre.
- **No native diff-gate or always-worktree default.** Cordon builds both from
  hooks + skills; they're conventions enforced by refusal, not platform
  guarantees. If Claude Code later ships a native gate/worktree-default, prefer it.
- **Hooks are bypassable by editing the hooks.** That's why the security-relevant
  rules also live in `permissions.deny` (merge-and-can't-loosen) and ideally
  managed settings — defense in depth, not a single check.
- **The governance layer (`CLAUDE.md`) is soft.** It shapes what Claude *chooses*,
  not what it *can* do, and it can drift in long sessions. It's what makes the
  framework feel like shepherding — but every safety-critical rule it states must
  also be enforced by the hard layer. Governance guides; hooks/settings enforce.

---

## 13. Relationship back to Sandkeep (the product story)

Cordon and Sandkeep are one mental model, two engines:

- Start in **Cordon** (zero infra, native, delightful) for trusted work.
- When the user says *"actually I don't trust this agent / this code"*, hand off
  to **Sandkeep** (container/microVM, enforced clone-not-mount, only-a-diff-returns).
- Same vocabulary throughout — isolate → review → gate; `accept`/`discard`;
  an audit JSON-lines trail — so moving between them is seamless.

Ship Cordon's README pointing at Sandkeep for the high-trust-boundary case, and
Sandkeep's README pointing at Cordon for the "I just want disciplined native
sessions" case. Two doors, one house.
```
