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

from .audit import AuditLog
from .config import Config, load_api_key, resource_path, stored_api_key
from .controller import Controller, ControllerError
from .models import TaskState
from .sandbox.docker_provider import DockerConfig, DockerProvider, build_image
from .state_store import StateStore, TaskNotFound

SECURITY_BANNER = (
    "⚠  sandkeep is alpha: the Docker backend is a mechanics harness, NOT a\n"
    "   security boundary. Do not run agents or code you genuinely distrust.\n"
)


def _make_controller(cfg: Config, *, network: str) -> Controller:
    cfg.ensure_dirs()
    audit = AuditLog(cfg.audit_log_path)
    store = StateStore(cfg.db_path, audit=audit)
    provider = DockerProvider(DockerConfig(image=cfg.image, network=network))
    return Controller(cfg, store, audit, provider, network_denied=(network == "none"))


def _require_api_key(cfg: Config) -> bool:
    """Resolve the API key (env wins, else `sandkeep auth set` storage) and
    export it for the controller. False if none is available."""
    key = load_api_key(cfg)
    if not key:
        print(
            "error: no Anthropic API key — run `sandkeep auth set` "
            "or export ANTHROPIC_API_KEY",
            file=sys.stderr,
        )
        return False
    os.environ["ANTHROPIC_API_KEY"] = key
    return True


def _mask(key: str) -> str:
    return f"{key[:7]}…{key[-4:]}" if len(key) > 14 else "…"


def _cmd_auth(cfg: Config, args: argparse.Namespace) -> int:
    if args.auth_command == "set":
        if sys.stdin.isatty():
            key = getpass.getpass("Anthropic API key (input hidden): ").strip()
        else:
            key = sys.stdin.readline().strip()  # piped: echo $KEY | sandkeep auth set
        if not key:
            print("error: empty key", file=sys.stderr)
            return 1
        if not key.startswith("sk-ant-"):
            print("warning: key does not look like an Anthropic key (sk-ant-…); storing anyway",
                  file=sys.stderr)
        cfg.home.mkdir(parents=True, exist_ok=True)
        cfg.env_file.write_text(f"ANTHROPIC_API_KEY={key}\n")
        cfg.env_file.chmod(0o600)
        print(f"stored {_mask(key)} at {cfg.env_file} (mode 0600)")
        print("note: plaintext on disk — treat like ~/.aws/credentials. "
              "An exported ANTHROPIC_API_KEY always takes precedence.")
        return 0
    if args.auth_command == "status":
        env_key = os.environ.get("ANTHROPIC_API_KEY")
        file_key = stored_api_key(cfg)
        print(f"environment: {_mask(env_key) if env_key else '(not set)'}")
        print(f"stored file: {_mask(file_key) if file_key else '(none)'}"
              + (f"  [{cfg.env_file}]" if file_key else ""))
        effective = env_key or file_key
        print(f"effective:   {_mask(effective) if effective else 'NO KEY — runs will fail'}"
              + ("  (from environment)" if env_key else "  (from stored file)" if file_key else ""))
        return 0
    if args.auth_command == "clear":
        if cfg.env_file.exists():
            cfg.env_file.unlink()
            print(f"removed {cfg.env_file}")
        else:
            print("nothing stored")
        return 0
    return 1


def _cmd_image_build(cfg: Config, args: argparse.Namespace) -> int:
    build_image(resource_path("sandbox_image"), cfg.image)
    print(f"built {cfg.image}")
    return 0


def _cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    if not _require_api_key(cfg):
        return 2
    print(SECURITY_BANNER, file=sys.stderr)
    # the agent needs egress to api.anthropic.com — gated here on purpose;
    # TODO(phase-2): brokering egress proxy + secret broker
    controller = _make_controller(cfg, network="egress")
    task = controller.run_task(
        args.repo,
        args.task,
        model=args.model,
        max_turns=args.max_turns,
    )
    if task.state is TaskState.REVIEW:
        results_path = cfg.outputs_dir / f"{task.id}.results.json"
        contract = json.loads(results_path.read_text())
        print(f"task {task.id}: ready for review")
        print(f"\n  summary: {contract['summary']}")
        print(f"  files:   {', '.join(contract['files_changed'])}")
        print(f"  patch:   {task.patch_path}")
        print(f"\n  sandkeep accept {task.id}   # apply to a fresh branch")
        print(f"  sandkeep reject {task.id}   # discard")
        return 0
    print(f"task {task.id} ended in state: {task.state.value}", file=sys.stderr)
    last = controller.store.get_transitions(task.id)[-1]
    print(f"  detail: {last['detail']}", file=sys.stderr)
    return 1


def _cmd_shell(cfg: Config, args: argparse.Namespace) -> int:
    if not _require_api_key(cfg):
        return 2
    print(SECURITY_BANNER, file=sys.stderr)
    print("provisioning sandbox — your repo is mounted read-only, work happens "
          "on a clone inside\n", file=sys.stderr)
    # interactive agent needs egress to api.anthropic.com (gated on purpose;
    # TODO(phase-2): brokering egress proxy)
    controller = _make_controller(cfg, network="egress")
    task = controller.run_interactive(args.repo, model=args.model, seed=args.task)
    if task.state is TaskState.REVIEW:
        print(f"\nsession ended — task {task.id} is ready for review")
        print(f"  patch: {task.patch_path}")
        print(f"\n  sandkeep show   {task.id}")
        print(f"  sandkeep accept {task.id}   # apply to a fresh branch")
        print(f"  sandkeep reject {task.id}   # discard")
        return 0
    last = controller.store.get_transitions(task.id)[-1]
    print(f"\ntask {task.id} ended in state: {task.state.value} ({last['detail']})",
          file=sys.stderr)
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
    return 0


def _cmd_accept(cfg: Config, args: argparse.Namespace) -> int:
    controller = _make_controller(cfg, network="none")
    sha = controller.accept(args.task_id)
    task = controller.store.get_task(args.task_id)
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
    image_sub.add_parser("build", help="build the sandbox image")

    auth = sub.add_parser("auth", help="manage the Anthropic API key")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_sub.add_parser("set", help="store the API key (hidden prompt, or piped stdin)")
    auth_sub.add_parser("status", help="show where the key would come from (masked)")
    auth_sub.add_parser("clear", help="remove the stored key")

    run = sub.add_parser("run", help="run a single governed task")
    run.add_argument("--repo", required=True, help="path to the target git repo")
    run.add_argument("--task", required=True, help="instruction for the agent")
    run.add_argument("--model", default=None)
    run.add_argument("--max-turns", type=int, default=None)

    shell = sub.add_parser(
        "shell", help="open an interactive Claude Code session inside a sandbox"
    )
    shell.add_argument("--repo", required=True, help="path to the target git repo")
    shell.add_argument(
        "--task", default=None, help="optional seed for the agent's first message"
    )
    shell.add_argument("--model", default=None)

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
            "status": _cmd_status,
            "show": _cmd_show,
            "accept": _cmd_accept,
            "reject": _cmd_reject,
        }[args.command]
        return handler(cfg, args)
    except TaskNotFound as exc:
        print(f"error: no such task: {exc}", file=sys.stderr)
        return 1
    except ControllerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def entrypoint() -> None:  # console_script shim
    raise SystemExit(main())
