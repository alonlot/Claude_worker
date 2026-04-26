from app.db import Database


def test_queue_reorder(tmp_path):
    db = Database(tmp_path / "worker.sqlite3")
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    db.upsert_ticket({"key": "A-2", "summary": "Two", "status": "To Do", "eligibility": "eligible"})
    db.enqueue("A-1")
    db.enqueue("A-2")
    rows = db.fetchall("SELECT id FROM queue_items ORDER BY priority")
    db.reorder_queue([rows[1]["id"], rows[0]["id"]])
    keys = [row["ticket_key"] for row in db.fetchall("SELECT ticket_key FROM queue_items ORDER BY priority")]
    assert keys == ["A-2", "A-1"]


def test_agent_questions_inputs_and_subagents(tmp_path):
    db = Database(tmp_path / "worker.sqlite3")
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")

    question_id = db.create_agent_question(run_id, "Which path?", ["API", "UI", "Both"])
    db.answer_agent_question(question_id, "Both", "option")
    input_id = db.add_agent_input(run_id, "Focus on the UI first.")
    db.upsert_sub_agent(run_id, "ui-agent", "Update forms", "running", 55, "Editing templates")
    db.upsert_sub_agent(run_id, "ui-agent", "Update forms", "done", 100, "Templates updated")

    question = db.fetchone("SELECT * FROM agent_questions WHERE id=?", (question_id,))
    user_input = db.fetchone("SELECT * FROM agent_inputs WHERE id=?", (input_id,))
    sub_agents = db.fetchall("SELECT * FROM sub_agents WHERE run_id=?", (run_id,))
    assert question["state"] == "answered"
    assert question["answer"] == "Both"
    assert user_input["message"] == "Focus on the UI first."
    assert len(sub_agents) == 1
    assert sub_agents[0]["status"] == "done"
    assert sub_agents[0]["progress"] == 100
