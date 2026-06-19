import asyncio
from pathlib import Path

from app.config import Config, _config_from_data
from app.db import Database
from app.runner import Worker


def _worker(tmp_path, **gate):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    for key, value in gate.items():
        setattr(config.test_gate, key, value)
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")
    return Worker(config, db, "local"), db, run_id


def test_test_gate_config_parses():
    cfg = _config_from_data({"test_gate": {"enabled": "true", "command": "pytest -q", "timeout_seconds": 600}})
    assert cfg.test_gate.enabled is True
    assert cfg.test_gate.command == "pytest -q"
    assert cfg.test_gate.timeout_seconds == 600


def test_test_gate_passes(tmp_path):
    worker, db, run_id = _worker(tmp_path, enabled=True, command='python -c "import sys; sys.exit(0)"')
    ok, _ = asyncio.run(worker._run_test_gate(run_id, Path(tmp_path)))
    assert ok is True


def test_test_gate_fails_and_captures_output(tmp_path):
    worker, db, run_id = _worker(
        tmp_path,
        enabled=True,
        command='python -c "import sys; sys.stderr.write(\'BOOM_FAIL\'); sys.exit(1)"',
    )
    ok, output = asyncio.run(worker._run_test_gate(run_id, Path(tmp_path)))
    assert ok is False
    assert "BOOM_FAIL" in output
    # The failure is logged to the run.
    logs = db.fetchall("SELECT line FROM logs WHERE run_id=? AND phase='test'", (run_id,))
    assert any("failed" in row["line"].lower() for row in logs)


def test_test_gate_empty_command_is_noop(tmp_path):
    worker, db, run_id = _worker(tmp_path, enabled=True, command="")
    ok, output = asyncio.run(worker._run_test_gate(run_id, Path(tmp_path)))
    assert ok is True
    assert output == ""
