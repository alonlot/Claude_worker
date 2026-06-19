import asyncio

import pytest

from app.claude_runner import ClaudeRunner, discovery_prompt, parse_discovery, parse_plan, planning_prompt, review_prompt
from app.config import Config, DockerConfig


def test_parse_discovery_json_from_output():
    parsed = parse_discovery('hello\n{"repo_url":"https://example/repo.git","base_branch":"main","summary":"fix auth"}')
    assert parsed["repo_url"] == "https://example/repo.git"
    assert parsed["base_branch"] == "main"
    assert parsed["summary"] == "fix auth"


def test_discovery_prompt_includes_default_git_context():
    prompt = discovery_prompt({"key": "A-1", "summary": "Fix it"}, "git@github.com:acme/app.git", "develop")
    assert "git@github.com:acme/app.git" in prompt
    assert "develop" in prompt
    assert "Return only JSON" in prompt


def test_review_prompt_requests_result_marker():
    prompt = review_prompt({"key": "A-1"})
    assert "REVIEW_RESULT: pass" in prompt
    assert "REVIEW_RESULT: needs_fix" in prompt


def test_parse_plan_and_prompt_revision_context():
    parsed = parse_plan(
        '{"repo_url":"git@example/repo.git","base_branch":"main","summary":"fix auth","mission":"Fix auth","plan_text":"Edit login"}'
    )
    assert parsed["mission"] == "Fix auth"
    assert parsed["plan_text"] == "Edit login"
    prompt = planning_prompt({"key": "A-1", "summary": "Fix it"}, "git@example/repo.git", "main", "old", "new")
    assert "User requested changes" in prompt
    assert "new" in prompt


def test_docker_mode_requires_api_key():
    config = Config()
    config.docker = DockerConfig(enabled=True)
    config.claude.api_key = ""
    runner = ClaudeRunner(config, lambda phase, line: None)
    with pytest.raises(RuntimeError, match="claude.api_key is not set"):
        asyncio.run(runner.run_prompt("claude", "do the thing"))


def test_docker_mode_with_api_key_passes_guard(monkeypatch):
    # With a key set, the guard is skipped; we stop the run right after by
    # making the backend build raise a sentinel so no real process spawns.
    config = Config()
    config.docker = DockerConfig(enabled=True)
    config.claude.api_key = "sk-test"
    runner = ClaudeRunner(config, lambda phase, line: None)

    def boom(*a, **k):
        raise RuntimeError("reached-backend")

    monkeypatch.setattr(runner.backend, "build", boom)
    with pytest.raises(RuntimeError, match="reached-backend"):
        asyncio.run(runner.run_prompt("claude", "do the thing"))
