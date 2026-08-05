"""Draft-PR gate (improvement plan, step 16).

The GitHub call is infra-bound (needs a remote + token), so it's injected: the
gateway's git+http seams are faked to prove push + draft-PR open happen with
the right branch/title/body, remote parsing is correct, and the missing-token /
missing-remote paths fail loud. The default local-apply gate is unchanged.
"""

from __future__ import annotations

import uuid

import pytest

from sandkeep import gate
from sandkeep.gate import DraftPRGate, PRGateError, build_pr_body, parse_github_remote
from sandkeep.policy import RiskFlag


# -- remote parsing ------------------------------------------------------

@pytest.mark.parametrize(
    "url,owner,repo",
    [
        ("git@github.com:ggoosen/Sandkeep.git", "ggoosen", "Sandkeep"),
        ("https://github.com/ggoosen/Sandkeep.git", "ggoosen", "Sandkeep"),
        ("https://github.com/ggoosen/Sandkeep", "ggoosen", "Sandkeep"),
        ("ssh://git@github.com/o/r.git", "o", "r"),
    ],
)
def test_parse_github_remote(url, owner, repo):
    assert parse_github_remote(url) == (owner, repo)


def test_parse_github_remote_rejects_non_github():
    with pytest.raises(PRGateError):
        parse_github_remote("https://gitlab.com/o/r.git")


# -- body building -------------------------------------------------------

def test_build_pr_body_includes_summary_files_and_flags():
    body = build_pr_body(
        instruction="Add validation", summary="Added it.",
        files=["a.py", "b.py"],
        risk_flags=[RiskFlag("dependency", "requirements.txt")],
        task_id="t123",
    )
    assert "Add validation" in body and "Added it." in body
    assert "`a.py`" in body and "`b.py`" in body
    assert "dependency" in body
    assert "Draft" in body  # states a human still merges


# -- gateway with faked seams --------------------------------------------

def test_missing_token_fails_loud():
    with pytest.raises(PRGateError, match="token"):
        DraftPRGate(remote="origin", base="main", token="")


class _FakeGit:
    def __init__(self):
        self.calls = []

    def __call__(self, repo_path, *args):
        import subprocess
        self.calls.append(args)
        if args[:2] == ("remote", "get-url"):
            return subprocess.CompletedProcess(args, 0, "git@github.com:o/r.git\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")


def test_open_pushes_branch_and_opens_draft_pr():
    git = _FakeGit()
    seen = {}

    def http(url, payload):
        seen["url"] = url
        seen["payload"] = payload
        return {"html_url": "https://github.com/o/r/pull/7", "number": 7}

    gw = DraftPRGate(remote="origin", base="main", token="tok", git=git, http=http)
    res = gw.open(repo_path="/repo", branch="sandkeep-accepted/x",
                  title="sandkeep: do", body="body")

    assert res.number == 7 and res.url.endswith("/pull/7")
    # pushed the accepted branch
    assert ("push", "origin", "sandkeep-accepted/x:sandkeep-accepted/x") in git.calls
    # opened a DRAFT PR against the base branch on the parsed owner/repo
    assert seen["url"] == "https://api.github.com/repos/o/r/pulls"
    assert seen["payload"]["draft"] is True
    assert seen["payload"]["base"] == "main"
    assert seen["payload"]["head"] == "sandkeep-accepted/x"


def test_open_reports_push_failure():
    class FailPush(_FakeGit):
        def __call__(self, repo_path, *args):
            import subprocess
            if args[0] == "push":
                return subprocess.CompletedProcess(args, 1, "", "permission denied")
            return super().__call__(repo_path, *args)

    gw = DraftPRGate(remote="origin", base="main", token="t", git=FailPush(),
                     http=lambda u, p: {})
    with pytest.raises(PRGateError, match="push failed"):
        gw.open(repo_path="/r", branch="b", title="t", body="b")


# -- controller wiring with a fake gate ----------------------------------

class _FakeGate:
    def __init__(self):
        self.opened = None

    def open(self, *, repo_path, branch, title, body):
        self.opened = dict(repo_path=repo_path, branch=branch, title=title)
        return gate.PRResult(url="https://github.com/o/r/pull/1", number=1)


def test_config_rejects_bad_gate(monkeypatch):
    from sandkeep.config import Config

    monkeypatch.setenv("SANDKEEP_GATE", "carrier-pigeon")
    with pytest.raises(ValueError):
        Config.from_env()
