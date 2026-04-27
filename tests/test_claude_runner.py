from app.claude_runner import discovery_prompt, parse_discovery, parse_plan, planning_prompt, review_prompt


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
