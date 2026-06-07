"""Capability authoring acceptance (BUILD_SPEC §16, Phase 4).

Pure host-side parse/store tests (no docker) plus a docker-backed round-trip:
an agent authors a skill under .sandkeep/skills/ (excluded from the patch); the
controller reads it at land, registers it on accept, and injects it into the
next run's sandbox at the Claude Code skill path.
"""

from __future__ import annotations

import json
import shlex

import pytest

from sandkeep import skills


def _skill_md(name: str = "run-tests", desc: str = "run the suite") -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\nDo the thing.\n"


# -- parsing / validation -------------------------------------------------


def test_parse_valid_skill():
    s = skills.parse_skill(_skill_md("foo", "does foo"))
    assert s.name == "foo"
    assert s.description == "does foo"
    assert s.filename == "foo.md"


def test_parse_rejects_missing_frontmatter():
    with pytest.raises(skills.SkillError):
        skills.parse_skill("just a body, no frontmatter")


def test_parse_rejects_missing_name():
    with pytest.raises(skills.SkillError):
        skills.parse_skill("---\ndescription: x\n---\nbody")


def test_parse_rejects_bad_name():
    with pytest.raises(skills.SkillError):
        skills.parse_skill("---\nname: bad/name\n---\nbody")


# -- per-repo store -------------------------------------------------------


def test_store_roundtrip(tmp_path):
    store = skills.SkillStore(tmp_path, "/some/repo")
    store.save(skills.parse_skill(_skill_md("a", "first")))
    store.save(skills.parse_skill(_skill_md("b", "second")))
    assert {s.name for s in store.list()} == {"a", "b"}


def test_store_is_scoped_per_repo(tmp_path):
    skills.SkillStore(tmp_path, "/repo/one").save(skills.parse_skill(_skill_md("a")))
    assert skills.SkillStore(tmp_path, "/repo/two").list() == []


# -- docker-backed: author → read → accept → register → inject -----------


def _author_skill_work(task, provider, handle):
    """A well-behaved agent: makes a real code change AND authors a skill under
    .sandkeep/skills/ (sandkeep metadata, excluded from the patch)."""
    from sandkeep.agent_runner import AgentRunResult

    skill = _skill_md("repo-deploy", "how to deploy this repo")
    contract = json.dumps({
        "task_id": task.id, "status": "succeeded", "summary": "Worked + authored a skill.",
        "files_changed": ["parse_config.py"],
    })
    script = (
        "echo '# real change' >> /work/repo/parse_config.py"
        f" && mkdir -p /work/repo/{skills.AUTHOR_DIR}"
        f" && printf %s {shlex.quote(skill)} > /work/repo/{skills.AUTHOR_DIR}/repo-deploy.md"
        " && mkdir -p /work/repo/.sandkeep"
        f" && echo {shlex.quote(contract)} > /work/repo/.sandkeep/results.json"
    )
    assert provider.exec(handle, ["sh", "-c", script], timeout=60).exit_code == 0
    return AgentRunResult(
        exit_code=0, timed_out=False, stdout="", stderr="",
        output={"usage": {"input_tokens": 1, "output_tokens": 1}}, detail="",
    )


@pytest.fixture
def controller(tmp_path, monkeypatch, store, audit, provider):
    from sandkeep.config import Config
    from sandkeep.controller import Controller

    monkeypatch.setenv("SANDKEEP_HOME", str(tmp_path / "skhome"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return Controller(Config.from_env(), store, audit, provider, network_denied=True)


def test_authored_skill_excluded_from_patch_but_captured(controller, store, host_repo, monkeypatch):
    from sandkeep import agent_runner

    monkeypatch.setattr(
        agent_runner, "run_agent",
        lambda task, provider, handle, audit, **kw: _author_skill_work(task, provider, handle),
    )
    task = controller.run_task(str(host_repo), "author a skill")
    assert task.state.value == "review"
    # the authored skill is captured for the gate...
    assert [s.name for s in controller.authored_skills(task)] == ["repo-deploy"]
    # ...but it is NOT in the returned patch (it's sandkeep metadata)
    from pathlib import Path

    patch = Path(task.patch_path).read_text()
    assert ".sandkeep/skills" not in patch
    assert "parse_config.py" in patch  # the real change is
    controller.reject(task.id)


def test_skill_registers_on_accept_and_injects_next_run(controller, store, host_repo, monkeypatch):
    from sandkeep import agent_runner

    monkeypatch.setattr(
        agent_runner, "run_agent",
        lambda task, provider, handle, audit, **kw: _author_skill_work(task, provider, handle),
    )
    first = controller.run_task(str(host_repo), "author a skill")
    controller.accept(first.id)

    # registered in the repo-scoped store
    stored = skills.SkillStore(controller.config.home, str(host_repo)).list()
    assert [s.name for s in stored] == ["repo-deploy"]

    # next run against the same repo injects it at the Claude Code skill path
    second = controller.run_task(str(host_repo), "use the skill")
    assert second.state.value == "review"
    import subprocess

    out = subprocess.run(
        ["docker", "exec", second.sandbox_id, "cat",
         f"/work/repo/{skills.INJECT_DIR}/repo-deploy/SKILL.md"],
        capture_output=True, text=True,
    )
    assert "repo-deploy" in out.stdout
    assert "skills_injected" in controller.audit.path.read_text()
    controller.reject(second.id)
