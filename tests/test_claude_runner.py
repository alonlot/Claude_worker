from app.claude_runner import parse_discovery


def test_parse_discovery_json_from_output():
    parsed = parse_discovery('hello\n{"repo_url":"https://example/repo.git","base_branch":"main","summary":"fix auth"}')
    assert parsed["repo_url"] == "https://example/repo.git"
    assert parsed["base_branch"] == "main"
    assert parsed["summary"] == "fix auth"
