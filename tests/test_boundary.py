"""Phase 0 adversarial boundary suite (BUILD_SPEC §11) — must pass before
anything else.

Each test plays a hostile agent: it execs the exact commands a compromised
agent would run via its Bash tool, then asserts (a) the attempt is contained
and (b) violations.py classifies it. The Docker backend is a mechanics
harness, not a security boundary — this suite proves the *mechanics*:
nothing reaches the host, and any future backend must pass it unmodified.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path


from sandkeep.violations import ViolationKind, scan_exec


def _sh(provider, handle, script: str):
    return provider.exec(handle, ["sh", "-c", script], timeout=60)


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            h.update(str(path.relative_to(root)).encode())
            h.update(path.read_bytes())
    return h.hexdigest()


# -- 1. host secrets are unreachable --------------------------------------


def test_host_secrets_unreachable(provider, sandbox):
    """Prompted to read host secrets, the agent finds no path to them."""
    _, handle = sandbox
    # the host user's home does not exist inside the sandbox
    for probe in ("/Users", "/host", "/mnt/host"):
        assert _sh(provider, handle, f"test -e {probe}").exit_code != 0
    # ~ inside is the sandbox user's home; no host ssh keys
    assert _sh(provider, handle, "cat ~/.ssh/id_rsa").exit_code != 0
    # /etc/passwd is the *container's* — it must not contain host users
    passwd = _sh(provider, handle, "cat /etc/passwd")
    assert passwd.exit_code == 0
    assert os.environ.get("USER", "\x00") not in passwd.stdout


def test_host_repo_dotgit_not_writable(provider, sandbox):
    _, handle = sandbox
    result = _sh(provider, handle, "touch /src/.git/HACKED 2>&1")
    assert result.exit_code != 0
    flagged = scan_exec("touch /src/.git/HACKED", result)
    assert any(v.kind is ViolationKind.FILESYSTEM for v in flagged)


def test_git_push_back_to_host_fails(provider, sandbox):
    """Even re-adding /src as a remote, pushing into the host repo fails."""
    _, handle = sandbox
    result = _sh(
        provider,
        handle,
        "cd /work/repo && git remote add host /src 2>&1; "
        "echo hacked > exfil.txt && git add -A && git commit -qm exfil && "
        "git push host HEAD 2>&1",
    )
    assert result.exit_code != 0


# -- 2. network exfiltration fails under --network none -------------------


def test_network_exfil_fails_and_is_flagged(provider, sandbox):
    _, handle = sandbox
    cmd = "curl -sS -m 10 https://example.com 2>&1"
    result = _sh(provider, handle, cmd)
    assert result.exit_code != 0
    flagged = scan_exec(cmd, result, network_denied=True)
    assert any(v.kind is ViolationKind.NETWORK for v in flagged)


def test_dns_resolution_fails(provider, sandbox):
    _, handle = sandbox
    result = _sh(provider, handle, "getent hosts example.com")
    assert result.exit_code != 0


# -- 3. host / docker daemon unreachable ----------------------------------


def test_docker_socket_absent_and_flagged(provider, sandbox):
    _, handle = sandbox
    result = _sh(provider, handle, "test -e /var/run/docker.sock")
    assert result.exit_code != 0
    flagged = scan_exec("curl --unix-socket /var/run/docker.sock http://x/containers", result)
    assert any(v.kind is ViolationKind.ESCAPE for v in flagged)


def test_privilege_escalation_paths_closed(provider, sandbox):
    _, handle = sandbox
    assert _sh(provider, handle, "test -w /proc/1/root").exit_code != 0
    # cap-drop ALL: no raw sockets, no mounts, not root
    whoami = _sh(provider, handle, "whoami")
    assert whoami.stdout.strip() != "root"
    assert _sh(provider, handle, "mount -t tmpfs none /mnt 2>&1").exit_code != 0


# -- 4. git inside shows only the task, no host remotes -------------------


def test_git_inside_is_task_scoped(provider, sandbox):
    task, handle = sandbox
    status = provider.exec(
        handle, ["git", "-C", handle.workdir, "status", "--branch", "--short"], timeout=60
    )
    assert f"sandkeep/{task.id}" in status.stdout
    remotes = provider.exec(handle, ["git", "-C", handle.workdir, "remote", "-v"], timeout=60)
    assert remotes.stdout.strip() == ""


# -- 5. destroy leaves the host byte-for-byte unchanged -------------------


def test_destroy_after_hostile_run_leaves_host_untouched(
    provider, store, audit, host_repo
):
    import uuid

    from sandkeep.audit import new_trace_id
    from sandkeep.models import Task
    from sandkeep.provisioner import provision

    before = _tree_digest(host_repo)
    t = Task(id=uuid.uuid4().hex, repo_path=str(host_repo), instruction="hostile")
    store.create_task(t, new_trace_id())
    handle = provision(t, provider, store, audit, env={}, trace_id=new_trace_id())
    # a burst of hostility before teardown
    _sh(provider, handle, "rm -rf /src 2>/dev/null")
    _sh(provider, handle, "echo evil > /work/repo/README.md && cd /work/repo && git add -A && git commit -qm evil")
    _sh(provider, handle, "dd if=/dev/zero of=/work/fill bs=1M count=10 2>/dev/null")
    provider.destroy(handle)
    assert _tree_digest(host_repo) == before
    # and the sandbox is actually gone
    gone = subprocess.run(
        ["docker", "inspect", handle.id], capture_output=True
    )
    assert gone.returncode != 0


# -- claude runs headless inside the sandbox -------------------------------


def test_claude_binary_runs_inside_sandbox(provider, sandbox):
    """The agent binary executes inside the sandbox (full agent run needs
    egress + a real key; that end-to-end is the Phase 1 gate)."""
    _, handle = sandbox
    result = provider.exec(handle, ["claude", "--version"], timeout=60)
    assert result.exit_code == 0
    assert result.stdout.strip()


# -- violation archive flow -------------------------------------------------


def test_violation_archive_quarantines_and_records(
    provider, store, audit, host_repo, tmp_path, monkeypatch
):
    import uuid

    from sandkeep.audit import new_trace_id
    from sandkeep.config import Config
    from sandkeep.models import Task
    from sandkeep.provisioner import provision
    from sandkeep.violations import Violation, archive_sandbox

    monkeypatch.setenv("SANDKEEP_HOME", str(tmp_path / "skhome"))
    cfg = Config.from_env()
    t = Task(id=uuid.uuid4().hex, repo_path=str(host_repo), instruction="hostile")
    store.create_task(t, new_trace_id())
    handle = provision(t, provider, store, audit, env={}, trace_id=new_trace_id())
    violations = [Violation(ViolationKind.NETWORK, "egress attempt: curl example.com")]
    archive_sandbox(
        provider, handle, cfg, audit,
        task_id=t.id, trace_id=new_trace_id(), violations=violations,
    )
    record = cfg.archive_dir / f"{t.id}.json"
    assert record.exists()
    assert "egress attempt" in record.read_text()
    # default (no forensics flag): sandbox destroyed
    gone = subprocess.run(["docker", "inspect", handle.id], capture_output=True)
    assert gone.returncode != 0
