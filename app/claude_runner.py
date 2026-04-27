from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import contextlib
from pathlib import Path
from typing import Callable

from app.config import Config, secret_values
from app.utils import mask_secrets


LogFn = Callable[[str, str], None]


class ClaudeRunner:
    def __init__(self, config: Config, log: LogFn):
        self.config = config
        self.log = log
        self.current_process: asyncio.subprocess.Process | None = None

    def command(self) -> list[str]:
        base = shlex.split(self.config.claude.command)
        args = list(self.config.claude.args)
        if self.config.claude.model and "--model" not in args:
            args.extend(["--model", self.config.claude.model])
        return base + args

    async def run_prompt(self, phase: str, prompt: str, cwd: str | Path | None = None) -> str:
        env = os.environ.copy()
        if self.config.claude.api_key:
            env["ANTHROPIC_API_KEY"] = self.config.claude.api_key
        cmd = self.command()
        self.log(phase, f"$ {' '.join(shlex.quote(part) for part in cmd)}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd) if cwd else None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        self.current_process = proc
        assert proc.stdin is not None
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        output: list[str] = []
        assert proc.stdout is not None
        try:
            while True:
                line = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=max(1, int(self.config.claude.timeout_seconds)),
                )
                if not line:
                    break
                text = mask_secrets(line.decode("utf-8", errors="replace").rstrip(), secret_values(self.config))
                output.append(text)
                self.log(phase, text)
        except asyncio.TimeoutError as exc:
            self.cancel()
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise RuntimeError("Claude timed out") from exc

        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        finally:
            self.current_process = None
        if proc.returncode != 0:
            raise RuntimeError(f"Claude exited with code {proc.returncode}")
        return "\n".join(output)

    def cancel(self) -> None:
        if self.current_process and self.current_process.returncode is None:
            self.current_process.terminate()


def parse_discovery(output: str) -> dict[str, str]:
    match = re.search(r"\{.*\}", output, re.DOTALL)
    if not match:
        raise ValueError("Claude did not return JSON discovery data")
    data = json.loads(match.group(0))
    return {
        "repo_url": str(data.get("repo_url", "")).strip(),
        "base_branch": str(data.get("base_branch", "")).strip(),
        "summary": str(data.get("summary", data.get("branch_summary", ""))).strip(),
    }


def discovery_prompt(ticket: dict[str, str], default_repo_url: str = "", default_base_branch: str = "main") -> str:
    default_repo = default_repo_url or "unknown"
    default_base = default_base_branch or "main"
    return f"""
You are helping a local automation prepare a Jira ticket for implementation.
Return only JSON with keys: repo_url, base_branch, summary.
The Python worker will do all Git operations after reading this JSON.
Use this configured default repo_url unless the ticket explicitly names a different repository:
{default_repo}
Use this configured default base_branch unless the ticket explicitly names a different base branch:
{default_base}
The summary must be a short branch-safe English phrase used by Python to create the feature branch.
Do not include Markdown, commentary, code fences, commands, or any keys other than repo_url, base_branch, summary.

Ticket: {ticket['key']}
Title: {ticket.get('summary', '')}
Status: {ticket.get('status', '')}
URL: {ticket.get('url', '')}
Description:
{ticket.get('description', '')}
"""


def implementation_prompt(ticket: dict[str, str], branch: str) -> str:
    return f"""
You are Claude Code working on Jira ticket {ticket['key']} on branch {branch}.
Implement the requested change in this working tree.

Important rules:
- Do not run git commands.
- Do not create, checkout, merge, commit, rebase, reset, or push branches.
- Python automation owns all Git interactions.
- You may spawn subagents when the ticket naturally splits across independent components.
- Update progress in plain text when major phases complete using lines like: PROGRESS 40%.

Ticket title: {ticket.get('summary', '')}
Ticket URL: {ticket.get('url', '')}
Ticket description:
{ticket.get('description', '')}
"""


def review_prompt(ticket: dict[str, str]) -> str:
    return f"""
Review the completed work for Jira ticket {ticket['key']}.
Look for bugs, missing tests, regressions, and incomplete requirements.
Do not run git commands.
Start the response with exactly one marker line:
REVIEW_RESULT: pass
or
REVIEW_RESULT: needs_fix
Use pass only when there are no actionable findings.
After the marker, return a concise review with findings first. If there are no findings, say so clearly.
"""


def cr_fix_prompt(ticket: dict[str, str], review_output: str) -> str:
    return f"""
Fix the review findings for Jira ticket {ticket['key']}.
Do not run git commands. Python automation owns all Git interactions.

Review findings:
{review_output}
"""
