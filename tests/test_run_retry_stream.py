import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.runner import Worker
from app.web import create_app


class _FakeGit:
    def status(self, repo_path):
        return " M f.py"

    def changed_files(self, repo_path):
        return "f.py"

    def diff_stat(self, repo_path):
        return " f.py | 2 +-"

    def commit_all(self, repo_path, message):
        return "committed"

    def head_sha(self, repo_path):
        return "cafe1234"


def _db(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "w.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    return config, db


def test_retry_run_reuses_workspace_and_finishes(tmp_path, monkeypatch):
    config, db = _db(tmp_path)
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f.py").write_text("x = 1\n", encoding="utf-8")
    failed = db.create_run("A-1")
    db.update_run(failed, state="failed", workspace_path=str(ws), repo_url="git@x/r.git",
                  base_branch="main", branch_name="A-1/work")

    async def fake_run_prompt(self, phase, prompt, cwd=None):
        if phase == "review":
            return "REVIEW_RESULT: pass\nlooks good"
        Path(cwd, "f.py").write_text("x = 2\n", encoding="utf-8")  # simulate edits
        return "done PROGRESS 80%"

    monkeypatch.setattr("app.claude_runner.ClaudeRunner.run_prompt", fake_run_prompt)
    worker = Worker(config, db, "local")
    worker.git = _FakeGit()
    new_id = asyncio.run(worker.retry_run(failed))

    assert new_id != failed
    new_run = db.fetchone("SELECT * FROM runs WHERE id=?", (new_id,))
    assert new_run["workspace_path"] == str(ws)  # same workspace, no re-clone
    assert new_run["state"] == "done"
    assert new_run["commit_sha"] == "cafe1234"


def test_retry_run_rejects_non_failed(tmp_path):
    config, db = _db(tmp_path)
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    rid = db.create_run("A-1")
    db.update_run(rid, state="done", workspace_path=str(tmp_path))
    worker = Worker(config, db, "local")
    try:
        asyncio.run(worker.retry_run(rid))
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "failed or cancelled" in str(exc)


def test_retry_run_rejects_missing_workspace(tmp_path):
    config, db = _db(tmp_path)
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    rid = db.create_run("A-1")
    db.update_run(rid, state="failed", workspace_path=str(tmp_path / "gone"))
    worker = Worker(config, db, "local")
    try:
        asyncio.run(worker.retry_run(rid))
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "workspace is gone" in str(exc)


def test_retry_route_flashes_for_done_run(tmp_path):
    config, db = _db(tmp_path)
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    rid = db.create_run("A-1")
    db.update_run(rid, state="done", workspace_path=str(tmp_path))
    client = TestClient(create_app(config, db))
    resp = client.post(f"/runs/{rid}/retry", follow_redirects=False)
    assert resp.status_code == 303
    page = client.get(f"/runs/{rid}")
    assert "Only a failed or cancelled run" in page.text


def test_logs_stream_emits_lines_and_done(tmp_path):
    config, db = _db(tmp_path)
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")
    db.add_log(run_id, "git", "cloned to /tmp/x")
    db.add_log(run_id, "claude", "PROGRESS 40%")
    db.update_run(run_id, state="done")  # terminal so the stream completes
    client = TestClient(create_app(config, db))
    resp = client.get(f"/runs/{run_id}/logs/stream?after=0")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert "data: [git] cloned to /tmp/x" in resp.text
    assert "data: [claude] PROGRESS 40%" in resp.text
    assert "event: done" in resp.text


def test_logs_stream_resumes_after_id(tmp_path):
    config, db = _db(tmp_path)
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")
    db.add_log(run_id, "git", "first line")
    first_id = db.fetchone("SELECT MAX(id) AS m FROM logs WHERE run_id=?", (run_id,))["m"]
    db.add_log(run_id, "git", "second line")
    db.update_run(run_id, state="done")
    client = TestClient(create_app(config, db))
    resp = client.get(f"/runs/{run_id}/logs/stream?after={first_id}")
    assert "second line" in resp.text
    assert "first line" not in resp.text  # skipped: already seen
