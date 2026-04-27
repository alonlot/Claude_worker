from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from app.claude_runner import (
    ClaudeRunner,
    cr_fix_prompt,
    discovery_prompt,
    implementation_prompt,
    parse_plan,
    planning_prompt,
    parse_discovery,
    review_prompt,
)
from app.config import Config, secret_values
from app.db import Database
from app.git_ops import GitOps
from app.jira_client import JiraClient, classify_ticket
from app.utils import branch_name, mask_secrets


class Worker:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.git = GitOps(config)
        self.lock = asyncio.Lock()
        self.interval_task: asyncio.Task[None] | None = None
        self.claude: ClaudeRunner | None = None
        self.cancel_requested = False

    async def scan_jira(self) -> int:
        client = JiraClient(self.config.jira)
        tickets = await client.search_assigned()
        count = 0
        for ticket in tickets:
            classified = classify_ticket(ticket, self.config.jira)
            self.db.upsert_ticket(classified)
            if classified["eligibility"] == "eligible":
                self.db.enqueue(classified["key"])
            count += 1
        return count

    async def scan_and_run_once(self) -> None:
        try:
            await self.scan_jira()
        except Exception as exc:
            print(f"Jira scan failed: {exc}")
        if self.db.queue_paused():
            return
        await self.run_next()

    async def run_interval_forever(self) -> None:
        while True:
            await self.scan_and_run_once()
            await asyncio.sleep(max(5, int(self.config.app.interval_seconds)))

    def start_interval(self) -> bool:
        if self.interval_task and not self.interval_task.done():
            return False
        self.interval_task = asyncio.create_task(self.run_interval_forever())
        return True

    def stop_interval(self) -> None:
        if self.interval_task and not self.interval_task.done():
            self.interval_task.cancel()

    def cancel_current(self) -> None:
        self.cancel_requested = True
        if self.claude:
            self.claude.cancel()

    async def run_next(self) -> int | None:
        if self.db.queue_paused():
            return None
        if self.lock.locked():
            return None
        async with self.lock:
            item = self.db.next_queue_item()
            if not item:
                return None
            return await self._run_queue_item(item)

    async def run_queue_item(self, queue_id: int) -> int | None:
        if self.lock.locked():
            return None
        async with self.lock:
            item = self.db.queue_item(queue_id)
            if not item or item["state"] not in ("needs_plan", "plan_ready", "queued"):
                return None
            return await self._run_queue_item(item)

    async def _run_queue_item(self, item: Any) -> int:
        self.db.set_queue_state(int(item["id"]), "running")
        run_id = self.db.create_run(item["ticket_key"])
        try:
            await self._run_ticket(run_id, item)
            self.db.set_queue_state(int(item["id"]), "done")
        except asyncio.CancelledError:
            self.db.update_run(run_id, state="cancelled", error="cancelled", finished_at=datetime.utcnow().isoformat())
            self.db.add_notification("Run cancelled", item["ticket_key"], "warning", run_id)
            self.db.set_queue_state(int(item["id"]), "cancelled")
        except Exception as exc:
            friendly = self._friendly_error(str(exc))
            self._log(run_id, "error", friendly)
            self.db.update_run(run_id, state="failed", error=friendly, finished_at=datetime.utcnow().isoformat())
            self.db.add_notification("Run failed", f"{item['ticket_key']}: {friendly}", "error", run_id)
            self.db.set_queue_state(int(item["id"]), "failed")
        finally:
            self.cancel_requested = False
        return run_id

    async def prepare_queue_item(self, queue_id: int, user_notes: str = "") -> int:
        item = self.db.queue_item(queue_id)
        if not item:
            raise RuntimeError("Queue item not found")
        if item["state"] == "running":
            raise RuntimeError("Cannot revise a running item")
        self.db.set_queue_state(queue_id, "planning")
        self.claude = ClaudeRunner(self.config, lambda phase, line: print(f"[{phase}] {line}"))
        existing = self.db.plan_for_queue_item(queue_id)
        previous = ""
        if existing:
            previous = f"Mission:\n{existing['mission']}\n\nPlan:\n{existing['plan_text']}"
        output = await self.claude.run_prompt(
            "plan",
            planning_prompt(
                dict(item),
                self.config.git.default_repo_url,
                self.config.git.default_base_branch,
                previous,
                user_notes,
            ),
        )
        parsed = parse_plan(output)
        repo_url = parsed["repo_url"] or self.config.git.default_repo_url
        base_branch = parsed["base_branch"] or self.config.git.default_base_branch or "main"
        summary = parsed["summary"] or item["summary"] or "work"
        branch = branch_name(item["ticket_key"], summary)
        plan_id = self.db.upsert_ticket_plan(
            {
                "ticket_key": item["ticket_key"],
                "queue_item_id": queue_id,
                "state": "draft",
                "repo_url": repo_url,
                "base_branch": base_branch,
                "branch_name": branch,
                "mission": parsed["mission"] or item["summary"],
                "plan_text": parsed["plan_text"] or "Claude did not provide a detailed plan.",
                "user_notes": user_notes,
                "raw_output": output,
            }
        )
        self.db.set_queue_state(queue_id, "plan_ready")
        self.db.add_notification("Plan ready", item["ticket_key"], "info")
        return plan_id

    async def _run_ticket(self, run_id: int, item: Any) -> None:
        ticket = dict(item)
        queue_id = int(ticket["id"])
        plan = self.db.plan_for_queue_item(queue_id)
        self.db.update_run(run_id, state="preparing_git", progress=5)
        self.claude = ClaudeRunner(self.config, lambda phase, line: self._log(run_id, phase, line))

        if plan:
            discovery = {
                "repo_url": plan["repo_url"],
                "base_branch": plan["base_branch"],
                "summary": plan["mission"] or ticket.get("summary", "work"),
            }
            branch = plan["branch_name"]
            self._log(run_id, "plan", f"Approved mission: {plan['mission']}")
            self._log(run_id, "plan", f"Repo: {plan['repo_url']} Base: {plan['base_branch']} Branch: {plan['branch_name']}")
        else:
            self.db.upsert_sub_agent(run_id, "claude-discovery", "Pick repo, base branch, and branch summary", "running", 10)
            discovery_output = await self.claude.run_prompt(
                "discover",
                discovery_prompt(
                    ticket,
                    self.config.git.default_repo_url,
                    self.config.git.default_base_branch,
                ),
            )
            discovery = parse_discovery(discovery_output)
            if not discovery["repo_url"]:
                discovery["repo_url"] = self.config.git.default_repo_url
            if not discovery["base_branch"]:
                discovery["base_branch"] = self.config.git.default_base_branch or "main"
            summary = discovery["summary"] or ticket.get("summary", "work")
            branch = branch_name(ticket["ticket_key"], summary)
            self.db.upsert_sub_agent(run_id, "claude-discovery", "Pick repo, base branch, and branch summary", "done", 100, branch)
        if not discovery["repo_url"]:
            raise RuntimeError("Claude discovery did not provide repo_url and git.default_repo_url is not configured")
        self.db.update_run(
            run_id,
            repo_url=discovery["repo_url"],
            base_branch=discovery["base_branch"],
            branch_name=branch,
            progress=15,
        )

        repo_path = self.git.clone_for_ticket(ticket["ticket_key"], discovery["repo_url"])
        self._log(run_id, "git", f"cloned to {repo_path}")
        self.git.checkout_base_and_branch(repo_path, discovery["base_branch"], branch)
        self._log(run_id, "git", f"checked out {branch}")
        self.db.update_run(run_id, workspace_path=str(repo_path), progress=25)
        self.git.cleanup_old_clones()

        self._raise_if_cancelled()
        self.db.update_run(run_id, state="running_claude", progress=30)
        plan_context = ""
        if plan:
            plan_context = f"\n\nApproved mission:\n{plan['mission']}\n\nApproved plan:\n{plan['plan_text']}\n"
        impl_prompt = implementation_prompt(ticket, branch) + plan_context + self._consume_agent_inputs(run_id)
        self.db.upsert_sub_agent(run_id, "claude-implementation", "Implement the Jira ticket", "running", 30)
        impl_output = await self.claude.run_prompt("claude", impl_prompt, cwd=repo_path)
        self.db.upsert_sub_agent(run_id, "claude-implementation", "Implement the Jira ticket", "done", 100)
        self.db.update_run(run_id, progress=max(70, self._progress_from_output(impl_output, 70)))
        git_status = self.git.status(repo_path)
        if git_status:
            self._log(run_id, "git", "Working tree changes after Claude:")
            for line in git_status.splitlines():
                self._log(run_id, "git", line)
            self.db.update_run(
                run_id,
                changed_files=self.git.changed_files(repo_path),
                diff_summary=self.git.diff_stat(repo_path),
            )
        else:
            self._log(run_id, "git", "No file changes detected after Claude implementation.")
            raise RuntimeError("Claude finished without changing files")

        self._raise_if_cancelled()
        self.db.update_run(run_id, state="reviewing", progress=82)
        review_prompt_text = review_prompt(ticket) + self._consume_agent_inputs(run_id)
        self.db.upsert_sub_agent(run_id, "claude-review", "Review the implementation", "running", 82)
        review_output = await self.claude.run_prompt("review", review_prompt_text, cwd=repo_path)
        self.db.upsert_sub_agent(run_id, "claude-review", "Review the implementation", "done", 100)
        needs_fix = self._review_needs_fix(review_output)
        self.db.update_run(run_id, review_output=review_output, state="needs_cr_fix" if needs_fix else "done", progress=92)

        if needs_fix and self.config.claude.auto_cr_fix and self.config.claude.allow_cr_fix:
            await self.run_cr_fix(run_id)
        else:
            final_state = "needs_cr_fix" if needs_fix else "done"
            commit_sha = ""
            commit_message = ""
            if not needs_fix:
                commit_message = f"{ticket['ticket_key']}: {ticket.get('summary', 'Implement ticket')}"
                self._log(run_id, "git", f"Committing changes: {commit_message}")
                self.git.commit_all(repo_path, commit_message)
                commit_sha = self.git.head_sha(repo_path)
                self._log(run_id, "git", f"Commit created: {commit_sha}")
            report = self._run_report(ticket, discovery, branch, review_output, final_state, commit_sha)
            self.db.update_run(
                run_id,
                state=final_state,
                progress=100,
                commit_sha=commit_sha,
                commit_message=commit_message,
                run_report=report,
                finished_at=datetime.utcnow().isoformat(),
            )
            if needs_fix:
                self.db.add_notification("Run needs CR fix", ticket["ticket_key"], "warning", run_id)
            else:
                self.db.add_notification("Run finished", ticket["ticket_key"], "success", run_id)

    async def run_cr_fix(self, run_id: int) -> None:
        run = self.db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
        ticket = self.db.fetchone("SELECT * FROM tickets WHERE key=?", (run["ticket_key"],)) if run else None
        if not run or not ticket:
            raise RuntimeError("Run or ticket not found")
        if not self.config.claude.allow_cr_fix:
            raise RuntimeError("CR fix is disabled in config")
        self.claude = ClaudeRunner(self.config, lambda phase, line: self._log(run_id, phase, line))
        self.db.update_run(run_id, state="running_claude", progress=94)
        fix_prompt = cr_fix_prompt(dict(ticket), run["review_output"]) + self._consume_agent_inputs(run_id)
        self.db.upsert_sub_agent(run_id, "claude-cr-fix", "Fix review findings", "running", 94)
        await self.claude.run_prompt("cr-fix", fix_prompt, cwd=run["workspace_path"])
        self.db.upsert_sub_agent(run_id, "claude-cr-fix", "Fix review findings", "done", 100)
        review_output = await self.claude.run_prompt("review", review_prompt(dict(ticket)), cwd=run["workspace_path"])
        repo_path = Path(run["workspace_path"])
        changed_files = self.git.changed_files(repo_path)
        diff_summary = self.git.diff_stat(repo_path)
        commit_message = f"{run['ticket_key']}: Address review findings"
        self._log(run_id, "git", f"Committing CR fix changes: {commit_message}")
        self.git.commit_all(repo_path, commit_message)
        commit_sha = self.git.head_sha(repo_path)
        self._log(run_id, "git", f"Commit created: {commit_sha}")
        report = self._run_report(
            dict(ticket),
            {"repo_url": run["repo_url"], "base_branch": run["base_branch"]},
            run["branch_name"],
            review_output,
            "done",
            commit_sha,
        )
        self.db.update_run(
            run_id,
            review_output=review_output,
            state="done",
            progress=100,
            changed_files=changed_files,
            diff_summary=diff_summary,
            commit_sha=commit_sha,
            commit_message=commit_message,
            run_report=report,
            finished_at=datetime.utcnow().isoformat(),
        )
        self.db.add_notification("CR fix finished", run["ticket_key"], "success", run_id)

    def push_run(self, run_id: int) -> str:
        run = self.db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run:
            raise RuntimeError("Run not found")
        if run["state"] not in ("done", "needs_cr_fix", "pushed"):
            raise RuntimeError("Run must be done before pushing")
        if run["state"] == "needs_cr_fix":
            raise RuntimeError("Run still needs CR fix before pushing")
        if not run["commit_sha"]:
            raise RuntimeError("Run has no commit. Open the run report before pushing.")
        output = self.git.push_branch(Path(run["workspace_path"]), run["branch_name"])
        self.db.update_run(run_id, state="pushed", pushed_at=datetime.utcnow().isoformat())
        self._log(run_id, "git", output or "pushed")
        self.db.add_notification("Branch pushed", run["ticket_key"], "success", run_id)
        return output

    def rerun_ticket(self, ticket_key: str) -> None:
        self.db.enqueue(ticket_key)

    def _log(self, run_id: int, phase: str, line: str) -> None:
        clean = mask_secrets(line, secret_values(self.config))
        self.db.add_log(run_id, phase, clean)
        progress = self._progress_from_line(clean)
        if progress is not None:
            self.db.update_run(run_id, progress=progress)

    def _raise_if_cancelled(self) -> None:
        if self.cancel_requested:
            raise asyncio.CancelledError()

    def _consume_agent_inputs(self, run_id: int) -> str:
        rows = self.db.unconsumed_agent_inputs(run_id)
        if not rows:
            return ""
        messages: list[str] = []
        for row in rows:
            messages.append(str(row["message"]))
            self.db.mark_agent_input_consumed(int(row["id"]))
            self._log(run_id, "system", f"Consumed user input #{row['id']} for next Claude phase.")
        return "\n\nAdditional user instructions submitted during this run:\n" + "\n".join(f"- {message}" for message in messages)

    @staticmethod
    def _review_needs_fix(review_output: str) -> bool:
        lower = review_output.lower()
        marker = re.search(r"review_result:\s*(pass|needs_fix)", lower)
        if marker:
            return marker.group(1) == "needs_fix"
        pass_phrases = [
            "no findings",
            "no actionable findings",
            "no issues",
            "no bugs",
            "looks good",
            "nothing to fix",
        ]
        if any(phrase in lower for phrase in pass_phrases):
            return False
        finding_phrases = [
            "finding",
            "bug",
            "regression",
            "missing test",
            "needs fix",
            "request changes",
            "::code-comment",
        ]
        return any(phrase in lower for phrase in finding_phrases)

    @staticmethod
    def _friendly_error(message: str) -> str:
        lower = message.lower()
        if "authentication failed" in lower or "permission denied" in lower:
            return "Git authentication failed. Check SSH keys or Git token settings."
        if "could not read from remote repository" in lower:
            return "Git could not read the remote repository. Check repo URL and access."
        if "claude timed out" in lower:
            return "Claude timed out before finishing."
        if "claude exited" in lower:
            return message
        if "without changing files" in lower:
            return "Claude finished without changing files. Review the ticket/plan and rerun."
        return message

    @staticmethod
    def _run_report(ticket: dict[str, Any], discovery: dict[str, str], branch: str, review: str, state: str, commit_sha: str) -> str:
        ticket_key = ticket.get("ticket_key") or ticket.get("key") or ""
        return "\n".join(
            [
                f"Ticket: {ticket_key}",
                f"State: {state}",
                f"Repo: {discovery.get('repo_url', '')}",
                f"Base branch: {discovery.get('base_branch', '')}",
                f"Work branch: {branch}",
                f"Commit: {commit_sha or 'not created'}",
                "",
                "Review:",
                review,
            ]
        )

    @staticmethod
    def _progress_from_line(line: str) -> int | None:
        match = re.search(r"PROGRESS\s+(\d{1,3})%", line, re.IGNORECASE)
        if match:
            return max(0, min(100, int(match.group(1))))
        return None

    @classmethod
    def _progress_from_output(cls, output: str, default: int) -> int:
        values = [cls._progress_from_line(line) for line in output.splitlines()]
        values = [value for value in values if value is not None]
        return max(values) if values else default
