"""Coordination & policy (BUILD_SPEC §14, Phase 3).

Host-side, deterministic analysis of a finished patch — it never runs agent
code, it only reads the diff that already crossed back. Two jobs:

1. **Diff risk analysis** — flag changes that touch sensitive surfaces
   (CI/workflow, deploy, auth, secret material, dependencies) so the human
   gate sees *what kind* of change it is approving, not just a file list.
2. **Cross-task conflict detection** — surface when another task awaiting
   review touches the same files, so accepting both isn't a silent collision.

This is advisory: it informs the human gate, it does not auto-block. The
test-gated merge queue (running the repo's tests against the patch inside a
sandbox) is the deferred piece — see §14.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import diff

# -- diff risk analysis --------------------------------------------------

# Path-based categories: (category, human reason, list of regexes matched
# against each changed path, case-insensitive).
_PATH_RULES: list[tuple[str, str, list[str]]] = [
    ("ci/workflow", "CI/automation config", [
        r"^\.github/workflows/", r"^\.gitlab-ci\.yml$", r"(^|/)Jenkinsfile$",
        r"^\.circleci/", r"(^|/)\.travis\.yml$", r"(^|/)azure-pipelines\.yml$",
    ]),
    ("deploy", "deployment/infra config", [
        r"(^|/)Dockerfile$", r"(^|/)docker-compose.*\.ya?ml$", r"\.tf$",
        r"(^|/)Procfile$", r"(^|/)fly\.toml$", r"(^|/)vercel\.json$",
        r"(^|/)serverless\.ya?ml$", r"(^|/)k8s/", r"(^|/)helm/", r"(^|/)deploy/",
    ]),
    ("auth", "authentication/authorization code", [
        r"auth", r"login", r"session", r"permission", r"rbac",
        r"(^|/)middleware", r"oauth", r"(^|/)acl",
    ]),
    ("secret", "secret/credential material", [
        r"(^|/)\.env($|\.)", r"secret", r"credential", r"(^|/)\.npmrc$",
        r"(^|/)id_rsa", r"\.pem$", r"\.key$", r"(^|/)\.pypirc$",
    ]),
    ("dependency", "dependency manifest/lockfile", [
        r"(^|/)requirements.*\.txt$", r"(^|/)pyproject\.toml$", r"(^|/)poetry\.lock$",
        r"(^|/)package\.json$", r"(^|/)package-lock\.json$", r"(^|/)yarn\.lock$",
        r"(^|/)pnpm-lock\.yaml$", r"(^|/)Cargo\.(toml|lock)$", r"(^|/)go\.(mod|sum)$",
        r"(^|/)Gemfile(\.lock)?$",
    ]),
    # Agent-controlled tool config that would execute on the host in a later
    # session (Claude Code hooks/permissions) or in CI. .claude/ is normally
    # excluded from the patch (diff._EXCLUDE_PATHSPECS); this flag is
    # defence-in-depth for anything that slips through.
    ("agent-config", "agent/editor tool config (hooks, permissions)", [
        r"(^|/)\.claude/", r"(^|/)\.cursor/", r"(^|/)\.vscode/tasks\.json$",
    ]),
]

# Content-based: added lines (in the patch body) that look like a hardcoded
# secret, regardless of which file they land in.
_SECRET_LINE_PATTERNS: list[tuple[str, str]] = [
    (r"sk-ant-[A-Za-z0-9_\-]{8,}", "Anthropic API key"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key id"),
    (r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", "private key"),
    (r"gh[pousr]_[A-Za-z0-9]{20,}", "GitHub token"),
    (r"(?i)\b(password|passwd|secret|api[_-]?key|token)\b\s*[:=]\s*['\"][^'\"]{6,}['\"]",
     "hardcoded credential"),
]


@dataclass(frozen=True)
class RiskFlag:
    category: str
    detail: str  # human-readable: what + where


def analyze_patch(patch_text: str) -> list[RiskFlag]:
    """Risk flags for a patch, deduped and sorted by category. Empty list for
    an ordinary change."""
    flags: set[RiskFlag] = set()

    for path in diff.files_in_patch(patch_text):
        for category, reason, patterns in _PATH_RULES:
            if any(re.search(p, path, re.IGNORECASE) for p in patterns):
                flags.add(RiskFlag(category, f"{reason}: {path}"))

    for line in _added_lines(patch_text):
        for pattern, label in _SECRET_LINE_PATTERNS:
            if re.search(pattern, line):
                snippet = line.strip()[:60]
                flags.add(RiskFlag("secret", f"possible {label} in added line: {snippet}"))
                break  # one flag per line is enough

    if _has_binary_hunk(patch_text):
        flags.add(RiskFlag("binary", "patch contains a binary hunk (opaque to review)"))

    return sorted(flags, key=lambda f: (f.category, f.detail))


def _has_binary_hunk(patch_text: str) -> bool:
    """A binary change can't be eyeballed at the gate — flag it (advisory)."""
    return (
        "GIT binary patch" in patch_text
        or bool(re.search(r"^Binary files .* differ$", patch_text, re.MULTILINE))
    )


def cross_check_files(claimed: list[str], patch_text: str) -> RiskFlag | None:
    """Advisory flag when the contract's files_changed disagrees with the paths
    actually in the patch — an agent under-reporting what it touched. None if
    they match (order-insensitive) or nothing was claimed."""
    if not claimed:
        return None
    actual = set(diff.files_in_patch(patch_text))
    if set(claimed) == actual:
        return None
    missing = sorted(actual - set(claimed))
    extra = sorted(set(claimed) - actual)
    bits = []
    if missing:
        bits.append(f"in patch but not reported: {', '.join(missing)}")
    if extra:
        bits.append(f"reported but not in patch: {', '.join(extra)}")
    return RiskFlag("contract-mismatch", "; ".join(bits))


def find_secrets_in_text(text: str) -> list[str]:
    """Secret-shaped hits anywhere in `text` (not just added lines), for the
    pre-provision scan of what /src exposes to the agent (step 15). Returns the
    labels of matches, one per line at most."""
    out: list[str] = []
    for line in text.splitlines():
        for pattern, label in _SECRET_LINE_PATTERNS:
            if re.search(pattern, line):
                out.append(label)
                break
    return out


def _added_lines(patch_text: str) -> list[str]:
    """Lines added by the patch (body '+' lines, excluding the '+++' header)."""
    out = []
    for line in patch_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            out.append(line[1:])
    return out


# -- cross-task conflict detection ---------------------------------------


@dataclass(frozen=True)
class Conflict:
    other_task_id: str
    files: tuple[str, ...]  # overlapping paths


def find_conflicts(
    this_files: list[str], others: dict[str, list[str]]
) -> list[Conflict]:
    """Conflicts between ``this_files`` and a mapping of other_task_id ->
    its changed files. A conflict is any non-empty file overlap."""
    mine = set(this_files)
    conflicts = []
    for other_id, other_files in sorted(others.items()):
        overlap = mine & set(other_files)
        if overlap:
            conflicts.append(Conflict(other_id, tuple(sorted(overlap))))
    return conflicts
