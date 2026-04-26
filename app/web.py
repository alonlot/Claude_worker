from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Config, load_config, load_config_text, save_config_text
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
        return templates.TemplateResponse("index.html", context(request, db, config))

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

    @app.post("/demo/running")
    async def demo_running_ticket(request: Request):
        create_demo_running_ticket(db)
        request.app.state.flash = "Demo running ticket created."
        return RedirectResponse("/", status_code=303)

    @app.post("/demo/clear")
    async def clear_demo_ticket(request: Request):
        clear_demo_running_ticket(db)
        request.app.state.flash = "Demo ticket cleared."
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
        return templates.TemplateResponse("_status.html", context(request, db, config))

    @app.get("/settings", response_class=HTMLResponse)
    async def settings(request: Request):
        return templates.TemplateResponse(
            "settings.html",
            {"request": request, "config_text": load_config_text(), "title": config.ui.title},
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

    return app


def context(request: Request, db: Database, config: Config) -> dict:
    current = db.fetchone("SELECT * FROM runs WHERE state IN ('preparing_git','running_claude','reviewing') ORDER BY id DESC LIMIT 1")
    current_ticket = None
    current_logs = []
    if current:
        current_ticket = db.fetchone("SELECT * FROM tickets WHERE key=?", (current["ticket_key"],))
        current_logs = db.fetchall("SELECT * FROM logs WHERE run_id=? ORDER BY id DESC LIMIT 120", (current["id"],))
        current_logs = list(reversed(current_logs))
    flash = getattr(request.app.state, "flash", "")
    request.app.state.flash = ""
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


def create_demo_running_ticket(db: Database) -> int:
    clear_demo_running_ticket(db)
    db.upsert_ticket(
        {
            "key": "DEMO-101",
            "summary": "Preview active ticket layout",
            "status": "In Progress",
            "url": "https://jira.local/browse/DEMO-101",
            "description": (
                "This is a temporary preview ticket. It shows how the dashboard will look while Claude is working: "
                "ticket number, name, status, a compact description, live progress, branch metadata, and terminal output."
            ),
            "labels": ["demo", "preview"],
            "eligibility": "eligible",
            "skip_reason": "",
        }
    )
    run_id = db.create_run("DEMO-101")
    db.update_run(
        run_id,
        state="running_claude",
        progress=47,
        repo_url="git@gitlab.com:example/demo-repo.git",
        base_branch="main",
        branch_name="DEMO-101/by_claude_preview_active_ticket_layout",
        workspace_path="workspaces/DEMO-101",
    )
    for phase, line in [
        ("discover", "Resolved repo git@gitlab.com:example/demo-repo.git and base branch main."),
        ("git", "checked out DEMO-101/by_claude_preview_active_ticket_layout"),
        ("claude", "Reading ticket requirements and locating the relevant UI files."),
        ("claude", "PROGRESS 47%"),
        ("claude", "Updating the dashboard view and preparing a focused test pass."),
    ]:
        db.add_log(run_id, phase, line)
    return run_id


def clear_demo_running_ticket(db: Database) -> None:
    demo_runs = db.fetchall("SELECT id FROM runs WHERE ticket_key='DEMO-101'")
    for run in demo_runs:
        db.execute("DELETE FROM logs WHERE run_id=?", (run["id"],))
    db.execute("DELETE FROM runs WHERE ticket_key='DEMO-101'")
    db.execute("DELETE FROM queue_items WHERE ticket_key='DEMO-101'")
    db.execute("DELETE FROM tickets WHERE key='DEMO-101'")
