from __future__ import annotations

import asyncio
import shutil
import shlex
import subprocess
from datetime import datetime
from urllib.parse import quote

from collections.abc import Awaitable

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Config, load_config_data, load_config_text, save_config_text, write_config_data
from app.db import Database
from app.runner import Worker


templates = Jinja2Templates(directory="app/templates")


def create_app(config: Config, db: Database) -> FastAPI:
    app = FastAPI(title=config.ui.title)
    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    worker = Worker(config, db)
    app.state.worker = worker
    app.state.config = config
    app.state.db = db

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return templates.TemplateResponse(request, "index.html", context(request, db, request.app.state.config))

    @app.post("/jira/scan")
    async def jira_scan(request: Request):
        try:
            count = await worker.scan_jira()
            request.app.state.flash = f"Scanned {count} Jira tickets."
        except Exception as exc:
            request.app.state.flash = f"Jira scan failed: {exc}"
        return RedirectResponse("/", status_code=303)

    @app.post("/queue/run-once")
    async def run_once(request: Request):
        if db.queue_paused():
            request.app.state.flash = "Queue is paused. Resume it before running tickets."
        else:
            spawn_tracked_task(
                worker.run_next(),
                db,
                title="Run loop failed",
                message="Background run-once task crashed. Check logs for details.",
            )
            request.app.state.flash = "Run started."
        return RedirectResponse("/", status_code=303)

    @app.post("/queue/start-interval")
    async def start_interval(request: Request):
        worker.start_interval()
        request.app.state.flash = "Interval runner started."
        return RedirectResponse("/", status_code=303)

    @app.post("/queue/stop-interval")
    async def stop_interval(request: Request):
        worker.stop_interval()
        request.app.state.flash = "Interval runner stopped."
        return RedirectResponse("/", status_code=303)

    @app.post("/queue/pause")
    async def pause_queue(request: Request):
        db.set_state("queue_paused", "1")
        request.app.state.flash = "Queue paused. Jira scans can still run."
        return RedirectResponse("/", status_code=303)

    @app.post("/queue/resume")
    async def resume_queue(request: Request):
        db.set_state("queue_paused", "0")
        request.app.state.flash = "Queue resumed."
        return RedirectResponse("/", status_code=303)

    @app.post("/queue/reorder")
    async def reorder_queue(order: str = Form("")):
        ids = [int(value) for value in order.split(",") if value.strip().isdigit()]
        db.reorder_queue(ids)
        return RedirectResponse("/", status_code=303)

    @app.post("/tickets/{ticket_key}/enqueue")
    async def enqueue_ticket(ticket_key: str):
        db.enqueue(ticket_key)
        return RedirectResponse("/", status_code=303)

    @app.post("/tickets/{ticket_key}/rerun")
    async def rerun_ticket(ticket_key: str):
        worker.rerun_ticket(ticket_key)
        return RedirectResponse("/", status_code=303)

    @app.post("/dry-run/enqueue")
    async def enqueue_test_ticket(request: Request):
        ticket_key = "LOCAL-" + datetime.utcnow().strftime("%Y%m%d%H%M%S")
        db.upsert_ticket(
            {
                "key": ticket_key,
                "summary": "Local dry-run ticket",
                "status": "To Do",
                "url": "",
                "description": (
                    "Dry-run ticket created from the dashboard. Use this to verify the Git and Claude "
                    "worker flow without connecting Jira."
                ),
                "labels": ["dry-run", "local"],
                "eligibility": "eligible",
                "skip_reason": "",
            }
        )
        db.enqueue(ticket_key)
        request.app.state.flash = f"Created dry-run ticket {ticket_key}."
        return RedirectResponse("/", status_code=303)

    @app.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: int, request: Request):
        active_run = db.fetchone(
            "SELECT id FROM runs WHERE state IN ('preparing_git','running_claude','reviewing') ORDER BY id DESC LIMIT 1"
        )
        if not active_run:
            request.app.state.flash = "No active run to cancel."
            return RedirectResponse("/", status_code=303)

        active_run_id = int(active_run["id"])
        if active_run_id != run_id:
            request.app.state.flash = f"Cancel ignored: run #{run_id} is not active. Active run is #{active_run_id}."
            return RedirectResponse(f"/runs/{active_run_id}", status_code=303)

        worker.cancel_current()
        db.update_run(active_run_id, state="cancelled", error="cancel requested")
        request.app.state.flash = f"Cancel requested for run #{active_run_id}."
        return RedirectResponse(f"/runs/{active_run_id}", status_code=303)

    @app.post("/runs/{run_id}/review-fix")
    async def review_fix(run_id: int, request: Request):
        spawn_tracked_task(
            worker.run_cr_fix(run_id),
            db,
            title="CR fix failed",
            message=f"Background CR fix crashed for run #{run_id}.",
            run_id=run_id,
        )
        request.app.state.flash = "CR fix started."
        return RedirectResponse("/", status_code=303)

    @app.post("/runs/{run_id}/push")
    async def push_run(run_id: int, request: Request):
        try:
            worker.push_run(run_id)
            request.app.state.flash = "Branch pushed."
        except Exception as exc:
            request.app.state.flash = f"Push failed: {exc}"
        return RedirectResponse("/", status_code=303)

    @app.get("/notifications/unread")
    async def unread_notifications():
        rows = db.unread_notifications()
        db.mark_notifications_read([int(row["id"]) for row in rows])
        return JSONResponse(
            [
                {
                    "id": row["id"],
                    "run_id": row["run_id"],
                    "level": row["level"],
                    "title": row["title"],
                    "message": row["message"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_detail(run_id: int, request: Request):
        detail = run_detail_context(request, db, request.app.state.config, run_id)
        if detail["run"] is None:
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "run_detail.html", detail)

    @app.post("/runs/{run_id}/input")
    async def add_run_input(run_id: int, message: str = Form(...)):
        clean = message.strip()
        if clean:
            db.add_agent_input(run_id, clean)
            db.add_log(run_id, "user", clean)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/agent-questions/{question_id}/answer")
    async def answer_agent_question(
        question_id: int,
        run_id: int = Form(...),
        selected_answer: str = Form(""),
        free_answer: str = Form(""),
    ):
        free_answer = free_answer.strip()
        selected_answer = selected_answer.strip()
        answer = free_answer or selected_answer
        source = "free_input" if free_answer else "option"
        if answer:
            db.answer_agent_question(question_id, answer, source)
            db.add_log(run_id, "user", f"Answered question #{question_id}: {answer}")
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.get("/runs/{run_id}/logs", response_class=PlainTextResponse)
    async def run_logs(run_id: int):
        rows = db.fetchall("SELECT phase, line FROM logs WHERE run_id=? ORDER BY id", (run_id,))
        return "\n".join(f"[{row['phase']}] {row['line']}" for row in rows)

    @app.get("/partials/status", response_class=HTMLResponse)
    async def status_partial(request: Request):
        return templates.TemplateResponse(request, "_status.html", context(request, db, request.app.state.config))

    @app.get("/settings", response_class=HTMLResponse)
    async def settings(request: Request):
        active_config = request.app.state.config
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "request": request,
                "config": active_config,
                "config_text": load_config_text(),
                "title": active_config.ui.title,
                "flash": pop_flash(request),
                "claude_args_text": "\n".join(active_config.claude.args),
                "excluded_statuses_text": "\n".join(active_config.jira.excluded_statuses),
            },
        )

    @app.post("/settings/test/{target}")
    async def test_connection(target: str, request: Request):
        try:
            message = await run_connection_test(target, request.app.state.config)
            request.app.state.flash = message
        except Exception as exc:
            request.app.state.flash = f"{target} test failed: {exc}"
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings")
    async def save_settings(request: Request, config_text: str = Form(...)):
        try:
            new_config = save_config_text(config_text)
            request.app.state.config = new_config
            worker.config = new_config
            worker.git.config = new_config
            request.app.state.flash = "Config saved."
        except Exception as exc:
            request.app.state.flash = f"Config save failed: {exc}"
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/visual")
    async def save_visual_settings(
        request: Request,
        app_host: str = Form(...),
        app_port: int = Form(...),
        database_path: str = Form(...),
        workspace_dir: str = Form(...),
        interval_seconds: int = Form(...),
        clone_retention_limit: int = Form(...),
        jira_url: str = Form(...),
        jira_email: str = Form(...),
        jira_token: str = Form(...),
        jira_jql: str = Form(...),
        excluded_statuses: str = Form(""),
        required_text: str = Form(""),
        max_results: int = Form(...),
        git_username: str = Form(""),
        git_token: str = Form(""),
        git_remote_name: str = Form(...),
        claude_command: str = Form(...),
        claude_args: str = Form(""),
        claude_model: str = Form(""),
        claude_api_key: str = Form(""),
        claude_timeout_seconds: int = Form(...),
        allow_cr_fix: str | None = Form(None),
        auto_cr_fix: str | None = Form(None),
        ui_title: str = Form(...),
    ):
        try:
            data = load_config_data()
            data.setdefault("app", {}).update(
                {
                    "host": app_host,
                    "port": app_port,
                    "database_path": database_path,
                    "workspace_dir": workspace_dir,
                    "interval_seconds": interval_seconds,
                    "clone_retention_limit": clone_retention_limit,
                }
            )
            data.setdefault("jira", {}).update(
                {
                    "url": jira_url,
                    "email": jira_email,
                    "token": jira_token,
                    "jql": jira_jql,
                    "excluded_statuses": split_lines(excluded_statuses),
                    "required_text": required_text,
                    "max_results": max_results,
                }
            )
            data.setdefault("git", {}).update(
                {
                    "username": git_username,
                    "token": git_token,
                    "remote_name": git_remote_name,
                }
            )
            data.setdefault("claude", {}).update(
                {
                    "command": claude_command,
                    "args": split_lines(claude_args),
                    "model": claude_model,
                    "api_key": claude_api_key,
                    "timeout_seconds": claude_timeout_seconds,
                    "allow_cr_fix": allow_cr_fix == "on",
                    "auto_cr_fix": auto_cr_fix == "on",
                }
            )
            data.setdefault("ui", {}).update({"title": ui_title})
            new_config = write_config_data(data)
            request.app.state.config = new_config
            worker.config = new_config
            worker.git.config = new_config
            request.app.state.flash = "Config saved."
        except Exception as exc:
            request.app.state.flash = f"Config save failed: {exc}"
        return RedirectResponse("/settings", status_code=303)

    return app


def context(request: Request, db: Database, config: Config) -> dict:
    current = db.fetchone("SELECT * FROM runs WHERE state IN ('preparing_git','running_claude','reviewing') ORDER BY id DESC LIMIT 1")
    current_ticket = None
    current_logs = []
    if current:
        current_ticket = db.fetchone("SELECT * FROM tickets WHERE key=?", (current["ticket_key"],))
        current_logs = db.fetchall("SELECT * FROM logs WHERE run_id=? ORDER BY id DESC LIMIT 120", (current["id"],))
        current_logs = list(reversed(current_logs))
    current_questions = []
    current_sub_agents = []
    if current:
        current_questions = db.fetchall(
            "SELECT * FROM agent_questions WHERE run_id=? AND state='pending' ORDER BY id",
            (current["id"],),
        )
        current_sub_agents = db.fetchall("SELECT * FROM sub_agents WHERE run_id=? ORDER BY updated_at DESC", (current["id"],))
    flash = pop_flash(request)
    return {
        "request": request,
        "title": config.ui.title,
        "flash": flash,
        "current": current,
        "current_ticket": current_ticket,
        "current_logs": current_logs,
        "current_questions": current_questions,
        "current_sub_agents": current_sub_agents,
        "queue_paused": db.queue_paused(),
        "queue": db.fetchall(
            """
            SELECT q.*, t.summary, t.status
            FROM queue_items q JOIN tickets t ON t.key=q.ticket_key
            WHERE q.state IN ('queued', 'running')
            ORDER BY q.priority ASC, q.id ASC
            """
        ),
        "skipped": db.fetchall("SELECT * FROM tickets WHERE eligibility='skipped' ORDER BY updated_at DESC LIMIT 50"),
        "manual": db.fetchall("SELECT * FROM tickets WHERE eligibility!='eligible' ORDER BY updated_at DESC LIMIT 50"),
        "done": db.fetchall(
            """
            SELECT r.*, t.summary
            FROM runs r LEFT JOIN tickets t ON t.key=r.ticket_key
            WHERE r.state IN ('done','needs_cr_fix','pushed','failed','cancelled')
            ORDER BY r.id DESC LIMIT 50
            """
        ),
        "notifications": db.fetchall("SELECT * FROM notifications ORDER BY id DESC LIMIT 10"),
        "allow_cr_fix": config.claude.allow_cr_fix,
        "interval_running": bool(request.app.state.worker.interval_task and not request.app.state.worker.interval_task.done()),
        "setup_status": setup_status(config),
    }


def setup_status(config: Config) -> list[dict[str, str | bool]]:
    try:
        claude_command = shlex.split(config.claude.command)[0] if config.claude.command.strip() else ""
    except ValueError:
        claude_command = ""
    jira_ready = (
        _real_value(config.jira.url, ["your-domain.atlassian.net"])
        and _real_value(config.jira.email, ["you@example.com"])
        and _real_value(config.jira.token, ["paste-jira-token-here"])
    )
    return [
        {
            "name": "Jira",
            "ok": jira_ready,
            "detail": "Configured" if jira_ready else "Missing real URL, email, or token",
        },
        {
            "name": "Git",
            "ok": bool(shutil.which("git") and config.git.default_repo_url),
            "detail": config.git.default_repo_url or "Missing git.default_repo_url",
        },
        {
            "name": "Claude",
            "ok": bool(claude_command and shutil.which(claude_command)),
            "detail": claude_command or "Missing claude.command",
        },
    ]


def _real_value(value: str, placeholders: list[str]) -> bool:
    clean = (value or "").strip().lower()
    return bool(clean) and all(placeholder not in clean for placeholder in placeholders)


def run_detail_context(request: Request, db: Database, config: Config, run_id: int) -> dict:
    run = db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
    ticket = db.fetchone("SELECT * FROM tickets WHERE key=?", (run["ticket_key"],)) if run else None
    return {
        "request": request,
        "title": config.ui.title,
        "flash": pop_flash(request),
        "run": run,
        "ticket": ticket,
        "logs": db.fetchall("SELECT * FROM logs WHERE run_id=? ORDER BY id", (run_id,)),
        "questions": db.fetchall("SELECT * FROM agent_questions WHERE run_id=? ORDER BY id DESC", (run_id,)),
        "pending_questions": db.fetchall(
            "SELECT * FROM agent_questions WHERE run_id=? AND state='pending' ORDER BY id",
            (run_id,),
        ),
        "agent_inputs": db.fetchall("SELECT * FROM agent_inputs WHERE run_id=? ORDER BY id DESC", (run_id,)),
        "sub_agents": db.fetchall("SELECT * FROM sub_agents WHERE run_id=? ORDER BY updated_at DESC", (run_id,)),
        "ide_url": build_ide_url(run_id, run),
    }


def pop_flash(request: Request) -> str:
    flash = getattr(request.app.state, "flash", "")
    request.app.state.flash = ""
    return flash


def split_lines(value: str) -> list[str]:
    return [line.strip() for line in value.replace(",", "\n").splitlines() if line.strip()]


def build_ide_url(run_id: int, run) -> str:
    try:
        template = (load_config_data().get("ui") or {}).get("ide_url_template", "")
    except Exception:
        return ""
    if not template or not run:
        return ""
    workspace_path = run["workspace_path"] or ""
    try:
        return template.format(
            run_id=run_id,
            ticket_key=quote(run["ticket_key"] or ""),
            workspace_path=quote(workspace_path),
            workspace_path_raw=workspace_path,
            branch_name=quote(run["branch_name"] or ""),
        )
    except (KeyError, ValueError):
        return ""


async def run_connection_test(target: str, config: Config) -> str:
    if target == "jira":
        import httpx

        if not config.jira.url or not config.jira.email or not config.jira.token:
            raise RuntimeError("Jira URL, email, and token are required")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                config.jira.url.rstrip("/") + "/rest/api/3/myself",
                auth=(config.jira.email, config.jira.token),
            )
            resp.raise_for_status()
        return "Jira connection OK."

    if target == "git":
        data = load_config_data()
        test_repo = ((data.get("git") or {}).get("test_repo_url") or "").strip()
        if test_repo:
            await run_command(["git", "ls-remote", test_repo, "HEAD"], timeout=30)
            return "Git remote/auth test OK."
        version = await run_command(["git", "--version"], timeout=10)
        return f"Git command OK. Add git.test_repo_url in raw YAML to test remote auth. {version.strip()}"

    if target == "claude":
        command = shlex.split(config.claude.command) + ["--version"]
        output = await run_command(command, timeout=15)
        return f"Claude command OK. {output.strip()}"

    raise RuntimeError("Unknown connection test")


async def run_command(args: list[str], timeout: int) -> str:
    def _run() -> str:
        proc = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
        output = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode != 0:
            raise RuntimeError(output or f"{args[0]} exited with {proc.returncode}")
        return output

    return await asyncio.to_thread(_run)


def spawn_tracked_task(
    coro: Awaitable[object],
    db: Database,
    *,
    title: str,
    message: str,
    run_id: int | None = None,
) -> asyncio.Task[object]:
    task = asyncio.create_task(coro)

    def _on_done(done_task: asyncio.Task[object]) -> None:
        try:
            done_task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            error_message = f"{message} Error: {exc}"
            db.add_notification(title, error_message, "error", run_id)
            print(error_message)

    task.add_done_callback(_on_done)
    return task
