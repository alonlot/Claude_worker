from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.runner import Worker
from app.web import create_app


def _db(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    return config, db


def test_review_note_stores_diff_hunk(tmp_path):
    _, db = _db(tmp_path)
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")
    db.upsert_code_review_note(
        run_id,
        {
            "external_id": "99",
            "kind": "review",
            "author": "reviewer",
            "file_path": "app/x.py",
            "line": 12,
            "body": "Use a constant here",
            "diff_hunk": "@@ -10,3 +10,4 @@\n-old = 1\n+new = 2",
            "source_url": "https://github.com/o/r/pull/1",
        },
    )
    note = db.code_review_notes(run_id)[0]
    assert note["diff_hunk"].startswith("@@ -10,3 +10,4 @@")


def test_format_review_note_includes_git_hunk(tmp_path):
    config, db = _db(tmp_path)
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")
    db.upsert_code_review_note(
        run_id,
        {"external_id": "1", "kind": "review", "file_path": "f.py", "line": 3,
         "body": "fix", "diff_hunk": "@@ hunk @@\n+code"},
    )
    note = db.code_review_notes(run_id)[0]
    text = Worker(config, db, "local")._format_review_note(note)
    assert "CODE FROM GIT:" in text
    assert "@@ hunk @@" in text
    assert "NOTE_ID:" in text


def test_code_review_live_renders_git_hunk(tmp_path):
    config, db = _db(tmp_path)
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")
    db.update_run(run_id, state="done")
    db.upsert_code_review_note(
        run_id,
        {"external_id": "1", "kind": "review", "file_path": "f.py", "line": 3,
         "body": "fix", "diff_hunk": "@@ visible-hunk @@\n+x = 1"},
    )
    client = TestClient(create_app(config, db))
    page = client.get(f"/runs/{run_id}/code-review/live")
    assert page.status_code == 200
    assert "Code from git" in page.text
    assert "visible-hunk" in page.text
