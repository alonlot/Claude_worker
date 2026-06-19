from pathlib import Path

from fastapi.testclient import TestClient

from app import execution
from app.config import Config, DockerConfig
from app.db import Database
from app.execution import docker_preflight
from app.web import create_app


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _simulate_workspace_write(argv):
    """Mimic a container writing the marker into the bind-mounted temp dir."""
    if "-v" not in argv:
        return
    host_path = argv[argv.index("-v") + 1].rsplit(":", 1)[0]
    (Path(host_path) / ".cw_preflight_write").write_text("ok", encoding="utf-8")


def _docker_config(api_key="k"):
    config = Config()
    config.docker = DockerConfig(enabled=True, image="claude-worker-agent:latest")
    config.claude.command = "claude"
    config.claude.api_key = api_key
    return config


def test_preflight_off_when_docker_disabled():
    ok, msg = docker_preflight(Config())  # docker.enabled is False by default
    assert ok is False
    assert "Docker mode is off" in msg


def test_preflight_reports_missing_docker_cli(monkeypatch):
    monkeypatch.setattr(execution.shutil, "which", lambda _: None)
    ok, msg = docker_preflight(_docker_config())
    assert ok is False
    assert "not on PATH" in msg


def test_preflight_reports_daemon_down(monkeypatch):
    monkeypatch.setattr(execution.shutil, "which", lambda _: "/usr/bin/docker")

    def fake_run(argv, capture_output=None, text=None, timeout=None):
        return _Proc(returncode=1, stderr="Cannot connect to the Docker daemon")

    monkeypatch.setattr(execution.subprocess, "run", fake_run)
    ok, msg = docker_preflight(_docker_config())
    assert ok is False
    assert "not reachable" in msg


def test_preflight_reports_missing_image(monkeypatch):
    monkeypatch.setattr(execution.shutil, "which", lambda _: "/usr/bin/docker")

    def fake_run(argv, capture_output=None, text=None, timeout=None):
        if argv[:2] == ["docker", "info"]:
            return _Proc(returncode=0, stdout="24.0.5")
        if argv[:3] == ["docker", "image", "inspect"]:
            return _Proc(returncode=1, stderr="No such image")
        return _Proc(returncode=0)

    monkeypatch.setattr(execution.subprocess, "run", fake_run)
    ok, msg = docker_preflight(_docker_config())
    assert ok is False
    assert "is missing" in msg and "docker build" in msg


def test_preflight_reports_container_smoke_failure(monkeypatch):
    monkeypatch.setattr(execution.shutil, "which", lambda _: "/usr/bin/docker")

    def fake_run(argv, capture_output=None, text=None, timeout=None):
        if argv[:2] == ["docker", "info"]:
            return _Proc(returncode=0, stdout="24.0.5")
        if argv[:3] == ["docker", "image", "inspect"]:
            return _Proc(returncode=0)
        # the smoke `docker run ... claude --version`
        return _Proc(returncode=127, stderr="claude: not found")

    monkeypatch.setattr(execution.subprocess, "run", fake_run)
    ok, msg = docker_preflight(_docker_config())
    assert ok is False
    assert "failed inside it" in msg


def test_preflight_passes_and_warns_without_api_key(monkeypatch):
    monkeypatch.setattr(execution.shutil, "which", lambda _: "/usr/bin/docker")
    seen = {}

    def fake_run(argv, capture_output=None, text=None, timeout=None):
        if argv[:2] == ["docker", "info"]:
            return _Proc(returncode=0, stdout="24.0.5")
        if argv[:3] == ["docker", "image", "inspect"]:
            return _Proc(returncode=0)
        if any(".cw_preflight_write" in a for a in argv):
            _simulate_workspace_write(argv)
            return _Proc(returncode=0)
        seen["smoke"] = argv
        return _Proc(returncode=0, stdout="claude 1.2.3")

    monkeypatch.setattr(execution.subprocess, "run", fake_run)
    ok, msg = docker_preflight(_docker_config(api_key=""))
    assert ok is True
    assert "claude 1.2.3" in msg
    assert "Workspace write: OK" in msg
    assert "WARNING: claude.api_key is empty" in msg
    # The smoke test goes through the real docker-run invocation.
    assert seen["smoke"][:3] == ["docker", "run", "--rm"]
    assert "--version" in seen["smoke"]


def test_preflight_passes_clean_with_api_key(monkeypatch):
    monkeypatch.setattr(execution.shutil, "which", lambda _: "/usr/bin/docker")

    def fake_run(argv, capture_output=None, text=None, timeout=None):
        if argv[:2] == ["docker", "info"]:
            return _Proc(returncode=0, stdout="24.0.5")
        if argv[:3] == ["docker", "image", "inspect"]:
            return _Proc(returncode=0)
        if any(".cw_preflight_write" in a for a in argv):
            _simulate_workspace_write(argv)
            return _Proc(returncode=0)
        return _Proc(returncode=0, stdout="claude 1.2.3")

    monkeypatch.setattr(execution.subprocess, "run", fake_run)
    ok, msg = docker_preflight(_docker_config(api_key="sk-key"))
    assert ok is True
    assert "Workspace write: OK" in msg
    assert "WARNING" not in msg


def test_preflight_reports_workspace_write_failure(monkeypatch):
    monkeypatch.setattr(execution.shutil, "which", lambda _: "/usr/bin/docker")

    def fake_run(argv, capture_output=None, text=None, timeout=None):
        if argv[:2] == ["docker", "info"]:
            return _Proc(returncode=0, stdout="24.0.5")
        if argv[:3] == ["docker", "image", "inspect"]:
            return _Proc(returncode=0)
        if any(".cw_preflight_write" in a for a in argv):
            # Simulate a uid mismatch: container cannot write the marker file.
            return _Proc(returncode=1, stderr="Permission denied")
        return _Proc(returncode=0, stdout="claude 1.2.3")

    monkeypatch.setattr(execution.subprocess, "run", fake_run)
    ok, msg = docker_preflight(_docker_config())
    assert ok is False
    assert "cannot write to a bind-mounted workspace" in msg


def test_test_docker_route_flashes(tmp_path, monkeypatch):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    monkeypatch.setattr("app.web.docker_preflight", lambda cfg: (True, "Docker OK (daemon 24.0.5)."))
    client = TestClient(create_app(config, db))
    resp = client.post("/settings/test-docker", follow_redirects=False)
    assert resp.status_code == 303
    page = client.get("/settings")
    assert "Docker preflight passed" in page.text
