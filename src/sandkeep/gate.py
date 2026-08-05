"""Human-gate delivery modes (improvement plan, step 16).

Default gate is local patch-apply (`controller.accept` → a fresh host branch).
This module adds a **draft-PR** gate: on accept, push the accepted branch to a
remote and open a *draft* pull request pre-filled from the results contract +
risk flags. It is additive — the local branch is still created first — and a
human still merges; Sandkeep never auto-merges.

The GitHub call needs a remote + a token (`GITHUB_TOKEN`); without them it fails
loud rather than silently falling back. The gateway is injectable so the wiring
is testable without a live remote.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass


class PRGateError(Exception):
    pass


@dataclass
class PRResult:
    url: str
    number: int


def parse_github_remote(url: str) -> tuple[str, str]:
    """(owner, repo) from a GitHub remote URL (ssh or https forms)."""
    url = url.strip()
    # git@github.com:owner/repo.git  |  ssh://git@github.com/owner/repo.git
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        raise PRGateError(f"not a recognizable GitHub remote URL: {url!r}")
    return m.group(1), m.group(2)


def build_pr_body(*, instruction: str, summary: str, files: list[str],
                  risk_flags: list, task_id: str) -> str:
    """The draft PR body, from the contract + gate signals. Advisory content
    only — no secrets, just what the diff is."""
    file_lines = [f"- `{f}`" for f in files] if files else ["- (none reported)"]
    lines = [
        "_Opened by Sandkeep at the review gate. Draft — a human still merges._",
        "",
        f"**Task:** {instruction}",
        "",
        f"**Summary:** {summary}",
        "",
        "**Files changed:**",
        *file_lines,
    ]
    if risk_flags:
        lines += ["", "**Risk flags (advisory):**"]
        lines += [f"- `{f.category}` — {f.detail}" for f in risk_flags]
    lines += ["", f"<sub>sandkeep task `{task_id}`</sub>"]
    return "\n".join(lines)


class DraftPRGate:
    """Pushes an accepted branch and opens a draft PR via the GitHub REST API.

    Injectable seams (`_git`, `_http`) keep it unit-testable; the defaults do
    the real thing. Needs `token` (GITHUB_TOKEN) and a working `remote`."""

    def __init__(self, *, remote: str, base: str, token: str, git=None, http=None) -> None:
        if not token:
            raise PRGateError(
                "draft-PR gate needs a GitHub token — set GITHUB_TOKEN "
                "(the local-apply gate needs none)"
            )
        self.remote = remote
        self.base = base
        self.token = token
        self._git = git or self._real_git
        self._http = http or self._real_http

    @staticmethod
    def _real_git(repo_path: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", repo_path, *args],
                              capture_output=True, text=True)

    def _real_http(self, url: str, payload: dict) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise PRGateError(f"GitHub API {exc.code}: {exc.read().decode()[:300]}") from None
        except OSError as exc:
            raise PRGateError(f"GitHub API unreachable: {exc}") from None

    def open(self, *, repo_path: str, branch: str, title: str, body: str) -> PRResult:
        url = self._git(repo_path, "remote", "get-url", self.remote)
        if url.returncode != 0:
            raise PRGateError(
                f"remote {self.remote!r} not found — configure it or pass "
                f"--gate local (git said: {url.stderr.strip()})"
            )
        owner, repo = parse_github_remote(url.stdout)
        push = self._git(repo_path, "push", self.remote, f"{branch}:{branch}")
        if push.returncode != 0:
            raise PRGateError(f"git push failed: {push.stderr.strip()}")
        data = self._http(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            {"title": title, "head": branch, "base": self.base,
             "body": body, "draft": True},
        )
        return PRResult(url=data.get("html_url", ""), number=int(data.get("number", 0)))
