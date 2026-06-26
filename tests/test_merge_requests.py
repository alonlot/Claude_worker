import json

import pytest

from app import merge_requests as mr
from app.code_review import CodeReviewError, GitlabAuth
from app.config import Config
from app.db import Database


# --------------------------------------------------------------------------
# CI status helpers
# --------------------------------------------------------------------------

def test_ci_overall_states():
    assert mr.ci_overall([]) == "none"
    assert mr.ci_overall([{"status": "success", "conclusion": "success"}]) == "passed"
    assert mr.ci_overall([{"status": "running"}, {"status": "success"}]) == "running"
    assert mr.ci_overall([{"status": "failed"}, {"status": "success"}]) == "failed"


def test_failed_ci_jobs_and_context():
    jobs = [
        {"name": "build", "status": "success", "conclusion": "success"},
        {"name": "test", "status": "failed", "conclusion": "failed", "summary": "2 failed"},
    ]
    failed = mr.failed_ci_jobs(jobs)
    assert [j["name"] for j in failed] == ["test"]
    ctx = mr.render_ci_context(failed)
    assert "test" in ctx and "failed" in ctx


def test_signature_is_stable_and_sensitive():
    a = mr.signature("hello", 1)
    assert a == mr.signature("hello", 1)
    assert a != mr.signature("hello", 2)


def test_parse_ci_job_suggestions():
    out = mr.parse_ci_job_suggestions('{"suggestions": [{"job": "unit", "fix": "pin dep"}]}')
    assert out == {"unit": "pin dep"}


def test_ci_job_signature_changes_with_outcome():
    a = {"name": "unit", "status": "failed", "conclusion": "failed", "summary": "2 failed"}
    b = {"name": "unit", "status": "failed", "conclusion": "failed", "summary": "3 failed"}
    assert mr.ci_job_signature(a) != mr.ci_job_signature(b)
    assert mr.ci_job_signature(a) == mr.ci_job_signature(dict(a))


def test_skills_block_builds_from_selection(tmp_path):
    db = _db(tmp_path)
    sid = db.create_skill("local", "CI rules", content="The lint job uses ruff.")
    assert mr.skills_block(db, "local", "ci") == ""  # nothing selected yet
    db.set_state("mr_skills:ci", '[%d]' % sid, owner="local")
    block = mr.skills_block(db, "local", "ci")
    assert "CI rules" in block and "ruff" in block
    assert "custom jobs" in block  # CI-flavored intro
    assert mr.skills_block(db, "local", "cr") == ""  # CR selection independent


def test_parse_cr_suggestions():
    out = mr.parse_cr_suggestions(
        'noise {"suggestions": [{"note_id": 7, "fix": "rename x", "reply": "Done, renamed."}]} tail'
    )
    assert out[7]["fix"] == "rename x"
    assert out[7]["reply"] == "Done, renamed."


def test_parse_cr_suggestions_bad_json():
    assert mr.parse_cr_suggestions("no json here") == {}


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def _db(tmp_path) -> Database:
    db = Database(str(tmp_path / "w.sqlite3"))
    db.init()
    return db


def test_discover_from_runs_links_run(tmp_path):
    db = _db(tmp_path)
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do"})
    run_id = db.create_run("A-1")
    db.update_run(
        run_id,
        repo_url="https://gitlab.acme.example.com/team/project.git",
        branch_name="feature/x",
        base_branch="main",
        commit_message="Add X",
    )
    db.set_state(f"merge_request_url:{run_id}", "https://gitlab.acme.example.com/team/project/-/merge_requests/9")

    ids = mr.discover_from_runs(db, "local")
    assert len(ids) == 1
    row = db.get_merge_request(ids[0])
    assert row["run_id"] == run_id
    assert row["discovery"] == "run"
    assert row["source_branch"] == "feature/x"
    assert row["iid"] == 9
    assert row["project"] == "team/project"


def test_refresh_listed_mrs(monkeypatch, tmp_path):
    db = _db(tmp_path)
    config = Config()
    config.git.gitlab_host = "https://gitlab.acme.example.com"
    config.git.gitlab_token = "tok"

    def fake_list(auth):
        return [
            {
                "web_url": "https://gitlab.acme.example.com/team/project/-/merge_requests/3",
                "provider": "gitlab",
                "title": "Feature",
                "source_branch": "feature/y",
                "target_branch": "main",
                "iid": 3,
                "reference": "team/project!3",
            }
        ]

    monkeypatch.setattr(mr, "list_open_merge_requests", fake_list)
    ids = mr.refresh_listed_mrs(db, config, "local")
    assert len(ids) == 1
    row = db.get_merge_request(ids[0])
    assert row["discovery"] == "listed"
    assert row["repo_url"] == "https://gitlab.acme.example.com/team/project.git"
    assert row["source_branch"] == "feature/y"


def test_refresh_listed_mrs_no_token_is_silent(monkeypatch, tmp_path):
    db = _db(tmp_path)
    config = Config()  # no gitlab token

    def boom(auth):
        raise CodeReviewError("no token")

    monkeypatch.setattr(mr, "list_open_merge_requests", boom)
    assert mr.refresh_listed_mrs(db, config, "local") == []


def test_register_manual_mr(tmp_path):
    db = _db(tmp_path)
    config = Config()
    config.git.gitlab_host = "https://gitlab.acme.example.com"
    mr_id = mr.register_manual_mr(db, config, "local", "https://gitlab.acme.example.com/team/p/-/merge_requests/5")
    row = db.get_merge_request(mr_id)
    assert row["provider"] == "gitlab"
    assert row["iid"] == 5
    assert row["discovery"] == "manual"


def test_register_manual_mr_rejects_bad_url(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(CodeReviewError):
        mr.register_manual_mr(db, Config(), "local", "https://example.com/not-a-pr")


def test_scan_and_store(monkeypatch, tmp_path):
    db = _db(tmp_path)
    config = Config()
    config.git.gitlab_host = "https://gitlab.acme.example.com"
    config.git.gitlab_token = "tok"
    mr_id = mr.register_manual_mr(db, config, "local", "https://gitlab.acme.example.com/team/p/-/merge_requests/5")
    row = db.get_merge_request(mr_id)

    monkeypatch.setattr(
        mr, "scan_ci_jobs", lambda url, auth: [{"name": "test", "status": "failed", "conclusion": "failed"}]
    )
    monkeypatch.setattr(
        mr,
        "scan_review_notes",
        lambda url, auth: [
            {
                "provider": "gitlab",
                "source_url": url,
                "external_id": "d1:n1",
                "kind": "review",
                "author": "rev",
                "file_path": "a.py",
                "line": 3,
                "body": "please fix",
            }
        ],
    )
    summary = mr.scan_and_store(db, config, "local", row)
    assert summary["ci_status"] == "failed"
    assert summary["ci_count"] == 1
    assert summary["note_count"] == 1
    assert summary["ci_changed"] is True

    refreshed = db.get_merge_request(mr_id)
    assert mr.ci_failed(mr.parse_ci_jobs(refreshed["ci_jobs"]))
    notes = db.mr_notes(mr_id)
    assert len(notes) == 1 and notes[0]["author"] == "rev"


# --------------------------------------------------------------------------
# DB: preservation of AI suggestions + responses across refresh
# --------------------------------------------------------------------------

def test_mr_note_suggestion_and_response_survive_refresh(tmp_path):
    db = _db(tmp_path)
    mr_id = db.upsert_merge_request("local", {"url": "https://gitlab.com/g/p/-/merge_requests/1"})
    note = {"external_id": "d:1", "kind": "review", "author": "rev", "body": "fix this"}
    note_id = db.upsert_mr_note(mr_id, note)
    db.set_mr_note_suggestion(note_id, "do X", "I did X", "sig1")
    db.mark_mr_note_responded(note_id, "Posted reply", "https://gitlab.com/reply")

    # A later scan re-reports the same note (maybe edited body).
    db.upsert_mr_note(mr_id, {**note, "body": "fix this please"})
    rows = db.mr_notes(mr_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["body"] == "fix this please"  # body updated
    assert row["suggestion"] == "do X"  # suggestion preserved
    assert row["suggested_response"] == "I did X"
    assert row["response"] == "Posted reply"  # posted response preserved
    assert row["state"] == "responded"


def test_upsert_merge_request_preserves_cached_scan(tmp_path):
    db = _db(tmp_path)
    mr_id = db.upsert_merge_request(
        "local",
        {"url": "https://gitlab.com/g/p/-/merge_requests/1", "title": "T", "source_branch": "feat"},
    )
    db.update_merge_request(mr_id, ci_jobs=json.dumps([{"name": "x"}]), ci_sig="abc")
    # A thinner discovery pass (no branch info) must not wipe cached CI or branch.
    again = db.upsert_merge_request("local", {"url": "https://gitlab.com/g/p/-/merge_requests/1"})
    assert again == mr_id
    row = db.get_merge_request(mr_id)
    assert row["ci_sig"] == "abc"
    assert row["source_branch"] == "feat"
