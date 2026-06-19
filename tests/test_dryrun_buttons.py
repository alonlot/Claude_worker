import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app import notify
from app.config import Config, DockerConfig
from app.db import Database
from app.runner import Worker
from app.web import create_app


def _client(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    return config, db, TestClient(create_app(config, db))


def test_test_notify_button_dispatches(tmp_path, monkeypatch):
    config, db, client = _client(tmp_path)
    db.set_user_config("local", {"notify": {"webhook_enabled": True, "webhook_url": "https://x"}})
    sent = {}

    def fake_dispatch(cfg, *a, **k):
        sent["called"] = True
        return []

    monkeypatch.setattr(notify, "dispatch", fake_dispatch)
    resp = client.post("/settings/test-notify", follow_redirects=False)
    assert resp.status_code == 303
    assert sent.get("called") is True


def test_test_notify_requires_a_channel(tmp_path):
    config, db, client = _client(tmp_path)
    client.post("/settings/test-notify", follow_redirects=False)
    # No channel enabled -> app keeps a guidance flash (no crash).
    page = client.get("/settings")
    assert "Enable email" in page.text


def test_test_gate_check_reports_missing_binary(tmp_path):
    config, db, client = _client(tmp_path)
    db.set_user_config("local", {"test_gate": {"command": "definitely-not-a-real-binary-xyz --run"}})
    client.post("/settings/test-gate", follow_redirects=False)
    page = client.get("/settings")
    assert "not found on PATH" in page.text


def test_test_gate_check_finds_python(tmp_path):
    config, db, client = _client(tmp_path)
    db.set_user_config("local", {"test_gate": {"command": "python -c \"pass\""}})
    client.post("/settings/test-gate", follow_redirects=False)
    page = client.get("/settings")
    assert "looks runnable" in page.text


def test_docker_test_gate_builds_container_command(tmp_path, monkeypatch):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    config.docker = DockerConfig(enabled=True, image="claude-worker-agent:latest")
    config.test_gate.enabled = True
    config.test_gate.command = "pytest -q"
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")

    captured = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(argv, cwd=None, capture_output=None, text=None, timeout=None):
        captured["argv"] = argv
        captured["cwd"] = cwd
        return _Proc()

    monkeypatch.setattr("app.runner.subprocess.run", fake_run)
    ok, _ = asyncio.run(Worker(config, db, "local")._run_test_gate(run_id, Path(tmp_path)))
    assert ok is True
    # Runs through docker with the test command inside the container.
    assert captured["argv"][:3] == ["docker", "run", "--rm"]
    assert "claude-worker-agent:latest" in captured["argv"]
    assert "pytest -q" in captured["argv"]
