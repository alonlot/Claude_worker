import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.runner import Worker
from app.web import create_app


def _setup(tmp_path, with_workspace=False):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")
    if with_workspace:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "f.py").write_text("x = 1\n", encoding="utf-8")
        db.update_run(run_id, state="done", workspace_path=str(ws))
    else:
        db.update_run(run_id, state="done")
    return config, db, run_id


def test_ide_comment_crud(tmp_path):
    config, db, run_id = _setup(tmp_path)
    cid = db.add_ide_comment(run_id, "f.py", 3, "Rename this variable")
    assert len(db.list_ide_comments(run_id)) == 1
    assert len(db.open_ide_comments(run_id)) == 1
    db.resolve_ide_comments(run_id)
    assert len(db.open_ide_comments(run_id)) == 0
    db.delete_ide_comment(cid, run_id)
    assert db.list_ide_comments(run_id) == []


def test_ide_comment_routes(tmp_path):
    config, db, run_id = _setup(tmp_path)
    client = TestClient(create_app(config, db))
    client.post(f"/runs/{run_id}/ide-comments", data={"file_path": "f.py", "line": 3, "body": "Fix naming"}, follow_redirects=False)
    assert len(db.open_ide_comments(run_id)) == 1
    page = client.get(f"/runs/{run_id}/ide-comments")
    assert page.status_code == 200
    assert "Fix naming" in page.text
    assert "f.py:3" in page.text


def test_apply_ide_comments_runs_and_resolves(tmp_path, monkeypatch):
    config, db, run_id = _setup(tmp_path, with_workspace=True)
    db.add_ide_comment(run_id, "f.py", 1, "Use a constant")

    async def fake_run_prompt(self, phase, prompt, cwd=None):
        # Simulate the agent editing the file.
        Path(cwd, "f.py").write_text("X = 1  # constant\n", encoding="utf-8")
        return "done"

    monkeypatch.setattr("app.claude_runner.ClaudeRunner.run_prompt", fake_run_prompt)

    worker = Worker(config, db, "local")
    worker.git = _FakeGit()
    asyncio.run(worker.run_ide_comment_fix(run_id))
    assert len(db.open_ide_comments(run_id)) == 0  # resolved
    assert db.fetchone("SELECT state FROM runs WHERE id=?", (run_id,))["state"] == "done"


class _FakeGit:
    def status(self, repo_path):
        return " M f.py"

    def changed_files(self, repo_path):
        return "f.py"

    def diff_stat(self, repo_path):
        return " f.py | 1 +-"

    def commit_all(self, repo_path, message):
        return "committed"

    def head_sha(self, repo_path):
        return "deadbeef"

    def review_diff(self, repo_path, base_branch):
        return "diff --git a/f.py b/f.py"
