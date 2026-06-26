from __future__ import annotations

import asyncio
import json
import re
import shlex
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
from app.code_review import create_merge_request, gitlab_auth_for, post_review_reply
from app.merge_requests import (
    ci_failed,
    ci_job_signature,
    ci_jobs_suggestion_prompt,
    cr_suggestion_prompt,
    failed_ci_jobs,
    parse_ci_job_suggestions,
    parse_ci_jobs as mr_parse_ci_jobs,
    parse_cr_suggestions,
    render_ci_context as mr_render_ci_context,
    signature as mr_signature,
    skills_block as mr_skills_block,
)
from app.db import Database
from app.git_ops import GitOps
from app.execution import get_execution_backend
from app import notify
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
            self._notify_external("Run failed", f"{item['ticket_key']}: {friendly}", "error", run_id)
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
        liked = self.db.liked_skills(self.owner)
        skills_hint = "\n".join(f"- {row['name']}: {row['description']}" for row in liked)
        output = await self.claude.run_prompt(
            "plan",
            planning_prompt(
                dict(item),
                self.config.git.default_repo_url,
                self.config.git.default_base_branch,
                previous,
                user_notes,
                skills_hint,
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
        # Queue rows carry ticket_key, but the Claude prompts read ticket["key"].
        ticket.setdefault("key", ticket.get("ticket_key", ""))
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

        await self._implement_and_finish(run_id, ticket, repo_path, branch, discovery, plan)

    async def _implement_and_finish(
        self,
        run_id: int,
        ticket: dict[str, Any],
        repo_path: Path,
        branch: str,
        discovery: dict[str, str],
        plan: Any,
    ) -> None:
        """Implement -> review -> (test gate) -> commit/finish, in an existing workspace.

        Shared by a fresh run (after clone/checkout) and a retry (which reuses
        the failed run's workspace instead of cloning again). self.claude must
        already be set by the caller.
        """
        self._raise_if_cancelled()
        self.db.update_run(run_id, state="running_claude", progress=30)
        plan_context = ""
        if plan:
            plan_context = f"\n\nApproved mission:\n{plan['mission']}\n\nApproved plan:\n{plan['plan_text']}\n"
        skills_context = self._selected_skills_context(plan)
        impl_prompt = implementation_prompt(ticket, branch) + plan_context + skills_context + self._consume_agent_inputs(run_id)
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
        if not needs_fix and self.config.test_gate.enabled:
            label = self.config.test_gate.command or "tests"
            self.db.upsert_sub_agent(run_id, "test-gate", label, "running", 88)
            gate_ok, gate_output = await self._run_test_gate(run_id, repo_path)
            self.db.upsert_sub_agent(run_id, "test-gate", label, "done" if gate_ok else "failed", 100)
            if not gate_ok:
                needs_fix = True
                review_output += (
                    "\n\nREVIEW_RESULT: needs_fix\n"
                    "Automated test gate failed. Fix the failing tests before this can be pushed:\n"
                    + gate_output
                )
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
                self._notify_external("Run needs CR fix", f"{ticket['ticket_key']}: review found issues", "warning", run_id)
            else:
                self.db.add_notification("Run finished", ticket["ticket_key"], "success", run_id, owner=self.owner)
                if self.config.git.auto_push:
                    self._auto_publish(run_id, ticket, discovery, branch)
                await self._jira_writeback(run_id)
                self._notify_external("Run finished", f"{ticket['ticket_key']} is done on {branch}", "success", run_id)

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
        url = create_merge_request(
            run["repo_url"], run["branch_name"], run["base_branch"], title, run["run_report"],
            auth=gitlab_auth_for(self.config),
        )
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
        await self._jira_writeback(run_id)

    async def _jira_writeback(self, run_id: int) -> None:
        """Comment on and/or transition the Jira ticket for a finished run.

        Config-gated (jira.writeback_enabled) and best-effort: any failure is
        logged and notified but never fails the run.
        """
        cfg = self.config.jira
        if not cfg.writeback_enabled:
            return
        run = self.db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run:
            return
        key = run["ticket_key"]
        client = JiraClient(cfg)
        try:
            if cfg.writeback_comment:
                mr_url = self.db.get_state(f"merge_request_url:{run_id}", "", owner=self.owner)
                lines = [
                    f"Automated run for {key} finished ({run['state']}).",
                    f"Branch: {run['branch_name']}",
                ]
                if run["commit_sha"]:
                    lines.append(f"Commit: {run['commit_sha']}")
                if mr_url:
                    lines.append(f"Merge request: {mr_url}")
                if run["run_report"]:
                    lines.append("")
                    lines.append(run["run_report"])
                await client.add_comment(key, "\n".join(lines))
                self._log(run_id, "jira", f"Commented on {key}")
            if cfg.writeback_transition:
                moved = await client.transition_issue(key, cfg.writeback_transition)
                if moved:
                    self._log(run_id, "jira", f"Transitioned {key} -> {cfg.writeback_transition}")
                else:
                    self._log(run_id, "jira", f"No transition named '{cfg.writeback_transition}' for {key}")
        except Exception as exc:
            self._log(run_id, "jira", f"Jira write-back failed: {exc}")
            self.db.add_notification("Jira write-back failed", f"{key}: {exc}", "warning", run_id, owner=self.owner)

    async def _run_test_gate(self, run_id: int, repo_path: Path) -> tuple[bool, str]:
        """Run the configured test command in the workspace. Returns (passed, tail_output)."""
        command = (self.config.test_gate.command or "").strip()
        if not command:
            self._log(run_id, "test", "Test gate enabled but no command configured; skipping.")
            return True, ""
        timeout = max(30, int(self.config.test_gate.timeout_seconds or 1800))
        # In Docker mode, run the tests inside a throwaway container with the
        # workspace mounted (same isolation as the agent); otherwise run on host.
        if self.config.docker.enabled:
            inv = get_execution_backend(self.config).build(
                ["sh", "-lc", command], repo_path, {}, f"cw_{self.owner}_{run_id}_testgate"
            )
            argv, cwd, where = inv.argv, inv.cwd, "docker"
        else:
            argv, cwd, where = shlex.split(command), str(repo_path), "host"
        self._log(run_id, "test", f"Running test gate ({where}): {command}")

        def _run() -> tuple[int, str]:
            proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

        try:
            code, output = await asyncio.to_thread(_run)
        except subprocess.TimeoutExpired:
            self._log(run_id, "test", f"Test gate timed out after {timeout}s")
            return False, f"Test command timed out after {timeout}s"
        except Exception as exc:
            self._log(run_id, "test", f"Test gate could not run: {exc}")
            return False, f"Test command could not run: {exc}"
        tail = mask_secrets(output, secret_values(self.config)).strip()[-4000:]
        for line in tail.splitlines()[-40:]:
            self._log(run_id, "test", line)
        ok = code == 0
        self._log(run_id, "test", f"Test gate {'passed' if ok else 'failed'} (exit {code})")
        return ok, tail

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
        notes_text = "\n\n".join(self._format_review_note(note) for note in open_notes)
        branch_diff = self._branch_diff_context(repo_path, run["base_branch"])
        prompt = f"""
You are fixing code review notes for Jira ticket {ticket['key']}.
Do not run git commands. Python owns all Git interactions.

For each review note:
- Read the "CODE FROM GIT" hunk attached to the note (the exact lines the reviewer commented on) and open the referenced file to see the surrounding code before editing.
- Fix code when the note is actionable and correct.
- If the note is a question, incorrect, ambiguous, or needs more information, do not invent certainty — answer it honestly on the user's behalf.
- Produce a response for EVERY note. This response is posted back to the reviewer as the user's answer, so write it in first person and explain what you changed or why no change was made.
- Do not resolve review threads or change review state.

Additional user instructions:
{user_notes}

CI status context (if available):
{ci_context}

Current branch diff (the work under review, straight from git):
{branch_diff}

Review notes:
{notes_text}

After editing files, return JSON with one key "responses".
responses must be an array of objects:
{{"note_id": 123, "response": "first-person reply to post back to the reviewer"}}
Include one object for every NOTE_ID above.
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
                response_url = post_review_reply(
                    note["source_url"], note["external_id"], note["kind"], response,
                    auth=gitlab_auth_for(self.config),
                )
                self._log(run_id, "cr", f"Commented on review note #{note['id']}")
            self.db.mark_code_review_note_responded(int(note["id"]), response, response_url)
        self.db.add_notification("Code review handled", run["ticket_key"], "success", run_id, owner=self.owner)

    async def run_ide_comment_fix(self, run_id: int, user_notes: str = "") -> None:
        """Apply the user's inline Web IDE comments as a local revision pass."""
        run = self.db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
        ticket = self.db.fetchone("SELECT * FROM tickets WHERE key=? AND owner=?", (run["ticket_key"], self.owner)) if run else None
        if not run or not ticket:
            raise RuntimeError("Run or ticket not found")
        comments = self.db.open_ide_comments(run_id)
        if not comments:
            raise RuntimeError("No open IDE comments to apply")
        repo_path = Path(run["workspace_path"] or "")
        if not run["workspace_path"] or not repo_path.exists():
            raise RuntimeError("Run workspace does not exist")
        self.claude = self._runner(run_id, lambda phase, line: self._log(run_id, phase, line))
        self.db.update_run(run_id, state="running_claude", progress=90)
        self.db.upsert_sub_agent(run_id, "claude-ide-comments", "Apply reviewer comments", "running", 90)
        comment_text = "\n\n".join(
            f"- {c['file_path']}:{c['line']}: {c['body']}" if c["file_path"] else f"- {c['body']}"
            for c in comments
        )
        branch_diff = self._branch_diff_context(repo_path, run["base_branch"])
        prompt = f"""
You are revising the branch for Jira ticket {ticket['key']} based on inline review
comments the user left in the Web IDE. Do not run git commands; Python owns Git.

Open the referenced files, address each comment, and keep changes focused.

Additional user instructions:
{user_notes}

Current branch diff (from git):
{branch_diff}

Reviewer comments (file:line: comment):
{comment_text}
"""
        await self.claude.run_prompt("ide-comments", prompt, cwd=repo_path)
        self.db.upsert_sub_agent(run_id, "claude-ide-comments", "Apply reviewer comments", "done", 100)
        changed = self.git.status(repo_path)
        if changed:
            commit_message = f"{run['ticket_key']}: Address Web IDE review comments"
            self._log(run_id, "git", f"Committing IDE comment fixes: {commit_message}")
            self.git.commit_all(repo_path, commit_message)
            commit_sha = self.git.head_sha(repo_path)
            self._log(run_id, "git", f"Commit created: {commit_sha}")
            self.db.update_run(
                run_id,
                changed_files=self.git.changed_files(repo_path),
                diff_summary=self.git.diff_stat(repo_path),
                commit_sha=commit_sha,
                commit_message=commit_message,
                state="done",
                progress=100,
            )
        else:
            self.db.update_run(run_id, state="done", progress=100)
            self._log(run_id, "ide-comments", "No file changes were needed for the comments.")
        self.db.resolve_ide_comments(run_id)
        self.db.add_notification("IDE comments applied", run["ticket_key"], "success", run_id, owner=self.owner)
        self._notify_external("IDE comments applied", f"{run['ticket_key']} updated from your comments", "success", run_id)

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

    # ----- Merge Request tab: AI suggestions + fresh-clone fixes ----------

    async def generate_mr_suggestions(self, mr_id: int) -> None:
        """Generate cheap, cached AI advice for an MR without a workspace run.

        When CI is failing, write a short fix-note. For each open review note,
        propose how to address it and a first-person reply to post back. Each
        kind is keyed by a content signature so it only regenerates when the
        underlying CI output or note text actually changed (the "auto-run the
        second the result lands" behaviour, without burning tokens on a loop).
        """
        mr_row = self.db.get_merge_request(mr_id, self.owner)
        if not mr_row:
            return
        runner = self._runner(None, lambda phase, line: None)

        # --- Per-failed-job CI fix suggestions (kept fresh per job signature) ---
        ci_jobs = mr_parse_ci_jobs(mr_row["ci_jobs"])
        failed = failed_ci_jobs(ci_jobs)
        if failed:
            try:
                stored = json.loads(mr_row["ci_job_suggestions"] or "{}")
                if not isinstance(stored, dict):
                    stored = {}
            except json.JSONDecodeError:
                stored = {}
            stale = [job for job in failed if stored.get(job["name"], {}).get("sig") != ci_job_signature(job)]
            if stale:
                ci_skills = mr_skills_block(self.db, mr_row, "ci")
                try:
                    output = await runner.run_prompt(
                        "mr-ci-suggest",
                        ci_jobs_suggestion_prompt(mr_row["title"] or mr_row["url"], stale, ci_skills),
                    )
                    fixes = parse_ci_job_suggestions(output)
                except Exception as exc:  # noqa: BLE001 - advice is best-effort
                    fixes = {job["name"]: f"(Could not generate a suggestion: {exc})" for job in stale}
                for job in stale:
                    stored[job["name"]] = {
                        "sig": ci_job_signature(job),
                        "text": fixes.get(job["name"], ""),
                    }
                self.db.update_merge_request(mr_id, ci_job_suggestions=json.dumps(stored))

        # --- Per-note CR fix + suggested reply ---
        pending: list[tuple[Any, str]] = []
        for note in self.db.open_mr_notes(mr_id):
            body_sig = mr_signature(note["body"])
            if not note["suggestion"] or (note["suggestion_sig"] or "") != body_sig:
                pending.append((note, body_sig))
        if pending:
            note_dicts = [dict(note) for note, _ in pending]
            cr_skills = mr_skills_block(self.db, mr_row, "cr")
            try:
                output = await runner.run_prompt("mr-cr-suggest", cr_suggestion_prompt(note_dicts, cr_skills))
                parsed = parse_cr_suggestions(output)
            except Exception:  # noqa: BLE001
                parsed = {}
            for note, body_sig in pending:
                advice = parsed.get(int(note["id"]), {})
                self.db.set_mr_note_suggestion(
                    int(note["id"]), advice.get("fix", ""), advice.get("reply", ""), body_sig
                )

    async def run_mr_fix(
        self, mr_id: int, mode: str, user_notes: str = "", comment_back: bool = True
    ) -> int:
        """Fix a merge request's failing CI or open review notes by fresh-cloning
        the MR's source branch, running the agent, committing, and pushing the
        branch so the MR updates. Returns the new run id. mode is 'ci' or 'cr'."""
        if mode not in ("ci", "cr"):
            raise RuntimeError("mode must be 'ci' or 'cr'")
        mr_row = self.db.get_merge_request(mr_id, self.owner)
        if not mr_row:
            raise RuntimeError("Merge request not found")
        source_branch = (mr_row["source_branch"] or "").strip()
        if not source_branch:
            raise RuntimeError("This MR has no known source branch yet. Open and Scan it from the MR list first.")
        repo_url = (mr_row["repo_url"] or "").strip()
        if not repo_url:
            raise RuntimeError(
                "No repository URL is known for this MR. Set git.gitlab_host so a clone URL can be derived, "
                "or open it from a run this tool created."
            )
        if self.lock.locked():
            raise RuntimeError("A run is already in progress.")
        iid = mr_row["iid"] or mr_id
        ticket_key = f"MR-{iid}"
        async with self.lock:
            run_id = self.db.create_run(ticket_key, owner=self.owner)
            self.db.update_merge_request(mr_id, run_id=run_id)
            try:
                self.claude = self._runner(run_id, lambda phase, line: self._log(run_id, phase, line))
                self.db.update_run(
                    run_id,
                    state="preparing_git",
                    repo_url=repo_url,
                    branch_name=source_branch,
                    base_branch=mr_row["target_branch"] or "",
                    progress=10,
                )
                self._log(run_id, "git", f"Fresh-cloning {repo_url} for MR {mode.upper()} fix")
                repo_path = self.git.clone_for_ticket(ticket_key, repo_url)
                self.git.checkout_existing_branch(repo_path, source_branch)
                self._log(run_id, "git", f"Checked out MR source branch {source_branch}")
                self.git.cleanup_old_clones()
                self.db.update_run(run_id, workspace_path=str(repo_path), state="running_claude", progress=40)

                open_notes: list[Any] = []
                if mode == "ci":
                    ci_jobs = mr_parse_ci_jobs(mr_row["ci_jobs"])
                    ci_context = mr_render_ci_context(ci_jobs)
                    if not ci_context.strip():
                        raise RuntimeError("No CI context stored. Scan the MR before running Fix CI.")
                    prompt = f"""
You are fixing CI failures on merge request {mr_row['url']} (branch {source_branch}).
Do not run git commands. Python owns all Git interactions.
{mr_skills_block(self.db, mr_row, "ci")}
CI output:
{ci_context}

Additional user instructions:
{user_notes}

Identify the failing checks and apply focused code/config fixes in this repository.
Use any project-specific CI knowledge above to interpret custom jobs.
If the CI output is incomplete, make the safest likely fix and explain assumptions in plain text.
"""
                    commit_message = f"{ticket_key}: Fix CI"
                else:
                    open_notes = self.db.open_mr_notes(mr_id)
                    if not open_notes:
                        raise RuntimeError("No open review notes to fix on this MR.")
                    notes_text = "\n\n".join(self._format_review_note(note) for note in open_notes)
                    branch_diff = self._branch_diff_context(repo_path, mr_row["target_branch"] or "")
                    prompt = f"""
You are addressing code review notes on merge request {mr_row['url']} (branch {source_branch}).
Do not run git commands. Python owns all Git interactions.
{mr_skills_block(self.db, mr_row, "cr")}
For each review note: read the CODE FROM GIT hunk and the referenced file, fix the code when the
note is actionable, and write a first-person reply to post back to the reviewer. If a note is a
question or incorrect, answer honestly without inventing certainty. Follow the code conventions
above. Do not resolve threads.

Additional user instructions:
{user_notes}

Current branch diff (from git):
{branch_diff}

Review notes:
{notes_text}

After editing, return JSON: {{"responses": [{{"note_id": 123, "response": "first-person reply"}}]}}
Include one object for every NOTE_ID above.
"""
                    commit_message = f"{ticket_key}: Address review notes"

                output = await self.claude.run_prompt(f"mr-{mode}-fix", prompt, cwd=repo_path)

                if self.git.status(repo_path):
                    self.db.update_run(
                        run_id,
                        changed_files=self.git.changed_files(repo_path),
                        diff_summary=self.git.diff_stat(repo_path),
                    )
                    self._log(run_id, "git", f"Committing MR fix: {commit_message}")
                    self.git.commit_all(repo_path, commit_message)
                    commit_sha = self.git.head_sha(repo_path)
                    self._log(run_id, "git", f"Commit created: {commit_sha}")
                    self.db.update_run(run_id, commit_sha=commit_sha, commit_message=commit_message)
                    pushed = self.git.push_branch(repo_path, source_branch)
                    self._log(run_id, "git", pushed or f"pushed {source_branch}")
                else:
                    self._log(run_id, "git", "Agent made no file changes.")

                if mode == "cr":
                    responses = self._parse_review_responses(output)
                    for note in open_notes:
                        reply = (
                            responses.get(int(note["id"]))
                            or note["suggested_response"]
                            or "Thanks - I addressed this in the latest push on the branch."
                        )
                        response_url = ""
                        if comment_back:
                            response_url = post_review_reply(
                                note["source_url"], note["external_id"], note["kind"], reply,
                                auth=gitlab_auth_for(self.config),
                            )
                            self._log(run_id, "cr", f"Replied to review note {note['external_id']}")
                        self.db.mark_mr_note_responded(int(note["id"]), reply, response_url)

                self.db.update_run(run_id, state="done", progress=100, finished_at=datetime.utcnow().isoformat())
                self.db.add_notification(
                    f"MR {mode.upper()} fix finished", f"{ticket_key} on {source_branch}", "success", run_id, owner=self.owner
                )
                self._notify_external(f"MR {mode.upper()} fix finished", f"{ticket_key} updated", "success", run_id)
                return run_id
            except asyncio.CancelledError:
                self.db.update_run(run_id, state="cancelled", error="cancelled", finished_at=datetime.utcnow().isoformat())
                raise
            except Exception as exc:
                self.db.update_run(run_id, state="failed", error=str(exc), finished_at=datetime.utcnow().isoformat())
                self.db.add_notification(f"MR {mode} fix failed", f"{ticket_key}: {exc}", "error", run_id, owner=self.owner)
                raise

    def rerun_ticket(self, ticket_key: str) -> None:
        self.db.enqueue(ticket_key, owner=self.owner)

    async def retry_run(self, failed_run_id: int) -> int:
        """Retry a failed/cancelled run in its existing workspace (no re-clone).

        Reuses the original clone, branch, and any partial changes the previous
        attempt left behind, then re-runs implementation -> review -> finish.
        """
        old = self.db.fetchone("SELECT * FROM runs WHERE id=? AND owner=?", (failed_run_id, self.owner))
        if not old:
            raise RuntimeError("Run not found")
        if old["state"] not in ("failed", "cancelled"):
            raise RuntimeError("Only failed or cancelled runs can be retried.")
        workspace = old["workspace_path"] or ""
        if not workspace or not Path(workspace).exists():
            raise RuntimeError("Original workspace is gone — use Rerun to start fresh from a new clone.")
        ticket_row = self.db.fetchone("SELECT * FROM tickets WHERE key=? AND owner=?", (old["ticket_key"], self.owner))
        if not ticket_row:
            raise RuntimeError("Ticket not found")
        if self.lock.locked():
            raise RuntimeError("A run is already in progress.")
        async with self.lock:
            ticket = dict(ticket_row)
            ticket["ticket_key"] = ticket_row["key"]
            branch = old["branch_name"] or branch_name(old["ticket_key"], ticket.get("summary", "work"))
            discovery = {
                "repo_url": old["repo_url"] or self.config.git.default_repo_url,
                "base_branch": old["base_branch"] or self.config.git.default_base_branch or "main",
                "summary": ticket.get("summary", "work"),
            }
            repo_path = Path(workspace)
            run_id = self.db.create_run(old["ticket_key"], owner=self.owner)
            self.db.update_run(
                run_id,
                repo_url=discovery["repo_url"],
                base_branch=discovery["base_branch"],
                branch_name=branch,
                workspace_path=workspace,
                progress=25,
            )
            self.claude = self._runner(run_id, lambda phase, line: self._log(run_id, phase, line))
            self._log(run_id, "git", f"Retrying run #{failed_run_id} in existing workspace {repo_path}")
            try:
                await self._implement_and_finish(run_id, ticket, repo_path, branch, discovery, None)
            except asyncio.CancelledError:
                self.db.update_run(run_id, state="cancelled", error="cancelled", finished_at=datetime.utcnow().isoformat())
                self.db.add_notification("Run cancelled", old["ticket_key"], "warning", run_id, owner=self.owner)
            except Exception as exc:
                friendly = self._friendly_error(str(exc))
                self._log(run_id, "error", friendly)
                self.db.update_run(run_id, state="failed", error=friendly, finished_at=datetime.utcnow().isoformat())
                self.db.add_notification("Run failed", f"{old['ticket_key']}: {friendly}", "error", run_id, owner=self.owner)
                self._notify_external("Run failed", f"{old['ticket_key']}: {friendly}", "error", run_id)
            finally:
                self.cancel_requested = False
        return run_id

    async def start_ticket(self, ticket_key: str) -> int:
        """Queue a ticket by key for an on-demand run, bypassing the Jira gate.

        Pulls the ticket from Jira when configured (so the agent has the real
        summary/description), but never requires it: an unknown key still
        becomes an eligible placeholder ticket so the run can proceed. Returns
        the queue item id to build, or 0 if it is already queued/running.
        """
        key = (ticket_key or "").strip().upper()
        if not key:
            raise RuntimeError("Enter a Jira ticket key to start a run.")
        existing = self.db.fetchone(
            "SELECT key FROM tickets WHERE key=? AND owner=?", (key, self.owner)
        )
        cfg = self.config.jira
        if cfg.url and cfg.email and cfg.token:
            try:
                fetched = await JiraClient(cfg).get_issue(key)
                fetched["eligibility"] = "eligible"
                fetched["skip_reason"] = ""
                self.db.upsert_ticket(fetched, owner=self.owner)
                existing = True
            except Exception as exc:
                print(f"Could not fetch {key} from Jira (using placeholder): {exc}")
        if not existing:
            self.db.upsert_ticket(
                {
                    "key": key,
                    "summary": f"Manual run for {key}",
                    "status": "Manual",
                    "url": "",
                    "description": f"Manually started from the dashboard for {key}.",
                    "labels": ["manual"],
                    "eligibility": "eligible",
                    "skip_reason": "",
                },
                owner=self.owner,
            )
        self.db.enqueue(key, owner=self.owner)
        item = self.db.fetchone(
            """
            SELECT id FROM queue_items
            WHERE ticket_key=? AND owner=?
              AND state IN ('needs_plan', 'plan_ready', 'queued')
            ORDER BY id DESC LIMIT 1
            """,
            (key, self.owner),
        )
        return int(item["id"]) if item else 0

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

    def _notify_external(self, title: str, message: str, level: str = "info", run_id: int | None = None) -> None:
        """Send outbound email/webhook for a run event (best-effort, never raises)."""
        try:
            errors = notify.dispatch(self.config.notify, title, message, level)
        except Exception as exc:  # noqa: BLE001 - notifications must not break runs
            errors = [str(exc)]
        for error in errors:
            if run_id is not None:
                self._log(run_id, "notify", f"Notification failed: {error}")
            else:
                print(f"Notification failed: {error}")

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
        self._notify_external("Agent is waiting for you", question, "warning", run_id)
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

    def _selected_skills_context(self, plan: Any) -> str:
        if not plan:
            return ""
        raw = plan["skill_ids"] if "skill_ids" in plan.keys() else ""
        skill_ids = [int(part) for part in str(raw or "").split(",") if part.strip().isdigit()]
        skills = self.db.skills_by_ids(skill_ids)
        if not skills:
            return ""
        body = "\n\n".join(f"## Skill: {row['name']}\n{row['content']}" for row in skills)
        return (
            "\n\nFollow these selected team skills (conventions, testing, review rules):\n" + body + "\n"
        )

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
    def _format_review_note(note: Any) -> str:
        lines = [
            f"NOTE_ID: {note['id']}",
            f"EXTERNAL_ID: {note['external_id']}",
            f"KIND: {note['kind']}",
            f"AUTHOR: {note['author']}",
            f"FILE: {note['file_path']}:{note['line']}",
        ]
        hunk = note["diff_hunk"] if "diff_hunk" in note.keys() else ""
        if hunk:
            lines.append("CODE FROM GIT:\n" + str(hunk))
        lines.append(f"BODY:\n{note['body']}")
        return "\n".join(lines)

    def _branch_diff_context(self, repo_path: Path, base_branch: str, limit: int = 12000) -> str:
        """The branch's own diff, read straight from git, to ground the fixes."""
        try:
            diff = self.git.review_diff(repo_path, base_branch)
        except Exception:
            return "(diff unavailable)"
        diff = diff.strip()
        if not diff:
            return "(no diff detected)"
        if len(diff) > limit:
            return diff[:limit] + "\n... (diff truncated)"
        return diff

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
