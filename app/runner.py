from __future__ import annotations

import asyncio
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from app.claude_runner import (
    ClaudeRunner,
    cr_fix_prompt,
    discovery_prompt,
    implementation_prompt,
    parse_ask_user,
    parse_plan,
    planning_prompt,
    parse_discovery,
    review_prompt,
)
from app.config import Config, DEFAULT_OWNER, apply_user_sections, secret_values
from app.code_review import create_merge_request, post_review_reply
from app.db import Database
from app.git_ops import GitOps
from app.jira_client import JiraClient, classify_ticket
from app.utils import branch_name, mask_secrets


class Worker:
    """Automation engine for a single owner (user).

    Each user gets their own Worker so runs are isolated: per-user lock (one
    active run per user, many users in parallel), per-user interval task,
    per-user clone workspace, and a config built from that user's saved settings.
    """

    def __init__(self, config: Config, db: Database, owner: str = DEFAULT_OWNER):
        self.config = config
        self.db = db
        self.owner = owner
        self.git = GitOps(config, owner)
        self.lock = asyncio.Lock()
        self.interval_task: asyncio.Task[None] | None = None
        self.claude: ClaudeRunner | None = None
        self.cancel_requested = False

    def _runner(self, run_id: int | None, log) -> ClaudeRunner:
        suffix = run_id if run_id is not None else "plan"
        return ClaudeRunner(self.config, log, label_prefix=f"cw_{self.owner}_{suffix}")

    async def scan_jira(self) -> int:
        client = JiraClient(self.config.jira)
        tickets = await client.search_assigned()
        count = 0
        for ticket in tickets:
            classified = classify_ticket(ticket, self.config.jira)
            self.db.upsert_ticket(classified, owner=self.owner)
            if classified["eligibility"] == "eligible":
                self.db.enqueue(classified["key"], owner=self.owner)
            count += 1
        return count

    async def scan_and_run_once(self) -> None:
        try:
            await self.scan_jira()
        except Exception as exc:
            print(f"Jira scan failed: {exc}")
        if self.db.queue_paused(owner=self.owner):
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
        if self.db.queue_paused(owner=self.owner):
            return None
        if self.lock.locked():
            return None
        async with self.lock:
            item = self.db.next_queue_item(owner=self.owner)
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
        run_id = self.db.create_run(item["ticket_key"], owner=self.owner)
        try:
            await self._run_ticket(run_id, item)
            self.db.set_queue_state(int(item["id"]), "done")
        except asyncio.CancelledError:
            self.db.update_run(run_id, state="cancelled", error="cancelled", finished_at=datetime.utcnow().isoformat())
            self.db.add_notification("Run cancelled", item["ticket_key"], "warning", run_id, owner=self.owner)
            self.db.set_queue_state(int(item["id"]), "cancelled")
        except Exception as exc:
            friendly = self._friendly_error(str(exc))
            self._log(run_id, "error", friendly)
            self.db.update_run(run_id, state="failed", error=friendly, finished_at=datetime.utcnow().isoformat())
            self.db.add_notification("Run failed", f"{item['ticket_key']}: {friendly}", "error", run_id, owner=self.owner)
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
        self.claude = self._runner(None, lambda phase, line: print(f"[{phase}] {line}"))
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
                "owner": self.owner,
                "queue_item_id": queue_id,
                "state": "draft",
                "repo_url": repo_url,
                "base_branch": base_branch,
                "branch_name": branch,
                "mission": parsed["mission"] or item["summary"],
                "plan_text": parsed["plan_text"] or "Claude did not provide a detailed plan.",
                "user_notes": user_notes,
                "raw_output": output,
            },
            owner=self.owner,
        )
        self.db.set_queue_state(queue_id, "plan_ready")
        self.db.add_notification("Plan ready", item["ticket_key"], "info", owner=self.owner)
        return plan_id

    async def _run_ticket(self, run_id: int, item: Any) -> None:
        ticket = dict(item)
        queue_id = int(ticket["id"])
        plan = self.db.plan_for_queue_item(queue_id)
        self.db.update_run(run_id, state="preparing_git", progress=5)
        self.claude = self._runner(run_id, lambda phase, line: self._log(run_id, phase, line))

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
        impl_output = await self._resolve_agent_questions(run_id, impl_output, repo_path)
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
                self.db.add_notification("Run needs CR fix", ticket["ticket_key"], "warning", run_id, owner=self.owner)
            else:
                self.db.add_notification("Run finished", ticket["ticket_key"], "success", run_id, owner=self.owner)
                if self.config.git.auto_push:
                    self._auto_publish(run_id, ticket, discovery, branch)

    def _auto_publish(self, run_id: int, ticket: dict[str, Any], discovery: dict[str, str], branch: str) -> None:
        """Push (and optionally open a merge request) for a clean done run."""
        try:
            self.push_run(run_id)
            if self.config.git.auto_merge_request:
                self.open_merge_request(run_id)
        except Exception as exc:
            self._log(run_id, "git", f"Auto-publish failed: {exc}")
            self.db.add_notification("Auto-push failed", f"{ticket['ticket_key']}: {exc}", "error", run_id, owner=self.owner)

    def open_merge_request(self, run_id: int) -> str:
        run = self.db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run:
            raise RuntimeError("Run not found")
        title = f"{run['ticket_key']}: {run['commit_message'] or 'Automated change'}"
        url = create_merge_request(run["repo_url"], run["branch_name"], run["base_branch"], title, run["run_report"])
        if url:
            self._log(run_id, "git", f"Opened merge request: {url}")
            self.db.set_state(f"merge_request_url:{run_id}", url, owner=self.owner)
            self.db.add_notification("Merge request opened", url, "success", run_id, owner=self.owner)
        return url

    async def run_cr_fix(self, run_id: int) -> None:
        run = self.db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
        ticket = self.db.fetchone("SELECT * FROM tickets WHERE key=? AND owner=?", (run["ticket_key"], self.owner)) if run else None
        if not run or not ticket:
            raise RuntimeError("Run or ticket not found")
        if not self.config.claude.allow_cr_fix:
            raise RuntimeError("CR fix is disabled in config")
        self.claude = self._runner(run_id, lambda phase, line: self._log(run_id, phase, line))
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
        self.db.add_notification("CR fix finished", run["ticket_key"], "success", run_id, owner=self.owner)

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
        self.db.add_notification("Branch pushed", run["ticket_key"], "success", run_id, owner=self.owner)
        return output

    async def run_external_code_review_fix(
        self,
        run_id: int,
        user_notes: str = "",
        comment_back: bool = True,
        ci_context: str = "",
    ) -> None:
        run = self.db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
        ticket = self.db.fetchone("SELECT * FROM tickets WHERE key=? AND owner=?", (run["ticket_key"], self.owner)) if run else None
        if not run or not ticket:
            raise RuntimeError("Run or ticket not found")
        notes = self.db.code_review_notes(run_id)
        open_notes = [note for note in notes if note["state"] != "responded"]
        if not open_notes:
            raise RuntimeError("No open code review notes to process")
        repo_path = Path(run["workspace_path"])
        if not repo_path.exists():
            raise RuntimeError("Run workspace does not exist")
        self.claude = self._runner(run_id, lambda phase, line: self._log(run_id, phase, line))
        self.db.update_run(run_id, state="running_claude", progress=90)
        notes_text = "\n\n".join(
            f"NOTE_ID: {note['id']}\nEXTERNAL_ID: {note['external_id']}\nKIND: {note['kind']}\n"
            f"AUTHOR: {note['author']}\nFILE: {note['file_path']}:{note['line']}\nBODY:\n{note['body']}"
            for note in open_notes
        )
        prompt = f"""
You are fixing code review notes for Jira ticket {ticket['key']}.
Do not run git commands. Python owns all Git interactions.

For each review note:
- Fix code when the note is actionable and correct.
- If the note is a question, incorrect, ambiguous, or needs more information, do not invent certainty.
- Produce a response for every note.
- Do not resolve review threads or change review state.

Additional user instructions:
{user_notes}

CI status context (if available):
{ci_context}

Review notes:
{notes_text}

After editing files, return JSON with one key "responses".
responses must be an array of objects:
{{"note_id": 123, "response": "short reply to post back to the reviewer"}}
"""
        output = await self.claude.run_prompt("cr-notes", prompt, cwd=repo_path)
        responses = self._parse_review_responses(output)
        changed = self.git.status(repo_path)
        if changed:
            self.db.update_run(run_id, changed_files=self.git.changed_files(repo_path), diff_summary=self.git.diff_stat(repo_path))
            commit_message = f"{run['ticket_key']}: Address code review notes"
            self._log(run_id, "git", f"Committing code review fixes: {commit_message}")
            self.git.commit_all(repo_path, commit_message)
            commit_sha = self.git.head_sha(repo_path)
            self._log(run_id, "git", f"Commit created: {commit_sha}")
            self.db.update_run(run_id, commit_sha=commit_sha, commit_message=commit_message, state="done", progress=100)
        for note in open_notes:
            response = responses.get(int(note["id"])) or "Thanks. I reviewed this note and updated the branch or left the requested context in the latest run report."
            response_url = ""
            if comment_back:
                response_url = post_review_reply(note["source_url"], note["external_id"], note["kind"], response)
                self._log(run_id, "cr", f"Commented on review note #{note['id']}")
            self.db.mark_code_review_note_responded(int(note["id"]), response, response_url)
        self.db.add_notification("Code review handled", run["ticket_key"], "success", run_id, owner=self.owner)

    async def run_ci_fix(self, run_id: int, ci_context: str, user_notes: str = "") -> None:
        run = self.db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
        ticket = self.db.fetchone("SELECT * FROM tickets WHERE key=? AND owner=?", (run["ticket_key"], self.owner)) if run else None
        if not run or not ticket:
            raise RuntimeError("Run or ticket not found")
        repo_path = Path(run["workspace_path"] or "")
        if not run["workspace_path"] or not repo_path.exists():
            raise RuntimeError("Run workspace does not exist")
        if not ci_context.strip():
            raise RuntimeError("No CI context available. Scan review first.")
        self.claude = self._runner(run_id, lambda phase, line: self._log(run_id, phase, line))
        self.db.update_run(run_id, state="running_claude", progress=88)
        prompt = f"""
You are fixing CI failures for Jira ticket {ticket['key']}.
Do not run git commands. Python owns all Git interactions.

CI output:
{ci_context}

Additional user instructions:
{user_notes}

Tasks:
- Identify failing checks/jobs from the CI output.
- Apply focused code or config fixes in this repository.
- If CI output is incomplete, make the safest likely fix and explain assumptions in normal output text.
"""
        await self.claude.run_prompt("ci-fix", prompt, cwd=repo_path)
        changed = self.git.status(repo_path)
        if not changed:
            raise RuntimeError("CI fix run finished without changes")
        self.db.update_run(run_id, changed_files=self.git.changed_files(repo_path), diff_summary=self.git.diff_stat(repo_path))
        commit_message = f"{run['ticket_key']}: Fix CI issues"
        self._log(run_id, "git", f"Committing CI fixes: {commit_message}")
        self.git.commit_all(repo_path, commit_message)
        commit_sha = self.git.head_sha(repo_path)
        self._log(run_id, "git", f"Commit created: {commit_sha}")
        self.db.update_run(run_id, commit_sha=commit_sha, commit_message=commit_message, state="done", progress=100)
        self.db.add_notification("CI fix finished", run["ticket_key"], "success", run_id, owner=self.owner)

    def rerun_ticket(self, ticket_key: str) -> None:
        self.db.enqueue(ticket_key, owner=self.owner)

    def _scp_command(self, source: str) -> list[str]:
        d = self.config.delivery
        if not d.scp_host or not d.scp_path:
            raise RuntimeError("Configure delivery scp host and path in Settings first.")
        dest_user = f"{d.scp_user}@" if d.scp_user else ""
        destination = f"{dest_user}{d.scp_host}:{d.scp_path}"
        cmd = ["scp", "-r", "-o", "StrictHostKeyChecking=accept-new"]
        if d.ssh_port and int(d.ssh_port) != 22:
            cmd += ["-P", str(int(d.ssh_port))]
        if d.ssh_key:
            cmd += ["-i", d.ssh_key]
        cmd += [source, destination]
        return cmd

    def deliver_locally(self, run_id: int) -> str:
        """scp the finished run's workspace to the user's machine over SSH."""
        run = self.db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run or not run["workspace_path"]:
            raise RuntimeError("Run has no workspace to deliver yet.")
        repo_path = Path(run["workspace_path"])
        if not repo_path.exists():
            raise RuntimeError("Run workspace folder no longer exists.")
        cmd = self._scp_command(str(repo_path))
        d = self.config.delivery
        self._log(run_id, "deliver", f"scp -r workspace -> {d.scp_host}:{d.scp_path}")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        output = mask_secrets((proc.stdout or "") + (proc.stderr or ""), secret_values(self.config)).strip()
        if proc.returncode != 0:
            raise RuntimeError(output or "scp failed")
        self.db.add_notification(
            "Workspace delivered", f"{run['ticket_key']} copied to {d.scp_host}", "success", run_id, owner=self.owner
        )
        return output or f"Delivered to {d.scp_host}:{d.scp_path}"

    def _log(self, run_id: int, phase: str, line: str) -> None:
        clean = mask_secrets(line, secret_values(self.config))
        self.db.add_log(run_id, phase, clean)
        progress = self._progress_from_line(clean)
        if progress is not None:
            self.db.update_run(run_id, progress=progress)

    def _raise_if_cancelled(self) -> None:
        if self.cancel_requested:
            raise asyncio.CancelledError()

    async def _ask_user(self, run_id: int, question: str, options: list[str], agent_name: str = "main") -> str:
        """Insert a pending question and block the run until the user answers it.

        The UI surfaces pending questions on the dashboard and run page; the user
        answers there. Cancellation interrupts the wait.
        """
        question_id = self.db.create_agent_question(
            run_id, question, options, agent_name=agent_name, owner=self.owner
        )
        self._log(run_id, "ask", f"Waiting for the user to answer: {question}")
        while True:
            self._raise_if_cancelled()
            row = self.db.fetchone("SELECT state, answer FROM agent_questions WHERE id=?", (question_id,))
            if row and row["state"] == "answered":
                self._log(run_id, "ask", f"User answered #{question_id}: {row['answer']}")
                return str(row["answer"])
            await asyncio.sleep(2)

    async def _resolve_agent_questions(self, run_id: int, output: str, repo_path: Path) -> str:
        """If the agent emitted ASK_USER markers, wait for answers and let it react.

        Returns the (possibly extended) agent output so downstream progress parsing
        still works.
        """
        pending = parse_ask_user(output)
        if not pending:
            return output
        answered: list[tuple[str, str]] = []
        for item in pending:
            answer = await self._ask_user(run_id, str(item["question"]), list(item["options"]))
            answered.append((str(item["question"]), answer))
        followup = (
            "The user answered the questions you asked. Apply any changes needed in the working tree.\n"
            "Do not run git commands.\n\n"
            + "\n".join(f"- {question} => {answer}" for question, answer in answered)
        )
        self.db.upsert_sub_agent(run_id, "claude-implementation", "Apply the user's answers", "running", 70)
        followup_output = await self.claude.run_prompt("claude", followup, cwd=repo_path)
        return output + "\n" + followup_output

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
    def _parse_review_responses(output: str) -> dict[int, str]:
        match = re.search(r"\{.*\}", output, re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
        responses: dict[int, str] = {}
        for item in data.get("responses", []) or []:
            try:
                note_id = int(item.get("note_id"))
            except (TypeError, ValueError):
                continue
            response = str(item.get("response") or "").strip()
            if response:
                responses[note_id] = response
        return responses

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


class WorkerRegistry:
    """Holds one Worker per user, each with that user's effective config.

    The effective config = server base config (app/auth/docker) + the user's
    saved jira/git/claude/ui sections.
    """

    def __init__(self, base_config: Config, db: Database):
        self.base_config = base_config
        self.db = db
        self._workers: dict[str, Worker] = {}

    def set_base_config(self, base_config: Config) -> None:
        self.base_config = base_config
        # Existing workers rebuild lazily on next access via refresh().

    def user_config(self, owner: str) -> Config:
        return apply_user_sections(self.base_config, self.db.get_user_config(owner))

    def for_user(self, owner: str) -> Worker:
        worker = self._workers.get(owner)
        if worker is None:
            worker = Worker(self.user_config(owner), self.db, owner)
            self._workers[owner] = worker
        return worker

    def refresh(self, owner: str) -> Worker:
        """Rebuild a user's worker config after they save settings.

        Preserves the running interval task so saving settings does not stop a
        background loop.
        """
        worker = self.for_user(owner)
        worker.config = self.user_config(owner)
        worker.git = GitOps(worker.config, owner)
        return worker

    def active_workers(self) -> list[Worker]:
        return list(self._workers.values())
