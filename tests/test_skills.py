from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.runner import Worker
from app.web import create_app


def _db(tmp_path):
    db = Database(tmp_path / "worker.sqlite3")
    db.init()
    return db


def test_skill_crud_and_likes(tmp_path):
    db = _db(tmp_path)
    sid = db.create_skill("alice", "Tests", "how we test", "RUN PYTEST", "public")
    assert db.get_skill(sid)["name"] == "Tests"
    assert len(db.list_skills("alice")) == 1
    assert len(db.list_public_skills()) == 1

    db.like_skill("bob", sid)
    db.like_skill("bob", sid)  # idempotent
    assert sid in db.liked_skill_ids("bob")
    assert len(db.liked_skills("bob")) == 1
    assert db.list_public_skills()[0]["like_count"] == 1

    db.unlike_skill("bob", sid)
    assert sid not in db.liked_skill_ids("bob")

    assert db.update_skill(sid, "alice", "Tests2", "d", "c", "private") is True
    assert db.update_skill(sid, "bob", "hack", "", "", "public") is False  # not owner
    assert db.list_public_skills() == []  # now private

    assert db.delete_skill(sid, "bob") is False
    assert db.delete_skill(sid, "alice") is True
    assert db.get_skill(sid) is None


def test_skill_categories_crud_and_assignment(tmp_path):
    db = _db(tmp_path)
    cid = db.create_category("alice", "Testing")
    assert cid > 0
    # idempotent: same name returns the same category id
    assert db.create_category("alice", "Testing") == cid
    assert {row["name"] for row in db.list_categories("alice")} == {"Testing"}
    # categories are per-owner
    assert db.list_categories("bob") == []

    # creating a skill with a brand-new category auto-registers the category
    sid = db.create_skill("alice", "Pytest", "how", "RUN", "private", "Conventions")
    assert db.get_skill(sid)["category"] == "Conventions"
    assert {row["name"] for row in db.list_categories("alice")} == {"Testing", "Conventions"}

    # deleting a category detaches it from skills but keeps the skill
    conventions = next(row for row in db.list_categories("alice") if row["name"] == "Conventions")
    assert db.delete_category(conventions["id"], "alice") is True
    assert db.get_skill(sid)["category"] == ""
    assert db.delete_category(conventions["id"], "bob") is False  # gone / not owner


def test_skill_category_web_routes(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    client = TestClient(create_app(config, db))

    client.post("/skills/categories/create", data={"name": "Security"}, follow_redirects=False)
    cat = db.fetchone("SELECT id FROM skill_categories WHERE name='Security'")
    assert cat is not None

    client.post(
        "/skills/create",
        data={"name": "Threat model", "description": "", "content": "x", "category": "Security", "visibility": "public"},
        follow_redirects=False,
    )
    page = client.get("/skills")
    assert "Security" in page.text  # category heading rendered

    client.post(f"/skills/categories/{cat['id']}/delete", follow_redirects=False)
    assert db.fetchone("SELECT id FROM skill_categories WHERE name='Security'") is None


def test_skills_by_ids(tmp_path):
    db = _db(tmp_path)
    a = db.create_skill("u", "A", "", "AA")
    b = db.create_skill("u", "B", "", "BB")
    names = {row["name"] for row in db.skills_by_ids([a, b, 9999])}
    assert names == {"A", "B"}


def test_skill_web_routes_and_marketplace(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    client = TestClient(create_app(config, db))

    client.post("/skills/create", data={"name": "Conventions", "description": "d", "content": "x", "visibility": "public"}, follow_redirects=False)
    skill_id = db.fetchone("SELECT id FROM skills")["id"]
    page = client.get("/skills")
    assert "Conventions" in page.text

    client.post(f"/skills/{skill_id}/like", follow_redirects=False)
    assert skill_id in db.liked_skill_ids("local")
    client.post(f"/skills/{skill_id}/unlike", follow_redirects=False)
    assert skill_id not in db.liked_skill_ids("local")


def test_marketplace_has_search_and_filter_data(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.create_skill("local", "How we write tests", "Pytest", "RUN", "public", "Testing")
    client = TestClient(create_app(config, db))
    page = client.get("/skills")
    assert page.status_code == 200
    # Search controls present.
    assert 'id="market-search-text"' in page.text
    assert 'id="market-search-category"' in page.text
    # Category appears as a filter option.
    assert '<option value="Testing">Testing</option>' in page.text
    # Cards carry the data used for client-side text + category filtering.
    assert 'data-category="Testing"' in page.text
    assert "data-search=" in page.text
    assert "how we write tests" in page.text  # lowercased into data-search


def test_plan_skill_attachment_and_prompt_injection(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"}, owner="local")
    db.enqueue("A-1", owner="local")
    queue_id = db.fetchone("SELECT id FROM queue_items WHERE ticket_key='A-1'")["id"]
    db.upsert_ticket_plan({"ticket_key": "A-1", "queue_item_id": queue_id, "mission": "m", "plan_text": "p"}, owner="local")
    skill_id = db.create_skill("local", "Style", "conv", "ALWAYS ADD TESTS", "private")
    db.like_skill("local", skill_id)

    client = TestClient(create_app(config, db))
    resp = client.post(f"/queue/{queue_id}/skills", data={"skill_ids": [skill_id]}, follow_redirects=False)
    assert resp.status_code == 303
    plan = db.plan_for_queue_item(queue_id)
    assert str(skill_id) in plan["skill_ids"]

    worker = Worker(config, db, "local")
    context = worker._selected_skills_context(plan)
    assert "ALWAYS ADD TESTS" in context


def test_attach_skills_ignores_unliked(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"}, owner="local")
    db.enqueue("A-1", owner="local")
    queue_id = db.fetchone("SELECT id FROM queue_items WHERE ticket_key='A-1'")["id"]
    db.upsert_ticket_plan({"ticket_key": "A-1", "queue_item_id": queue_id, "mission": "m", "plan_text": "p"}, owner="local")
    other = db.create_skill("someone-else", "X", "", "Y", "public")  # not liked by local

    client = TestClient(create_app(config, db))
    client.post(f"/queue/{queue_id}/skills", data={"skill_ids": [other]}, follow_redirects=False)
    plan = db.plan_for_queue_item(queue_id)
    assert plan["skill_ids"] == ""  # unliked skill rejected
