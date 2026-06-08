"""Paths, environment, and defaults for the Sandkeep controller.

Everything host-side lives under SANDKEEP_HOME (default: ~/.sandkeep).
Treat values as config, not hardcodes (BUILD_SPEC §2 note on model alias).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_AGENT = "claude"
DEFAULT_NETWORK = "egress"  # "egress" (bridge) | "none" (no network at all)
NETWORK_MODES = ("egress", "none")
DEFAULT_BACKEND = "docker"  # "docker" (Phase 0–1 harness) | "e2b" (microVM, Phase 2)
BACKENDS = ("docker", "e2b")
DEFAULT_E2B_TEMPLATE = "sandkeep"
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
    agent: str = DEFAULT_AGENT
    network: str = DEFAULT_NETWORK
    backend: str = DEFAULT_BACKEND
    e2b_template: str = DEFAULT_E2B_TEMPLATE
    # Optional test-gated merge: a command run INSIDE the sandbox against the
    # agent's changes before `accept` will merge (BUILD_SPEC §14). Empty = off.
    test_command: str = ""
    max_turns: int = DEFAULT_MAX_TURNS
    task_timeout_seconds: int = DEFAULT_TASK_TIMEOUT_SECONDS
    exec_timeout_seconds: int = DEFAULT_EXEC_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls()
        cfg.image = os.environ.get("SANDKEEP_IMAGE", cfg.image)
        cfg.model = os.environ.get("SANDKEEP_MODEL", cfg.model)
        cfg.agent = os.environ.get("SANDKEEP_AGENT", cfg.agent)
        cfg.network = os.environ.get("SANDKEEP_NETWORK", cfg.network)
        if cfg.network not in NETWORK_MODES:
            raise ValueError(
                f"SANDKEEP_NETWORK must be one of {NETWORK_MODES}, got {cfg.network!r}"
            )
        cfg.backend = os.environ.get("SANDKEEP_BACKEND", cfg.backend)
        if cfg.backend not in BACKENDS:
            raise ValueError(
                f"SANDKEEP_BACKEND must be one of {BACKENDS}, got {cfg.backend!r}"
            )
        cfg.e2b_template = os.environ.get("SANDKEEP_E2B_TEMPLATE", cfg.e2b_template)
        cfg.test_command = os.environ.get("SANDKEEP_TEST_COMMAND", cfg.test_command)
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
        # `sandkeep auth set [NAME]` stores secrets here (0600, env format so it
        # is also `source`-able): ANTHROPIC_API_KEY, E2B_API_KEY, and any agent
        # driver's secret_env. Plaintext on disk — same trust level as
        # ~/.aws/credentials. TODO(phase-2): secret broker removes this.
        return self.home / "env"

    def image_for(self, agent: str) -> str:
        """Sandbox image tag for an agent: the default `image` for the default
        agent (back-compat), else a per-agent tag (BUILD_SPEC §13)."""
        if agent == DEFAULT_AGENT:
            return self.image
        return f"sandkeep-sandbox:{agent}"

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)


def stored_secrets(cfg: "Config") -> dict[str, str]:
    """All NAME=value secrets in cfg.env_file (no environment fallback)."""
    out: dict[str, str] = {}
    if not cfg.env_file.exists():
        return out
    for line in cfg.env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        value = value.strip().strip("'\"")
        if value:
            out[name.strip()] = value
    return out


def stored_secret(cfg: "Config", name: str) -> str | None:
    """One stored secret by name (no environment fallback)."""
    return stored_secrets(cfg).get(name)


def write_secret(cfg: "Config", name: str, value: str) -> None:
    """Upsert one secret in cfg.env_file, preserving the others, 0600."""
    secrets = stored_secrets(cfg)
    secrets[name] = value
    cfg.home.mkdir(parents=True, exist_ok=True)
    cfg.env_file.write_text("".join(f"{k}={secrets[k]}\n" for k in sorted(secrets)))
    cfg.env_file.chmod(0o600)


def clear_secret(cfg: "Config", name: str | None = None) -> bool:
    """Remove one secret (by name) or all (name=None). True if anything removed."""
    if name is None:
        if cfg.env_file.exists():
            cfg.env_file.unlink()
            return True
        return False
    secrets = stored_secrets(cfg)
    if name not in secrets:
        return False
    del secrets[name]
    if secrets:
        cfg.env_file.write_text("".join(f"{k}={secrets[k]}\n" for k in sorted(secrets)))
        cfg.env_file.chmod(0o600)
    elif cfg.env_file.exists():
        cfg.env_file.unlink()
    return True


def load_secret(cfg: "Config", name: str) -> str | None:
    """A secret for a run: the environment wins (an explicit export is never
    silently overridden), else the value stored by `sandkeep auth set`."""
    return os.environ.get(name) or stored_secret(cfg, name)


# Back-compat aliases (ANTHROPIC_API_KEY was the only key before Phase 2).
def stored_api_key(cfg: "Config") -> str | None:
    return stored_secret(cfg, "ANTHROPIC_API_KEY")


def load_api_key(cfg: "Config") -> str | None:
    return load_secret(cfg, "ANTHROPIC_API_KEY")


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
