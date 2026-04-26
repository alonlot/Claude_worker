from app.config import JiraConfig
from app.jira_client import classify_ticket


def test_classify_skips_excluded_status():
    ticket = {"summary": "Do it", "description": "for claude", "labels": [], "status": "Done"}
    result = classify_ticket(ticket, JiraConfig(required_text="for claude"))
    assert result["eligibility"] == "skipped"
    assert "status" in result["skip_reason"]


def test_classify_skips_missing_required_text():
    ticket = {"summary": "Do it", "description": "", "labels": [], "status": "To Do"}
    result = classify_ticket(ticket, JiraConfig(required_text="for claude"))
    assert result["eligibility"] == "skipped"
    assert "missing required text" in result["skip_reason"]


def test_classify_eligible():
    ticket = {"summary": "Do it", "description": "for claude", "labels": [], "status": "To Do"}
    result = classify_ticket(ticket, JiraConfig(required_text="for claude"))
    assert result["eligibility"] == "eligible"
