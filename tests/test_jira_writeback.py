import asyncio

from app.config import Config, _config_from_data
from app.db import Database
from app.jira_client import JiraClient
from app.runner import Worker


class _FakeResp:
    def __init__(self, json_data=None, status=200):
        self._json = json_data or {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")

    def json(self):
        return self._json


class _FakeClient:
    calls: list = []
    get_response: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, auth=None):
        _FakeClient.calls.append(("POST", url, json))
        return _FakeResp({})

    async def get(self, url, auth=None, params=None):
        _FakeClient.calls.append(("GET", url))
        return _FakeResp(_FakeClient.get_response)


def _jira_config():
    cfg = Config().jira
    cfg.url = "https://example.atlassian.net"
    cfg.email = "me@example.com"
    cfg.token = "tok"
    return cfg


def test_writeback_config_parses():
    cfg = _config_from_data({"jira": {"writeback_enabled": "true", "writeback_transition": "In Review", "writeback_comment": "false"}})
    assert cfg.jira.writeback_enabled is True
    assert cfg.jira.writeback_transition == "In Review"
    assert cfg.jira.writeback_comment is False


def test_add_comment_posts_adf(monkeypatch):
    _FakeClient.calls = []
    monkeypatch.setattr("app.jira_client.httpx.AsyncClient", _FakeClient)
    asyncio.run(JiraClient(_jira_config()).add_comment("A-1", "line one\nline two"))
    posts = [c for c in _FakeClient.calls if c[0] == "POST"]
    assert posts and posts[0][1].endswith("/issue/A-1/comment")
    assert posts[0][2]["body"]["type"] == "doc"


def test_transition_issue_matches_by_name(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.get_response = {"transitions": [{"id": "31", "name": "In Review", "to": {"name": "In Review"}}]}
    monkeypatch.setattr("app.jira_client.httpx.AsyncClient", _FakeClient)
    ok = asyncio.run(JiraClient(_jira_config()).transition_issue("A-1", "In Review"))
    assert ok is True
    assert ("POST", "https://example.atlassian.net/rest/api/3/issue/A-1/transitions", {"transition": {"id": "31"}}) in _FakeClient.calls


def test_transition_issue_unknown_status_returns_false(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.get_response = {"transitions": [{"id": "31", "name": "Done", "to": {"name": "Done"}}]}
    monkeypatch.setattr("app.jira_client.httpx.AsyncClient", _FakeClient)
    ok = asyncio.run(JiraClient(_jira_config()).transition_issue("A-1", "In Review"))
    assert ok is False
    assert not [c for c in _FakeClient.calls if c[0] == "POST"]


def _done_run(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")
    db.update_run(run_id, state="done", branch_name="A-1/work", commit_sha="abc123", run_report="Ticket: A-1\nState: done")
    return config, db, run_id


def test_runner_writeback_comments_and_transitions(tmp_path, monkeypatch):
    config, db, run_id = _done_run(tmp_path)
    config.jira.writeback_enabled = True
    config.jira.writeback_transition = "In Review"
    config.jira.writeback_comment = True
    recorded = {}

    async def fake_comment(self, key, body):
        recorded["comment"] = (key, body)

    async def fake_transition(self, key, status):
        recorded["transition"] = (key, status)
        return True

    monkeypatch.setattr(JiraClient, "add_comment", fake_comment)
    monkeypatch.setattr(JiraClient, "transition_issue", fake_transition)
    asyncio.run(Worker(config, db, "local")._jira_writeback(run_id))
    assert recorded["comment"][0] == "A-1"
    assert "A-1/work" in recorded["comment"][1]
    assert recorded["transition"] == ("A-1", "In Review")


def test_runner_writeback_disabled_is_noop(tmp_path, monkeypatch):
    config, db, run_id = _done_run(tmp_path)
    config.jira.writeback_enabled = False

    async def boom(self, *a, **k):
        raise AssertionError("write-back should not run when disabled")

    monkeypatch.setattr(JiraClient, "add_comment", boom)
    monkeypatch.setattr(JiraClient, "transition_issue", boom)
    asyncio.run(Worker(config, db, "local")._jira_writeback(run_id))  # no exception = no calls
