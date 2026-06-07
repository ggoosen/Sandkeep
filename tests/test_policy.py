"""Coordination & policy acceptance (BUILD_SPEC §14, Phase 3).

Pure host-side analysis of patches — no docker needed. Pins the risk
categories, the added-line secret scan, and cross-task conflict detection.
"""

from __future__ import annotations

from sandkeep import policy


def _patch(path: str, added: str = "x = 1") -> str:
    """A minimal unified-diff touching one file with one added line."""
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1 @@\n"
        f"+{added}\n"
    )


# -- diff risk analysis --------------------------------------------------


def test_clean_change_has_no_flags():
    assert policy.analyze_patch(_patch("src/util.py")) == []


def test_flags_ci_workflow():
    flags = policy.analyze_patch(_patch(".github/workflows/ci.yml"))
    assert any(f.category == "ci/workflow" for f in flags)


def test_flags_deploy_dockerfile():
    flags = policy.analyze_patch(_patch("Dockerfile"))
    assert any(f.category == "deploy" for f in flags)


def test_flags_auth_path():
    flags = policy.analyze_patch(_patch("app/auth/session.py"))
    assert any(f.category == "auth" for f in flags)


def test_flags_dependency_manifest():
    flags = policy.analyze_patch(_patch("requirements.txt"))
    assert any(f.category == "dependency" for f in flags)


def test_flags_secret_by_path_and_by_content():
    # by path
    assert any(f.category == "secret" for f in policy.analyze_patch(_patch(".env")))
    # by added-line content (a hardcoded key in an otherwise innocent file)
    leaked = _patch("src/config.py", added='ANTHROPIC_API_KEY = "sk-ant-abc12345xyz"')
    secret_flags = [f for f in policy.analyze_patch(leaked) if f.category == "secret"]
    assert secret_flags
    assert "sk-ant" not in secret_flags[0].detail or "Anthropic" in secret_flags[0].detail


def test_added_line_scan_ignores_the_plusplusplus_header():
    # a removed line that looks secret must NOT flag (only '+' additions do)
    removed = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +0,0 @@\n"
        '-password = "supersecret"\n'
    )
    assert [f for f in policy.analyze_patch(removed) if f.category == "secret"] == []


def test_flags_are_deduped_and_sorted():
    flags = policy.analyze_patch(_patch("package.json") + _patch("package.json"))
    assert len(flags) == len({(f.category, f.detail) for f in flags})


# -- cross-task conflict detection ---------------------------------------


def test_conflict_on_overlapping_files():
    conflicts = policy.find_conflicts(
        ["a.py", "b.py"], {"task-2": ["b.py", "c.py"], "task-3": ["d.py"]}
    )
    assert len(conflicts) == 1
    assert conflicts[0].other_task_id == "task-2"
    assert conflicts[0].files == ("b.py",)


def test_no_conflict_when_disjoint():
    assert policy.find_conflicts(["a.py"], {"task-2": ["b.py"]}) == []


def test_no_conflict_with_empty_others():
    assert policy.find_conflicts(["a.py"], {}) == []
