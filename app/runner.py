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
        if self.lock.locked():
            return None
        async with self.lock:
            item = self.db.next_queue_item()
            if not item:
                return None
            self.db.set_queue_state(int(item["id"]), "running")
            run_id = self.db.create_run(item["ticket_key"])
            try:
                await self._run_ticket(run_id, item)
                self.db.set_queue_state(int(item["id"]), "done")
            except asyncio.CancelledError:
                self.db.update_run(run_id, state="cancelled", error="cancelled", finished_at=datetime.utcnow().isoformat())
                self.db.set_queue_state(int(item["id"]), "cancelled")
            except Exception as exc:
                self._log(run_id, "error", str(exc))
                self.db.update_run(run_id, state="failed", error=str(exc), finished_at=datetime.utcnow().isoformat())
                self.db.set_queue_state(int(item["id"]), "failed")
            finally:
                self.cancel_requested = False
            return run_id

    async def _run_ticket(self, run_id: int, item: Any) -> None:
        ticket = dict(item)
        self.db.update_run(run_id, state="preparing_git", progress=5)
        self.claude = ClaudeRunner(self.config, lambda phase, line: self._log(run_id, phase, line))

        discovery_output = await self.claude.run_prompt("discover", discovery_prompt(ticket))
        discovery = parse_discovery(discovery_output)
        if not discovery["repo_url"]:
            raise RuntimeError("Claude discovery did not provide repo_url")
        summary = discovery["summary"] or ticket.get("summary", "work")
        branch = branch_name(ticket["ticket_key"], summary)
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
        impl_output = await self.claude.run_prompt("claude", implementation_prompt(ticket, branch), cwd=repo_path)
        self.db.update_run(run_id, progress=max(70, self._progress_from_output(impl_output, 70)))

        self._raise_if_cancelled()
        self.db.update_run(run_id, state="reviewing", progress=82)
        review_output = await self.claude.run_prompt("review", review_prompt(ticket), cwd=repo_path)
        self.db.update_run(run_id, review_output=review_output, state="needs_cr_fix", progress=92)

        if self.config.claude.auto_cr_fix and self.config.claude.allow_cr_fix:
            await self.run_cr_fix(run_id)
        else:
            self.db.update_run(run_id, state="done", progress=100, finished_at=datetime.utcnow().isoformat())

    async def run_cr_fix(self, run_id: int) -> None:
        run = self.db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
        ticket = self.db.fetchone("SELECT * FROM tickets WHERE key=?", (run["ticket_key"],)) if run else None
        if not run or not ticket:
            raise RuntimeError("Run or ticket not found")
        if not self.config.claude.allow_cr_fix:
            raise RuntimeError("CR fix is disabled in config")
        self.claude = ClaudeRunner(self.config, lambda phase, line: self._log(run_id, phase, line))
        self.db.update_run(run_id, state="running_claude", progress=94)
        await self.claude.run_prompt("cr-fix", cr_fix_prompt(dict(ticket), run["review_output"]), cwd=run["workspace_path"])
        review_output = await self.claude.run_prompt("review", review_prompt(dict(ticket)), cwd=run["workspace_path"])
        self.db.update_run(
            run_id,
            review_output=review_output,
            state="done",
            progress=100,
            finished_at=datetime.utcnow().isoformat(),
        )

    def push_run(self, run_id: int) -> str:
        run = self.db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run:
            raise RuntimeError("Run not found")
        if run["state"] not in ("done", "needs_cr_fix", "pushed"):
            raise RuntimeError("Run must be done before pushing")
        output = self.git.push_branch(Path(run["workspace_path"]), run["branch_name"])
        self.db.update_run(run_id, state="pushed", pushed_at=datetime.utcnow().isoformat())
        self._log(run_id, "git", output or "pushed")
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
