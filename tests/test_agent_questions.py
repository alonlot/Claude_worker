import asyncio

from app.claude_runner import parse_ask_user
from app.config import Config
from app.db import Database
from app.runner import Worker


def test_parse_ask_user_extracts_question_and_options():
    output = "working...\nASK_USER: Which database? || Postgres || MySQL || SQLite\ndone"
    questions = parse_ask_user(output)
    assert len(questions) == 1
    assert questions[0]["question"] == "Which database?"
    assert questions[0]["options"] == ["Postgres", "MySQL", "SQLite"]


def test_parse_ask_user_without_options():
    assert parse_ask_user("ASK_USER: Proceed with the risky refactor?")[0]["options"] == []
    assert parse_ask_user("nothing here") == []


def test_ask_user_blocks_until_answered(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "x", "status": "To Do"}, owner="local")
    run_id = db.create_run("A-1", owner="local")
    worker = Worker(config, db, "local")

    async def scenario():
        task = asyncio.create_task(worker._ask_user(run_id, "Which path?", ["API", "UI", "Both"]))
        # Wait for the worker to publish the pending question.
        question_id = None
        for _ in range(200):
            row = db.fetchone("SELECT id, state FROM agent_questions WHERE run_id=?", (run_id,))
            if row:
                question_id = int(row["id"])
                assert row["state"] == "pending"
                break
            await asyncio.sleep(0.01)
        assert question_id is not None
        db.answer_agent_question(question_id, "UI", "option")
        return await asyncio.wait_for(task, timeout=5)

    assert asyncio.run(scenario()) == "UI"


def test_ask_user_wait_is_cancellable(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "x", "status": "To Do"}, owner="local")
    run_id = db.create_run("A-1", owner="local")
    worker = Worker(config, db, "local")
    worker.cancel_requested = True

    async def scenario():
        await worker._ask_user(run_id, "Which path?", ["A", "B", "C"])

    try:
        asyncio.run(scenario())
        raised = False
    except asyncio.CancelledError:
        raised = True
    assert raised
