import io
import zipfile

from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.web import create_app


def _run_with_workspace(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "src").mkdir(parents=True)
    (workspace / "README.md").write_text("hello\n", encoding="utf-8")
    (workspace / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    # Should be excluded from the zip.
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("secret\n", encoding="utf-8")

    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")
    db.update_run(run_id, state="done", workspace_path=str(workspace))
    return config, db, run_id


def test_workspace_zip_downloads_and_excludes_git(tmp_path):
    config, db, run_id = _run_with_workspace(tmp_path)
    client = TestClient(create_app(config, db))
    resp = client.get(f"/runs/{run_id}/workspace.zip")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert f"A-1-{run_id}.zip" in resp.headers["content-disposition"]

    names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
    assert "README.md" in names
    assert "src/app.py" in names
    assert not any(name.startswith(".git/") for name in names)


def test_workspace_zip_missing_workspace_404(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")  # no workspace_path
    client = TestClient(create_app(config, db))
    assert client.get(f"/runs/{run_id}/workspace.zip").status_code == 404
