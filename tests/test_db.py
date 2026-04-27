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
    db.mark_agent_input_consumed(input_id)
    consumed_input = db.fetchone("SELECT * FROM agent_inputs WHERE id=?", (input_id,))
    sub_agents = db.fetchall("SELECT * FROM sub_agents WHERE run_id=?", (run_id,))
    assert question["state"] == "answered"
    assert question["answer"] == "Both"
    assert user_input["message"] == "Focus on the UI first."
    assert consumed_input["consumed"] == 1
    assert len(sub_agents) == 1
    assert sub_agents[0]["status"] == "done"
    assert sub_agents[0]["progress"] == 100


def test_notifications_and_queue_pause_state(tmp_path):
    db = Database(tmp_path / "worker.sqlite3")
    db.init()

    assert db.queue_paused() is False
    db.set_state("queue_paused", "1")
    assert db.queue_paused() is True

    note_id = db.add_notification("Finished", "A-1", "success", 7)
    unread = db.unread_notifications()
    assert unread[0]["id"] == note_id
    db.mark_notifications_read([note_id])
    assert db.unread_notifications() == []


def test_ticket_plan_round_trip(tmp_path):
    db = Database(tmp_path / "worker.sqlite3")
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    db.enqueue("A-1")
    queue_id = db.fetchone("SELECT id FROM queue_items WHERE ticket_key='A-1'")["id"]
    plan_id = db.upsert_ticket_plan(
        {
            "ticket_key": "A-1",
            "queue_item_id": queue_id,
            "repo_url": "git@example/repo.git",
            "base_branch": "main",
            "branch_name": "A-1/by_claude_one",
            "mission": "Do one",
            "plan_text": "Change files",
        }
    )
    plan = db.plan_for_queue_item(queue_id)
    assert plan["id"] == plan_id
    assert plan["mission"] == "Do one"
