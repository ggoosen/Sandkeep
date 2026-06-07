# Quickstart — your first Sandkeep run

A complete first run in under five minutes, end to end, on a throwaway repo so
nothing real is at risk.

> **Reminder:** this reflects the Phase 1 single-task loop, on the Docker backend.
> Docker is not a security boundary — keep this to code you trust for now.

## Prerequisites

- **Python 3.12+** and **Docker** running on your machine.
- An **Anthropic API key**: `export ANTHROPIC_API_KEY=sk-ant-...`
- (Node 22 and the `claude` CLI are installed *inside* the sandbox image by
  `sandkeep image build` — you don't need them on the host.)

## 1. Install Sandkeep and build the sandbox image

```bash
pip install sandkeep            # or: uvx sandkeep
sandkeep image build            # one-time; builds the Node 22 + claude + git + mise image
```

## 2. Make a throwaway repo to experiment on

```bash
mkdir /tmp/sk-demo && cd /tmp/sk-demo
git init -q
cat > parse_config.py <<'PY'
def parse_config(raw):
    # no validation yet
    return {k: v for k, v in (line.split("=", 1) for line in raw.splitlines())}
PY
git add -A && git commit -qm "initial"
```

## 3. Run a task

```bash
sandkeep run \
  --repo /tmp/sk-demo \
  --task "Add input validation to parse_config(): skip blank lines, raise ValueError on malformed lines, and add a short docstring."
```

Sandkeep will: mount `/tmp/sk-demo` read-only, make an independent clone on a task
branch inside the sandbox, run the agent with a scoped tool set, then extract a
patch and a results summary. It stops at the review gate and prints a `task_id`.

## 4. Review what the agent produced

```bash
sandkeep status <task_id>       # current state
sandkeep show <task_id>         # summary, files changed, and the patch path
```

Open the patch and read it. This is the human gate — nothing has touched your repo
yet.

## 5. Accept or reject

```bash
# happy with it — apply to a NEW branch on your repo (never your working tree)
sandkeep accept <task_id>
cd /tmp/sk-demo && git log --oneline -1 && git branch

# or discard it and tear down the sandbox
sandkeep reject <task_id>
```

On `accept`, Sandkeep checks out a fresh `sandkeep-accepted/<task_id>` branch from
your base commit and applies the patch there, so you can review/merge it with your
normal git workflow. On `reject` (or any violation), the sandbox is destroyed and
your repo is untouched.

## What just happened

You ran an untrusted agent against real code, saw exactly what it wanted to change,
and decided whether it lands — with your actual repo never modified until you said
so. That's the whole loop. Everything after this (microVM isolation, parallelism,
conflict detection) builds on top of it.

## Troubleshooting

- **`auth error` / exit code 2** — check `ANTHROPIC_API_KEY` is exported.
- **Docker errors** — confirm the Docker daemon is running and `sandkeep image build` succeeded.
- **Patch won't apply on accept** — the base repo moved; re-run against a clean checkout.
