from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.web import create_app


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
