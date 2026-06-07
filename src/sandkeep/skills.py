"""Capability authoring (BUILD_SPEC §15, Phase 4).

An agent can author **per-repo scoped skills** inside its sandbox: small
markdown capability files it writes under ``.sandkeep/skills/`` in the clone.

That path is deliberately **excluded from the returned patch** (see `diff.py`),
so authored skills are sandkeep-managed *metadata*, not changes to the user's
repo — they never land in the host working tree or `.git`. Instead the
controller reads them from the sandbox at land time (a gated channel in
sandkeep's own namespace, still subject to the human gate), persists them in a
host-side per-repo store on accept, and injects them into ``.claude/skills/`` on
the next run so the agent's Claude Code loads them.

This module is host-side and deterministic: it parses/validates skill files,
persists them per repo, reads authored skills from a sandbox, and pushes stored
skills back into one. It never executes agent code.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .sandbox.base import SandboxHandle, SandboxProvider

# Where the agent authors skills inside the clone (excluded from the patch).
AUTHOR_DIR = ".sandkeep/skills"
# Where stored skills are injected so Claude Code loads them next run.
INJECT_DIR = ".claude/skills"


class SkillError(Exception):
    """A skill file is malformed (missing frontmatter / name)."""


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    text: str  # the full file content, verbatim

    @property
    def filename(self) -> str:
        return f"{self.name}.md"


def parse_skill(text: str, *, source: str = "") -> Skill:
    """Parse a skill markdown file with YAML-ish frontmatter:

        ---
        name: my-skill
        description: what it does
        ---
        <body>

    Raises SkillError if the frontmatter or a non-empty, valid ``name`` is
    missing.
    """
    where = f" ({source})" if source else ""
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    if not m:
        raise SkillError(f"skill missing frontmatter{where}")
    front = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            front[key.strip().lower()] = value.strip().strip("'\"")
    name = front.get("name", "")
    if not name:
        raise SkillError(f"skill missing 'name'{where}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise SkillError(f"invalid skill name {name!r}{where}")
    return Skill(name=name, description=front.get("description", ""), text=text)


# -- reading authored skills from a sandbox ------------------------------


def read_authored(
    provider: SandboxProvider, handle: SandboxHandle, *, timeout: int = 30
) -> list[Skill]:
    """Skills the agent authored under AUTHOR_DIR in the clone. Malformed files
    raise SkillError so the gate sees the problem rather than dropping it."""
    listing = provider.exec(
        handle,
        ["sh", "-c", f"ls /work/repo/{AUTHOR_DIR}/*.md 2>/dev/null || true"],
        timeout=timeout,
    )
    paths = [p for p in listing.stdout.split() if p.strip()]
    skills_out = []
    for path in sorted(paths):
        text = provider.read_file(handle, path)
        skills_out.append(parse_skill(text, source=path))
    return skills_out


# -- per-repo skill store -------------------------------------------------


def repo_key(repo_path: str) -> str:
    resolved = str(Path(repo_path).resolve())
    return hashlib.sha256(resolved.encode()).hexdigest()[:16]


class SkillStore:
    """Persisted, per-repo skill library under ``<home>/skills/<repo_key>/``."""

    def __init__(self, home: Path, repo_path: str) -> None:
        self.dir = Path(home) / "skills" / repo_key(repo_path)

    def save(self, skill: Skill) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / skill.filename).write_text(skill.text)

    def save_all(self, skills: list[Skill]) -> None:
        for skill in skills:
            self.save(skill)

    def list(self) -> list[Skill]:
        if not self.dir.is_dir():
            return []
        return [parse_skill(p.read_text(), source=str(p)) for p in sorted(self.dir.glob("*.md"))]


# -- injection into a sandbox --------------------------------------------


def inject(
    provider: SandboxProvider,
    handle: SandboxHandle,
    skills: list[Skill],
    *,
    timeout: int = 30,
) -> None:
    """Push stored skills into the sandbox at INJECT_DIR/<name>/SKILL.md so the
    agent's Claude Code loads them. Base64 transport so arbitrary markdown
    survives the shell (same approach as the system-prompt installer)."""
    for skill in skills:
        encoded = base64.b64encode(skill.text.encode()).decode()
        target = f"/work/repo/{INJECT_DIR}/{skill.name}/SKILL.md"
        result = provider.exec(
            handle,
            ["sh", "-c",
             f"mkdir -p /work/repo/{INJECT_DIR}/{skill.name}"
             f" && echo {encoded} | base64 -d > {target}"],
            timeout=timeout,
        )
        if result.exit_code != 0:
            raise RuntimeError(
                f"failed to inject skill {skill.name}: {result.stderr.strip()}"
            )
