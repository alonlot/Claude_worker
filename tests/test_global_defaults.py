from fastapi.testclient import TestClient

from app import web
from app.config import (
    Config,
    apply_user_sections,
    global_defaults,
    load_config,
    update_global_defaults,
)
from app.db import Database
from app.web import create_app


def _admin_client(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    config.auth.default_user = ""
    config.auth.admin_users = ["root"]
    db = Database(config.app.database_path)
    db.init()
    return config, db, TestClient(create_app(config, db))


# ---- inheritance logic -------------------------------------------------------

def test_blank_user_value_inherits_global():
    base = Config()
    base.jira.url = "https://global.atlassian.net"
    base.git.gitlab_host = "https://gitlab.acme.example.com"
    sections = {"jira": {"url": "", "email": "me@acme.com"}, "git": {"gitlab_host": ""}}
    merged = apply_user_sections(base, sections)
    assert merged.jira.url == "https://global.atlassian.net"   # inherited
    assert merged.jira.email == "me@acme.com"                  # per-user kept
    assert merged.git.gitlab_host == "https://gitlab.acme.example.com"  # inherited


def test_user_value_overrides_global():
    base = Config()
    base.jira.url = "https://global.atlassian.net"
    merged = apply_user_sections(base, {"jira": {"url": "https://mine.atlassian.net"}})
    assert merged.jira.url == "https://mine.atlassian.net"


def test_non_inheritable_blank_still_blanks():
    # delivery.scp_host is per-user, not inheritable: a blank value stays blank.
    base = Config()
    base.delivery.scp_host = "server.local"
    merged = apply_user_sections(base, {"delivery": {"scp_host": ""}})
    assert merged.delivery.scp_host == ""


def test_global_defaults_shape():
    cfg = Config()
    cfg.jira.url = "https://g.atlassian.net"
    cfg.claude.model = "claude-opus-4-8"
    defaults = global_defaults(cfg)
    assert defaults["jira"]["url"] == "https://g.atlassian.net"
    assert defaults["claude"]["model"] == "claude-opus-4-8"
    assert "gitlab_host" in defaults["git"]


def test_update_global_defaults_writes_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("jira:\n  email: keep@me.com\n", encoding="utf-8")
    update_global_defaults(
        {
            "jira": {"url": "https://g.atlassian.net"},
            "git": {"gitlab_host": "https://gl.example.com"},
            "claude": {"model": "claude-opus-4-8"},
        },
        path,
    )
    reloaded = load_config(path)
    assert reloaded.jira.url == "https://g.atlassian.net"
    assert reloaded.git.gitlab_host == "https://gl.example.com"
    assert reloaded.claude.model == "claude-opus-4-8"
    # Unrelated existing keys are preserved.
    assert reloaded.jira.email == "keep@me.com"


# ---- admin route -------------------------------------------------------------

def test_admin_can_save_defaults(tmp_path, monkeypatch):
    config, db, client = _admin_client(tmp_path)
    captured = {}

    def fake_update(values, *args, **kwargs):
        captured.update(values)
        return config  # avoid touching the real config.yaml

    monkeypatch.setattr(web, "update_global_defaults", fake_update)
    resp = client.post(
        "/admin/defaults",
        data={"jira_url": "  https://g.atlassian.net  ", "git_gitlab_host": "https://gl.example.com"},
        headers={"X-Forwarded-User": "root"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin"
    assert captured["jira"]["url"] == "https://g.atlassian.net"  # trimmed
    assert captured["git"]["gitlab_host"] == "https://gl.example.com"
    # Repo and base branch are per-user, so the global-defaults form never sends them.
    assert "default_repo_url" not in captured["git"]
    assert "default_base_branch" not in captured["git"]


def test_non_admin_cannot_save_defaults(tmp_path, monkeypatch):
    config, db, client = _admin_client(tmp_path)
    called = {"n": 0}
    monkeypatch.setattr(web, "update_global_defaults", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or config)
    resp = client.post(
        "/admin/defaults",
        data={"jira_url": "https://evil"},
        headers={"X-Forwarded-User": "joe"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert called["n"] == 0  # the save never ran


def test_settings_shows_company_default_placeholder(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    config.jira.url = "https://global.example.net"
    db = Database(config.app.database_path)
    db.init()
    client = TestClient(create_app(config, db))
    page = client.get("/settings")
    assert "Company default: https://global.example.net" in page.text
