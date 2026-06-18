from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.web import create_app


def _make(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    config.auth.default_user = ""  # force explicit identity via header
    db = Database(config.app.database_path)
    db.init()
    return config, db


def test_runs_are_isolated_between_users(tmp_path):
    config, db = _make(tmp_path)
    db.upsert_ticket({"key": "A-1", "summary": "Alice work", "status": "To Do", "eligibility": "eligible"}, owner="alice")
    run_id = db.create_run("A-1", owner="alice")
    db.update_run(run_id, state="done", workspace_path="")
    client = TestClient(create_app(config, db))

    owner = client.get(f"/runs/{run_id}", headers={"X-Forwarded-User": "alice"}, follow_redirects=False)
    assert owner.status_code == 200

    intruder = client.get(f"/runs/{run_id}", headers={"X-Forwarded-User": "bob"}, follow_redirects=False)
    assert intruder.status_code == 303
    assert intruder.headers["location"] == "/"

    logs = client.get(f"/runs/{run_id}/logs", headers={"X-Forwarded-User": "bob"})
    assert logs.status_code == 404


def test_dashboard_only_shows_own_tickets(tmp_path):
    config, db = _make(tmp_path)
    db.upsert_ticket(
        {"key": "A-1", "summary": "AliceTicketXYZ", "status": "To Do", "eligibility": "skipped", "skip_reason": "x"},
        owner="alice",
    )
    db.upsert_ticket(
        {"key": "B-1", "summary": "BobTicketXYZ", "status": "To Do", "eligibility": "skipped", "skip_reason": "y"},
        owner="bob",
    )
    client = TestClient(create_app(config, db))
    page = client.get("/", headers={"X-Forwarded-User": "alice"})
    assert "AliceTicketXYZ" in page.text
    assert "BobTicketXYZ" not in page.text


def test_queue_pause_is_per_user(tmp_path):
    config, db = _make(tmp_path)
    client = TestClient(create_app(config, db))
    client.post("/queue/pause", headers={"X-Forwarded-User": "alice"}, follow_redirects=False)
    assert db.queue_paused(owner="alice") is True
    assert db.queue_paused(owner="bob") is False


def test_visual_settings_saved_under_username(tmp_path):
    config, db = _make(tmp_path)
    client = TestClient(create_app(config, db))
    resp = client.post(
        "/settings/visual",
        headers={"X-Forwarded-User": "alice"},
        data={
            "jira_url": "https://alice.example.net",
            "jira_email": "alice@example.net",
            "jira_token": "secret-token",
            "claude_command": "claude",
            "git_default_repo_url": "git@example.com:alice/repo.git",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    saved = db.get_user_config("alice")
    assert saved["jira"]["url"] == "https://alice.example.net"
    assert saved["git"]["default_repo_url"] == "git@example.com:alice/repo.git"
    # Bob has no config of his own.
    assert db.get_user_config("bob") == {}


def test_enqueue_route_scopes_to_user(tmp_path):
    config, db = _make(tmp_path)
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "skipped"}, owner="alice")
    client = TestClient(create_app(config, db))
    client.post("/tickets/A-1/enqueue", headers={"X-Forwarded-User": "alice"}, follow_redirects=False)
    alice_q = db.fetchone("SELECT * FROM queue_items WHERE ticket_key='A-1' AND owner='alice'")
    bob_q = db.fetchone("SELECT * FROM queue_items WHERE ticket_key='A-1' AND owner='bob'")
    assert alice_q is not None
    assert bob_q is None
