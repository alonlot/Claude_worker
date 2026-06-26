import pytest
from fastapi.testclient import TestClient

from app import confluence
from app.confluence import ConfluenceClient, ConfluenceError, ConfluencePage, page_id_from_url, storage_to_text
from app.config import Config, ConfluenceConfig, JiraConfig
from app.db import Database
from app.web import create_app


def test_page_id_from_modern_and_legacy_urls():
    assert page_id_from_url("https://x.atlassian.net/wiki/spaces/ENG/pages/123456/Title") == "123456"
    assert page_id_from_url("https://x.atlassian.net/wiki/pages/viewpage.action?pageId=987") == "987"
    assert page_id_from_url("555") == "555"


def test_page_id_from_url_rejects_garbage():
    with pytest.raises(ConfluenceError):
        page_id_from_url("https://example.com/not-a-page")


def test_storage_to_text_strips_html():
    html = "<h1>Title</h1><p>First para</p><ul><li>one</li><li>two</li></ul><p>&amp; done</p>"
    text = storage_to_text(html)
    assert "Title" in text
    assert "- one" in text and "- two" in text
    assert "& done" in text
    assert "<" not in text


def test_resolve_confluence_falls_back_to_jira():
    base, email, token = confluence.resolve_confluence(
        ConfluenceConfig(), JiraConfig(url="https://co.atlassian.net", email="me@co", token="tok")
    )
    assert base == "https://co.atlassian.net/wiki"
    assert email == "me@co" and token == "tok"


def test_resolve_confluence_requires_credentials():
    with pytest.raises(ConfluenceError):
        confluence.resolve_confluence(ConfluenceConfig(), JiraConfig())


def _client(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "w.sqlite3")
    # Atlassian creds so ConfluenceClient can construct (page fetch is mocked).
    config.jira.url = "https://co.atlassian.net"
    config.jira.email = "me@co"
    config.jira.token = "tok"
    db = Database(config.app.database_path)
    db.init()
    return config, db, TestClient(create_app(config, db))


def test_confluence_import_route_creates_skill(tmp_path, monkeypatch):
    config, db, client = _client(tmp_path)

    async def fake_fetch(self, url):
        return ConfluencePage(id="1", title="Coding Conventions", text="Use 4 spaces.", url="https://wiki/page/1")

    monkeypatch.setattr(ConfluenceClient, "fetch_page", fake_fetch)
    resp = client.post(
        "/skills/from-confluence", data={"source_url": "https://x/wiki/pages/1/Conv"}, follow_redirects=False
    )
    assert resp.status_code == 303
    skills = db.list_skills("local")
    assert len(skills) == 1
    skill = skills[0]
    assert skill["name"] == "Coding Conventions"
    assert skill["content"] == "Use 4 spaces."
    assert skill["source_url"] == "https://wiki/page/1"
    assert skill["source_type"] == "confluence"
    # source link shows on the skills page
    assert "https://wiki/page/1" in client.get("/skills").text


def test_confluence_import_route_reports_failure(tmp_path, monkeypatch):
    config, db, client = _client(tmp_path)

    async def boom(self, url):
        raise ConfluenceError("page not found")

    monkeypatch.setattr(ConfluenceClient, "fetch_page", boom)
    # follow the redirect so the flash renders on the resulting /skills page
    resp = client.post("/skills/from-confluence", data={"source_url": "https://x/wiki/pages/1/Conv"})
    assert "Confluence import failed" in resp.text
    assert db.list_skills("local") == []


def test_skill_source_columns_roundtrip(tmp_path):
    config, db, _ = _client(tmp_path)
    sid = db.create_skill("local", "S", content="c", source_url="https://src", source_type="confluence")
    row = db.get_skill(sid)
    assert row["source_url"] == "https://src"
    # a plain edit (no source args) must not wipe the source
    db.update_skill(sid, "local", "S2", "d", "c2", "private", "")
    row = db.get_skill(sid)
    assert row["source_url"] == "https://src"
    assert row["name"] == "S2"
