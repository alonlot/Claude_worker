from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.web import create_app, split_lines


def test_split_lines_accepts_lines_and_commas():
    assert split_lines("Review\nDone,Blocked\n\n") == ["Review", "Done", "Blocked"]


def test_run_detail_accepts_questions_and_mid_run_input(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket(
        {
            "key": "A-1",
            "summary": "Build UI",
            "status": "In Progress",
            "description": "Ticket body",
            "eligibility": "eligible",
        }
    )
    run_id = db.create_run("A-1")
    db.update_run(run_id, state="running_claude", progress=42)
    question_id = db.create_agent_question(run_id, "Which scope?", ["API", "UI", "Both"])
    app = create_app(config, db)
    client = TestClient(app)

    page = client.get(f"/runs/{run_id}")
    assert page.status_code == 200
    assert "Which scope?" in page.text

    answer = client.post(
        f"/agent-questions/{question_id}/answer",
        data={"run_id": str(run_id), "selected_answer": "UI", "free_answer": ""},
        follow_redirects=False,
    )
    assert answer.status_code == 303
    assert db.fetchone("SELECT answer FROM agent_questions WHERE id=?", (question_id,))["answer"] == "UI"

    user_input = client.post(f"/runs/{run_id}/input", data={"message": "Use the compact layout."}, follow_redirects=False)
    assert user_input.status_code == 303
    assert db.fetchone("SELECT message FROM agent_inputs WHERE run_id=?", (run_id,))["message"] == "Use the compact layout."


def test_queue_pause_and_notifications_routes(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.add_notification("Done", "A-1", "success", 1)
    app = create_app(config, db)
    client = TestClient(app)

    pause = client.post("/queue/pause", follow_redirects=False)
    assert pause.status_code == 303
    assert db.queue_paused() is True

    resume = client.post("/queue/resume", follow_redirects=False)
    assert resume.status_code == 303
    assert db.queue_paused() is False

    notes = client.get("/notifications/unread")
    assert notes.status_code == 200
    assert notes.json()[0]["title"] == "Done"
    assert db.unread_notifications() == []
