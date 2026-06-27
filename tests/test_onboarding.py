from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.web import create_app


def _client(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    return TestClient(create_app(config, db)), db


def test_first_time_user_is_redirected_to_wizard(tmp_path):
    client, _ = _client(tmp_path)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/onboarding"


def test_user_with_tickets_is_not_forced_into_wizard(tmp_path):
    client, db = _client(tmp_path)
    db.upsert_ticket(
        {"key": "A-1", "summary": "Existing work", "status": "To Do", "eligibility": "skipped"},
        owner="local",
    )
    # No saved config, but the user already has activity -> straight to dashboard.
    assert client.get("/", follow_redirects=False).status_code == 200


def test_wizard_renders_first_step(tmp_path):
    client, _ = _client(tmp_path)
    page = client.get("/onboarding")
    assert page.status_code == 200
    assert "Connect Jira" in page.text
    assert 'name="jira_url"' in page.text


def test_saving_a_step_persists_config_and_advances(tmp_path):
    client, db = _client(tmp_path)
    resp = client.post(
        "/onboarding/step/jira",
        data={
            "jira_url": "https://acme.atlassian.net",
            "jira_email": "dev@acme.com",
            "jira_token": "secret-token",
            "jira_jql": "assignee = currentUser()",
            "max_results": "30",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/onboarding?step=git"

    saved = db.get_user_config("local")
    assert saved["jira"]["url"] == "https://acme.atlassian.net"
    assert saved["jira"]["max_results"] == 30

    # With config saved, the dashboard no longer forces the wizard.
    assert client.get("/", follow_redirects=False).status_code == 200


def test_last_step_advances_to_done(tmp_path):
    client, _ = _client(tmp_path)
    resp = client.post(
        "/onboarding/step/claude",
        data={"claude_command": "claude", "claude_timeout_seconds": "3600"},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/onboarding?step=done"
    assert "You're all set" in client.get("/onboarding?step=done").text


def test_completed_steps_show_in_stepper(tmp_path):
    client, _ = _client(tmp_path)
    client.post(
        "/onboarding/step/jira",
        data={"jira_url": "https://acme.atlassian.net", "jira_email": "d@acme.com", "jira_token": "t"},
    )
    page = client.get("/onboarding?step=git")
    assert "current" in page.text  # git is the current step
    assert "done" in page.text  # jira is marked complete


def test_skip_stops_the_redirect(tmp_path):
    client, db = _client(tmp_path)
    resp = client.post("/onboarding/skip", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert db.get_state("onboarding_skipped", owner="local") == "1"
    # Empty config but skipped -> dashboard renders instead of redirecting.
    assert client.get("/", follow_redirects=False).status_code == 200


def test_inline_test_saves_values_and_reports_failure(tmp_path):
    client, db = _client(tmp_path)
    resp = client.post(
        "/onboarding/test/jira",
        data={
            "jira_url": "http://127.0.0.1:9",  # nothing listening -> connection refused
            "jira_email": "dev@acme.com",
            "jira_token": "secret-token",
        },
    )
    assert resp.status_code == 200
    assert "test-banner err" in resp.text
    # The entered values are persisted so the test runs against them.
    assert db.get_user_config("local")["jira"]["url"] == "http://127.0.0.1:9"


def test_unknown_step_falls_back_to_first(tmp_path):
    client, _ = _client(tmp_path)
    assert "Connect Jira" in client.get("/onboarding?step=bogus").text
    assert client.post("/onboarding/step/bogus", follow_redirects=False).headers["location"] == "/onboarding"
