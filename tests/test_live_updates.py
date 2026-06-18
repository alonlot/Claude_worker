from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.web import create_app


def _client(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    return config, db, TestClient(create_app(config, db))


def test_plan_body_partial_renders_plan(tmp_path):
    config, db, client = _client(tmp_path)
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    db.enqueue("A-1")
    queue_id = db.fetchone("SELECT id FROM queue_items WHERE ticket_key='A-1'")["id"]
    db.upsert_ticket_plan(
        {"ticket_key": "A-1", "queue_item_id": queue_id, "mission": "Do one", "plan_text": "Steps"}
    )
    page = client.get(f"/queue/{queue_id}/plan-body")
    assert page.status_code == 200
    assert 'id="plan-live"' in page.text
    assert "Do one" in page.text


def test_plan_body_polls_only_while_planning(tmp_path):
    config, db, client = _client(tmp_path)
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    db.enqueue("A-1")
    queue_id = db.fetchone("SELECT id FROM queue_items WHERE ticket_key='A-1'")["id"]

    db.set_queue_state(queue_id, "planning")
    planning = client.get(f"/queue/{queue_id}/plan-body")
    assert "every 2s" in planning.text

    db.set_queue_state(queue_id, "plan_ready")
    ready = client.get(f"/queue/{queue_id}/plan-body")
    assert "every 2s" not in ready.text


def test_run_interaction_partial_polls_while_active(tmp_path):
    config, db, client = _client(tmp_path)
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")
    db.update_run(run_id, state="running_claude")
    db.create_agent_question(run_id, "Which path?", ["A", "B", "C"])

    active = client.get(f"/runs/{run_id}/interaction")
    assert active.status_code == 200
    assert "every 3s" in active.text
    assert "Which path?" in active.text

    db.update_run(run_id, state="done")
    finished = client.get(f"/runs/{run_id}/interaction")
    assert "every 3s" not in finished.text


def test_code_review_live_partial_reflects_state(tmp_path):
    config, db, client = _client(tmp_path)
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")
    db.update_run(run_id, state="running_claude", progress=88)
    db.upsert_code_review_note(
        run_id,
        {"external_id": "1", "kind": "review", "author": "rev", "body": "please fix UNIQUEBODY"},
    )
    live = client.get(f"/runs/{run_id}/code-review/live")
    assert live.status_code == 200
    assert 'id="cr-live"' in live.text
    assert "every 2s" in live.text  # active run polls fast
    assert "UNIQUEBODY" in live.text

    db.update_run(run_id, state="done")
    idle = client.get(f"/runs/{run_id}/code-review/live")
    assert "every 6s" in idle.text  # idle run polls slowly
