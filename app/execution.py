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
import shlex
import shutil
import subprocess
import tempfile
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
        # Run as the host user so the agent can write to the bind-mounted
        # workspace (whose files are owned by the worker user) and so files it
        # creates stay owned by that user. The image's baked-in user (uid 10001)
        # would otherwise hit "permission denied" on a native-Linux host.
        # os.getuid only exists on POSIX; on Docker Desktop/Windows the mount
        # translates ownership and no --user is needed.
        if cfg.run_as_host_user and hasattr(os, "getuid"):
            argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
            # The host uid has no home dir inside the image, so point the Claude
            # CLI at a writable HOME for its config/cache.
            agent_env = {"HOME": "/tmp", **agent_env}
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


def docker_preflight(config: Config) -> tuple[bool, str]:
    """Verify the Docker execution path end-to-end on the current host.

    Runs the same ``DockerBackend`` invocation real runs use, but with a
    harmless ``claude --version``, so a green result means containers actually
    start from the agent image with the Claude CLI on PATH. Blocking (shells
    out to ``docker``); call from a thread. Safe to run repeatedly.
    """
    docker = config.docker
    if not docker.enabled:
        return False, "Docker mode is off (set docker.enabled: true to sandbox runs in containers)."
    if not shutil.which("docker"):
        return False, "The 'docker' CLI is not on PATH on this host. Install Docker Engine / Docker Desktop."

    def _run(argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)

    try:
        info = _run(["docker", "info", "--format", "{{.ServerVersion}}"], 20)
    except Exception as exc:  # noqa: BLE001 - report any docker failure as text
        return False, f"Could not run docker: {exc}"
    if info.returncode != 0:
        return False, "Docker daemon is not reachable: " + (info.stderr or info.stdout).strip()[:300]
    server = info.stdout.strip()

    inspect = _run(["docker", "image", "inspect", docker.image], 20)
    if inspect.returncode != 0:
        return False, (
            f"Agent image '{docker.image}' is missing on this host. Build it: "
            f"docker build -t {docker.image} ./docker"
        )

    try:
        base_cmd = shlex.split(config.claude.command or "claude")
    except ValueError:
        base_cmd = ["claude"]
    inv = DockerBackend(docker).build(base_cmd + ["--version"], None, {}, "cw_preflight")
    try:
        smoke = _run(inv.argv, 90)
    except Exception as exc:  # noqa: BLE001
        return False, f"Container smoke test could not run: {exc}"
    if smoke.returncode != 0:
        detail = (smoke.stderr or smoke.stdout).strip()[-300:]
        return False, (
            f"Container started but '{' '.join(base_cmd)} --version' failed inside it. "
            f"Make sure claude.command is just 'claude' in Docker mode. Detail: {detail}"
        )

    version = (smoke.stdout.strip().splitlines() or ["(no output)"])[0]

    # The implementation agent must be able to WRITE to the bind-mounted
    # workspace. The image runs as a non-root user, so a uid mismatch with the
    # host clone is the most common silent failure — verify it for real.
    write_ok, write_detail = _docker_workspace_write_test(docker)
    if not write_ok:
        return False, (
            f"Docker starts (daemon {server}, image {docker.image}), but the container cannot write to a "
            f"bind-mounted workspace, so the agent could not edit files. {write_detail}"
        )

    note = "" if config.claude.api_key else (
        " WARNING: claude.api_key is empty — the container will NOT inherit your host Claude login, "
        "so real runs need an API key set in config."
    )
    return True, (
        f"Docker OK (daemon {server}, image {docker.image}). Smoke test: {version}. "
        f"Workspace write: OK.{note}"
    )


def _docker_workspace_write_test(docker: DockerConfig) -> tuple[bool, str]:
    """Bind-mount a temp dir and confirm the container can write into it."""
    marker = ".cw_preflight_write"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            inv = DockerBackend(docker).build(
                ["sh", "-lc", f"echo ok > {docker.workspace_mount}/{marker}"], tmp, {}, "cw_preflight_write"
            )
            try:
                proc = subprocess.run(inv.argv, capture_output=True, text=True, timeout=60)
            except Exception as exc:  # noqa: BLE001
                return False, f"Write test could not run: {exc}"
            if not (Path(tmp) / marker).exists():
                detail = (proc.stderr or proc.stdout).strip()[-300:]
                hint = (
                    "docker.run_as_host_user is off — turn it on so the container runs as your uid."
                    if not docker.run_as_host_user
                    else "Even running as the host user the write failed; check the mounted path's permissions."
                )
                return False, f"{hint} Detail: {detail}"
    except Exception as exc:  # noqa: BLE001
        return False, f"Write test setup failed: {exc}"
    return True, "ok"
