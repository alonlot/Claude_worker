# Agent image used by DockerBackend (one throwaway container per run).
#
# Build once on the server:
#   docker build -t claude-worker-agent:latest ./docker
#
# The worker bind-mounts each run's cloned workspace to /workspace and pipes the
# Claude prompt in on stdin. Git is NOT used inside this container — the Python
# worker performs every Git operation on the host.
FROM node:20-bookworm-slim

# Tools an implementation agent commonly needs in the working tree. Add language
# runtimes/build tools here to match the repositories your team works on.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        ca-certificates \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Install the Claude Code CLI globally so `claude` is on PATH inside the container.
RUN npm install -g @anthropic-ai/claude-code

# Run as a non-root user; the mounted workspace is owned by the host user, but
# dropping root limits blast radius inside the sandbox.
RUN useradd --create-home --uid 10001 agent
USER agent

WORKDIR /workspace

# claude.command in config.yaml should be "claude" when docker.enabled is true.
ENTRYPOINT []
