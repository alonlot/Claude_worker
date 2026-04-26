from app.db import Database
from app.web import clear_demo_running_ticket, create_demo_running_ticket


def test_demo_running_ticket_can_be_created_and_cleared(tmp_path):
    db = Database(tmp_path / "worker.sqlite3")
    db.init()

    run_id = create_demo_running_ticket(db)

    run = db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
    ticket = db.fetchone("SELECT * FROM tickets WHERE key='DEMO-101'")
    logs = db.fetchall("SELECT * FROM logs WHERE run_id=?", (run_id,))
    assert run["state"] == "running_claude"
    assert run["progress"] == 47
    assert ticket["summary"] == "Preview active ticket layout"
    assert len(logs) == 5

    clear_demo_running_ticket(db)

    assert db.fetchone("SELECT * FROM runs WHERE ticket_key='DEMO-101'") is None
    assert db.fetchone("SELECT * FROM tickets WHERE key='DEMO-101'") is None
