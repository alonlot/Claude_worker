from __future__ import annotations

import asyncio
import json
import re
import shlex
import subprocess
import contextlib
from pathlib import Path
from typing import Callable

from app.config import Config, secret_values
from app.execution import ExecutionBackend, Invocation, SubprocessBackend, get_execution_backend
from app.utils import mask_secrets


LogFn = Callable[[str, str], None]


class ClaudeRunner:
    def __init__(
        self,
        config: Config,
        log: LogFn,
        backend: ExecutionBackend | None = None,
        label_prefix: str = "claude",
    ):
        self.config = config
        self.log = log
        self.backend = backend or get_execution_backend(config)
        self.label_prefix = label_prefix
        self.current_process: asyncio.subprocess.Process | None = None
        self._cancel_argv: list[str] | None = None

    def command(self) -> list[str]:
        base = shlex.split(self.config.claude.command)
        args = list(self.config.claude.args)
        if self.config.claude.model and "--model" not in args:
            args.extend(["--model", self.config.claude.model])
        return base + args

    def _agent_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.config.claude.api_key:
            env["ANTHROPIC_API_KEY"] = self.config.claude.api_key
        return env

    async def run_prompt(self, phase: str, prompt: str, cwd: str | Path | None = None) -> str:
        label = f"{self.label_prefix}_{phase}"
        invocation: Invocation = self.backend.build(self.command(), cwd, self._agent_env(), label)
        self._cancel_argv = invocation.cancel_argv
        display = " ".join(shlex.quote(part) for part in invocation.argv)
        self.log(phase, f"[{self.backend.name}] $ {display}")
        proc = await asyncio.create_subprocess_exec(
            *invocation.argv,
            cwd=invocation.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=invocation.env,
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
            self._cancel_argv = None
        if proc.returncode != 0:
            raise RuntimeError(f"Claude exited with code {proc.returncode}")
        return "\n".join(output)

    def cancel(self) -> None:
        # In Docker mode the spawned process is the `docker run` client; stopping
        # the container itself requires `docker kill`.
        if self._cancel_argv:
            with contextlib.suppress(Exception):
                subprocess.run(self._cancel_argv, capture_output=True, timeout=15)
        if self.current_process and self.current_process.returncode is None:
            self.current_process.terminate()


def parse_ask_user(output: str) -> list[dict[str, object]]:
    """Find ASK_USER marker lines emitted by the agent.

    Protocol (documented in implementation_prompt):
        ASK_USER: question text || option A || option B || option C

    Options are optional; up to three are kept. Returns one dict per question
    with keys "question" and "options".
    """
    questions: list[dict[str, object]] = []
    for line in output.splitlines():
        match = re.match(r"\s*ASK_USER:\s*(.+)$", line)
        if not match:
            continue
        parts = [part.strip() for part in match.group(1).split("||")]
        question = parts[0].strip()
        options = [opt for opt in parts[1:] if opt][:3]
        if question:
            questions.append({"question": question, "options": options})
    return questions


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


def parse_plan(output: str) -> dict[str, str]:
    match = re.search(r"\{.*\}", output, re.DOTALL)
    if not match:
        raise ValueError("Claude did not return JSON plan data")
    data = json.loads(match.group(0))
    return {
        "repo_url": str(data.get("repo_url", "")).strip(),
        "base_branch": str(data.get("base_branch", "")).strip(),
        "summary": str(data.get("summary", data.get("branch_summary", ""))).strip(),
        "mission": str(data.get("mission", "")).strip(),
        "plan_text": str(data.get("plan_text", data.get("plan", ""))).strip(),
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


def planning_prompt(
    ticket: dict[str, str],
    default_repo_url: str = "",
    default_base_branch: str = "main",
    previous_plan: str = "",
    user_notes: str = "",
) -> str:
    return f"""
You are preparing a human-approved build plan for a Jira automation worker.
Return only JSON with keys: repo_url, base_branch, summary, mission, plan_text.

The Python worker owns every Git command. Claude must not clone, checkout, commit, push, rebase, merge, reset, or run Git.
Use this configured default repo_url unless the ticket or user notes explicitly say otherwise:
{default_repo_url or "unknown"}
Use this configured default base_branch unless the ticket or user notes explicitly say otherwise:
{default_base_branch or "main"}

mission: explain in plain language what you think the ticket asks you to build.
plan_text: concise implementation plan, risks, likely files/areas, validation approach, and any assumptions.
summary: short branch-safe phrase for Python to create the final branch name.

If user_notes are provided, revise the previous plan to reflect them and return the full updated plan.

Ticket: {ticket['key']}
Title: {ticket.get('summary', '')}
Status: {ticket.get('status', '')}
URL: {ticket.get('url', '')}
Description:
{ticket.get('description', '')}

Previous plan:
{previous_plan}

User requested changes:
{user_notes}
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
- If you need a decision from the user before you can proceed correctly, ask by printing a line:
      ASK_USER: your question || option A || option B || option C
  The options are optional. The worker will pause, collect the user's answer, and
  give it to you in a follow-up message. Ask only when it genuinely blocks you.

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
