import json

import pytest

from app import code_review
from app.code_review import (
    CodeReviewError,
    GitlabAuth,
    _is_github_repo,
    _is_gitlab_repo,
    _repo_slug,
    gitlab_auth_for,
    list_open_merge_requests,
    parse_review_url,
    scan_ci_jobs,
)


def test_parse_github_pr_url():
    source = parse_review_url("https://github.com/alonlot/Claude_worker/pull/12")
    assert source.provider == "github"
    assert source.repo == "alonlot/Claude_worker"
    assert source.number == 12


def test_parse_gitlab_mr_url():
    source = parse_review_url("https://gitlab.com/group/project/-/merge_requests/7")
    assert source.provider == "gitlab"
    assert source.repo == "group/project"
    assert source.number == 7


def test_parse_self_hosted_gitlab_mr_url_with_subgroups():
    source = parse_review_url("https://gitlab.acme.example.com/team/sub/project/-/merge_requests/42")
    assert source.provider == "gitlab"
    assert source.repo == "team/sub/project"
    assert source.number == 42


def test_parse_unknown_review_url_fails():
    with pytest.raises(CodeReviewError):
        parse_review_url("https://example.com/review/1")


def test_repo_slug_from_git_urls():
    assert _repo_slug("git@github.com:alonlot/Claude_worker.git") == "alonlot/Claude_worker"
    assert _repo_slug("https://gitlab.com/group/project.git") == "group/project"


def test_repo_slug_self_hosted_gitlab():
    auth = GitlabAuth(host="https://gitlab.acme.example.com")
    assert _repo_slug("https://gitlab.acme.example.com/team/sub/project.git", auth) == "team/sub/project"
    assert _repo_slug("git@gitlab.acme.example.com:team/sub/project.git", auth) == "team/sub/project"


def test_is_gitlab_vs_github_detection():
    auth = GitlabAuth(host="https://gitlab.acme.example.com")
    assert _is_github_repo("git@github.com:o/r.git")
    assert not _is_gitlab_repo("git@github.com:o/r.git", auth)
    assert _is_gitlab_repo("https://gitlab.acme.example.com/team/project.git", auth)
    assert _is_gitlab_repo("https://gitlab.com/group/project.git", auth)
    assert not _is_gitlab_repo("https://example.com/x/y.git", auth)


def test_gitlab_auth_base_api_and_token(monkeypatch):
    monkeypatch.delenv("GITLAB_HOST", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    default = GitlabAuth()
    assert default.base() == "https://gitlab.com"
    assert default.api() == "https://gitlab.com/api/v4"

    hosted = GitlabAuth(host="gitlab.acme.example.com/", token="abc123")
    assert hosted.base() == "https://gitlab.acme.example.com"
    assert hosted.api() == "https://gitlab.acme.example.com/api/v4"
    assert hosted.domain() == "gitlab.acme.example.com"
    assert hosted.auth_token() == "abc123"


def test_gitlab_auth_token_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "env-token")
    assert GitlabAuth().auth_token() == "env-token"


def test_gitlab_auth_for_config_uses_git_token_fallback():
    class _Git:
        gitlab_host = "https://gitlab.acme.example.com"
        gitlab_token = ""
        token = "general-git-token"

    class _Cfg:
        git = _Git()

    auth = gitlab_auth_for(_Cfg())
    assert auth.host == "https://gitlab.acme.example.com"
    assert auth.token == "general-git-token"


def test_gitlab_json_targets_configured_host(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"[]"

    def fake_urlopen(request, timeout=30):
        captured["url"] = request.full_url
        captured["token"] = request.headers.get("Private-token")
        return _Resp()

    monkeypatch.setattr(code_review, "urlopen", fake_urlopen)
    auth = GitlabAuth(host="https://gitlab.acme.example.com", token="tok")
    code_review._gitlab_json("/merge_requests?scope=created_by_me", auth=auth)
    assert captured["url"] == "https://gitlab.acme.example.com/api/v4/merge_requests?scope=created_by_me"
    assert captured["token"] == "tok"


def test_gitlab_json_requires_token(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    with pytest.raises(CodeReviewError):
        code_review._gitlab_json("/merge_requests", auth=GitlabAuth(host="https://gitlab.acme.example.com"))


def test_list_open_merge_requests_dedupes_and_normalizes(monkeypatch):
    pages = {
        "created_by_me": [
            {
                "title": "Add feature",
                "web_url": "https://gitlab.acme.example.com/team/project/-/merge_requests/5",
                "source_branch": "feature/x",
                "target_branch": "main",
                "project_id": 99,
                "iid": 5,
                "state": "opened",
                "references": {"full": "team/project!5"},
                "author": {"username": "me"},
                "updated_at": "2026-06-26T00:00:00Z",
            }
        ],
        "assigned_to_me": [
            {  # duplicate web_url -> should be deduped
                "title": "Add feature",
                "web_url": "https://gitlab.acme.example.com/team/project/-/merge_requests/5",
                "iid": 5,
            },
            {
                "title": "Other",
                "web_url": "https://gitlab.acme.example.com/team/project/-/merge_requests/6",
                "iid": 6,
                "state": "opened",
            },
        ],
    }

    def fake_json(path, method="GET", form=None, auth=None):
        for scope, data in pages.items():
            if scope in path:
                return data
        return []

    monkeypatch.setattr(code_review, "_gitlab_json", fake_json)
    mrs = list_open_merge_requests(GitlabAuth(host="https://gitlab.acme.example.com", token="t"))
    urls = sorted(m["web_url"] for m in mrs)
    assert urls == [
        "https://gitlab.acme.example.com/team/project/-/merge_requests/5",
        "https://gitlab.acme.example.com/team/project/-/merge_requests/6",
    ]
    first = next(m for m in mrs if m["iid"] == 5)
    assert first["reference"] == "team/project!5"
    assert first["author"] == "me"


def test_scan_ci_jobs_routes_self_hosted_gitlab(monkeypatch):
    calls = []

    def fake_json(path, method="GET", form=None, auth=None):
        calls.append(path)
        if path.endswith("/pipelines"):
            return [{"id": 123}]
        if "/pipelines/123/jobs" in path:
            return [{"name": "test", "status": "failed", "web_url": "u", "failure_reason": "boom"}]
        return []

    monkeypatch.setattr(code_review, "_gitlab_json", fake_json)
    jobs = scan_ci_jobs(
        "https://gitlab.acme.example.com/team/project/-/merge_requests/8",
        GitlabAuth(host="https://gitlab.acme.example.com", token="t"),
    )
    assert jobs and jobs[0]["name"] == "test"
    assert jobs[0]["status"] == "failed"
    assert any("/merge_requests/8/pipelines" in c for c in calls)
