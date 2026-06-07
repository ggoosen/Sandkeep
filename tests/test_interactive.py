"""Interactive session acceptance (BUILD_SPEC §10b) against a live sandbox
with the TTY session stubbed: same provisioning + diff gate as headless;
synthesized contract; empty diff rolls back; accept lands a fresh branch.

The real TTY (`docker exec -it claude`) can't run in pytest, so we patch
the provider's exec_interactive to simulate what a user-driven session
leaves behind in the clone, then assert the controller's host-side
handling end to end.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sandkeep.config import Config
from sandkeep.controller import Controller
from sandkeep.models import TaskState


def _container_exists(name: str) -> bool:
    return subprocess.run(["docker", "inspect", name], capture_output=True).returncode == 0


def _chain(store, task_id):
    return [(r["from_state"], r["to_state"]) for r in store.get_transitions(task_id)]


@pytest.fixture
def controller(tmp_path, monkeypatch, store, audit, provider) -> Controller:
    monkeypatch.setenv("SANDKEEP_HOME", str(tmp_path / "skhome"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # interactive needs egress in real use; the stub doesn't, so a network-none
    # provider is fine here and keeps the test hermetic
    return Controller(Config.from_env(), store, audit, provider, network_denied=False)


def _simulate_session(edits):
    """Return an exec_interactive replacement that runs `edits` (a shell
    script) inside the sandbox as if the user had done it, then 'exits'."""

    def fake(handle, cmd):
        import subprocess as sp

        sp.run(["docker", "exec", handle.id, "sh", "-c", edits], check=True)
        return 0

    return fake


def test_interactive_lands_in_review_with_synthesized_contract(
    controller, store, host_repo, monkeypatch
):
    monkeypatch.setattr(
        controller.provider,
        "exec_interactive",
        _simulate_session(
            "echo '# edited interactively' >> /work/repo/parse_config.py"
            " && echo 'note' >> /work/repo/README.md"
        ),
    )
    task = controller.run_interactive(str(host_repo), seed="add validation")
    assert task.state is TaskState.REVIEW
    assert _chain(store, task.id) == [
        ("new", "provisioning"),
        ("provisioning", "running"),
        ("running", "succeeded"),
        ("succeeded", "review"),
    ]
    # contract synthesized host-side from the patch
    contract = (controller.config.outputs_dir / f"{task.id}.results.json").read_text()
    assert "parse_config.py" in contract and "README.md" in contract
    assert "Interactive" in contract
    assert Path(task.patch_path).exists()
    assert _container_exists(task.sandbox_id)  # rollback target until gated
    controller.reject(task.id)


def test_interactive_empty_diff_rolls_back(controller, store, host_repo, monkeypatch):
    monkeypatch.setattr(
        controller.provider, "exec_interactive", _simulate_session("true")  # no edits
    )
    task = controller.run_interactive(str(host_repo))
    assert task.state is TaskState.ROLLED_BACK
    assert ("running", "failed") in _chain(store, task.id)
    assert not _container_exists(task.sandbox_id)


def test_interactive_accept_lands_fresh_branch(controller, store, host_repo, monkeypatch):
    monkeypatch.setattr(
        controller.provider,
        "exec_interactive",
        _simulate_session("echo '# interactive change' >> /work/repo/parse_config.py"),
    )
    task = controller.run_interactive(str(host_repo))
    sha = controller.accept(task.id)
    assert store.get_task(task.id).state is TaskState.MERGED
    shown = subprocess.run(
        ["git", "-C", str(host_repo), "show", f"sandkeep-accepted/{task.id}:parse_config.py"],
        capture_output=True, text=True,
    )
    assert "# interactive change" in shown.stdout
    assert sha
    assert not _container_exists(task.sandbox_id)


def test_interactive_host_repo_untouched_until_accept(
    controller, store, host_repo, monkeypatch
):
    import hashlib

    def digest(root: Path) -> str:
        h = hashlib.sha256()
        for p in sorted(root.rglob("*")):
            if p.is_file():
                h.update(str(p.relative_to(root)).encode())
                h.update(p.read_bytes())
        return h.hexdigest()

    before = digest(Path(host_repo))
    monkeypatch.setattr(
        controller.provider,
        "exec_interactive",
        _simulate_session("echo x >> /work/repo/parse_config.py"),
    )
    task = controller.run_interactive(str(host_repo))
    assert digest(Path(host_repo)) == before  # at REVIEW: still untouched
    controller.reject(task.id)
    assert digest(Path(host_repo)) == before  # after reject: still untouched


def test_provisioning_failure_rolls_back_interactive(
    controller, store, monkeypatch, tmp_path
):
    task = controller.run_interactive(str(tmp_path / "does-not-exist"))
    assert task.state is TaskState.ROLLED_BACK
