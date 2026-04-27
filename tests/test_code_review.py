import pytest

from app.code_review import CodeReviewError, _repo_slug, parse_review_url


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


def test_parse_unknown_review_url_fails():
    with pytest.raises(CodeReviewError):
        parse_review_url("https://example.com/review/1")


def test_repo_slug_from_git_urls():
    assert _repo_slug("git@github.com:alonlot/Claude_worker.git") == "alonlot/Claude_worker"
    assert _repo_slug("https://gitlab.com/group/project.git") == "group/project"
