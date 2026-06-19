from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.web import create_app, run_stats


def _db(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    return config, db


def test_run_stats_aggregates(tmp_path):
    config, db = _db(tmp_path)
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    for state in ["done", "pushed", "failed", "running_claude"]:
        rid = db.create_run("A-1")
        db.update_run(rid, state=state, finished_at="2026-06-19T10:00:05")
    stats = run_stats(db, "local")
    assert stats["total"] == 4
    assert stats["success"] == 2  # done + pushed
    assert stats["failed"] == 1  # failed
    assert stats["success_rate"] == 50
    assert len(stats["series"]) == 14
    assert len(stats["recent"]) == 4


def test_stats_page_renders(tmp_path):
    config, db = _db(tmp_path)
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    db.create_run("A-1")
    client = TestClient(create_app(config, db))
    page = client.get("/stats")
    assert page.status_code == 200
    assert "Run Stats" in page.text
    assert "Success rate" in page.text


def test_stats_empty_is_safe(tmp_path):
    config, db = _db(tmp_path)
    stats = run_stats(db, "local")
    assert stats["total"] == 0
    assert stats["success_rate"] == 0
    assert stats["avg_duration"] == "-"
