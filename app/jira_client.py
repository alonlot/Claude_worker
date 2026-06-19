from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import JiraConfig


def _plain_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts: list[str] = []
        if value.get("text"):
            parts.append(str(value["text"]))
        for child in value.get("content", []) or []:
            text = _plain_text(child)
            if text:
                parts.append(text)
        return " ".join(parts)
    if isinstance(value, list):
        return " ".join(_plain_text(item) for item in value)
    return str(value)


class JiraClient:
    def __init__(self, config: JiraConfig):
        self.config = config

    async def search_assigned(self) -> list[dict[str, Any]]:
        if not self.config.url or not self.config.email or not self.config.token:
            raise RuntimeError("Jira url, email, and token must be configured")

        url = self.config.url.rstrip("/") + "/rest/api/3/search"
        params = {
            "jql": self.config.jql,
            "maxResults": self.config.max_results,
            "fields": "summary,status,description,labels,comment",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params=params, auth=(self.config.email, self.config.token))
            resp.raise_for_status()
            data = resp.json()

        return [self._normalize_issue(issue) for issue in data.get("issues", [])]

    def _require_auth(self) -> tuple[str, str, str]:
        if not self.config.url or not self.config.email or not self.config.token:
            raise RuntimeError("Jira url, email, and token must be configured")
        return self.config.url.rstrip("/"), self.config.email, self.config.token

    async def add_comment(self, issue_key: str, body: str) -> None:
        """Post a plain-text comment on a Jira issue (Cloud ADF body)."""
        base, email, token = self._require_auth()
        url = f"{base}/rest/api/3/issue/{issue_key}/comment"
        payload = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": line or " "}],
                    }
                    for line in body.split("\n")
                ],
            }
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, auth=(email, token))
            resp.raise_for_status()

    async def transition_issue(self, issue_key: str, status_name: str) -> bool:
        """Move an issue to the named status. Returns False if no match exists."""
        target = (status_name or "").strip().lower()
        if not target:
            return False
        base, email, token = self._require_auth()
        url = f"{base}/rest/api/3/issue/{issue_key}/transitions"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, auth=(email, token))
            resp.raise_for_status()
            transitions = resp.json().get("transitions", []) or []
            match = next(
                (
                    t
                    for t in transitions
                    if str(t.get("name", "")).lower() == target
                    or str((t.get("to") or {}).get("name", "")).lower() == target
                ),
                None,
            )
            if not match:
                return False
            post = await client.post(url, json={"transition": {"id": match["id"]}}, auth=(email, token))
            post.raise_for_status()
            return True

    def _normalize_issue(self, issue: dict[str, Any]) -> dict[str, Any]:
        fields = issue.get("fields", {})
        status = (fields.get("status") or {}).get("name", "")
        comments = fields.get("comment", {}).get("comments", [])
        comment_text = "\n".join(_plain_text(comment.get("body")) for comment in comments)
        description = "\n".join(part for part in [_plain_text(fields.get("description")), comment_text] if part)
        return {
            "key": issue.get("key", ""),
            "summary": fields.get("summary", ""),
            "status": status,
            "url": self.config.url.rstrip("/") + "/browse/" + issue.get("key", ""),
            "description": description,
            "labels": fields.get("labels", []) or [],
            "raw": json.dumps(issue),
        }


def classify_ticket(ticket: dict[str, Any], config: JiraConfig) -> dict[str, Any]:
    excluded = {status.lower() for status in config.excluded_statuses}
    status = ticket.get("status", "").lower()
    required_text = (config.required_text or "").strip().lower()
    combined_text = " ".join(
        [
            ticket.get("summary", ""),
            ticket.get("description", ""),
            " ".join(ticket.get("labels", [])),
        ]
    ).lower()

    if status in excluded:
        ticket["eligibility"] = "skipped"
        ticket["skip_reason"] = f"status is {ticket.get('status')}"
    elif required_text and required_text not in combined_text:
        ticket["eligibility"] = "skipped"
        ticket["skip_reason"] = f'missing required text "{config.required_text}"'
    else:
        ticket["eligibility"] = "eligible"
        ticket["skip_reason"] = ""
    return ticket
