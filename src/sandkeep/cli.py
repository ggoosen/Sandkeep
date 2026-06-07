"""`sandkeep` CLI (BUILD_SPEC §10) — the Phase 1 human gate.

User-facing text goes to stdout/stderr here and only here; everything
machine-readable goes to the audit log. The human gate is a local patch
apply. TODO(phase-2): draft PR instead.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .audit import AuditLog
from .config import Config, resource_path
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


def _cmd_image_build(cfg: Config, args: argparse.Namespace) -> int:
    build_image(resource_path("sandbox_image"), cfg.image)
    print(f"built {cfg.image}")
    return 0


def _cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY is not set", file=sys.stderr)
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
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY is not set", file=sys.stderr)
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
