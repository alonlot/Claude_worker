from fastapi.testclient import TestClient

from app.config import Config, apply_user_sections, config_to_user_sections
from app.db import Database
from app.runner import Worker
from app.web import create_app


def test_delivery_section_round_trips():
    cfg = Config()
    cfg.delivery.scp_host = "laptop.local"
    cfg.delivery.scp_path = "/home/me/runs"
    sections = config_to_user_sections(cfg)
    assert sections["delivery"]["scp_host"] == "laptop.local"
    rebuilt = apply_user_sections(Config(), sections)
    assert rebuilt.delivery.scp_host == "laptop.local"
    assert rebuilt.delivery.scp_path == "/home/me/runs"


def _worker(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    return config, db, Worker(config, db, "local")


def test_scp_command_with_key_and_port(tmp_path):
    config, db, worker = _worker(tmp_path)
    config.delivery.scp_host = "host"
    config.delivery.scp_user = "me"
    config.delivery.scp_path = "/tmp/x"
    config.delivery.ssh_key = "/k"
    config.delivery.ssh_port = 2222
    cmd = worker._scp_command("/src")
    assert cmd[:2] == ["scp", "-r"]
    assert "-P" in cmd and "2222" in cmd
    assert "-i" in cmd and "/k" in cmd
    assert cmd[-2:] == ["/src", "me@host:/tmp/x"]


def test_scp_command_default_port_has_no_flag(tmp_path):
    config, db, worker = _worker(tmp_path)
    config.delivery.scp_host = "host"
    config.delivery.scp_path = "/tmp/x"
    cmd = worker._scp_command("/src")
    assert "-P" not in cmd
    assert cmd[-1] == "host:/tmp/x"  # no user prefix


def test_deliver_locally_requires_config(tmp_path):
    config, db, worker = _worker(tmp_path)
    db.upsert_ticket({"key": "A-1", "summary": "x", "status": "To Do"}, owner="local")
    run_id = db.create_run("A-1", owner="local")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    db.update_run(run_id, state="done", workspace_path=str(workspace))
    try:
        worker.deliver_locally(run_id)
        raised = False
    except RuntimeError as exc:
        raised = "Configure delivery" in str(exc)
    assert raised


def test_deliver_route_and_settings_persist(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    client = TestClient(create_app(config, db))
    saved = client.post(
        "/settings/visual",
        data={"scp_host": "host", "scp_path": "/p", "claude_command": "claude"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert db.get_user_config("local")["delivery"]["scp_host"] == "host"

    db.upsert_ticket({"key": "A-1", "summary": "x", "status": "To Do"}, owner="local")
    run_id = db.create_run("A-1", owner="local")
    db.update_run(run_id, state="done", workspace_path=str(tmp_path))
    resp = client.post(f"/runs/{run_id}/deliver", follow_redirects=False)
    assert resp.status_code == 303
