from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen


class CodeReviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReviewSource:
    provider: str
    repo: str
    number: int


def parse_review_url(url: str) -> ReviewSource:
    github = re.search(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)", url)
    if github:
        return ReviewSource("github", github.group(1), int(github.group(2)))
    gitlab = re.search(r"gitlab\.com/([^?\s]+?)/-/merge_requests/(\d+)", url)
    if gitlab:
        return ReviewSource("gitlab", gitlab.group(1), int(gitlab.group(2)))
    raise CodeReviewError("Only GitHub pull request and GitLab merge request URLs are recognized")


def scan_review_notes(url: str) -> list[dict[str, Any]]:
    source = parse_review_url(url)
    if source.provider == "github":
        return _scan_github(source, url)
    if source.provider == "gitlab":
        return _scan_gitlab(source, url)
    raise CodeReviewError("Unsupported review provider")


def post_review_reply(source_url: str, external_id: str, kind: str, body: str) -> str:
    source = parse_review_url(source_url)
    if source.provider == "gitlab":
        return _post_gitlab_reply(source, external_id, body)
    if source.provider != "github":
        raise CodeReviewError("Unsupported review provider")
    if kind == "review":
        endpoint = f"repos/{source.repo}/pulls/{source.number}/comments/{external_id}/replies"
    else:
        endpoint = f"repos/{source.repo}/issues/{source.number}/comments"
    payload = _gh(["api", endpoint, "-f", f"body={body}"])
    try:
        data = json.loads(payload or "{}")
    except json.JSONDecodeError:
        data = {}
    return str(data.get("html_url") or "")


def _scan_github(source: ReviewSource, source_url: str) -> list[dict[str, Any]]:
    review_comments = json.loads(_gh(["api", f"repos/{source.repo}/pulls/{source.number}/comments", "--paginate"]) or "[]")
    issue_comments = json.loads(_gh(["api", f"repos/{source.repo}/issues/{source.number}/comments", "--paginate"]) or "[]")
    notes: list[dict[str, Any]] = []
    for item in review_comments:
        body = str(item.get("body") or "").strip()
        if not body:
            continue
        notes.append(
            {
                "provider": "github",
                "source_url": source_url,
                "external_id": str(item.get("id")),
                "kind": "review",
                "author": (item.get("user") or {}).get("login", ""),
                "file_path": item.get("path") or "",
                "line": item.get("line") or item.get("original_line") or 0,
                "body": body,
                "html_url": item.get("html_url") or "",
            }
        )
    for item in issue_comments:
        body = str(item.get("body") or "").strip()
        if not body:
            continue
        notes.append(
            {
                "provider": "github",
                "source_url": source_url,
                "external_id": str(item.get("id")),
                "kind": "conversation",
                "author": (item.get("user") or {}).get("login", ""),
                "file_path": "",
                "line": 0,
                "body": body,
                "html_url": item.get("html_url") or "",
            }
        )
    return notes


def _scan_gitlab(source: ReviewSource, source_url: str) -> list[dict[str, Any]]:
    data = _gitlab_json(f"/projects/{quote(source.repo, safe='')}/merge_requests/{source.number}/discussions")
    notes: list[dict[str, Any]] = []
    for discussion in data:
        discussion_id = str(discussion.get("id") or "")
        for item in discussion.get("notes", []) or []:
            body = str(item.get("body") or "").strip()
            if not body or item.get("system"):
                continue
            position = item.get("position") or {}
            notes.append(
                {
                    "provider": "gitlab",
                    "source_url": source_url,
                    "external_id": f"{discussion_id}:{item.get('id')}",
                    "kind": "review",
                    "author": (item.get("author") or {}).get("username", ""),
                    "file_path": position.get("new_path") or position.get("old_path") or "",
                    "line": position.get("new_line") or position.get("old_line") or 0,
                    "body": body,
                    "html_url": item.get("url") or "",
                }
            )
    return notes


def _post_gitlab_reply(source: ReviewSource, external_id: str, body: str) -> str:
    discussion_id = external_id.split(":", 1)[0]
    if not discussion_id:
        raise CodeReviewError("GitLab discussion id missing")
    data = _gitlab_json(
        f"/projects/{quote(source.repo, safe='')}/merge_requests/{source.number}/discussions/{discussion_id}/notes",
        method="POST",
        form={"body": body},
    )
    return str(data.get("url") or "")


def _gh(args: list[str]) -> str:
    proc = subprocess.run(["gh", *args], text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise CodeReviewError((proc.stderr or proc.stdout or "gh command failed").strip())
    return proc.stdout.strip()


def _gitlab_json(path: str, method: str = "GET", form: dict[str, str] | None = None) -> Any:
    token = os.environ.get("GITLAB_TOKEN", "").strip()
    if not token:
        raise CodeReviewError("GITLAB_TOKEN is required to scan or comment on GitLab merge requests")
    data = None
    headers = {"PRIVATE-TOKEN": token}
    if form is not None:
        from urllib.parse import urlencode

        data = urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = Request("https://gitlab.com/api/v4" + path, data=data, headers=headers, method=method)
    with urlopen(request, timeout=30) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload or "null")
