import asyncio

from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.jira_client import JiraClient
from app.runner import Worker
from app.web import create_app


def _setup(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    return config, db


def test_start_ticket_creates_placeholder_when_jira_absent(tmp_path):
    config, db = _setup(tmp_path)
    worker = Worker(config, db, "local")  # no Jira configured
    queue_id = asyncio.run(worker.start_ticket("proj-7"))
    assert queue_id > 0
    ticket = db.fetchone("SELECT * FROM tickets WHERE key=? AND owner=?", ("PROJ-7", "local"))
    assert ticket["eligibility"] == "eligible"
    assert "PROJ-7" in ticket["summary"]


def test_start_ticket_fetches_from_jira_when_configured(tmp_path, monkeypatch):
    config, db = _setup(tmp_path)
    config.jira.url = "https://example.atlassian.net"
    config.jira.email = "me@example.com"
    config.jira.token = "tok"

    async def fake_get_issue(self, key):
        return {"key": key, "summary": "Real summary", "status": "In Progress", "labels": []}

    monkeypatch.setattr(JiraClient, "get_issue", fake_get_issue)
    worker = Worker(config, db, "local")
    queue_id = asyncio.run(worker.start_ticket("ABC-1"))
    assert queue_id > 0
    ticket = db.fetchone("SELECT * FROM tickets WHERE key=? AND owner=?", ("ABC-1", "local"))
    assert ticket["summary"] == "Real summary"
    assert ticket["eligibility"] == "eligible"  # forced eligible regardless of status


def test_start_ticket_falls_back_to_placeholder_on_jira_error(tmp_path, monkeypatch):
    config, db = _setup(tmp_path)
    config.jira.url = "https://example.atlassian.net"
    config.jira.email = "me@example.com"
    config.jira.token = "tok"

    async def boom(self, key):
        raise RuntimeError("404 not found")

    monkeypatch.setattr(JiraClient, "get_issue", boom)
    worker = Worker(config, db, "local")
    queue_id = asyncio.run(worker.start_ticket("GONE-9"))
    assert queue_id > 0
    ticket = db.fetchone("SELECT * FROM tickets WHERE key=? AND owner=?", ("GONE-9", "local"))
    assert ticket["eligibility"] == "eligible"


def test_start_ticket_blank_key_raises(tmp_path):
    config, db = _setup(tmp_path)
    worker = Worker(config, db, "local")
    try:
        asyncio.run(worker.start_ticket("   "))
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_start_route_enqueues_and_redirects(tmp_path):
    config, db = _setup(tmp_path)
    client = TestClient(create_app(config, db))
    resp = client.post("/tickets/start", data={"ticket_key": "demo-5"}, follow_redirects=False)
    assert resp.status_code == 303
    item = db.fetchone(
        "SELECT * FROM queue_items WHERE ticket_key=? AND owner=?", ("DEMO-5", "local")
    )
    assert item is not None


def test_start_route_blank_key_flashes(tmp_path):
    config, db = _setup(tmp_path)
    client = TestClient(create_app(config, db))
    client.post("/tickets/start", data={"ticket_key": ""}, follow_redirects=False)
    page = client.get("/")
    assert "Enter a Jira ticket key" in page.text
