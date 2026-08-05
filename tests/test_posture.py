"""Containment posture + doctor (improvement plan, step 13).

The user should always know the REAL posture of a run. These test the
backend↔posture mapping, the honest banner, and the doctor readiness report.
Host-side (no docker/e2b needed).
"""

from __future__ import annotations

import pytest

from sandkeep import cli
from sandkeep.config import Config


# -- posture selection ---------------------------------------------------

def test_default_posture_is_hardened_docker():
    assert Config().posture == "hardened-docker"


def test_posture_env_selects_microvm_backend(monkeypatch):
    monkeypatch.delenv("SANDKEEP_BACKEND", raising=False)
    monkeypatch.setenv("SANDKEEP_POSTURE", "microvm")
    cfg = Config.from_env()
    assert cfg.backend == "e2b" and cfg.posture == "microvm"


def test_explicit_backend_wins_over_posture(monkeypatch):
    monkeypatch.setenv("SANDKEEP_POSTURE", "microvm")
    monkeypatch.setenv("SANDKEEP_BACKEND", "docker")
    assert Config.from_env().backend == "docker"


def test_bad_posture_rejected(monkeypatch):
    monkeypatch.delenv("SANDKEEP_BACKEND", raising=False)
    monkeypatch.setenv("SANDKEEP_POSTURE", "vibes")
    with pytest.raises(ValueError):
        Config.from_env()


# -- honest banner -------------------------------------------------------

def test_banner_names_microvm():
    cfg = Config()
    cfg.backend = "e2b"
    assert "microVM" in cli.security_banner(cfg, "egress")


def test_banner_names_proxy_broker():
    b = cli.security_banner(Config(), "proxy")
    assert "key broker" in b and "shared-kernel" in b


def test_banner_warns_open_docker():
    b = cli.security_banner(Config(), "egress")
    assert "NOT a security boundary" in b


# -- doctor --------------------------------------------------------------

def test_doctor_reports_posture_and_key_state(monkeypatch, tmp_path):
    monkeypatch.setenv("SANDKEEP_HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = Config.from_env()
    labels = {label: (ok, detail) for label, ok, detail in cli.doctor_checks(cfg)}
    assert "posture" in labels
    assert labels["posture"][1].startswith("hardened-docker")
    # no key stored/exported → flagged not-ok with a fix hint
    assert labels["ANTHROPIC_API_KEY"][0] is False
    assert "auth set" in labels["ANTHROPIC_API_KEY"][1]


def test_doctor_e2b_checks_package_and_key(monkeypatch, tmp_path):
    monkeypatch.setenv("SANDKEEP_HOME", str(tmp_path))
    monkeypatch.setenv("SANDKEEP_BACKEND", "e2b")
    cfg = Config.from_env()
    labels = [label for label, _, _ in cli.doctor_checks(cfg)]
    assert "e2b package" in labels and "E2B_API_KEY" in labels
