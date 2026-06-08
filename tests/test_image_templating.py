"""Per-agent image templating (BUILD_SPEC §13, Phase 5).

`image build --agent <name>` renders the sandbox Dockerfile from the driver's
install_steps() instead of erroring. Pure-render + tag-selection tests (no
docker build needed).
"""

from __future__ import annotations

from sandkeep.config import Config
from sandkeep.sandbox.docker_provider import render_dockerfile


def test_render_includes_base_and_install_steps():
    df = render_dockerfile("codex", ["npm install -g @openai/codex", "echo hi"])
    assert "FROM node:22-slim" in df
    assert "RUN npm install -g @openai/codex" in df
    assert "RUN echo hi" in df
    assert "USER node" in df  # still drops to non-root
    assert "codex" in df  # labelled with the agent


def test_render_handles_no_install_steps():
    df = render_dockerfile("noop", [])
    assert "FROM node:22-slim" in df
    assert "# (no agent install steps)" in df


def test_install_steps_run_before_user_drop():
    """Agent CLI installs need root (npm -g), so they must precede USER node."""
    df = render_dockerfile("codex", ["npm install -g @openai/codex"])
    assert df.index("RUN npm install -g @openai/codex") < df.index("USER node")


def test_image_tag_is_default_for_default_agent():
    cfg = Config()
    assert cfg.image_for("claude") == cfg.image


def test_image_tag_is_per_agent_for_others():
    cfg = Config()
    assert cfg.image_for("codex") == "sandkeep-sandbox:codex"


def test_claude_static_dockerfile_matches_rendered_install():
    """The static claude Dockerfile and the rendered base must install claude
    the same way, so the two image paths don't diverge."""
    from pathlib import Path

    from sandkeep.agent.claude import ClaudeDriver

    static = (Path(__file__).parents[1] / "sandbox_image" / "Dockerfile").read_text()
    for step in ClaudeDriver().install_steps():
        assert f"RUN {step}" in static or step in static
