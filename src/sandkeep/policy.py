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

    return sorted(flags, key=lambda f: (f.category, f.detail))


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
