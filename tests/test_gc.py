"""Sandbox housekeeping: `ps` classification, `gc` reaping, and the
provisioner not leaking its own container on failure (BUILD_SPEC §12).

Docker-backed.
"""

from __future__ import annotations

import json
import shlex
import uuid

import pytest

from sandkeep import agent_runner
from sandkeep.agent_runner import AgentRunResult
from sandkeep.audit import new_trace_id
from sandkeep.config import Config
from sandkeep.controller import Controller
from sandkeep.models import Task, TaskState
from sandkeep.provisioner import ProvisioningError, provision
from sandkeep.sandbox.base import SandboxHandle


@pytest.fixture
def controller(tmp_path, monkeypatch, store, audit, provider) -> Controller:
    monkeypatch.setenv("SANDKEEP_HOME", str(tmp_path / "skhome"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return Controller(Config.from_env(), store, audit, provider, network_denied=True)


def _container_exists(name: str) -> bool:
    import subprocess

    return subprocess.run(["docker", "inspect", name], capture_output=True).returncode == 0


def _do_work(task, provider, handle):
    contract = json.dumps({
        "task_id": task.id, "status": "succeeded",
        "summary": "x", "files_changed": ["parse_config.py"],
    })
    script = (
        "echo '# x' >> /work/repo/parse_config.py && mkdir -p /work/repo/.sandkeep"
        f" && echo {shlex.quote(contract)} > /work/repo/.sandkeep/results.json"
    )
    assert provider.exec(handle, ["sh", "-c", script], timeout=60).exit_code == 0
    return AgentRunResult(0, False, "", "", {"usage": {"input_tokens": 1, "output_tokens": 1}}, "")


# -- provisioner no longer leaks on failure ------------------------------


def test_provision_failure_destroys_its_container(provider, store, audit, host_repo, monkeypatch):
    # force a provisioning step to fail AFTER the container is created
    real_exec = provider.exec
    calls = {"n": 0}

    def flaky_exec(handle, cmd, timeout):
        calls["n"] += 1
        if calls["n"] == 1:  # first git step → fail
            from sandkeep.sandbox.base import ExecResult
            return ExecResult(1, "", "boom")
        return real_exec(handle, cmd, timeout)

    monkeypatch.setattr(provider, "exec", flaky_exec)
    t = Task(id=uuid.uuid4().hex, repo_path=str(host_repo), instruction="x")
    store.create_task(t, new_trace_id())
    with pytest.raises(ProvisioningError):
        provision(t, provider, store, audit, env={}, trace_id=new_trace_id())
    # the container that was created must have been cleaned up
    sid = store.get_task(t.id).sandbox_id
    assert sid
    assert not _container_exists(sid)


# -- ps / gc -------------------------------------------------------------


def test_ps_classifies_orphan_review_and_stale(controller, store, host_repo, monkeypatch):
    monkeypatch.setattr(agent_runner, "run_agent",
                        lambda task, p, h, audit, **kw: _do_work(task, p, h))
    # a review sandbox (alive on purpose)
    review = controller.run_task(str(host_repo), "review one")
    assert review.state is TaskState.REVIEW
    # an orphan: a container with no task record
    orphan = controller.provider.create(str(host_repo), {})

    kinds = {s.id: s.kind for s in controller.list_sandboxes()}
    assert kinds.get(review.sandbox_id) == "review"
    assert kinds.get(orphan.id) == "orphan"

    # gc (default) reaps the orphan, keeps the review
    reaped = controller.gc()
    assert orphan.id in {s.id for s in reaped}
    assert review.sandbox_id not in {s.id for s in reaped}
    assert not _container_exists(orphan.id)
    assert _container_exists(review.sandbox_id)

    # gc --include-review rejects + reaps the review sandbox too
    reaped2 = controller.gc(include_review=True)
    assert review.sandbox_id in {s.id for s in reaped2}
    assert not _container_exists(review.sandbox_id)
    assert store.get_task(review.id).state is TaskState.REJECTED


def test_gc_dry_run_removes_nothing(controller, store, host_repo):
    orphan = controller.provider.create(str(host_repo), {})
    try:
        planned = controller.gc(dry_run=True)
        assert orphan.id in {s.id for s in planned}
        assert _container_exists(orphan.id)  # still there
    finally:
        controller.provider.destroy(orphan)
