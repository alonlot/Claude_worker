import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import merge_requests as mrlib
from app.config import Config
from app.db import Database
from app.runner import Worker
from app.web import create_app


def _setup(tmp_path):
    config = Config()
    config.app.database_path = str(tmp_path / "w.sqlite3")
    config.app.workspace_dir = str(tmp_path / "ws")
    config.git.gitlab_host = "https://gitlab.acme.example.com"
    config.git.gitlab_token = "tok"
    db = Database(config.app.database_path)
    db.init()
    return config, db


def _seed_mr(db, **over):
    mr = {
        "url": "https://gitlab.acme.example.com/team/project/-/merge_requests/5",
        "provider": "gitlab",
        "title": "Add feature",
        "project": "team/project",
        "iid": 5,
        "source_branch": "feature/x",
        "target_branch": "main",
        "repo_url": "https://gitlab.acme.example.com/team/project.git",
        "discovery": "listed",
    }
    mr.update(over)
    return db.upsert_merge_request("local", mr)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

def test_merge_requests_page_lists_and_has_nav(tmp_path, monkeypatch):
    config, db = _setup(tmp_path)
    _seed_mr(db)
    monkeypatch.setattr(mrlib, "refresh_listed_mrs", lambda *a, **k: [])
    client = TestClient(create_app(config, db))
    resp = client.get("/merge-requests")
    assert resp.status_code == 200
    assert "Merge Requests" in resp.text
    assert "Add feature" in resp.text
    # nav link present on every page
    assert 'href="/merge-requests"' in client.get("/").text


def test_add_manual_mr_redirects_to_detail(tmp_path):
    config, db = _setup(tmp_path)
    client = TestClient(create_app(config, db))
    resp = client.post(
        "/merge-requests/add",
        data={"source_url": "https://gitlab.acme.example.com/team/p/-/merge_requests/9"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/merge-requests/")
    assert db.list_merge_requests("local")


def test_add_manual_mr_rejects_bad_url(tmp_path):
    config, db = _setup(tmp_path)
    client = TestClient(create_app(config, db))
    resp = client.post("/merge-requests/add", data={"source_url": "https://example.com/x"})
    assert "Could not add merge request" in resp.text


def test_detail_shows_ci_and_notes(tmp_path):
    config, db = _setup(tmp_path)
    mr_id = _seed_mr(db)
    db.update_merge_request(
        mr_id, ci_jobs=json.dumps([{"name": "test", "status": "failed", "conclusion": "failed"}]), ci_sig="s"
    )
    db.update_merge_request(
        mr_id, ci_job_suggestions=json.dumps({"test": {"sig": "x", "text": "Pin the dependency."}})
    )
    db.upsert_mr_note(mr_id, {"external_id": "d:1", "kind": "review", "author": "rev", "body": "rename x"})
    client = TestClient(create_app(config, db))
    page = client.get(f"/merge-requests/{mr_id}")
    assert "CI overview" in page.text
    assert "Pin the dependency." in page.text  # per-job fix shown on the failed job
    assert "rename x" in page.text
    assert "Fix CI" in page.text and "Fix CR" in page.text


def test_scan_route_stores_ci_and_notes(tmp_path, monkeypatch):
    config, db = _setup(tmp_path)
    mr_id = _seed_mr(db)
    monkeypatch.setattr(
        mrlib, "scan_ci_jobs", lambda url, auth: [{"name": "t", "status": "failed", "conclusion": "failed"}]
    )
    monkeypatch.setattr(
        mrlib,
        "scan_review_notes",
        lambda url, auth: [{"external_id": "d:1", "kind": "review", "author": "r", "body": "b", "source_url": url}],
    )
    # Don't actually launch the suggestion coroutine.
    monkeypatch.setattr(Worker, "generate_mr_suggestions", lambda self, mid: _noop())
    client = TestClient(create_app(config, db))
    resp = client.post(f"/merge-requests/{mr_id}/scan", follow_redirects=False)
    assert resp.status_code == 303
    row = db.get_merge_request(mr_id)
    assert mrlib.ci_failed(mrlib.parse_ci_jobs(row["ci_jobs"]))
    assert len(db.mr_notes(mr_id)) == 1


def test_auto_scan_returns_json(tmp_path, monkeypatch):
    config, db = _setup(tmp_path)
    mr_id = _seed_mr(db)
    monkeypatch.setattr(mrlib, "scan_ci_jobs", lambda url, auth: [])
    monkeypatch.setattr(mrlib, "scan_review_notes", lambda url, auth: [])
    monkeypatch.setattr(Worker, "generate_mr_suggestions", lambda self, mid: _noop())
    client = TestClient(create_app(config, db))
    resp = client.post(f"/merge-requests/{mr_id}/auto-scan")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_note_reply_posts(tmp_path, monkeypatch):
    config, db = _setup(tmp_path)
    mr_id = _seed_mr(db)
    note_id = db.upsert_mr_note(
        mr_id, {"external_id": "d:1", "kind": "review", "author": "r", "body": "b", "source_url": "u"}
    )
    monkeypatch.setattr("app.web.post_review_reply", lambda *a, **k: "https://reply")
    client = TestClient(create_app(config, db))
    resp = client.post(
        f"/merge-requests/{mr_id}/notes/{note_id}/reply", data={"reply": "Fixed it."}, follow_redirects=False
    )
    assert resp.status_code == 303
    note = db.fetchone("SELECT * FROM mr_notes WHERE id=?", (note_id,))
    assert note["state"] == "responded"
    assert note["response"] == "Fixed it."


def test_fix_ci_route_spawns(tmp_path, monkeypatch):
    config, db = _setup(tmp_path)
    mr_id = _seed_mr(db)
    seen = {}

    async def fake_fix(self, mid, mode, user_notes="", comment_back=True):
        seen["mode"] = mode
        return 1

    monkeypatch.setattr(Worker, "run_mr_fix", fake_fix)
    client = TestClient(create_app(config, db))
    resp = client.post(f"/merge-requests/{mr_id}/fix-ci", data={"user_notes": "x"}, follow_redirects=False)
    assert resp.status_code == 303


def test_save_mr_skills_route_is_per_mr(tmp_path):
    config, db = _setup(tmp_path)
    mr_id = _seed_mr(db)
    ci_skill = db.create_skill("local", "CI", content="x")
    cr_skill = db.create_skill("local", "CR", content="y")
    client = TestClient(create_app(config, db))
    resp = client.post(
        f"/merge-requests/{mr_id}/skills",
        data={"ci_skill_ids": [ci_skill], "cr_skill_ids": [cr_skill]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = db.get_merge_request(mr_id)
    assert mrlib.selected_skill_ids(row, "ci") == [ci_skill]
    assert mrlib.selected_skill_ids(row, "cr") == [cr_skill]
    # the selector renders on this MR's detail page, scoped to it
    assert "Skills this MR uses" in client.get(f"/merge-requests/{mr_id}").text


def test_delete_route(tmp_path):
    config, db = _setup(tmp_path)
    mr_id = _seed_mr(db)
    client = TestClient(create_app(config, db))
    client.post(f"/merge-requests/{mr_id}/delete")
    assert db.get_merge_request(mr_id) is None


async def _noop():
    return None


# --------------------------------------------------------------------------
# Worker: fresh-clone fixes + AI suggestions
# --------------------------------------------------------------------------

class _FakeGit:
    def __init__(self, repo_path):
        self.repo_path = repo_path
        self.pushed = None

    def clone_for_ticket(self, ticket_key, repo_url):
        return self.repo_path

    def checkout_existing_branch(self, repo_path, branch):
        self.checked_out = branch

    def cleanup_old_clones(self):
        pass

    def status(self, repo_path):
        return " M f.py"

    def changed_files(self, repo_path):
        return "f.py"

    def diff_stat(self, repo_path):
        return " f.py | 2 +-"

    def commit_all(self, repo_path, message):
        return "committed"

    def head_sha(self, repo_path):
        return "abc1234"

    def review_diff(self, repo_path, base):
        return "diff --git a/f.py b/f.py"

    def push_branch(self, repo_path, branch):
        self.pushed = branch
        return "pushed"


def test_run_mr_fix_ci_clones_commits_pushes(tmp_path, monkeypatch):
    config, db = _setup(tmp_path)
    repo = tmp_path / "clone"
    repo.mkdir()
    mr_id = _seed_mr(db)
    db.update_merge_request(
        mr_id, ci_jobs=json.dumps([{"name": "t", "status": "failed", "conclusion": "failed"}]), ci_sig="s"
    )

    async def fake_prompt(self, phase, prompt, cwd=None):
        return "fixed the build"

    monkeypatch.setattr("app.claude_runner.ClaudeRunner.run_prompt", fake_prompt)
    worker = Worker(config, db, "local")
    worker.git = _FakeGit(repo)
    run_id = asyncio.run(worker.run_mr_fix(mr_id, "ci"))

    run = db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
    assert run["state"] == "done"
    assert run["commit_sha"] == "abc1234"
    assert worker.git.pushed == "feature/x"
    assert db.get_merge_request(mr_id)["run_id"] == run_id


def test_run_mr_fix_cr_posts_replies(tmp_path, monkeypatch):
    config, db = _setup(tmp_path)
    repo = tmp_path / "clone"
    repo.mkdir()
    mr_id = _seed_mr(db)
    note_id = db.upsert_mr_note(
        mr_id, {"external_id": "d:1", "kind": "review", "author": "r", "body": "fix x", "source_url": "u"}
    )

    async def fake_prompt(self, phase, prompt, cwd=None):
        return json.dumps({"responses": [{"note_id": note_id, "response": "Done in latest push."}]})

    monkeypatch.setattr("app.claude_runner.ClaudeRunner.run_prompt", fake_prompt)
    monkeypatch.setattr("app.runner.post_review_reply", lambda *a, **k: "https://reply")
    worker = Worker(config, db, "local")
    worker.git = _FakeGit(repo)
    run_id = asyncio.run(worker.run_mr_fix(mr_id, "cr", comment_back=True))

    note = db.fetchone("SELECT * FROM mr_notes WHERE id=?", (note_id,))
    assert note["state"] == "responded"
    assert "Done in latest push." in note["response"]


def test_run_mr_fix_requires_source_branch(tmp_path):
    config, db = _setup(tmp_path)
    mr_id = _seed_mr(db, source_branch="")
    worker = Worker(config, db, "local")
    with pytest.raises(RuntimeError):
        asyncio.run(worker.run_mr_fix(mr_id, "ci"))


def test_generate_mr_suggestions(tmp_path, monkeypatch):
    config, db = _setup(tmp_path)
    mr_id = _seed_mr(db)
    db.update_merge_request(
        mr_id, ci_jobs=json.dumps([{"name": "unit", "status": "failed", "conclusion": "failed"}]), ci_sig="cisig"
    )
    note_id = db.upsert_mr_note(mr_id, {"external_id": "d:1", "kind": "review", "author": "r", "body": "fix x"})

    async def fake_prompt(self, phase, prompt, cwd=None):
        if phase == "mr-ci-suggest":
            return json.dumps({"suggestions": [{"job": "unit", "fix": "Pin the broken dependency."}]})
        return json.dumps({"suggestions": [{"note_id": note_id, "fix": "rename", "reply": "Renamed, thanks."}]})

    monkeypatch.setattr("app.claude_runner.ClaudeRunner.run_prompt", fake_prompt)
    worker = Worker(config, db, "local")
    asyncio.run(worker.generate_mr_suggestions(mr_id))

    row = db.get_merge_request(mr_id)
    stored = json.loads(row["ci_job_suggestions"])
    assert "Pin the broken dependency." in stored["unit"]["text"]
    note = db.fetchone("SELECT * FROM mr_notes WHERE id=?", (note_id,))
    assert note["suggestion"] == "rename"
    assert note["suggested_response"] == "Renamed, thanks."


def test_generate_mr_suggestions_uses_selected_skills(tmp_path, monkeypatch):
    config, db = _setup(tmp_path)
    mr_id = _seed_mr(db)
    db.update_merge_request(
        mr_id, ci_jobs=json.dumps([{"name": "deploy", "status": "failed", "conclusion": "failed"}]), ci_sig="s"
    )
    skill_id = db.create_skill("local", "Custom CI", content="The deploy job needs DEPLOY_TOKEN set.")
    db.update_merge_request(mr_id, ci_skill_ids=str(skill_id))
    seen = {}

    async def fake_prompt(self, phase, prompt, cwd=None):
        seen["prompt"] = prompt
        return json.dumps({"suggestions": [{"job": "deploy", "fix": "set the token"}]})

    monkeypatch.setattr("app.claude_runner.ClaudeRunner.run_prompt", fake_prompt)
    asyncio.run(Worker(config, db, "local").generate_mr_suggestions(mr_id))
    assert "DEPLOY_TOKEN" in seen["prompt"]  # the selected CI skill was injected
