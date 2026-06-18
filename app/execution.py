"""Execution backends for the Claude agent.

The worker should not care *where* the agent process runs. It hands a Claude CLI
command + working directory + the environment the agent needs, and a backend
turns that into an actual OS process:

- ``SubprocessBackend`` runs the agent directly on the host (dev / Windows).
- ``DockerBackend`` runs each invocation inside a throwaway container so that
  per-user runs are isolated from each other and from the host.

Git stays on the host in both cases (Python owns all Git operations, per the
worker contract); only the agent execution is sandboxed. In Docker mode the
host-side cloned workspace is bind-mounted into the container so the agent can
read and edit the files while git history/credentials never enter the container.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import Config, DockerConfig


@dataclass
class Invocation:
    """A ready-to-spawn process description."""

    argv: list[str]
    env: dict[str, str]
    cwd: str | None
    # Command that force-stops the process out-of-band (e.g. `docker kill`).
    # None means "just terminate the spawned process".
    cancel_argv: list[str] | None = None


def sanitize_label(label: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_.-]+", "_", label).strip("_")
    if not clean or not clean[0].isalnum():
        clean = f"x{clean}"
    return clean[:200]


class ExecutionBackend:
    name = "base"

    def build(self, claude_cmd: list[str], cwd: str | Path | None, agent_env: dict[str, str], label: str) -> Invocation:
        raise NotImplementedError


class SubprocessBackend(ExecutionBackend):
    name = "subprocess"

    def build(self, claude_cmd: list[str], cwd: str | Path | None, agent_env: dict[str, str], label: str) -> Invocation:
        env = os.environ.copy()
        env.update({k: v for k, v in agent_env.items() if v})
        return Invocation(argv=list(claude_cmd), env=env, cwd=str(cwd) if cwd else None)


class DockerBackend(ExecutionBackend):
    name = "docker"

    def __init__(self, docker: DockerConfig):
        self.docker = docker

    def build(self, claude_cmd: list[str], cwd: str | Path | None, agent_env: dict[str, str], label: str) -> Invocation:
        cfg = self.docker
        name = sanitize_label(label)
        argv: list[str] = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--name",
            name,
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
        ]
        if cwd:
            host_path = str(Path(cwd).resolve())
            argv += ["-v", f"{host_path}:{cfg.workspace_mount}", "-w", cfg.workspace_mount]
        if cfg.network:
            argv += ["--network", cfg.network]
        if cfg.memory:
            argv += ["--memory", cfg.memory]
        if cfg.cpus:
            argv += ["--cpus", cfg.cpus]
        for key, value in agent_env.items():
            if value:
                argv += ["-e", f"{key}={value}"]
        argv += list(cfg.extra_args)
        argv += [cfg.image]
        argv += list(claude_cmd)
        # The docker CLI itself needs the host env (DOCKER_HOST, PATH, ...).
        return Invocation(argv=argv, env=os.environ.copy(), cwd=None, cancel_argv=["docker", "kill", name])


def get_execution_backend(config: Config) -> ExecutionBackend:
    if config.docker.enabled:
        return DockerBackend(config.docker)
    return SubprocessBackend()
