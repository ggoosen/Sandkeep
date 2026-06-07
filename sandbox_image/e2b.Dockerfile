# Sandkeep E2B template (Phase 2 microVM backend).
#
# Build into an E2B template with the E2B CLI (see docs/phase-2-implementation.md):
#     e2b template build --name sandkeep --dockerfile sandbox_image/e2b.Dockerfile
# then run with:  SANDKEEP_BACKEND=e2b E2B_API_KEY=... sandkeep run ...
#
# Mirrors sandbox_image/Dockerfile: node 22 + claude code + git + mise.
#
# ⚠️ NON-ROOT IS LOAD-BEARING. The E2BProvider makes /src read-only with
# `chmod -R a-w /src`; that only stops writes if commands run as a NON-ROOT
# user. If your E2B sandbox executes as root, the read-only-/src and
# /src/.git-not-writable boundary tests WILL fail — that's the boundary suite
# doing its job. Ensure the template runs as a non-root user (USER below), and
# confirm with: SANDKEEP_TEST_BACKEND=e2b E2B_API_KEY=... pytest tests/test_boundary.py
FROM node:22-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

RUN useradd -m -s /bin/bash sandkeep \
    && mkdir -p /work && chown -R sandkeep:sandkeep /work

USER sandkeep
ENV HOME=/home/sandkeep

RUN curl -fsSL https://mise.run | sh
ENV PATH="/home/sandkeep/.local/bin:/home/sandkeep/.local/share/mise/shims:${PATH}"

COPY --chown=sandkeep:sandkeep settings.json /home/sandkeep/.claude/settings.json

WORKDIR /work
