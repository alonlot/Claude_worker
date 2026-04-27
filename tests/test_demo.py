import json

from app.db import Database
from app.demo import seed_demo


def test_seed_demo_creates_run_and_review_notes(tmp_path):
    db = Database(tmp_path / "worker.sqlite3")
    db.init()
    run_id = seed_demo(db)
    run = db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
    notes = db.code_review_notes(run_id)
    assert run["ticket_key"] == "DEMO-101"
    assert run["state"] == "done"
    assert run["commit_sha"]
    assert len(notes) == 2
    source_url = db.get_state(f"review_source_url:{run_id}")
    ci_jobs_raw = db.get_state(f"ci_jobs:{run_id}")
    ci_jobs = json.loads(ci_jobs_raw)
    assert source_url.endswith("/pull/101")
    assert len(ci_jobs) >= 2
    assert any(job["name"] == "pytest" for job in ci_jobs)
