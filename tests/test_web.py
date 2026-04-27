import asyncio

from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.web import create_app, spawn_tracked_task, setup_status, split_lines


def test_split_lines_accepts_lines_and_commas():
    assert split_lines("Review\nDone,Blocked\n\n") == ["Review", "Done", "Blocked"]


def test_run_detail_accepts_questions_and_mid_run_input(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket(
        {
            "key": "A-1",
            "summary": "Build UI",
            "status": "In Progress",
            "description": "Ticket body",
            "eligibility": "eligible",
        }
    )
    run_id = db.create_run("A-1")
    db.update_run(run_id, state="running_claude", progress=42)
    question_id = db.create_agent_question(run_id, "Which scope?", ["API", "UI", "Both"])
    app = create_app(config, db)
    client = TestClient(app)

    page = client.get(f"/runs/{run_id}")
    assert page.status_code == 200
    assert "Which scope?" in page.text

    answer = client.post(
        f"/agent-questions/{question_id}/answer",
        data={"run_id": str(run_id), "selected_answer": "UI", "free_answer": ""},
        follow_redirects=False,
    )
    assert answer.status_code == 303
    assert db.fetchone("SELECT answer FROM agent_questions WHERE id=?", (question_id,))["answer"] == "UI"

    user_input = client.post(f"/runs/{run_id}/input", data={"message": "Use the compact layout."}, follow_redirects=False)
    assert user_input.status_code == 303
    assert db.fetchone("SELECT message FROM agent_inputs WHERE run_id=?", (run_id,))["message"] == "Use the compact layout."


def test_run_interactions_support_htmx_without_redirect(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket(
        {
            "key": "A-1",
            "summary": "Build UI",
            "status": "In Progress",
            "description": "Ticket body",
            "eligibility": "eligible",
        }
    )
    run_id = db.create_run("A-1")
    question_id = db.create_agent_question(run_id, "Which scope?", ["API", "UI", "Both"])
    app = create_app(config, db)
    client = TestClient(app)

    answer = client.post(
        f"/agent-questions/{question_id}/answer",
        data={"run_id": str(run_id), "selected_answer": "Both", "free_answer": ""},
        headers={"HX-Request": "true"},
    )
    assert answer.status_code == 200
    assert "id=\"run-interaction\"" in answer.text
    assert "Message During Run" in answer.text

    user_input = client.post(
        f"/runs/{run_id}/input",
        data={"message": "Keep it compact."},
        headers={"HX-Request": "true"},
    )
    assert user_input.status_code == 200
    assert "Keep it compact." in user_input.text
    assert db.fetchone("SELECT consumed FROM agent_inputs WHERE run_id=?", (run_id,))["consumed"] == 0


def test_queue_pause_and_notifications_routes(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.add_notification("Done", "A-1", "success", 1)
    app = create_app(config, db)
    client = TestClient(app)

    pause = client.post("/queue/pause", follow_redirects=False)
    assert pause.status_code == 303
    assert db.queue_paused() is True

    resume = client.post("/queue/resume", follow_redirects=False)
    assert resume.status_code == 303
    assert db.queue_paused() is False

    notes = client.get("/notifications/unread")
    assert notes.status_code == 200
    assert notes.json()[0]["title"] == "Done"
    assert db.unread_notifications() == []
    dashboard = client.get("/partials/dashboard")
    assert dashboard.status_code == 200
    assert "Queue" in dashboard.text


def test_enqueue_test_ticket_route(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    app = create_app(config, db)
    client = TestClient(app)

    response = client.post("/dry-run/enqueue", follow_redirects=False)
    assert response.status_code == 303
    ticket = db.fetchone("SELECT * FROM tickets WHERE key LIKE 'LOCAL-%'")
    assert ticket is not None
    assert ticket["eligibility"] == "eligible"
    queue_item = db.fetchone("SELECT * FROM queue_items WHERE ticket_key=?", (ticket["key"],))
    assert queue_item is not None


def test_setup_status_flags_missing_config():
    config = Config()
    config.jira.url = "https://your-domain.atlassian.net"
    config.jira.email = "you@example.com"
    config.jira.token = "paste-jira-token-here"
    rows = setup_status(config)
    by_name = {row["name"]: row for row in rows}
    assert by_name["Jira"]["ok"] is False
    assert by_name["Git"]["ok"] is False
    assert "Claude" in by_name


def test_queue_delete_route(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    db.enqueue("A-1")
    queue_id = db.fetchone("SELECT id FROM queue_items WHERE ticket_key='A-1'")["id"]
    app = create_app(config, db)
    client = TestClient(app)

    response = client.post(f"/queue/{queue_id}/delete", follow_redirects=False)
    assert response.status_code == 303
    assert db.fetchone("SELECT id FROM queue_items WHERE id=?", (queue_id,)) is None


def test_ticket_plan_page_renders(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "description": "Body", "eligibility": "eligible"})
    db.enqueue("A-1")
    queue_id = db.fetchone("SELECT id FROM queue_items WHERE ticket_key='A-1'")["id"]
    db.upsert_ticket_plan(
        {
            "ticket_key": "A-1",
            "queue_item_id": queue_id,
            "repo_url": "git@example/repo.git",
            "base_branch": "main",
            "branch_name": "A-1/by_claude_one",
            "mission": "Do one",
            "plan_text": "Change files",
        }
    )
    app = create_app(config, db)
    client = TestClient(app)

    response = client.get(f"/queue/{queue_id}/plan")
    assert response.status_code == 200
    assert "Mission Plan" in response.text
    assert "Do one" in response.text


def test_build_accepts_unplanned_ticket(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    db.enqueue("A-1")
    queue_id = db.fetchone("SELECT id FROM queue_items WHERE ticket_key='A-1'")["id"]
    app = create_app(config, db)
    client = TestClient(app)

    response = client.post(f"/queue/{queue_id}/build", follow_redirects=False)
    assert response.status_code == 303
    assert db.fetchone("SELECT state FROM queue_items WHERE id=?", (queue_id,))["state"] in {"needs_plan", "running", "failed"}


def test_code_review_demo_ticket_shows_fallback_ci_jobs(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket(
        {
            "key": "DEMO-101",
            "summary": "Demo",
            "status": "To Do",
            "description": "",
            "eligibility": "eligible",
        }
    )
    run_id = db.create_run("DEMO-101")
    db.update_run(run_id, state="done", commit_sha="abc")
    app = create_app(config, db)
    client = TestClient(app)
    page = client.get(f"/runs/{run_id}/code-review")
    assert page.status_code == 200
    assert "pytest" in page.text


def test_push_preview_and_code_review_pages_render(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")
    db.update_run(
        run_id,
        state="done",
        branch_name="A-1/by_claude_one",
        repo_url="git@example/repo.git",
        base_branch="main",
        commit_sha="abc123",
        changed_files="app.py",
        diff_summary="1 file changed",
    )
    app = create_app(config, db)
    client = TestClient(app)

    preview = client.get(f"/runs/{run_id}/push-preview")
    assert preview.status_code == 200
    assert "Push Preview" in preview.text
    run_page = client.get(f"/runs/{run_id}")
    assert run_page.status_code == 200
    assert "Status Timeline" in run_page.text
    cr = client.get(f"/runs/{run_id}/code-review")
    assert cr.status_code == 200
    assert "Code Review" in cr.text

    missing = client.post(f"/runs/{run_id}/code-review/scan", data={}, follow_redirects=False)
    assert missing.status_code == 303


def test_code_review_checkbox_preferences_persist(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")
    db.update_run(run_id, state="done", commit_sha="abc123")
    app = create_app(config, db)
    client = TestClient(app)

    set_scan_pref = client.post(
        f"/runs/{run_id}/code-review/scan",
        data={"source_url": "https://github.com/alonlot/Claude_worker/pull/101", "scan_mode": "notes"},
        follow_redirects=False,
    )
    assert set_scan_pref.status_code == 303
    assert db.get_state(f"auto_cr:{run_id}", "1") == "0"

    set_comment_back = client.post(
        f"/runs/{run_id}/code-review/fix",
        data={"user_notes": "n/a"},
        follow_redirects=False,
    )
    assert set_comment_back.status_code == 303
    assert db.get_state(f"comment_back:{run_id}", "1") == "0"

    page = client.get(f"/runs/{run_id}/code-review")
    assert page.status_code == 200
    assert 'id="review-auto-cr" type="checkbox" name="auto_cr"' in page.text
    assert 'name="comment_back"' in page.text
    auto_cr_line = next(line for line in page.text.splitlines() if 'id="review-auto-cr"' in line)
    comment_back_line = next(line for line in page.text.splitlines() if 'name="comment_back"' in line)
    assert "checked" not in auto_cr_line
    assert "checked" not in comment_back_line


def test_workspace_file_api(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hello", encoding="utf-8")
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")
    db.update_run(run_id, state="done", workspace_path=str(workspace))
    app = create_app(config, db)
    client = TestClient(app)

    files = client.get(f"/runs/{run_id}/workspace/files")
    assert files.status_code == 200
    assert files.json()[0]["path"] == "hello.txt"
    opened = client.get(f"/runs/{run_id}/workspace/file", params={"path": "hello.txt"})
    assert opened.json()["content"] == "hello"
    saved = client.post(f"/runs/{run_id}/workspace/file", data={"path": "hello.txt", "content": "updated"})
    assert saved.status_code == 200
    assert (workspace / "hello.txt").read_text(encoding="utf-8") == "updated"
    outside = client.get(f"/runs/{run_id}/workspace/file", params={"path": "../secret.txt"})
    assert outside.status_code == 400


def test_cancel_mismatch_does_not_cancel_requested_run(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket(
        {
            "key": "A-1",
            "summary": "Build UI",
            "status": "In Progress",
            "description": "Ticket body",
            "eligibility": "eligible",
        }
    )
    run_one = db.create_run("A-1")
    run_two = db.create_run("A-1")
    db.update_run(run_one, state="done")
    db.update_run(run_two, state="running_claude", progress=42)

    app = create_app(config, db)
    client = TestClient(app)

    response = client.post(f"/runs/{run_one}/cancel", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert db.fetchone("SELECT state FROM runs WHERE id=?", (run_one,))["state"] == "done"
    assert db.fetchone("SELECT state FROM runs WHERE id=?", (run_two,))["state"] == "failed"


def test_spawn_tracked_task_creates_error_notification(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()

    async def explode():
        raise RuntimeError("boom")

    async def run_and_drain():
        task = spawn_tracked_task(
            explode(),
            db,
            title="Run loop failed",
            message="Background run-once task crashed.",
        )
        try:
            await task
        except RuntimeError:
            pass
        await asyncio.sleep(0)

    asyncio.run(run_and_drain())
    note = db.fetchone("SELECT title, message, level FROM notifications ORDER BY id DESC LIMIT 1")
    assert note is not None
    assert note["title"] == "Run loop failed"
    assert note["level"] == "error"
    assert "boom" in note["message"]
