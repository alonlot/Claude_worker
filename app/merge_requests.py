"""Merge Request tab: discovery, scanning, and AI-suggestion helpers.

Pure, side-effect-light helpers live here so they can be unit-tested without a
running Claude CLI or a live GitLab/GitHub instance. The Worker (runner.py) owns
the Claude calls and Git operations; this module owns parsing, CI status logic,
signatures (for suggestion caching), and turning scan results into DB rows.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.code_review import (
    GitlabAuth,
    gitlab_auth_for,
    list_open_merge_requests,
    parse_review_url,
    scan_ci_jobs,
    scan_review_notes,
)

# CI conclusions/statuses that count as a failure worth auto-suggesting a fix for.
FAILED_CI = {"failed", "failure", "error", "canceled", "cancelled", "broken"}
# Statuses that mean "still running" — not failed, not yet successful.
PENDING_CI = {"running", "pending", "created", "queued", "waiting", "manual", "scheduled", "in_progress"}


# --------------------------------------------------------------------------
# CI helpers
# --------------------------------------------------------------------------

def parse_ci_jobs(raw: str) -> list[dict[str, str]]:
    if not (raw or "").strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    rows: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "name": str(item.get("name") or ""),
                "status": str(item.get("status") or ""),
                "conclusion": str(item.get("conclusion") or ""),
                "details_url": str(item.get("details_url") or ""),
                "summary": str(item.get("summary") or ""),
                "text": str(item.get("text") or ""),
            }
        )
    return rows


def _job_tokens(job: dict[str, str]) -> set[str]:
    return {str(job.get("status") or "").lower(), str(job.get("conclusion") or "").lower()}


def ci_failed(ci_jobs: list[dict[str, str]]) -> bool:
    return any(_job_tokens(job) & FAILED_CI for job in ci_jobs)


def ci_pending(ci_jobs: list[dict[str, str]]) -> bool:
    return any(_job_tokens(job) & PENDING_CI for job in ci_jobs)


def ci_overall(ci_jobs: list[dict[str, str]]) -> str:
    """One-word health for the MR list badge: failed | running | passed | none."""
    if not ci_jobs:
        return "none"
    if ci_failed(ci_jobs):
        return "failed"
    if ci_pending(ci_jobs):
        return "running"
    return "passed"


def failed_ci_jobs(ci_jobs: list[dict[str, str]]) -> list[dict[str, str]]:
    return [job for job in ci_jobs if _job_tokens(job) & FAILED_CI]


def render_ci_context(ci_jobs: list[dict[str, str]]) -> str:
    if not ci_jobs:
        return ""
    lines = []
    for job in ci_jobs:
        lines.append(
            f"- {job.get('name', 'job')}: status={job.get('status', '')}, "
            f"conclusion={job.get('conclusion', '')}, summary={job.get('summary', '')}"
        )
    return "\n".join(lines)


def signature(*parts: Any) -> str:
    """Stable short hash of the inputs, used to cache AI suggestions so they only
    regenerate when the underlying CI output / note text actually changes."""
    blob = "".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def gitlab_clone_url(project: str, auth: GitlabAuth) -> str:
    if not project:
        return ""
    return f"{auth.base()}/{project}.git"


def discover_from_runs(db: Any, owner: str) -> list[int]:
    """Find merge requests this tool itself opened (a merge_request_url:<run_id>
    state row was written when the MR was created) and register them in the MR
    table linked back to the originating run. Returns the affected MR ids."""
    affected: list[int] = []
    rows = db.fetchall(
        "SELECT key, value FROM app_state WHERE owner=? AND key LIKE 'merge_request_url:%'",
        (owner,),
    )
    for row in rows:
        url = (row["value"] or "").strip()
        if not url:
            continue
        try:
            run_id = int(str(row["key"]).split(":", 1)[1])
        except (ValueError, IndexError):
            continue
        run = db.fetchone("SELECT * FROM runs WHERE id=? AND owner=?", (run_id, owner))
        mr: dict[str, Any] = {
            "url": url,
            "discovery": "run",
            "run_id": run_id,
        }
        if run:
            mr["repo_url"] = run["repo_url"]
            mr["source_branch"] = run["branch_name"]
            mr["target_branch"] = run["base_branch"]
            mr["title"] = run["commit_message"] or run["ticket_key"]
        try:
            source = parse_review_url(url)
            mr["provider"] = source.provider
            mr["project"] = source.repo
            mr["iid"] = source.number
        except Exception:
            pass
        affected.append(db.upsert_merge_request(owner, mr))
    return affected


def refresh_listed_mrs(db: Any, config: Any, owner: str) -> list[int]:
    """Pull the token user's open GitLab merge requests and register them.
    Best-effort: returns [] (without raising) when no GitLab token is configured."""
    auth = gitlab_auth_for(config)
    try:
        listed = list_open_merge_requests(auth)
    except Exception:
        return []
    affected: list[int] = []
    for item in listed:
        mr = {
            "url": item.get("web_url", ""),
            "provider": item.get("provider", "gitlab"),
            "title": item.get("title", ""),
            "project": item.get("reference", "").split("!")[0] or "",
            "iid": item.get("iid") or 0,
            "source_branch": item.get("source_branch", ""),
            "target_branch": item.get("target_branch", ""),
            "repo_url": gitlab_clone_url(item.get("reference", "").split("!")[0], auth),
            "discovery": "listed",
        }
        if mr["url"]:
            affected.append(db.upsert_merge_request(owner, mr))
    return affected


def register_manual_mr(db: Any, config: Any, owner: str, url: str) -> int:
    """Register a pasted MR/PR URL. Raises CodeReviewError if the URL is not a
    recognized PR/MR. Returns the MR id."""
    url = (url or "").strip()
    source = parse_review_url(url, gitlab_auth_for(config))
    auth = gitlab_auth_for(config)
    mr = {
        "url": url,
        "provider": source.provider,
        "project": source.repo,
        "iid": source.number,
        "discovery": "manual",
        "repo_url": gitlab_clone_url(source.repo, auth) if source.provider == "gitlab" else "",
    }
    return db.upsert_merge_request(owner, mr)


# --------------------------------------------------------------------------
# Scanning (writes CI + notes into the DB)
# --------------------------------------------------------------------------

def scan_and_store(db: Any, config: Any, owner: str, mr_row: Any) -> dict[str, Any]:
    """Scan the MR's CI jobs and review notes and persist them. Returns a small
    summary dict: {ci_count, note_count, ci_status, ci_changed, notes_changed}."""
    auth = gitlab_auth_for(config)
    url = mr_row["url"]
    mr_id = int(mr_row["id"])

    ci_jobs = scan_ci_jobs(url, auth)
    ci_raw = json.dumps(ci_jobs)
    ci_sig = signature(ci_raw)
    ci_changed = ci_sig != (mr_row["ci_sig"] or "")
    db.update_merge_request(
        mr_id,
        ci_jobs=ci_raw,
        ci_sig=ci_sig,
        last_scanned_at=_now_iso(),
    )

    notes = scan_review_notes(url, auth)
    for note in notes:
        db.upsert_mr_note(mr_id, note, owner=owner)

    return {
        "ci_count": len(ci_jobs),
        "note_count": len(notes),
        "ci_status": ci_overall(ci_jobs),
        "ci_changed": ci_changed,
        "notes_changed": True,
    }


def _now_iso() -> str:
    # Imported lazily so tests that patch datetime elsewhere are unaffected.
    from datetime import datetime

    return datetime.utcnow().isoformat()


# --------------------------------------------------------------------------
# AI-suggestion prompts + parsers (the Worker runs these against Claude)
# --------------------------------------------------------------------------

def ci_suggestion_prompt(mr_title: str, ci_context: str) -> str:
    return f"""
You are advising a developer on how to fix failing CI for a merge request.
Merge request: {mr_title}

Failing CI job output:
{ci_context}

Write a short, concrete note (3-8 sentences, plain text, no code fences) that:
- Names the most likely root cause of the failure.
- Lists the specific steps or file areas to change to make CI pass.
- Flags anything that needs human judgement.
Return only the note text.
"""


def cr_suggestion_prompt(notes: list[dict[str, Any]]) -> str:
    blocks = []
    for note in notes:
        loc = f" ({note.get('file_path')}:{note.get('line')})" if note.get("file_path") else ""
        hunk = f"\nCODE:\n{note.get('diff_hunk')}" if note.get("diff_hunk") else ""
        blocks.append(
            f"NOTE_ID {note.get('id')} by {note.get('author') or 'reviewer'}{loc}:\n{note.get('body')}{hunk}"
        )
    joined = "\n\n".join(blocks)
    return f"""
You are helping a developer respond to code review notes on a merge request.
For EACH note below, propose (a) how to address it in the code and (b) a polite,
first-person reply the developer could post back to the reviewer.

{joined}

Return ONLY JSON of the form:
{{"suggestions": [{{"note_id": 123, "fix": "how to address it", "reply": "first-person reply to post"}}]}}
Include one object for every NOTE_ID above.
"""


def parse_cr_suggestions(output: str) -> dict[int, dict[str, str]]:
    match = re.search(r"\{.*\}", output or "", re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    out: dict[int, dict[str, str]] = {}
    for item in data.get("suggestions", []) or []:
        try:
            note_id = int(item.get("note_id"))
        except (TypeError, ValueError):
            continue
        out[note_id] = {
            "fix": str(item.get("fix") or "").strip(),
            "reply": str(item.get("reply") or "").strip(),
        }
    return out
