from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.web import create_app, list_workspace_files


def test_vendored_assets_present():
    base = Path("app/static/vendor")
    for name in ["htmx.min.js", "highlight.min.js", "highlight-theme.css"]:
        path = base / name
        assert path.exists(), f"missing vendored asset {name}"
        assert path.stat().st_size > 1000


def test_base_template_has_no_cdn_references():
    text = Path("app/templates/base.html").read_text(encoding="utf-8")
    assert "unpkg.com" not in text
    assert "cdn.jsdelivr" not in text
    assert "cdnjs.cloudflare" not in text
    assert "/static/vendor/htmx.min.js" in text
    assert "/static/vendor/highlight.min.js" in text


def test_app_js_is_cdn_free_and_monaco_free():
    text = Path("app/static/app.js").read_text(encoding="utf-8")
    assert "cdn.jsdelivr" not in text
    assert "monaco" not in text.lower()
    assert "lineDiff" in text  # vendored diff implementation present


def test_static_vendor_is_served(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    client = TestClient(create_app(config, db))
    resp = client.get("/static/vendor/htmx.min.js")
    assert resp.status_code == 200


def _run_with_files(tmp_path, file_rels):
    ws = tmp_path / "ws"
    for rel in file_rels:
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")
    db.update_run(run_id, state="done", workspace_path=str(ws))
    return config, db, run_id, ws


def test_list_workspace_files_is_uncapped_and_nested(tmp_path):
    # 500 files across nested folders — the old 300 cap would have truncated this.
    rels = [f"src/pkg{i % 5}/mod{i}.py" for i in range(500)]
    rels.append("README.md")
    config, db, run_id, ws = _run_with_files(tmp_path, rels)
    run = db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
    listed = list_workspace_files(run)
    assert len(listed) == 501  # nothing capped
    paths = {f["path"] for f in listed}
    assert "src/pkg0/mod0.py" in paths  # nested posix path preserved
    assert "README.md" in paths
    # Each entry carries the bare name for the tree leaf label.
    nested = next(f for f in listed if f["path"] == "src/pkg0/mod0.py")
    assert nested["name"] == "mod0.py"


def test_list_workspace_files_skips_noise_dirs(tmp_path):
    config, db, run_id, ws = _run_with_files(
        tmp_path, ["app.py", "node_modules/dep/index.js", ".git/config", "__pycache__/x.pyc"]
    )
    run = db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
    paths = {f["path"] for f in list_workspace_files(run)}
    assert "app.py" in paths
    assert not any(p.startswith(("node_modules/", ".git/", "__pycache__/")) for p in paths)


def test_run_detail_renders_offline_ide(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")
    db.update_run(run_id, state="done", workspace_path=str(tmp_path))
    client = TestClient(create_app(config, db))
    page = client.get(f"/runs/{run_id}")
    assert page.status_code == 200
    assert 'data-mode="diff"' in page.text
    assert 'id="web-ide-follow"' in page.text
