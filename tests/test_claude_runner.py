from app.claude_runner import discovery_prompt, parse_discovery


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
