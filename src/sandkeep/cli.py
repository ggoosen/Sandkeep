"""`sandkeep` CLI (BUILD_SPEC §10) — the Phase 1 human gate.

User-facing text goes to stdout/stderr here and only here; everything
machine-readable goes to the audit log. The human gate is a local patch
apply. TODO(phase-2): draft PR instead.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

from . import skills
from .agent import UnknownAgent, available_agents, get_driver
from .audit import AuditLog
from .config import (
    DEFAULT_AGENT,
    Config,
    clear_secret,
    load_secret,
    resource_path,
    stored_secrets,
    write_secret,
)
from .controller import Controller, ControllerError
from .models import TaskState
from .sandbox.docker_provider import DockerConfig, DockerProvider, build_image
from .state_store import StateStore, TaskNotFound

SECURITY_BANNER = (
    "⚠  sandkeep is alpha: the Docker backend is a mechanics harness, NOT a\n"
    "   security boundary. Do not run agents or code you genuinely distrust.\n"
)


def _make_provider(cfg: Config, *, network: str):
    """Construct the configured sandbox backend (SANDKEEP_BACKEND)."""
    if cfg.backend == "e2b":
        # Imported lazily so the optional 'e2b' dependency isn't required for
        # the default Docker backend.
        from .sandbox.e2b_provider import E2BConfig, E2BProvider

        return E2BProvider(E2BConfig(template=cfg.e2b_template, network=network))
    return DockerProvider(DockerConfig(image=cfg.image, network=network))


def _make_controller(cfg: Config, *, network: str) -> Controller:
    cfg.ensure_dirs()
    audit = AuditLog(cfg.audit_log_path)
    store = StateStore(cfg.db_path, audit=audit)
    provider = _make_provider(cfg, network=network)
    return Controller(cfg, store, audit, provider, network_denied=(network == "none"))


def _ensure_named_secret(cfg: Config, name: str) -> bool:
    """Resolve a secret by name (env wins, else `sandkeep auth set` storage) and
    export it into the environment for the run. False (with a hint) if missing."""
    value = load_secret(cfg, name)
    if not value:
        print(
            f"error: no {name} — run `sandkeep auth set {name}` or export {name}",
            file=sys.stderr,
        )
        return False
    os.environ[name] = value
    return True


def _ensure_secret(cfg: Config, driver) -> bool:
    """Make the selected agent's credential available before a run, by the
    driver's declared secret_env (claude → ANTHROPIC_API_KEY)."""
    return _ensure_named_secret(cfg, driver.secret_env)


def _mask(key: str) -> str:
    return f"{key[:7]}…{key[-4:]}" if len(key) > 14 else "…"


def _resolve_network(cfg: Config, args: argparse.Namespace) -> str:
    """Network mode for a run: `--no-network` forces off, else config/env
    (SANDKEEP_NETWORK, default egress). The agent needs egress for its API, so
    `none` is for boundary testing / offline agents (BUILD_SPEC §16)."""
    if getattr(args, "no_network", False):
        return "none"
    return cfg.network


def _warn_if_no_network(network: str) -> None:
    if network == "none":
        print(
            "⚠ --no-network: the sandbox has NO network. The agent cannot reach "
            "its API, so a normal run will fail — this is for boundary testing or "
            "an offline agent.",
            file=sys.stderr,
        )


def _print_policy(controller: Controller, task) -> None:
    """Surface Phase 3 diff-risk flags + cross-task conflicts at the gate
    (advisory — the human still decides)."""
    flags = controller.risk_flags(task)
    if flags:
        print("\n  ⚠ risk flags:")
        for f in flags:
            print(f"      [{f.category}] {f.detail}")
    conflicts = controller.conflicts(task)
    if conflicts:
        print("\n  ⚠ conflicts — other tasks in review touch the same files:")
        for c in conflicts:
            print(f"      {c.other_task_id}: {', '.join(c.files)}")
    authored = controller.authored_skills(task)
    if authored:
        print("\n  ✎ skills authored (registered for this repo on accept):")
        for s in authored:
            print(f"      {s.name} — {s.description}")


# Friendly "that doesn't look right" hints per known key (not enforced).
_KEY_PREFIX_HINTS = {"ANTHROPIC_API_KEY": "sk-ant-", "E2B_API_KEY": "e2b_"}


def _relevant_key_names(cfg: Config) -> list[str]:
    """Names worth showing in `auth status`: the well-known ones, every agent
    driver's secret_env, plus anything already stored."""
    names = {"ANTHROPIC_API_KEY", "E2B_API_KEY"}
    for agent in available_agents():
        names.add(get_driver(agent).secret_env)
    names |= set(stored_secrets(cfg))
    return sorted(names)


def _cmd_auth(cfg: Config, args: argparse.Namespace) -> int:
    if args.auth_command == "set":
        name = args.name
        if sys.stdin.isatty():
            key = getpass.getpass(f"{name} (input hidden): ").strip()
        else:
            key = sys.stdin.readline().strip()  # piped: echo $KEY | sandkeep auth set
        if not key:
            print("error: empty key", file=sys.stderr)
            return 1
        prefix = _KEY_PREFIX_HINTS.get(name)
        if prefix and not key.startswith(prefix):
            print(f"warning: {name} doesn't look like a {prefix}… key; storing anyway",
                  file=sys.stderr)
        write_secret(cfg, name, key)
        print(f"stored {name} = {_mask(key)} at {cfg.env_file} (mode 0600)")
        print(f"note: plaintext on disk — treat like ~/.aws/credentials. "
              f"An exported {name} always takes precedence.")
        return 0
    if args.auth_command == "status":
        stored = stored_secrets(cfg)
        for name in _relevant_key_names(cfg):
            env_key = os.environ.get(name)
            file_key = stored.get(name)
            effective = env_key or file_key
            src = ("from environment" if env_key
                   else "from stored file" if file_key else "MISSING")
            shown = _mask(effective) if effective else "(not set)"
            print(f"{name}: {shown}  ({src})")
        print(f"\nstored file: {cfg.env_file}")
        return 0
    if args.auth_command == "clear":
        if clear_secret(cfg, args.name):
            print(f"removed {args.name or 'all stored keys'}")
        else:
            print("nothing stored" if args.name is None else f"no stored {args.name}")
        return 0
    return 1


def _cmd_image_build(cfg: Config, args: argparse.Namespace) -> int:
    driver = get_driver(args.agent or cfg.agent)
    if driver.name != DEFAULT_AGENT:
        # The static sandbox_image/Dockerfile bakes in the default agent only.
        # TODO(phase-5): template the Dockerfile from driver.install_steps() so
        # `image build --agent <name>` renders a per-agent image.
        print(
            f"error: per-agent image build for '{driver.name}' is not implemented "
            "yet; only the default 'claude' image builds from sandbox_image/Dockerfile",
            file=sys.stderr,
        )
        return 1
    build_image(resource_path("sandbox_image"), cfg.image)
    print(f"built {cfg.image} (agent: {driver.name})")
    return 0


def _cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    driver = get_driver(args.agent or cfg.agent)
    if not _ensure_secret(cfg, driver):
        return 2
    if cfg.backend == "e2b" and not _ensure_named_secret(cfg, "E2B_API_KEY"):
        return 2
    print(SECURITY_BANNER, file=sys.stderr)
    # the agent needs egress to api.anthropic.com by default — gated here on
    # purpose; TODO(phase-2): brokering egress *allowlist* proxy + secret broker.
    network = _resolve_network(cfg, args)
    _warn_if_no_network(network)
    controller = _make_controller(cfg, network=network)
    task = controller.run_task(
        args.repo,
        args.task,
        model=args.model,
        agent=driver.name,
        max_turns=args.max_turns,
    )
    if task.state is TaskState.REVIEW:
        results_path = cfg.outputs_dir / f"{task.id}.results.json"
        contract = json.loads(results_path.read_text())
        print(f"task {task.id}: ready for review")
        print(f"\n  summary: {contract['summary']}")
        print(f"  files:   {', '.join(contract['files_changed'])}")
        print(f"  patch:   {task.patch_path}")
        _print_policy(controller, task)
        print(f"\n  sandkeep accept {task.id}   # apply to a fresh branch")
        print(f"  sandkeep reject {task.id}   # discard")
        return 0
    print(f"task {task.id} ended in state: {task.state.value}", file=sys.stderr)
    last = controller.store.get_transitions(task.id)[-1]
    print(f"  detail: {last['detail']}", file=sys.stderr)
    return 1


def _cmd_shell(cfg: Config, args: argparse.Namespace) -> int:
    driver = get_driver(args.agent or cfg.agent)
    if not _ensure_secret(cfg, driver):
        return 2
    if cfg.backend == "e2b" and not _ensure_named_secret(cfg, "E2B_API_KEY"):
        return 2
    print(SECURITY_BANNER, file=sys.stderr)
    print("provisioning sandbox — your repo is mounted read-only, work happens "
          "on a clone inside\n", file=sys.stderr)
    # interactive agent needs egress to api.anthropic.com by default (gated on
    # purpose; TODO(phase-2): brokering egress allowlist proxy)
    network = _resolve_network(cfg, args)
    _warn_if_no_network(network)
    controller = _make_controller(cfg, network=network)
    task = controller.run_interactive(
        args.repo, model=args.model, agent=driver.name, seed=args.task,
        skip_permissions=args.skip_permissions,
    )
    if task.state is TaskState.REVIEW:
        print(f"\nsession ended — task {task.id} is ready for review")
        print(f"  patch: {task.patch_path}")
        _print_policy(controller, task)
        print(f"\n  sandkeep show   {task.id}")
        print(f"  sandkeep accept {task.id}   # apply to a fresh branch")
        print(f"  sandkeep reject {task.id}   # discard")
        return 0
    last = controller.store.get_transitions(task.id)[-1]
    print(f"\ntask {task.id} ended in state: {task.state.value} ({last['detail']})",
          file=sys.stderr)
    return 1


def _cmd_skills(cfg: Config, args: argparse.Namespace) -> int:
    if args.skills_command == "list":
        store = skills.SkillStore(cfg.home, args.repo)
        items = store.list()
        if not items:
            print(f"no skills stored for {args.repo}")
            return 0
        print(f"skills for {args.repo}:")
        for s in items:
            print(f"  {s.name} — {s.description}")
        return 0
    return 1


def _cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    controller = _make_controller(cfg, network="none")
    task = controller.store.get_task(args.task_id)
    print(f"{task.id}  {task.state.value}")
    for row in controller.store.get_transitions(task.id):
        print(f"  {row['ts']}  {row['from_state']} → {row['to_state']}  {row['detail']}")
    return 0


def _cmd_show(cfg: Config, args: argparse.Namespace) -> int:
    controller = _make_controller(cfg, network="none")
    task = controller.store.get_task(args.task_id)
    print(f"task:  {task.id}")
    print(f"state: {task.state.value}")
    print(f"repo:  {task.repo_path}")
    print(f"base:  {task.base_ref}")
    results_path = cfg.outputs_dir / f"{task.id}.results.json"
    if results_path.exists():
        print(f"\nresults contract ({results_path}):")
        print(json.dumps(json.loads(results_path.read_text()), indent=2))
    if task.patch_path:
        print(f"\npatch: {task.patch_path}")
    _print_policy(controller, task)
    return 0


def _cmd_accept(cfg: Config, args: argparse.Namespace) -> int:
    controller = _make_controller(cfg, network="none")
    task = controller.store.get_task(args.task_id)
    conflicts = controller.conflicts(task)
    if conflicts:
        print("⚠ warning: this patch overlaps files with other tasks awaiting review:",
              file=sys.stderr)
        for c in conflicts:
            print(f"    {c.other_task_id}: {', '.join(c.files)}", file=sys.stderr)
        print("  applying anyway (human gate) — re-review the others before accepting them.",
              file=sys.stderr)
    sha = controller.accept(args.task_id)
    print(f"applied to branch sandkeep-accepted/{task.id} @ {sha[:12]}")
    print("your working tree and current branch are untouched")
    return 0


def _cmd_reject(cfg: Config, args: argparse.Namespace) -> int:
    controller = _make_controller(cfg, network="none")
    controller.reject(args.task_id)
    print(f"task {args.task_id} rejected; sandbox discarded, host repo untouched")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sandkeep")
    sub = parser.add_subparsers(dest="command", required=True)

    image = sub.add_parser("image", help="manage the sandbox image")
    image_sub = image.add_subparsers(dest="image_command", required=True)
    image_build = image_sub.add_parser("build", help="build the sandbox image")
    image_build.add_argument(
        "--agent", default=None,
        help=f"agent to build the image for (default: {DEFAULT_AGENT}; "
             f"available: {', '.join(available_agents())})",
    )

    auth = sub.add_parser("auth", help="manage stored API keys (Anthropic, E2B, …)")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_set = auth_sub.add_parser(
        "set", help="store a key (hidden prompt, or piped stdin)")
    auth_set.add_argument(
        "name", nargs="?", default="ANTHROPIC_API_KEY",
        help="which key to store (default: ANTHROPIC_API_KEY; e.g. E2B_API_KEY)")
    auth_sub.add_parser("status", help="show where each key would come from (masked)")
    auth_clear = auth_sub.add_parser("clear", help="remove a stored key (or all)")
    auth_clear.add_argument(
        "name", nargs="?", default=None,
        help="which key to remove (default: all stored keys)")

    run = sub.add_parser("run", help="run a single governed task")
    run.add_argument("--repo", required=True, help="path to the target git repo")
    run.add_argument("--task", required=True, help="instruction for the agent")
    run.add_argument("--model", default=None)
    run.add_argument(
        "--agent", default=None,
        help=f"agent to run in the sandbox (default: {DEFAULT_AGENT}; "
             f"available: {', '.join(available_agents())})",
    )
    run.add_argument("--max-turns", type=int, default=None)
    run.add_argument(
        "--no-network", action="store_true",
        help="run with no network at all (agent can't reach its API; for "
             "boundary testing / offline agents). Default: egress (SANDKEEP_NETWORK).",
    )

    shell = sub.add_parser(
        "shell", help="open an interactive Claude Code session inside a sandbox"
    )
    shell.add_argument("--repo", required=True, help="path to the target git repo")
    shell.add_argument(
        "--task", default=None, help="optional seed for the agent's first message"
    )
    shell.add_argument("--model", default=None)
    shell.add_argument(
        "--agent", default=None,
        help=f"agent to run in the sandbox (default: {DEFAULT_AGENT}; "
             f"available: {', '.join(available_agents())})",
    )
    shell.add_argument(
        "--no-skip-permissions",
        dest="skip_permissions",
        action="store_false",
        help="restore Claude Code permission prompts (default: skipped, since "
             "the session runs inside the sandbox)",
    )
    shell.add_argument(
        "--no-network", action="store_true",
        help="run with no network at all (agent can't reach its API; for "
             "boundary testing / offline agents). Default: egress (SANDKEEP_NETWORK).",
    )

    sk = sub.add_parser("skills", help="manage per-repo authored skills")
    sk_sub = sk.add_subparsers(dest="skills_command", required=True)
    sk_list = sk_sub.add_parser("list", help="list skills authored for a repo")
    sk_list.add_argument("--repo", required=True, help="path to the target git repo")

    for name in ("status", "show", "accept", "reject"):
        p = sub.add_parser(name)
        p.add_argument("task_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.from_env()
    try:
        if args.command == "image":
            return _cmd_image_build(cfg, args)
        if args.command == "auth":
            return _cmd_auth(cfg, args)
        handler = {
            "run": _cmd_run,
            "shell": _cmd_shell,
            "skills": _cmd_skills,
            "status": _cmd_status,
            "show": _cmd_show,
            "accept": _cmd_accept,
            "reject": _cmd_reject,
        }[args.command]
        return handler(cfg, args)
    except TaskNotFound as exc:
        print(f"error: no such task: {exc}", file=sys.stderr)
        return 1
    except UnknownAgent as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ControllerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def entrypoint() -> None:  # console_script shim
    raise SystemExit(main())
