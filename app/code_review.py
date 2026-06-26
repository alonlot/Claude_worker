from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen


class CodeReviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReviewSource:
    provider: str
    repo: str
    number: int


@dataclass(frozen=True)
class GitlabAuth:
    """Connection details for a GitLab instance (public or self-hosted).

    host  - base URL of the instance, e.g. "https://gitlab.mycompany.com".
            Empty means the public "https://gitlab.com" (or the GITLAB_HOST env).
    token - API token; empty falls back to the GITLAB_TOKEN environment variable.
    """

    host: str = ""
    token: str = ""

    def base(self) -> str:
        host = (self.host or os.environ.get("GITLAB_HOST") or "https://gitlab.com").strip().rstrip("/")
        if not host.startswith(("http://", "https://")):
            host = "https://" + host
        return host

    def domain(self) -> str:
        return _host_domain(self.base())

    def api(self) -> str:
        return self.base() + "/api/v4"

    def auth_token(self) -> str:
        return (self.token or os.environ.get("GITLAB_TOKEN") or "").strip()


def gitlab_auth_for(config: Any) -> GitlabAuth:
    """Build GitlabAuth from a Config (uses git.gitlab_host / git.gitlab_token,
    falling back to the general git.token)."""
    git = getattr(config, "git", None)
    if git is None:
        return GitlabAuth()
    host = getattr(git, "gitlab_host", "") or ""
    token = getattr(git, "gitlab_token", "") or getattr(git, "token", "") or ""
    return GitlabAuth(host=host, token=token)


def _host_domain(url: str) -> str:
    """Return the bare host of a repo/clone/web URL (handles scp-like syntax)."""
    clean = (url or "").strip()
    if not clean:
        return ""
    if "://" in clean:
        return urlsplit(clean).netloc.split("@")[-1].split(":")[0]
    match = re.match(r"[^@]+@([^:/]+)", clean)  # git@host:group/project.git
    return match.group(1) if match else ""


def _is_github_repo(repo_url: str) -> bool:
    return _host_domain(repo_url) == "github.com" or "github.com" in (repo_url or "")


def _is_gitlab_repo(repo_url: str, auth: GitlabAuth | None = None) -> bool:
    domain = _host_domain(repo_url)
    if not domain or domain == "github.com":
        return False
    auth = auth or GitlabAuth()
    return domain == "gitlab.com" or domain == auth.domain() or "gitlab" in domain


def suggest_review_url(repo_url: str, branch_name: str, auth: GitlabAuth | None = None) -> str:
    auth = auth or GitlabAuth()
    repo = _repo_slug(repo_url, auth)
    if not repo or not branch_name:
        return ""
    if _is_github_repo(repo_url):
        try:
            data = json.loads(
                _gh(["pr", "list", "--repo", repo, "--head", branch_name, "--state", "open", "--json", "url", "--limit", "1"])
                or "[]"
            )
            if data:
                return str(data[0].get("url") or "")
        except Exception:
            return ""
    if _is_gitlab_repo(repo_url, auth):
        try:
            data = _gitlab_json(
                f"/projects/{quote(repo, safe='')}/merge_requests?source_branch={quote(branch_name, safe='')}&state=opened",
                auth=auth,
            )
            if data:
                web_url = data[0].get("web_url")
                return str(web_url or "")
        except Exception:
            return ""
    return ""


def create_merge_request(
    repo_url: str, branch: str, base: str, title: str, body: str = "", auth: GitlabAuth | None = None
) -> str:
    """Open a GitHub pull request or GitLab merge request. Returns its URL."""
    auth = auth or GitlabAuth()
    slug = _repo_slug(repo_url, auth)
    if not slug or not branch:
        raise CodeReviewError("Cannot create a merge request without repo and branch")
    if _is_github_repo(repo_url):
        out = _gh(
            [
                "pr",
                "create",
                "--repo",
                slug,
                "--head",
                branch,
                "--base",
                base or "main",
                "--title",
                title,
                "--body",
                body or title,
            ]
        )
        return out.strip().splitlines()[-1] if out.strip() else ""
    if _is_gitlab_repo(repo_url, auth):
        data = _gitlab_json(
            f"/projects/{quote(slug, safe='')}/merge_requests",
            method="POST",
            form={
                "source_branch": branch,
                "target_branch": base or "main",
                "title": title,
                "description": body or title,
            },
            auth=auth,
        )
        return str(data.get("web_url") or "")
    raise CodeReviewError("Only GitHub and GitLab remotes support merge request creation")


def parse_review_url(url: str, auth: GitlabAuth | None = None) -> ReviewSource:
    # GitLab is detected by its distinctive "/-/merge_requests/N" path, which is
    # the same on gitlab.com and on any self-hosted instance regardless of host.
    gitlab = re.search(r"https?://[^/\s]+/(.+?)/-/merge_requests/(\d+)", url)
    if gitlab:
        return ReviewSource("gitlab", gitlab.group(1), int(gitlab.group(2)))
    github = re.search(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)", url)
    if github:
        return ReviewSource("github", github.group(1), int(github.group(2)))
    raise CodeReviewError("Only GitHub pull request and GitLab merge request URLs are recognized")


def list_open_merge_requests(auth: GitlabAuth | None = None) -> list[dict[str, Any]]:
    """List the current token user's open GitLab merge requests (authored + assigned)."""
    auth = auth or GitlabAuth()
    seen: dict[str, dict[str, Any]] = {}
    for scope in ("created_by_me", "assigned_to_me"):
        try:
            data = _gitlab_json(f"/merge_requests?scope={scope}&state=opened&per_page=50", auth=auth)
        except CodeReviewError:
            raise
        except Exception:
            data = []
        for mr in data or []:
            web_url = str(mr.get("web_url") or "")
            if not web_url or web_url in seen:
                continue
            seen[web_url] = {
                "provider": "gitlab",
                "title": str(mr.get("title") or ""),
                "web_url": web_url,
                "source_branch": str(mr.get("source_branch") or ""),
                "target_branch": str(mr.get("target_branch") or ""),
                "project_id": mr.get("project_id"),
                "iid": mr.get("iid"),
                "state": str(mr.get("state") or ""),
                "reference": str((mr.get("references") or {}).get("full") or ""),
                "author": str((mr.get("author") or {}).get("username") or ""),
                "updated_at": str(mr.get("updated_at") or ""),
            }
    return list(seen.values())


def scan_review_notes(url: str, auth: GitlabAuth | None = None) -> list[dict[str, Any]]:
    source = parse_review_url(url, auth)
    if source.provider == "github":
        return _scan_github(source, url)
    if source.provider == "gitlab":
        return _scan_gitlab(source, url, auth or GitlabAuth())
    raise CodeReviewError("Unsupported review provider")


def scan_ci_jobs(url: str, auth: GitlabAuth | None = None) -> list[dict[str, Any]]:
    source = parse_review_url(url, auth)
    if source.provider == "github":
        return _scan_github_ci(source)
    if source.provider == "gitlab":
        return _scan_gitlab_ci(source, auth or GitlabAuth())
    raise CodeReviewError("Unsupported review provider")


def post_review_reply(
    source_url: str, external_id: str, kind: str, body: str, auth: GitlabAuth | None = None
) -> str:
    source = parse_review_url(source_url, auth)
    if source.provider == "gitlab":
        return _post_gitlab_reply(source, external_id, body, auth or GitlabAuth())
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
                "diff_hunk": str(item.get("diff_hunk") or ""),
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


def _scan_gitlab(source: ReviewSource, source_url: str, auth: GitlabAuth) -> list[dict[str, Any]]:
    data = _gitlab_json(f"/projects/{quote(source.repo, safe='')}/merge_requests/{source.number}/discussions", auth=auth)
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


def _scan_github_ci(source: ReviewSource) -> list[dict[str, Any]]:
    payload = _gh(["pr", "view", "--repo", source.repo, str(source.number), "--json", "statusCheckRollup"])
    data = json.loads(payload or "{}")
    jobs: list[dict[str, Any]] = []
    for item in data.get("statusCheckRollup", []) or []:
        name = str(item.get("name") or item.get("context") or "check")
        status = str(item.get("status") or item.get("state") or "")
        conclusion = str(item.get("conclusion") or "")
        details_url = str(item.get("detailsUrl") or item.get("targetUrl") or "")
        summary = str(item.get("message") or "")
        jobs.append(
            {
                "provider": "github",
                "name": name,
                "status": status,
                "conclusion": conclusion,
                "details_url": details_url,
                "summary": summary.strip(),
                "text": "",
            }
        )
    return jobs


def _scan_gitlab_ci(source: ReviewSource, auth: GitlabAuth) -> list[dict[str, Any]]:
    data = _gitlab_json(
        f"/projects/{quote(source.repo, safe='')}/merge_requests/{source.number}/pipelines", auth=auth
    )
    if not data:
        return []
    pipeline = data[0]
    pipeline_id = pipeline.get("id")
    if not pipeline_id:
        return []
    jobs_data = _gitlab_json(f"/projects/{quote(source.repo, safe='')}/pipelines/{pipeline_id}/jobs", auth=auth)
    jobs: list[dict[str, Any]] = []
    for item in jobs_data or []:
        jobs.append(
            {
                "provider": "gitlab",
                "name": str(item.get("name") or "job"),
                "status": str(item.get("status") or ""),
                "conclusion": str(item.get("status") or ""),
                "details_url": str(item.get("web_url") or ""),
                "summary": str(item.get("failure_reason") or ""),
                "text": "",
            }
        )
    return jobs


def _post_gitlab_reply(source: ReviewSource, external_id: str, body: str, auth: GitlabAuth) -> str:
    discussion_id = external_id.split(":", 1)[0]
    if not discussion_id:
        raise CodeReviewError("GitLab discussion id missing")
    data = _gitlab_json(
        f"/projects/{quote(source.repo, safe='')}/merge_requests/{source.number}/discussions/{discussion_id}/notes",
        method="POST",
        form={"body": body},
        auth=auth,
    )
    return str(data.get("url") or "")


def _gh(args: list[str]) -> str:
    proc = subprocess.run(["gh", *args], text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "gh command failed").strip()
        if "Not Found" in message:
            raise CodeReviewError("GitHub resource not found. Check the PR URL and repository access.")
        raise CodeReviewError(message)
    return proc.stdout.strip()


def _repo_slug(repo_url: str, auth: GitlabAuth | None = None) -> str:
    clean = (repo_url or "").strip()
    github = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?$", clean)
    if github:
        return github.group(1).removesuffix(".git")
    if _is_gitlab_repo(clean, auth):
        if "://" in clean:
            path = urlsplit(clean).path
        elif ":" in clean:
            path = clean.split(":", 1)[1]  # git@host:group/project.git
        else:
            path = ""
        return path.strip("/").removesuffix(".git")
    gitlab = re.search(r"gitlab\.com[:/]([^?\s]+?)(?:\.git)?$", clean)
    if gitlab:
        return gitlab.group(1).removesuffix(".git")
    return ""


def _gitlab_json(
    path: str, method: str = "GET", form: dict[str, str] | None = None, auth: GitlabAuth | None = None
) -> Any:
    auth = auth or GitlabAuth()
    token = auth.auth_token()
    if not token:
        raise CodeReviewError("A GitLab token is required to scan or comment on GitLab merge requests")
    data = None
    headers = {"PRIVATE-TOKEN": token}
    if form is not None:
        data = urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = Request(auth.api() + path, data=data, headers=headers, method=method)
    with urlopen(request, timeout=30) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload or "null")
