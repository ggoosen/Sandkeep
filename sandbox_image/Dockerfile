# Sandkeep sandbox image (BUILD_SPEC §1): node 22 + claude code + git + mise.
# Runs as the non-root `node` user; the controller starts it with
# `--network none` (or gated egress), read-only /src, and resource caps.
FROM node:22-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# /work is the only writable workspace; /src arrives read-only at runtime.
RUN mkdir -p /work && chown -R node:node /work

USER node
ENV HOME=/home/node

# mise for best-effort toolchain pinning (BUILD_SPEC §5.4)
RUN curl -fsSL https://mise.run | sh
ENV PATH="/home/node/.local/bin:/home/node/.local/share/mise/shims:${PATH}"

# minimal scoped settings for the headless run (BUILD_SPEC §6)
COPY --chown=node:node settings.json /home/node/.claude/settings.json

WORKDIR /work
CMD ["sleep", "infinity"]
