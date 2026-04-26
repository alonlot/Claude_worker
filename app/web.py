from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
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
        return templates.TemplateResponse("index.html", context(request, db, request.app.state.config))

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
        asyncio.create_task(worker.run_next())
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

    @app.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: int):
        worker.cancel_current()
        db.update_run(run_id, state="cancelled", error="cancel requested")
        return RedirectResponse("/", status_code=303)

    @app.post("/runs/{run_id}/review-fix")
    async def review_fix(run_id: int, request: Request):
        asyncio.create_task(worker.run_cr_fix(run_id))
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

    @app.get("/runs/{run_id}/logs", response_class=PlainTextResponse)
    async def run_logs(run_id: int):
        rows = db.fetchall("SELECT phase, line FROM logs WHERE run_id=? ORDER BY id", (run_id,))
        return "\n".join(f"[{row['phase']}] {row['line']}" for row in rows)

    @app.get("/partials/status", response_class=HTMLResponse)
    async def status_partial(request: Request):
        return templates.TemplateResponse("_status.html", context(request, db, request.app.state.config))

    @app.get("/settings", response_class=HTMLResponse)
    async def settings(request: Request):
        active_config = request.app.state.config
        return templates.TemplateResponse(
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
    flash = pop_flash(request)
    return {
        "request": request,
        "title": config.ui.title,
        "flash": flash,
        "current": current,
        "current_ticket": current_ticket,
        "current_logs": current_logs,
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
        "done": db.fetchall("SELECT * FROM runs WHERE state IN ('done','needs_cr_fix','pushed','failed','cancelled') ORDER BY id DESC LIMIT 50"),
        "allow_cr_fix": config.claude.allow_cr_fix,
        "interval_running": bool(request.app.state.worker.interval_task and not request.app.state.worker.interval_task.done()),
    }


def pop_flash(request: Request) -> str:
    flash = getattr(request.app.state, "flash", "")
    request.app.state.flash = ""
    return flash


def split_lines(value: str) -> list[str]:
    return [line.strip() for line in value.replace(",", "\n").splitlines() if line.strip()]
