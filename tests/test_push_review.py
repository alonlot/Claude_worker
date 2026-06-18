import subprocess

import pytest
from fastapi.testclient import TestClient

from app.code_review import CodeReviewError, create_merge_request
from app.config import Config
from app.db import Database
from app.web import create_app, diff_lines


def test_diff_lines_classifies_unified_diff():
    rows = diff_lines("diff --git a b\n@@ -1 +1 @@\n-old\n+new\n ctx")
    assert [row["cls"] for row in rows] == ["diff-meta", "diff-hunk", "diff-del", "diff-add", "diff-ctx"]


def test_git_config_flags_parse():
    from app.config import _config_from_data

    cfg = _config_from_data({"git": {"auto_push": "true", "auto_merge_request": "yes"}})
    assert cfg.git.auto_push is True
    assert cfg.git.auto_merge_request is True


def test_create_merge_request_rejects_unknown_host():
    with pytest.raises(CodeReviewError):
        create_merge_request("https://example.com/x.git", "branch", "main", "title")


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_push_preview_renders_full_diff(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "a@b.c"], repo)
    _git(["config", "user.name", "tester"], repo)
    (repo / "f.txt").write_text("one\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "base"], repo)
    _git(["checkout", "-b", "work"], repo)
    (repo / "f.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "work"], repo)

    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")
    db.update_run(
        run_id,
        state="done",
        workspace_path=str(repo),
        base_branch="main",
        branch_name="work",
        commit_sha="abc123",
        commit_message="A-1: work",
    )
    client = TestClient(create_app(config, db))
    page = client.get(f"/runs/{run_id}/push-preview")
    assert page.status_code == 200
    assert "Full Diff" in page.text
    assert "two" in page.text
    assert "diff-add" in page.text
