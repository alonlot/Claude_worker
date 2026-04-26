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
