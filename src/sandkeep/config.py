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
# "egress"  — open bridge (agent reaches anything, and holds the key)
# "none"    — no network at all (boundary-test posture)
# "proxy"   — the sandbox runs on an internal (no-egress) network behind the
#             key-broker sidecar: it never holds the API key and can only reach
#             the allowlist (improvement plan, step 1)
DEFAULT_NETWORK = "egress"
NETWORK_MODES = ("egress", "none", "proxy")
DEFAULT_BROKER_IMAGE = "sandkeep-broker:latest"
DEFAULT_EGRESS_ALLOWLIST = (
    "api.anthropic.com,pypi.org,files.pythonhosted.org,registry.npmjs.org"
)
DEFAULT_BROWSER_IMAGE = "sandkeep-browser:latest"
GATE_MODES = ("local", "draft-pr")
DEFAULT_BACKEND = "docker"  # "docker" (Phase 0–1 harness) | "e2b" (microVM, Phase 2)
BACKENDS = ("docker", "e2b")
# Posture is a friendly selector over backend (improvement plan, step 13): it
# picks the backend and the banner/doctor report the *real* posture. The default
# stays hardened-docker until E2B reaches broker+browser parity (step 19).
POSTURES = ("hardened-docker", "microvm")
_POSTURE_BACKEND = {"hardened-docker": "docker", "microvm": "e2b"}
DEFAULT_E2B_TEMPLATE = "sandkeep"
DEFAULT_MAX_BUDGET_USD = 5.0  # hard spend cap passed to the agent CLI per run
DEFAULT_MAX_PATCH_BYTES = 5 * 1024 * 1024  # cap on the size of a returned patch
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
    broker_image: str = DEFAULT_BROKER_IMAGE
    egress_allowlist: str = DEFAULT_EGRESS_ALLOWLIST
    # browser bridge (improvement plan, step 11): a CDP sidecar the agent drives
    # instead of launching its own browser. Off by default; --browser turns it on.
    browser: bool = False
    browser_image: str = DEFAULT_BROWSER_IMAGE
    # Docker hardening (improvement plan, step 12): a custom seccomp profile path
    # ("" leaves Docker's built-in default), and an opt-in read-only rootfs.
    seccomp_profile: str = ""
    read_only_rootfs: bool = False
    # Human gate (improvement plan, step 16): "local" applies to a fresh host
    # branch; "draft-pr" also pushes that branch and opens a draft PR.
    gate: str = "local"
    git_remote: str = "origin"
    pr_base: str = "main"
    # Repo read-exposure (improvement plan, step 15). full_history: clone deep
    # history into the sandbox (default off = shallow, so the clone carries no
    # deep history). scan_repo_secrets: scan what /src exposes and warn/audit.
    full_history: bool = False
    scan_repo_secrets: bool = True
    backend: str = DEFAULT_BACKEND
    e2b_template: str = DEFAULT_E2B_TEMPLATE
    # Optional test-gated merge: a command run INSIDE the sandbox against the
    # agent's changes before `accept` will merge (BUILD_SPEC §14). Empty = off.
    test_command: str = ""
    # max_turns is gone: the upstream claude CLI removed the flag (see
    # agent/claude.py). Runs are bounded by max_budget_usd + task timeout.
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD
    # Fleet-level guard (improvement plan, step 18): a rolling 24h cap on the
    # spend COMMITTED across runs (sum of per-run --max-budget-usd), so a
    # runaway fleet can't rack up cost. None = no daily cap. Committed (not
    # measured) bounds worst-case without needing a live price table — each
    # run's actual spend is already ≤ its own --max-budget-usd.
    daily_budget_usd: float | None = None
    max_patch_bytes: int = DEFAULT_MAX_PATCH_BYTES
    # Cap per agent-produced artifact (screenshots/reports, step 24).
    max_artifact_bytes: int = 5 * 1024 * 1024
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
        cfg.broker_image = os.environ.get("SANDKEEP_BROKER_IMAGE", cfg.broker_image)
        cfg.egress_allowlist = os.environ.get("SANDKEEP_ALLOWLIST", cfg.egress_allowlist)
        if "SANDKEEP_BROWSER" in os.environ:
            cfg.browser = os.environ["SANDKEEP_BROWSER"].lower() in ("1", "on", "true", "yes")
        cfg.browser_image = os.environ.get("SANDKEEP_BROWSER_IMAGE", cfg.browser_image)
        cfg.seccomp_profile = os.environ.get("SANDKEEP_SECCOMP", cfg.seccomp_profile)
        if "SANDKEEP_READONLY_ROOTFS" in os.environ:
            cfg.read_only_rootfs = os.environ["SANDKEEP_READONLY_ROOTFS"].lower() in (
                "1", "on", "true", "yes")
        cfg.gate = os.environ.get("SANDKEEP_GATE", cfg.gate)
        if cfg.gate not in GATE_MODES:
            raise ValueError(f"SANDKEEP_GATE must be one of {GATE_MODES}, got {cfg.gate!r}")
        cfg.git_remote = os.environ.get("SANDKEEP_GIT_REMOTE", cfg.git_remote)
        cfg.pr_base = os.environ.get("SANDKEEP_PR_BASE", cfg.pr_base)
        if "SANDKEEP_FULL_HISTORY" in os.environ:
            cfg.full_history = os.environ["SANDKEEP_FULL_HISTORY"].lower() in (
                "1", "on", "true", "yes")
        if "SANDKEEP_SCAN_SECRETS" in os.environ:
            cfg.scan_repo_secrets = os.environ["SANDKEEP_SCAN_SECRETS"].lower() not in (
                "0", "off", "false", "no")
        cfg.backend = os.environ.get("SANDKEEP_BACKEND", cfg.backend)
        if cfg.backend not in BACKENDS:
            raise ValueError(
                f"SANDKEEP_BACKEND must be one of {BACKENDS}, got {cfg.backend!r}"
            )
        # Posture is an input alias for backend (explicit SANDKEEP_BACKEND, read
        # just above, still wins if both are set).
        if "SANDKEEP_POSTURE" in os.environ and "SANDKEEP_BACKEND" not in os.environ:
            posture = os.environ["SANDKEEP_POSTURE"]
            if posture not in POSTURES:
                raise ValueError(f"SANDKEEP_POSTURE must be one of {POSTURES}, got {posture!r}")
            cfg.backend = _POSTURE_BACKEND[posture]
        cfg.e2b_template = os.environ.get("SANDKEEP_E2B_TEMPLATE", cfg.e2b_template)
        cfg.test_command = os.environ.get("SANDKEEP_TEST_COMMAND", cfg.test_command)
        if "SANDKEEP_MAX_BUDGET_USD" in os.environ:
            raw = os.environ["SANDKEEP_MAX_BUDGET_USD"]
            try:
                cfg.max_budget_usd = float(raw)
            except ValueError:
                raise ValueError(
                    f"SANDKEEP_MAX_BUDGET_USD must be a number, got {raw!r}"
                ) from None
            if cfg.max_budget_usd <= 0:
                raise ValueError(
                    f"SANDKEEP_MAX_BUDGET_USD must be positive, got {raw!r}"
                )
        if "SANDKEEP_TASK_TIMEOUT" in os.environ:
            cfg.task_timeout_seconds = int(os.environ["SANDKEEP_TASK_TIMEOUT"])
        if "SANDKEEP_MAX_PATCH_BYTES" in os.environ:
            cfg.max_patch_bytes = int(os.environ["SANDKEEP_MAX_PATCH_BYTES"])
        if "SANDKEEP_DAILY_BUDGET_USD" in os.environ:
            raw = os.environ["SANDKEEP_DAILY_BUDGET_USD"]
            try:
                cfg.daily_budget_usd = float(raw)
            except ValueError:
                raise ValueError(
                    f"SANDKEEP_DAILY_BUDGET_USD must be a number, got {raw!r}"
                ) from None
        return cfg

    @property
    def posture(self) -> str:
        """The friendly containment posture label, derived from the backend so
        it can never disagree with what actually runs (step 13)."""
        return "microvm" if self.backend == "e2b" else "hardened-docker"

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
