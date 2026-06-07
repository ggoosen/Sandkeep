"""Paths, environment, and defaults for the Sandkeep controller.

Everything host-side lives under SANDKEEP_HOME (default: ~/.sandkeep).
Treat values as config, not hardcodes (BUILD_SPEC §2 note on model alias).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TURNS = 8
DEFAULT_TASK_TIMEOUT_SECONDS = 1800  # wall-clock cap for the agent run
DEFAULT_EXEC_TIMEOUT_SECONDS = 120  # cap for individual sandbox exec calls
DEFAULT_IMAGE = "sandkeep-sandbox:latest"


def _home() -> Path:
    return Path(os.environ.get("SANDKEEP_HOME", str(Path.home() / ".sandkeep")))


@dataclass
class Config:
    home: Path = field(default_factory=_home)
    image: str = DEFAULT_IMAGE
    model: str = DEFAULT_MODEL
    max_turns: int = DEFAULT_MAX_TURNS
    task_timeout_seconds: int = DEFAULT_TASK_TIMEOUT_SECONDS
    exec_timeout_seconds: int = DEFAULT_EXEC_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls()
        cfg.image = os.environ.get("SANDKEEP_IMAGE", cfg.image)
        cfg.model = os.environ.get("SANDKEEP_MODEL", cfg.model)
        if "SANDKEEP_MAX_TURNS" in os.environ:
            cfg.max_turns = int(os.environ["SANDKEEP_MAX_TURNS"])
        if "SANDKEEP_TASK_TIMEOUT" in os.environ:
            cfg.task_timeout_seconds = int(os.environ["SANDKEEP_TASK_TIMEOUT"])
        return cfg

    @property
    def db_path(self) -> Path:
        return self.home / "state.sqlite3"

    @property
    def audit_log_path(self) -> Path:
        return self.home / "audit.jsonl"

    @property
    def outputs_dir(self) -> Path:
        return self.home / "outputs"

    @property
    def archive_dir(self) -> Path:
        # Quarantined sandbox metadata for forensics (BUILD_SPEC §8)
        return self.home / "archive"

    @property
    def env_file(self) -> Path:
        # `sandkeep auth set` stores ANTHROPIC_API_KEY here (0600, env format
        # so it is also `source`-able). Plaintext on disk — same trust level
        # as ~/.aws/credentials. TODO(phase-2): secret broker removes this.
        return self.home / "env"

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)


def stored_api_key(cfg: "Config") -> str | None:
    """The key from cfg.env_file, if any (no environment fallback)."""
    if not cfg.env_file.exists():
        return None
    for line in cfg.env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("ANTHROPIC_API_KEY="):
            value = line.split("=", 1)[1].strip().strip("'\"")
            if value:
                return value
    return None


def load_api_key(cfg: "Config") -> str | None:
    """ANTHROPIC_API_KEY for a run: the environment wins (an explicit export
    is never silently overridden), else the key stored by `sandkeep auth set`."""
    return os.environ.get("ANTHROPIC_API_KEY") or stored_api_key(cfg)


def resource_path(name: str) -> Path:
    """Locate a repo-level resource dir (sandbox_image/, prompts/).

    These live at the repo root per BUILD_SPEC §1; wheels also bundle them
    inside the package (pyproject force-include) so installed copies work.
    """
    package_local = Path(__file__).parent / name  # installed wheel
    if package_local.is_dir():
        return package_local
    repo_root = Path(__file__).parents[2] / name  # editable/dev checkout
    if repo_root.is_dir():
        return repo_root
    raise FileNotFoundError(f"resource directory not found: {name}")
