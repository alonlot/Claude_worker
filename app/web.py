from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import shlex
import subprocess
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from collections.abc import Awaitable

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.auth import LoginPageAuthProvider, User, get_auth_provider
from app.config import (
    Config,
    DEFAULT_OWNER,
    load_config_data,
    load_config_text,
    save_config_text,
)
from app.code_review import scan_ci_jobs, scan_review_notes, suggest_review_url
from app.db import Database
from app.jira_client import JiraClient
from app import notify
from app.demo import demo_ci_jobs_for_display
from app.execution import docker_preflight
from app.runner import WorkerRegistry
from app.utils import ensure_child_path


templates = Jinja2Templates(directory="app/templates")

# Paths reachable without authentication.
PUBLIC_PATHS = {"/login", "/logout", "/healthz"}


def create_app(config: Config, db: Database) -> FastAPI:
    app = FastAPI(title=config.ui.title)
    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    registry = WorkerRegistry(config, db)
    provider = get_auth_provider(config, db)
    app.state.registry = registry
    app.state.provider = provider
    app.state.config = config
    app.state.db = db
    app.state.flash = ""
    recovered = db.recover_interrupted_work()
    if recovered:
        app.state.flash = f"Recovered {recovered} interrupted run(s) as failed after restart."

    @app.middleware("http")
    async def auth_gate(request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static"):
            return await call_next(request)
        user = request.app.state.provider.authenticate(request)
        if user is None:
            if getattr(request.app.state.provider, "requires_login_page", False):
                return RedirectResponse("/login", status_code=303)
            return PlainTextResponse("Unauthorized", status_code=401)
        request.state.user = user
        return await call_next(request)

    # SessionMiddleware is added last so it wraps the auth gate: request.session
    # is populated before the login-page provider reads it.
    app.add_middleware(SessionMiddleware, secret_key=config.auth.session_secret)

    def current_user(request: Request) -> User:
        return getattr(request.state, "user", User(DEFAULT_OWNER, DEFAULT_OWNER))

    def owner_of(request: Request) -> str:
        return current_user(request).username

    def worker_for(request: Request):
        return registry.for_user(owner_of(request))

    def owned_run(run_id: int, owner: str):
        return db.fetchone("SELECT * FROM runs WHERE id=? AND owner=?", (run_id, owner))

    def do_code_review_scan(run_id: int, source_url: str, auto_cr_on: bool, scan_mode: str, owner: str) -> str:
        """Returns a flash message. scan_mode is notes, ci, or both."""
        source_url = source_url.strip()
        if not source_url:
            return "Paste a pull request or merge request URL before scanning."
        mode = scan_mode if scan_mode in ("notes", "ci", "both") else "both"
        db.set_state(f"review_source_url:{run_id}", source_url, owner=owner)
        db.set_state(f"auto_cr:{run_id}", "1" if auto_cr_on else "0", owner=owner)
        try:
            notes: list = []
            ci_jobs: list = []
            if mode in ("notes", "both"):
                notes = scan_review_notes(source_url)
                for note in notes:
                    db.upsert_code_review_note(run_id, note, owner=owner)
            if mode in ("ci", "both"):
                ci_jobs = scan_ci_jobs(source_url)
                db.set_state(f"ci_jobs:{run_id}", json.dumps(ci_jobs), owner=owner)
            if mode == "notes":
                flash = f"Scanned {len(notes)} code review note(s)."
            elif mode == "ci":
                flash = f"Scanned {len(ci_jobs)} CI job(s)."
            else:
                flash = f"Scanned {len(notes)} code review note(s) and {len(ci_jobs)} CI job(s)."
            if auto_cr_on and notes and mode in ("notes", "both"):
                ci_ctx = render_ci_context(parse_ci_jobs(db.get_state(f"ci_jobs:{run_id}", "", owner=owner)))
                spawn_tracked_task(
                    worker_for_owner(owner).run_external_code_review_fix(run_id, ci_context=ci_ctx),
                    db,
                    title="Auto CR failed",
                    message=f"Auto code-review fix crashed for run #{run_id}.",
                    run_id=run_id,
                    owner=owner,
                )
            return flash
        except Exception as exc:
            return f"Scan failed: {exc}"

    def worker_for_owner(owner: str):
        return registry.for_user(owner)

    # ---------------- auth routes ----------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        login_provider = isinstance(request.app.state.provider, LoginPageAuthProvider)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "request": request,
                "title": request.app.state.config.ui.title,
                "flash": pop_flash(request),
                "login_provider": login_provider,
                "provider_name": request.app.state.config.auth.provider,
            },
        )

    @app.post("/login")
    async def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
        prov = request.app.state.provider
        if isinstance(prov, LoginPageAuthProvider):
            user = prov.login(username.strip(), password)
            if not user:
                request.app.state.flash = "Invalid username or password."
                return RedirectResponse("/login", status_code=303)
            request.session["user"] = user.username
            return RedirectResponse("/", status_code=303)
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        if "session" in request.scope:
            request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/healthz")
    async def healthz():
        return JSONResponse({"ok": True})

    # ---------------- dashboard ----------------

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        owner = owner_of(request)
        ucfg = registry.user_config(owner)
        return templates.TemplateResponse(request, "index.html", context(request, db, ucfg, owner, registry))

    @app.post("/jira/scan")
    async def jira_scan(request: Request):
        try:
            count = await worker_for(request).scan_jira()
            request.app.state.flash = f"Scanned {count} Jira tickets."
        except Exception as exc:
            request.app.state.flash = f"Jira scan failed: {exc}"
        return RedirectResponse("/", status_code=303)

    @app.post("/queue/run-once")
    async def run_once(request: Request):
        owner = owner_of(request)
        if db.queue_paused(owner=owner):
            request.app.state.flash = "Queue is paused. Resume it before running tickets."
        else:
            ready = db.fetchone(
                """
                SELECT id FROM queue_items
                WHERE owner=? AND state IN ('plan_ready', 'needs_plan', 'queued')
                ORDER BY priority ASC, id ASC
                LIMIT 1
                """,
                (owner,),
            )
            if not ready:
                request.app.state.flash = "No queued ticket is ready to build."
            else:
                spawn_tracked_task(
                    worker_for(request).run_queue_item(int(ready["id"])),
                    db,
                    title="Run loop failed",
                    message="Background run-once task crashed. Check logs for details.",
                    owner=owner,
                )
                request.app.state.flash = "Build started."
        return RedirectResponse("/", status_code=303)

    @app.post("/queue/start-interval")
    async def start_interval(request: Request):
        worker_for(request).start_interval()
        request.app.state.flash = "Interval runner started."
        return RedirectResponse("/", status_code=303)

    @app.post("/queue/stop-interval")
    async def stop_interval(request: Request):
        worker_for(request).stop_interval()
        request.app.state.flash = "Interval runner stopped."
        return RedirectResponse("/", status_code=303)

    @app.post("/queue/pause")
    async def pause_queue(request: Request):
        db.set_state("queue_paused", "1", owner=owner_of(request))
        request.app.state.flash = "Queue paused. Jira scans can still run."
        return RedirectResponse("/", status_code=303)

    @app.post("/queue/resume")
    async def resume_queue(request: Request):
        db.set_state("queue_paused", "0", owner=owner_of(request))
        request.app.state.flash = "Queue resumed."
        return RedirectResponse("/", status_code=303)

    @app.post("/queue/reorder")
    async def reorder_queue(request: Request, order: str = Form("")):
        owner = owner_of(request)
        ids = [int(value) for value in order.split(",") if value.strip().isdigit()]
        owned = {
            int(row["id"])
            for row in db.fetchall("SELECT id FROM queue_items WHERE owner=?", (owner,))
        }
        db.reorder_queue([item_id for item_id in ids if item_id in owned])
        return RedirectResponse("/", status_code=303)

    @app.post("/queue/{queue_id}/prepare")
    async def prepare_queue(queue_id: int, request: Request):
        if not _owns_queue(db, queue_id, owner_of(request)):
            request.app.state.flash = "Queue item not found."
            return RedirectResponse("/", status_code=303)
        spawn_tracked_task(
            worker_for(request).prepare_queue_item(queue_id),
            db,
            title="Plan failed",
            message=f"Claude could not prepare queue item #{queue_id}.",
            owner=owner_of(request),
        )
        request.app.state.flash = "Claude is preparing the mission plan."
        return RedirectResponse(f"/queue/{queue_id}/plan", status_code=303)

    @app.post("/queue/{queue_id}/revise")
    async def revise_plan(queue_id: int, request: Request, user_notes: str = Form("")):
        if not _owns_queue(db, queue_id, owner_of(request)):
            request.app.state.flash = "Queue item not found."
            return RedirectResponse("/", status_code=303)
        spawn_tracked_task(
            worker_for(request).prepare_queue_item(queue_id, user_notes.strip()),
            db,
            title="Plan revision failed",
            message=f"Claude could not revise queue item #{queue_id}.",
            owner=owner_of(request),
        )
        request.app.state.flash = "Claude is revising the plan."
        return RedirectResponse(f"/queue/{queue_id}/plan", status_code=303)

    @app.get("/queue/{queue_id}/plan", response_class=HTMLResponse)
    async def queue_plan(queue_id: int, request: Request):
        owner = owner_of(request)
        item = db.queue_item(queue_id)
        if not item or item["owner"] != owner:
            request.app.state.flash = "Queue item not found."
            return RedirectResponse("/", status_code=303)
        plan = db.plan_for_queue_item(queue_id)
        selected = set()
        if plan and plan["skill_ids"]:
            selected = {int(part) for part in plan["skill_ids"].split(",") if part.strip().isdigit()}
        return templates.TemplateResponse(
            request,
            "ticket_plan.html",
            {
                "request": request,
                "title": request.app.state.config.ui.title,
                "user": current_user(request),
                "flash": pop_flash(request),
                "item": item,
                "plan": plan,
                "liked_skills": db.liked_skills(owner),
                "selected_skill_ids": selected,
            },
        )

    @app.get("/queue/{queue_id}/plan-body", response_class=HTMLResponse)
    async def queue_plan_body(queue_id: int, request: Request):
        item = db.queue_item(queue_id)
        if not item or item["owner"] != owner_of(request):
            return HTMLResponse("", status_code=404)
        return templates.TemplateResponse(
            request,
            "_plan_body.html",
            {"request": request, "item": item, "plan": db.plan_for_queue_item(queue_id)},
        )

    @app.post("/queue/{queue_id}/build")
    async def build_queue(queue_id: int, request: Request):
        item = db.queue_item(queue_id)
        if not item or item["owner"] != owner_of(request):
            request.app.state.flash = "Queue item not found."
            return RedirectResponse("/", status_code=303)
        if item["state"] not in ("needs_plan", "plan_ready", "queued"):
            request.app.state.flash = "This queue item is not ready to build."
            return RedirectResponse("/", status_code=303)
        spawn_tracked_task(
            worker_for(request).run_queue_item(queue_id),
            db,
            title="Build failed",
            message=f"Build crashed for queue item #{queue_id}.",
            owner=owner_of(request),
        )
        request.app.state.flash = "Build started."
        return RedirectResponse("/", status_code=303)

    @app.post("/queue/{queue_id}/delete")
    async def delete_queue(queue_id: int, request: Request):
        if _owns_queue(db, queue_id, owner_of(request)):
            db.delete_queue_item(queue_id)
            request.app.state.flash = "Queue item removed."
        return RedirectResponse("/", status_code=303)

    @app.post("/queue/clear-finished")
    async def clear_finished_queue(request: Request):
        db.clear_finished_queue_items(owner=owner_of(request))
        request.app.state.flash = "Finished queue items cleared."
        return RedirectResponse("/", status_code=303)

    @app.post("/tickets/{ticket_key}/enqueue")
    async def enqueue_ticket(ticket_key: str, request: Request):
        db.enqueue(ticket_key, owner=owner_of(request))
        return RedirectResponse("/", status_code=303)

    @app.post("/tickets/{ticket_key}/rerun")
    async def rerun_ticket(ticket_key: str, request: Request):
        worker_for(request).rerun_ticket(ticket_key)
        return RedirectResponse("/", status_code=303)

    @app.post("/tickets/start")
    async def start_ticket_now(request: Request, ticket_key: str = Form("")):
        owner = owner_of(request)
        worker = worker_for(request)
        key = ticket_key.strip().upper()
        if not key:
            request.app.state.flash = "Enter a Jira ticket key to start a run."
            return RedirectResponse("/", status_code=303)
        try:
            queue_id = await worker.start_ticket(key)
        except Exception as exc:
            request.app.state.flash = f"Could not start {key}: {exc}"
            return RedirectResponse("/", status_code=303)
        if queue_id:
            spawn_tracked_task(
                worker.run_queue_item(queue_id),
                db,
                title="Manual run failed",
                message=f"Manual run for {key} crashed. Check logs for details.",
                owner=owner,
            )
            request.app.state.flash = f"Started run for {key}."
        else:
            request.app.state.flash = f"{key} is already queued or running."
        return RedirectResponse("/", status_code=303)

    @app.post("/dry-run/enqueue")
    async def enqueue_test_ticket(request: Request):
        owner = owner_of(request)
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
            },
            owner=owner,
        )
        db.enqueue(ticket_key, owner=owner)
        request.app.state.flash = f"Created dry-run ticket {ticket_key}."
        return RedirectResponse("/", status_code=303)

    @app.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: int, request: Request):
        owner = owner_of(request)
        active_run = db.fetchone(
            "SELECT id FROM runs WHERE owner=? AND state IN ('preparing_git','running_claude','reviewing') ORDER BY id DESC LIMIT 1",
            (owner,),
        )
        if not active_run:
            request.app.state.flash = "No active run to cancel."
            return RedirectResponse("/", status_code=303)

        active_run_id = int(active_run["id"])
        if active_run_id != run_id:
            request.app.state.flash = f"Cancel ignored: run #{run_id} is not active. Active run is #{active_run_id}."
            return RedirectResponse(f"/runs/{active_run_id}", status_code=303)

        worker_for(request).cancel_current()
        db.update_run(active_run_id, state="cancelled", error="cancel requested")
        request.app.state.flash = f"Cancel requested for run #{active_run_id}."
        return RedirectResponse(f"/runs/{active_run_id}", status_code=303)

    @app.post("/runs/{run_id}/review-fix")
    async def review_fix(run_id: int, request: Request):
        owner = owner_of(request)
        if not owned_run(run_id, owner):
            return RedirectResponse("/", status_code=303)
        spawn_tracked_task(
            worker_for(request).run_cr_fix(run_id),
            db,
            title="CR fix failed",
            message=f"Background CR fix crashed for run #{run_id}.",
            run_id=run_id,
            owner=owner,
        )
        request.app.state.flash = "CR fix started."
        return RedirectResponse("/", status_code=303)

    @app.post("/runs/{run_id}/retry")
    async def retry_run(run_id: int, request: Request):
        owner = owner_of(request)
        run = owned_run(run_id, owner)
        if not run:
            return RedirectResponse("/", status_code=303)
        if run["state"] not in ("failed", "cancelled"):
            request.app.state.flash = "Only a failed or cancelled run can be retried."
            return RedirectResponse(f"/runs/{run_id}", status_code=303)
        if not run["workspace_path"] or not Path(run["workspace_path"]).exists():
            request.app.state.flash = "That run's workspace is gone — use Rerun to start fresh."
            return RedirectResponse(f"/runs/{run_id}", status_code=303)
        spawn_tracked_task(
            worker_for(request).retry_run(run_id),
            db,
            title="Retry failed",
            message=f"Retry of run #{run_id} crashed.",
            run_id=run_id,
            owner=owner,
        )
        request.app.state.flash = f"Retrying run #{run_id} in its existing workspace."
        return RedirectResponse("/", status_code=303)

    @app.post("/runs/{run_id}/deliver")
    async def deliver_run(run_id: int, request: Request):
        if not owned_run(run_id, owner_of(request)):
            return RedirectResponse("/", status_code=303)
        try:
            result = worker_for(request).deliver_locally(run_id)
            request.app.state.flash = f"Workspace copied locally. {result}"
        except Exception as exc:
            request.app.state.flash = f"Local delivery failed: {exc}"
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/push")
    async def push_run(run_id: int, request: Request):
        if not owned_run(run_id, owner_of(request)):
            return RedirectResponse("/", status_code=303)
        try:
            worker_for(request).push_run(run_id)
            request.app.state.flash = "Branch pushed."
        except Exception as exc:
            request.app.state.flash = f"Push failed: {exc}"
        return RedirectResponse("/", status_code=303)

    @app.get("/runs/{run_id}/push-preview", response_class=HTMLResponse)
    async def push_preview(run_id: int, request: Request):
        owner = owner_of(request)
        detail = run_detail_context(request, db, request.app.state.config, run_id, owner)
        if detail["run"] is None:
            return RedirectResponse("/", status_code=303)
        run = detail["run"]
        diff_text = ""
        if run["workspace_path"]:
            try:
                diff_text = worker_for(request).git.review_diff(run["workspace_path"], run["base_branch"])
            except Exception:
                diff_text = ""
        detail["diff_lines"] = diff_lines(diff_text)
        detail["merge_request_url"] = db.get_state(f"merge_request_url:{run_id}", "", owner=owner)
        return templates.TemplateResponse(request, "push_preview.html", detail)

    @app.post("/runs/{run_id}/merge-request")
    async def create_run_merge_request(run_id: int, request: Request):
        if not owned_run(run_id, owner_of(request)):
            return RedirectResponse("/", status_code=303)
        try:
            url = worker_for(request).open_merge_request(run_id)
            request.app.state.flash = f"Merge request opened: {url}" if url else "No merge request was created."
        except Exception as exc:
            request.app.state.flash = f"Merge request failed: {exc}"
        return RedirectResponse(f"/runs/{run_id}/push-preview", status_code=303)

    @app.get("/runs/{run_id}/code-review", response_class=HTMLResponse)
    async def code_review(run_id: int, request: Request):
        owner = owner_of(request)
        detail = run_detail_context(request, db, request.app.state.config, run_id, owner)
        if detail["run"] is None:
            return RedirectResponse("/", status_code=303)
        detail["review_notes"] = db.code_review_notes(run_id)
        detail["auto_cr"] = db.get_state(f"auto_cr:{run_id}", "0", owner=owner) == "1"
        detail["comment_back_default"] = db.get_state(f"comment_back:{run_id}", "1", owner=owner) == "1"
        ci_jobs = parse_ci_jobs(db.get_state(f"ci_jobs:{run_id}", "", owner=owner))
        if not ci_jobs and detail["run"]["ticket_key"] == "DEMO-101":
            ci_jobs = demo_ci_jobs_for_display()
        detail["ci_jobs"] = ci_jobs
        saved_source_url = db.get_state(f"review_source_url:{run_id}", "", owner=owner)
        detail["suggested_review_url"] = saved_source_url or suggest_review_url(
            detail["run"]["repo_url"], detail["run"]["branch_name"]
        )
        return templates.TemplateResponse(request, "code_review.html", detail)

    @app.get("/runs/{run_id}/code-review/live", response_class=HTMLResponse)
    async def code_review_live(run_id: int, request: Request):
        owner = owner_of(request)
        run = owned_run(run_id, owner)
        if not run:
            return HTMLResponse("", status_code=404)
        ci_jobs = parse_ci_jobs(db.get_state(f"ci_jobs:{run_id}", "", owner=owner))
        if not ci_jobs and run["ticket_key"] == "DEMO-101":
            ci_jobs = demo_ci_jobs_for_display()
        return templates.TemplateResponse(
            request,
            "_code_review_live.html",
            {"request": request, "run": run, "review_notes": db.code_review_notes(run_id), "ci_jobs": ci_jobs},
        )

    @app.post("/runs/{run_id}/code-review/scan")
    async def scan_code_review(
        run_id: int,
        request: Request,
        source_url: str = Form(""),
        auto_cr: str | None = Form(None),
        auto_cr_state: str = Form(""),
        comment_back_state: str = Form(""),
        scan_mode: str = Form("both"),
    ):
        owner = owner_of(request)
        if not owned_run(run_id, owner):
            return RedirectResponse("/", status_code=303)
        auto_cr_on = auto_cr == "on" or auto_cr_state == "1"
        if comment_back_state in {"0", "1"}:
            db.set_state(f"comment_back:{run_id}", comment_back_state, owner=owner)
        request.app.state.flash = do_code_review_scan(run_id, source_url, auto_cr_on, scan_mode, owner)
        return RedirectResponse(f"/runs/{run_id}/code-review", status_code=303)

    @app.post("/runs/{run_id}/code-review/scan-notes")
    async def scan_code_review_notes_only(
        run_id: int,
        request: Request,
        source_url: str = Form(""),
        auto_cr: str | None = Form(None),
        auto_cr_state: str = Form(""),
        comment_back_state: str = Form(""),
    ):
        owner = owner_of(request)
        if not owned_run(run_id, owner):
            return RedirectResponse("/", status_code=303)
        auto_cr_on = auto_cr == "on" or auto_cr_state == "1"
        if comment_back_state in {"0", "1"}:
            db.set_state(f"comment_back:{run_id}", comment_back_state, owner=owner)
        request.app.state.flash = do_code_review_scan(run_id, source_url, auto_cr_on, "notes", owner)
        return RedirectResponse(f"/runs/{run_id}/code-review", status_code=303)

    @app.post("/runs/{run_id}/code-review/scan-ci")
    async def scan_code_review_ci_only(
        run_id: int,
        request: Request,
        source_url: str = Form(""),
        auto_cr_state: str = Form(""),
        comment_back_state: str = Form(""),
    ):
        owner = owner_of(request)
        if not owned_run(run_id, owner):
            return RedirectResponse("/", status_code=303)
        if auto_cr_state in {"0", "1"}:
            db.set_state(f"auto_cr:{run_id}", auto_cr_state, owner=owner)
        if comment_back_state in {"0", "1"}:
            db.set_state(f"comment_back:{run_id}", comment_back_state, owner=owner)
        request.app.state.flash = do_code_review_scan(run_id, source_url, False, "ci", owner)
        return RedirectResponse(f"/runs/{run_id}/code-review", status_code=303)

    @app.post("/runs/{run_id}/code-review/auto-scan")
    async def auto_scan_code_review(run_id: int, request: Request, source_url: str = Form(""), auto_cr: str = Form("0")):
        owner = owner_of(request)
        if not owned_run(run_id, owner):
            return JSONResponse({"ok": False, "reason": "not_found"}, status_code=404)
        source_url = source_url.strip()
        if not source_url:
            return JSONResponse({"ok": False, "reason": "missing_source"})
        try:
            db.set_state(f"review_source_url:{run_id}", source_url, owner=owner)
            db.set_state(f"auto_cr:{run_id}", "1" if auto_cr == "1" else "0", owner=owner)
            notes = scan_review_notes(source_url)
            for note in notes:
                db.upsert_code_review_note(run_id, note, owner=owner)
            ci_jobs = scan_ci_jobs(source_url)
            db.set_state(f"ci_jobs:{run_id}", json.dumps(ci_jobs), owner=owner)
            if auto_cr == "1" and notes:
                spawn_tracked_task(
                    worker_for(request).run_external_code_review_fix(run_id, ci_context=render_ci_context(ci_jobs)),
                    db,
                    title="Auto CR failed",
                    message=f"Auto code-review fix crashed for run #{run_id}.",
                    run_id=run_id,
                    owner=owner,
                )
            return JSONResponse({"ok": True, "notes_count": len(notes), "ci_count": len(ci_jobs)})
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.post("/runs/{run_id}/code-review/fix")
    async def fix_code_review(
        run_id: int,
        request: Request,
        user_notes: str = Form(""),
        comment_back: str | None = Form(None),
    ):
        owner = owner_of(request)
        if not owned_run(run_id, owner):
            return RedirectResponse("/", status_code=303)
        comment_back_on = comment_back == "on"
        db.set_state(f"comment_back:{run_id}", "1" if comment_back_on else "0", owner=owner)
        spawn_tracked_task(
            worker_for(request).run_external_code_review_fix(run_id, user_notes.strip(), comment_back_on),
            db,
            title="Code review fix failed",
            message=f"Code review fix crashed for run #{run_id}.",
            run_id=run_id,
            owner=owner,
        )
        request.app.state.flash = "Code review fix started."
        return RedirectResponse(f"/runs/{run_id}/code-review", status_code=303)

    @app.post("/runs/{run_id}/code-review/fix-ci")
    async def fix_ci_jobs(run_id: int, request: Request, user_notes: str = Form("")):
        owner = owner_of(request)
        if not owned_run(run_id, owner):
            return RedirectResponse("/", status_code=303)
        ci_jobs = parse_ci_jobs(db.get_state(f"ci_jobs:{run_id}", "", owner=owner))
        ci_context = render_ci_context(ci_jobs)
        if not ci_context:
            request.app.state.flash = "No CI jobs available. Scan review first."
            return RedirectResponse(f"/runs/{run_id}/code-review", status_code=303)
        spawn_tracked_task(
            worker_for(request).run_ci_fix(run_id, ci_context, user_notes.strip()),
            db,
            title="CI fix failed",
            message=f"CI fix crashed for run #{run_id}.",
            run_id=run_id,
            owner=owner,
        )
        request.app.state.flash = "CI fix started."
        return RedirectResponse(f"/runs/{run_id}/code-review", status_code=303)

    @app.get("/runs/{run_id}/summary", response_class=HTMLResponse)
    async def run_summary_partial(run_id: int, request: Request):
        detail = run_detail_context(request, db, request.app.state.config, run_id, owner_of(request))
        return templates.TemplateResponse(request, "_run_summary.html", detail)

    @app.get("/notifications/unread")
    async def unread_notifications(request: Request):
        rows = db.unread_notifications(owner=owner_of(request))
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
        detail = run_detail_context(request, db, request.app.state.config, run_id, owner_of(request))
        if detail["run"] is None:
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "run_detail.html", detail)

    @app.post("/runs/{run_id}/input")
    async def add_run_input(request: Request, run_id: int, message: str = Form(...)):
        if not owned_run(run_id, owner_of(request)):
            return RedirectResponse("/", status_code=303)
        clean = message.strip()
        if clean:
            db.add_agent_input(run_id, clean)
            db.add_log(run_id, "user", clean)
        if is_htmx(request):
            return templates.TemplateResponse(request, "_run_interaction.html", run_interaction_context(db, run_id))
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/agent-questions/{question_id}/answer")
    async def answer_agent_question(
        request: Request,
        question_id: int,
        run_id: int = Form(...),
        selected_answer: str = Form(""),
        free_answer: str = Form(""),
    ) -> Response:
        if not owned_run(run_id, owner_of(request)):
            return RedirectResponse("/", status_code=303)
        free_answer = free_answer.strip()
        selected_answer = selected_answer.strip()
        answer = free_answer or selected_answer
        source = "free_input" if free_answer else "option"
        if answer:
            db.answer_agent_question(question_id, answer, source)
            db.add_log(run_id, "user", f"Answered question #{question_id}: {answer}")
        if is_htmx(request):
            return templates.TemplateResponse(request, "_run_interaction.html", run_interaction_context(db, run_id))
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.get("/runs/{run_id}/interaction", response_class=HTMLResponse)
    async def run_interaction(run_id: int, request: Request):
        if not owned_run(run_id, owner_of(request)):
            return HTMLResponse("", status_code=404)
        return templates.TemplateResponse(request, "_run_interaction.html", run_interaction_context(db, run_id))

    @app.get("/runs/{run_id}/logs", response_class=PlainTextResponse)
    async def run_logs(run_id: int, request: Request):
        if not owned_run(run_id, owner_of(request)):
            return PlainTextResponse("", status_code=404)
        rows = db.fetchall("SELECT phase, line FROM logs WHERE run_id=? ORDER BY id", (run_id,))
        return "\n".join(f"[{row['phase']}] {row['line']}" for row in rows)

    @app.get("/runs/{run_id}/logs/stream")
    async def stream_logs(run_id: int, request: Request, after: int = 0):
        if not owned_run(run_id, owner_of(request)):
            return PlainTextResponse("", status_code=404)
        # Resume point: Last-Event-ID (set by the browser on auto-reconnect)
        # wins over the initial ?after= the page rendered with.
        resume = request.headers.get("last-event-id")
        start_id = int(resume) if (resume or "").isdigit() else after
        terminal = {"done", "failed", "cancelled", "pushed"}

        async def event_gen():
            last_id = start_id
            while True:
                if await request.is_disconnected():
                    break
                rows = db.fetchall(
                    "SELECT id, phase, line FROM logs WHERE run_id=? AND id>? ORDER BY id",
                    (run_id, last_id),
                )
                for row in rows:
                    last_id = int(row["id"])
                    text = f"[{row['phase']}] {row['line']}".replace("\r", "")
                    body = "".join(f"data: {line}\n" for line in text.split("\n"))
                    yield f"id: {last_id}\n{body}\n"
                state_row = db.fetchone("SELECT state FROM runs WHERE id=?", (run_id,))
                if state_row and state_row["state"] in terminal:
                    yield "event: done\ndata: end\n\n"
                    break
                await asyncio.sleep(0.6)

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/runs/{run_id}/workspace.zip")
    async def download_workspace_zip(run_id: int, request: Request):
        run = owned_run(run_id, owner_of(request))
        if not run or not run["workspace_path"]:
            return PlainTextResponse("Workspace not available.", status_code=404)
        try:
            data = build_workspace_zip(run)
        except ValueError as exc:
            return PlainTextResponse(str(exc), status_code=400)
        filename = f"{(run['ticket_key'] or 'run').replace('/', '-')}-{run_id}.zip"
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/runs/{run_id}/workspace/files")
    async def workspace_files(run_id: int, request: Request):
        run = owned_run(run_id, owner_of(request))
        return JSONResponse(list_workspace_files(run))

    @app.get("/runs/{run_id}/workspace/file")
    async def workspace_file(run_id: int, request: Request, path: str = "", base: str = ""):
        run = owned_run(run_id, owner_of(request))
        try:
            file_path = safe_workspace_file(run, path)
            if file_path.stat().st_size > 1_000_000:
                return JSONResponse({"error": "File is too large to edit in the browser."}, status_code=400)
            content = file_path.read_text(encoding="utf-8", errors="replace")
            original_content = ""
            if run and run["workspace_path"]:
                # When a base branch is requested (merge-request review), diff the
                # committed work against that base instead of HEAD; otherwise the
                # committed file equals HEAD and the diff would look empty.
                remote = request.app.state.config.git.remote_name
                if base.strip():
                    original_content = git_base_file(run["workspace_path"], remote, base.strip(), path)
                else:
                    original_content = git_head_file(run["workspace_path"], path)
            return JSONResponse({"path": path, "content": content, "original_content": original_content})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/runs/{run_id}/workspace/file")
    async def save_workspace_file(run_id: int, request: Request, path: str = Form(""), content: str = Form("")):
        run = owned_run(run_id, owner_of(request))
        try:
            file_path = safe_workspace_file(run, path)
            file_path.write_text(content, encoding="utf-8", newline="\n")
            db.add_log(run_id, "workspace", f"Saved {path} from Web IDE")
            return JSONResponse({"ok": True, "path": path})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.get("/stats", response_class=HTMLResponse)
    async def stats_page(request: Request):
        owner = owner_of(request)
        return templates.TemplateResponse(
            request,
            "stats.html",
            {
                "request": request,
                "title": registry.user_config(owner).ui.title,
                "user": current_user(request),
                "flash": pop_flash(request),
                "stats": run_stats(db, owner),
            },
        )

    def ide_comments_context(request: Request, run_id: int) -> dict:
        return {
            "request": request,
            "run": owned_run(run_id, owner_of(request)),
            "run_id": run_id,
            "comments": db.list_ide_comments(run_id),
        }

    @app.get("/runs/{run_id}/ide-comments", response_class=HTMLResponse)
    async def ide_comments_partial(run_id: int, request: Request):
        if not owned_run(run_id, owner_of(request)):
            return HTMLResponse("", status_code=404)
        return templates.TemplateResponse(request, "_ide_comments.html", ide_comments_context(request, run_id))

    @app.post("/runs/{run_id}/ide-comments")
    async def add_ide_comment(
        run_id: int,
        request: Request,
        file_path: str = Form(""),
        line: int = Form(0),
        body: str = Form(""),
    ):
        owner = owner_of(request)
        if not owned_run(run_id, owner):
            return RedirectResponse("/", status_code=303)
        if body.strip():
            db.add_ide_comment(run_id, file_path.strip(), line, body.strip(), owner=owner)
        if is_htmx(request):
            return templates.TemplateResponse(request, "_ide_comments.html", ide_comments_context(request, run_id))
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/ide-comments/{comment_id}/delete")
    async def delete_ide_comment(run_id: int, comment_id: int, request: Request):
        if not owned_run(run_id, owner_of(request)):
            return RedirectResponse("/", status_code=303)
        db.delete_ide_comment(comment_id, run_id)
        if is_htmx(request):
            return templates.TemplateResponse(request, "_ide_comments.html", ide_comments_context(request, run_id))
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/ide-comments/apply")
    async def apply_ide_comments(run_id: int, request: Request, user_notes: str = Form("")):
        owner = owner_of(request)
        if not owned_run(run_id, owner):
            return RedirectResponse("/", status_code=303)
        if not db.open_ide_comments(run_id):
            request.app.state.flash = "Add at least one comment before applying."
            return RedirectResponse(f"/runs/{run_id}", status_code=303)
        spawn_tracked_task(
            worker_for(request).run_ide_comment_fix(run_id, user_notes.strip()),
            db,
            title="IDE comment fix failed",
            message=f"Applying Web IDE comments crashed for run #{run_id}.",
            run_id=run_id,
            owner=owner,
        )
        request.app.state.flash = "Applying your Web IDE comments with Claude."
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.get("/partials/status", response_class=HTMLResponse)
    async def status_partial(request: Request):
        owner = owner_of(request)
        ucfg = registry.user_config(owner)
        return templates.TemplateResponse(request, "_status.html", context(request, db, ucfg, owner, registry))

    @app.get("/partials/dashboard", response_class=HTMLResponse)
    async def dashboard_partial(request: Request):
        owner = owner_of(request)
        ucfg = registry.user_config(owner)
        return templates.TemplateResponse(request, "_dashboard_live.html", context(request, db, ucfg, owner, registry))

    # ---------------- skills ----------------

    @app.get("/skills", response_class=HTMLResponse)
    async def skills_page(request: Request):
        owner = owner_of(request)
        liked_ids = db.liked_skill_ids(owner)
        return templates.TemplateResponse(
            request,
            "skills.html",
            {
                "request": request,
                "title": registry.user_config(owner).ui.title,
                "user": current_user(request),
                "flash": pop_flash(request),
                "my_groups": group_skills_by_category(db.list_skills(owner)),
                "market_groups": group_skills_by_category(db.list_public_skills()),
                "categories": db.list_categories(owner),
                "liked_skills": db.liked_skills(owner),
                "liked_ids": liked_ids,
            },
        )

    @app.post("/skills/create")
    async def create_skill(
        request: Request,
        name: str = Form(...),
        description: str = Form(""),
        content: str = Form(""),
        category: str = Form(""),
        visibility: str = Form("private"),
    ):
        owner = owner_of(request)
        if name.strip():
            db.create_skill(owner, name.strip(), description.strip(), content, visibility, category.strip())
            request.app.state.flash = f"Skill '{name.strip()}' created."
        return RedirectResponse("/skills", status_code=303)

    @app.post("/skills/{skill_id}/update")
    async def update_skill(
        skill_id: int,
        request: Request,
        name: str = Form(...),
        description: str = Form(""),
        content: str = Form(""),
        category: str = Form(""),
        visibility: str = Form("private"),
    ):
        if db.update_skill(
            skill_id, owner_of(request), name.strip(), description.strip(), content, visibility, category.strip()
        ):
            request.app.state.flash = "Skill updated."
        else:
            request.app.state.flash = "You can only edit your own skills."
        return RedirectResponse("/skills", status_code=303)

    @app.post("/skills/categories/create")
    async def create_skill_category(request: Request, name: str = Form(...)):
        owner = owner_of(request)
        if name.strip():
            db.create_category(owner, name.strip())
            request.app.state.flash = f"Category '{name.strip()}' created."
        return RedirectResponse("/skills", status_code=303)

    @app.post("/skills/categories/{category_id}/delete")
    async def delete_skill_category(category_id: int, request: Request):
        if db.delete_category(category_id, owner_of(request)):
            request.app.state.flash = "Category removed."
        return RedirectResponse("/skills", status_code=303)

    @app.post("/skills/{skill_id}/delete")
    async def delete_skill(skill_id: int, request: Request):
        db.delete_skill(skill_id, owner_of(request))
        request.app.state.flash = "Skill removed."
        return RedirectResponse("/skills", status_code=303)

    @app.post("/skills/{skill_id}/like")
    async def like_skill(skill_id: int, request: Request):
        db.like_skill(owner_of(request), skill_id)
        return RedirectResponse("/skills", status_code=303)

    @app.post("/skills/{skill_id}/unlike")
    async def unlike_skill(skill_id: int, request: Request):
        db.unlike_skill(owner_of(request), skill_id)
        return RedirectResponse("/skills", status_code=303)

    @app.post("/queue/{queue_id}/skills")
    async def attach_plan_skills(queue_id: int, request: Request, skill_ids: list[int] = Form(default=[])):
        owner = owner_of(request)
        item = db.queue_item(queue_id)
        if not item or item["owner"] != owner:
            return RedirectResponse("/", status_code=303)
        plan = db.plan_for_queue_item(queue_id)
        liked = db.liked_skill_ids(owner)
        chosen = [sid for sid in skill_ids if sid in liked]
        if plan:
            db.execute(
                "UPDATE ticket_plans SET skill_ids=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (",".join(str(sid) for sid in chosen), plan["id"]),
            )
            request.app.state.flash = f"Attached {len(chosen)} skill(s) to the plan."
        else:
            request.app.state.flash = "Create a plan first, then attach skills."
        return RedirectResponse(f"/queue/{queue_id}/plan", status_code=303)

    # ---------------- settings ----------------

    @app.get("/settings", response_class=HTMLResponse)
    async def settings(request: Request):
        owner = owner_of(request)
        user = current_user(request)
        ucfg = registry.user_config(owner)
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "request": request,
                "config": ucfg,
                "title": ucfg.ui.title,
                "flash": pop_flash(request),
                "user": user,
                "is_admin": user.is_admin,
                "config_text": load_config_text() if user.is_admin else "",
                "claude_args_text": "\n".join(ucfg.claude.args),
                "excluded_statuses_text": "\n".join(ucfg.jira.excluded_statuses),
            },
        )

    @app.post("/settings/test/{target}")
    async def test_connection(target: str, request: Request):
        try:
            message = await run_connection_test(target, registry.user_config(owner_of(request)))
            request.app.state.flash = message
        except Exception as exc:
            request.app.state.flash = f"{target} test failed: {exc}"
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/test-notify")
    async def test_notify(request: Request):
        cfg = registry.user_config(owner_of(request)).notify
        if not cfg.email_enabled and not cfg.webhook_enabled:
            request.app.state.flash = "Enable email and/or webhook first, then save."
            return RedirectResponse("/settings", status_code=303)
        errors = notify.dispatch(cfg, "Test notification", "This is a test from Claude Worker.", "info")
        if errors:
            request.app.state.flash = "Test notification failed: " + "; ".join(errors)
        else:
            request.app.state.flash = "Test notification sent."
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/test-writeback")
    async def test_writeback(request: Request):
        owner = owner_of(request)
        cfg = registry.user_config(owner).jira
        ticket = db.fetchone(
            "SELECT key FROM tickets WHERE owner=? ORDER BY updated_at DESC LIMIT 1", (owner,)
        )
        if not ticket:
            request.app.state.flash = "No ticket found yet. Scan Jira or add a dry-run ticket first."
            return RedirectResponse("/settings", status_code=303)
        try:
            names = await JiraClient(cfg).get_transitions(ticket["key"])
        except Exception as exc:
            request.app.state.flash = f"Jira write-back check failed: {exc}"
            return RedirectResponse("/settings", status_code=303)
        target = (cfg.writeback_transition or "").strip()
        match = "" if not target else (
            f" Configured transition '{target}' is "
            + ("valid." if target.lower() in {n.lower() for n in names} else "NOT in this list — fix the name.")
        )
        request.app.state.flash = f"Transitions for {ticket['key']}: {', '.join(names) or '(none)'}.{match}"
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/test-gate")
    async def test_gate_check(request: Request):
        cfg = registry.user_config(owner_of(request)).test_gate
        command = (cfg.command or "").strip()
        if not command:
            request.app.state.flash = "Set a test command first (e.g. 'pytest -q')."
            return RedirectResponse("/settings", status_code=303)
        program = shlex.split(command)[0]
        found = shutil.which(program)
        if found:
            request.app.state.flash = f"Test command looks runnable: '{program}' found at {found}."
        else:
            request.app.state.flash = f"'{program}' was not found on PATH (it must exist where runs execute)."
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/test-docker")
    async def test_docker(request: Request):
        config = registry.user_config(owner_of(request))
        ok, message = await asyncio.to_thread(docker_preflight, config)
        request.app.state.flash = ("Docker preflight passed. " if ok else "Docker preflight failed. ") + message
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings")
    async def save_server_settings(request: Request, config_text: str = Form(...)):
        # Server-level YAML (app/auth/docker) is admin-only.
        if not current_user(request).is_admin:
            request.app.state.flash = "Only an admin can edit server YAML."
            return RedirectResponse("/settings", status_code=303)
        try:
            new_config = save_config_text(config_text)
            request.app.state.config = new_config
            registry.set_base_config(new_config)
            request.app.state.provider = get_auth_provider(new_config, db)
            request.app.state.flash = "Server config saved."
        except Exception as exc:
            request.app.state.flash = f"Config save failed: {exc}"
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/visual")
    async def save_visual_settings(
        request: Request,
        jira_url: str = Form(""),
        jira_email: str = Form(""),
        jira_token: str = Form(""),
        jira_jql: str = Form(""),
        excluded_statuses: str = Form(""),
        required_text: str = Form(""),
        max_results: int = Form(25),
        jira_writeback_enabled: str | None = Form(None),
        jira_writeback_transition: str = Form(""),
        jira_writeback_comment: str | None = Form(None),
        git_username: str = Form(""),
        git_token: str = Form(""),
        git_remote_name: str = Form("origin"),
        git_default_repo_url: str = Form(""),
        git_default_base_branch: str = Form("main"),
        auto_push: str | None = Form(None),
        auto_merge_request: str | None = Form(None),
        claude_command: str = Form("claude"),
        claude_args: str = Form(""),
        claude_model: str = Form(""),
        claude_api_key: str = Form(""),
        claude_timeout_seconds: int = Form(7200),
        allow_cr_fix: str | None = Form(None),
        auto_cr_fix: str | None = Form(None),
        ui_title: str = Form("Jira Claude Worker"),
        scp_host: str = Form(""),
        scp_user: str = Form(""),
        scp_path: str = Form(""),
        ssh_key: str = Form(""),
        ssh_port: int = Form(22),
        notify_email_enabled: str | None = Form(None),
        notify_smtp_host: str = Form(""),
        notify_smtp_port: int = Form(587),
        notify_smtp_user: str = Form(""),
        notify_smtp_password: str = Form(""),
        notify_smtp_use_tls: str | None = Form(None),
        notify_email_from: str = Form(""),
        notify_email_to: str = Form(""),
        notify_webhook_enabled: str | None = Form(None),
        notify_webhook_url: str = Form(""),
        test_gate_enabled: str | None = Form(None),
        test_gate_command: str = Form(""),
        test_gate_timeout_seconds: int = Form(1800),
    ):
        owner = owner_of(request)
        try:
            sections = {
                "jira": {
                    "url": jira_url,
                    "email": jira_email,
                    "token": jira_token,
                    "jql": jira_jql,
                    "excluded_statuses": split_lines(excluded_statuses),
                    "required_text": required_text,
                    "max_results": max_results,
                    "writeback_enabled": jira_writeback_enabled == "on",
                    "writeback_transition": jira_writeback_transition,
                    "writeback_comment": jira_writeback_comment == "on",
                },
                "git": {
                    "username": git_username,
                    "token": git_token,
                    "remote_name": git_remote_name,
                    "default_repo_url": git_default_repo_url,
                    "default_base_branch": git_default_base_branch,
                    "auto_push": auto_push == "on",
                    "auto_merge_request": auto_merge_request == "on",
                },
                "claude": {
                    "command": claude_command,
                    "args": split_lines(claude_args),
                    "model": claude_model,
                    "api_key": claude_api_key,
                    "timeout_seconds": claude_timeout_seconds,
                    "allow_cr_fix": allow_cr_fix == "on",
                    "auto_cr_fix": auto_cr_fix == "on",
                },
                "ui": {"title": ui_title},
                "delivery": {
                    "scp_host": scp_host,
                    "scp_user": scp_user,
                    "scp_path": scp_path,
                    "ssh_key": ssh_key,
                    "ssh_port": ssh_port,
                },
                "notify": {
                    "email_enabled": notify_email_enabled == "on",
                    "smtp_host": notify_smtp_host,
                    "smtp_port": notify_smtp_port,
                    "smtp_user": notify_smtp_user,
                    "smtp_password": notify_smtp_password,
                    "smtp_use_tls": notify_smtp_use_tls == "on",
                    "email_from": notify_email_from,
                    "email_to": notify_email_to,
                    "webhook_enabled": notify_webhook_enabled == "on",
                    "webhook_url": notify_webhook_url,
                },
                "test_gate": {
                    "enabled": test_gate_enabled == "on",
                    "command": test_gate_command,
                    "timeout_seconds": test_gate_timeout_seconds,
                },
            }
            db.set_user_config(owner, sections)
            registry.refresh(owner)
            request.app.state.flash = "Your config was saved."
        except Exception as exc:
            request.app.state.flash = f"Config save failed: {exc}"
        return RedirectResponse("/settings", status_code=303)

    return app


def _owns_queue(db: Database, queue_id: int, owner: str) -> bool:
    row = db.fetchone("SELECT owner FROM queue_items WHERE id=?", (queue_id,))
    return bool(row and row["owner"] == owner)


def group_skills_by_category(skills) -> list[dict]:
    """Group skill rows by their category label for the skills page.

    Uncategorized skills land in a trailing "Uncategorized" bucket.
    """
    groups: dict[str, list] = {}
    for skill in skills:
        category = (skill["category"] if "category" in skill.keys() else "") or "Uncategorized"
        groups.setdefault(category, []).append(skill)
    ordered = sorted(groups.items(), key=lambda kv: (kv[0] == "Uncategorized", kv[0].lower()))
    return [{"category": name, "skills": rows} for name, rows in ordered]


def context(request: Request, db: Database, config: Config, owner: str, registry: WorkerRegistry) -> dict:
    current = db.fetchone(
        "SELECT * FROM runs WHERE owner=? AND state IN ('preparing_git','running_claude','reviewing') ORDER BY id DESC LIMIT 1",
        (owner,),
    )
    current_ticket = None
    current_logs = []
    if current:
        current_ticket = db.fetchone("SELECT * FROM tickets WHERE key=? AND owner=?", (current["ticket_key"], owner))
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
    worker = registry.for_user(owner)
    return {
        "request": request,
        "title": config.ui.title,
        "user": getattr(request.state, "user", None),
        "flash": flash,
        "current": current,
        "current_ticket": current_ticket,
        "current_logs": current_logs,
        "current_questions": current_questions,
        "current_sub_agents": current_sub_agents,
        "queue_paused": db.queue_paused(owner=owner),
        "queue": db.fetchall(
            """
            SELECT q.*, t.summary, t.status, p.id AS plan_id, p.mission, p.repo_url, p.base_branch, p.branch_name, p.plan_text
            FROM queue_items q JOIN tickets t ON t.key=q.ticket_key AND t.owner=q.owner
            LEFT JOIN ticket_plans p ON p.queue_item_id=q.id
            WHERE q.owner=? AND q.state IN ('needs_plan', 'planning', 'plan_ready', 'queued', 'running')
            ORDER BY q.priority ASC, q.id ASC
            """,
            (owner,),
        ),
        "skipped": db.fetchall(
            "SELECT * FROM tickets WHERE owner=? AND eligibility='skipped' ORDER BY updated_at DESC LIMIT 50",
            (owner,),
        ),
        "manual": db.fetchall(
            "SELECT * FROM tickets WHERE owner=? AND eligibility!='eligible' ORDER BY updated_at DESC LIMIT 50",
            (owner,),
        ),
        "done": db.fetchall(
            """
            SELECT r.*, t.summary
            FROM runs r LEFT JOIN tickets t ON t.key=r.ticket_key AND t.owner=r.owner
            WHERE r.owner=? AND r.state IN ('done','needs_cr_fix','pushed','failed','cancelled')
            ORDER BY r.id DESC LIMIT 50
            """,
            (owner,),
        ),
        "notifications": db.fetchall("SELECT * FROM notifications WHERE owner=? ORDER BY id DESC LIMIT 10", (owner,)),
        "allow_cr_fix": config.claude.allow_cr_fix,
        "interval_running": bool(worker.interval_task and not worker.interval_task.done()),
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


def run_detail_context(request: Request, db: Database, config: Config, run_id: int, owner: str) -> dict:
    run = db.fetchone("SELECT * FROM runs WHERE id=? AND owner=?", (run_id, owner))
    ticket = db.fetchone("SELECT * FROM tickets WHERE key=? AND owner=?", (run["ticket_key"], owner)) if run else None
    data = {
        "request": request,
        "title": config.ui.title,
        "user": getattr(request.state, "user", None),
        "flash": pop_flash(request),
        "run": run,
        "ticket": ticket,
        "logs": db.fetchall("SELECT * FROM logs WHERE run_id=? ORDER BY id", (run_id,)) if run else [],
        "questions": db.fetchall("SELECT * FROM agent_questions WHERE run_id=? ORDER BY id DESC", (run_id,)) if run else [],
        "pending_questions": db.fetchall(
            "SELECT * FROM agent_questions WHERE run_id=? AND state='pending' ORDER BY id",
            (run_id,),
        ) if run else [],
        "agent_inputs": db.fetchall("SELECT * FROM agent_inputs WHERE run_id=? ORDER BY id DESC", (run_id,)) if run else [],
        "sub_agents": db.fetchall("SELECT * FROM sub_agents WHERE run_id=? ORDER BY updated_at DESC", (run_id,)) if run else [],
        "ide_url": build_ide_url(run_id, run),
        "review_notes": db.code_review_notes(run_id) if run else [],
        "timeline": run_timeline(db, run_id, run),
        "log_markers": run_log_markers(db, run_id) if run else [],
    }
    return data


def run_timeline(db: Database, run_id: int, run) -> list[dict[str, str]]:
    if not run:
        return []
    items: list[dict[str, str]] = [
        {"time": run["created_at"], "title": "Run created", "detail": f"Ticket {run['ticket_key']} entered the worker."}
    ]
    state_titles = {
        "preparing_git": "Git preparation",
        "running_claude": "Claude implementation",
        "reviewing": "Review",
        "needs_cr_fix": "Needs CR fix",
        "done": "Done",
        "failed": "Failed",
        "cancelled": "Cancelled",
        "pushed": "Pushed",
    }
    if run["state"] in state_titles:
        items.append({"time": run["updated_at"], "title": state_titles[run["state"]], "detail": run["error"] or f"Progress {run['progress']}%"})
    if run["branch_name"]:
        items.append({"time": run["updated_at"], "title": "Branch selected", "detail": run["branch_name"]})
    if run["commit_sha"]:
        items.append({"time": run["updated_at"], "title": "Commit created", "detail": run["commit_sha"]})
    if run["pushed_at"]:
        items.append({"time": run["pushed_at"], "title": "Branch pushed", "detail": run["branch_name"]})
    phase_rows = db.fetchall(
        """
        SELECT phase, MIN(created_at) AS first_seen
        FROM logs
        WHERE run_id=?
        GROUP BY phase
        ORDER BY first_seen
        """,
        (run_id,),
    )
    for index, row in enumerate(phase_rows):
        end_time = run["finished_at"] or run["updated_at"] or row["first_seen"]
        if index + 1 < len(phase_rows):
            end_time = phase_rows[index + 1]["first_seen"] or end_time
        items.append(
            {
                "time": row["first_seen"],
                "title": f"{row['phase']} logs",
                "detail": f"Duration {duration_text(row['first_seen'], end_time)}",
                "anchor": phase_anchor(str(row["phase"])),
            }
        )
    return sorted(items, key=lambda item: item["time"] or "")


def run_log_markers(db: Database, run_id: int) -> list[dict[str, str]]:
    rows = db.fetchall(
        """
        SELECT phase, MIN(created_at) AS started_at, COUNT(*) AS line_count
        FROM logs
        WHERE run_id=?
        GROUP BY phase
        ORDER BY started_at
        """,
        (run_id,),
    )
    return [
        {
            "phase": str(row["phase"]),
            "started_at": str(row["started_at"] or ""),
            "line_count": str(row["line_count"] or "0"),
            "anchor": phase_anchor(str(row["phase"])),
        }
        for row in rows
    ]


def run_interaction_context(db: Database, run_id: int) -> dict:
    run = db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
    return {
        "run": run,
        "questions": db.fetchall("SELECT * FROM agent_questions WHERE run_id=? ORDER BY id DESC", (run_id,)),
        "agent_inputs": db.fetchall("SELECT * FROM agent_inputs WHERE run_id=? ORDER BY id DESC", (run_id,)),
        "sub_agents": db.fetchall("SELECT * FROM sub_agents WHERE run_id=? ORDER BY updated_at DESC", (run_id,)),
    }


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def pop_flash(request: Request) -> str:
    flash = getattr(request.app.state, "flash", "")
    request.app.state.flash = ""
    return flash


def split_lines(value: str) -> list[str]:
    return [line.strip() for line in value.replace(",", "\n").splitlines() if line.strip()]


def diff_lines(text: str) -> list[dict[str, str]]:
    """Classify unified-diff lines for colorized, air-gapped rendering."""
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        if line.startswith(("+++", "---", "diff ", "index ")):
            cls = "diff-meta"
        elif line.startswith("@@"):
            cls = "diff-hunk"
        elif line.startswith("+"):
            cls = "diff-add"
        elif line.startswith("-"):
            cls = "diff-del"
        else:
            cls = "diff-ctx"
        rows.append({"cls": cls, "text": line})
    return rows


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


def workspace_root(run) -> Path:
    if not run or not run["workspace_path"]:
        raise ValueError("Workspace is not available yet")
    root = Path(run["workspace_path"])
    if not root.exists() or not root.is_dir():
        raise ValueError("Workspace folder does not exist")
    return root


def list_workspace_files(run) -> list[dict[str, str]]:
    try:
        root = workspace_root(run)
    except ValueError:
        return []
    ignored_dirs = {".git", "node_modules", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache"}
    files: list[dict[str, str]] = []
    # No file-count cap: list every file in the repo so the IDE tree is complete.
    # Build/VCS noise dirs are still skipped (pruned so we don't descend into
    # them). Large files are listed too; the editor guards size on open.
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignored_dirs]
        for filename in filenames:
            full = Path(dirpath) / filename
            rel = full.relative_to(root).as_posix()
            files.append({"path": rel, "name": filename})
    return sorted(files, key=lambda item: item["path"].lower())


WORKSPACE_ZIP_IGNORED = {".git", "node_modules", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache"}


def build_workspace_zip(run, max_bytes: int = 200_000_000) -> bytes:
    """Zip the run's workspace (skipping VCS/build dirs) for an in-browser download."""
    root = workspace_root(run)  # raises ValueError when the workspace is gone
    buffer = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if any(part in WORKSPACE_ZIP_IGNORED for part in path.parts):
                continue
            if not path.is_file():
                continue
            try:
                total += path.stat().st_size
            except OSError:
                continue
            if total > max_bytes:
                raise ValueError("Workspace is too large to download (over 200 MB).")
            archive.write(path, path.relative_to(root).as_posix())
    return buffer.getvalue()


def safe_workspace_file(run, relative_path: str) -> Path:
    clean = (relative_path or "").strip().replace("\\", "/")
    if not clean:
        raise ValueError("Select a file first")
    root = workspace_root(run)
    path = ensure_child_path(root, root / clean)
    if not path.exists() or not path.is_file():
        raise ValueError("File does not exist in workspace")
    return path


def git_head_file(workspace_path: str, relative_path: str) -> str:
    return _git_show_file(workspace_path, "HEAD", relative_path)


def git_base_file(workspace_path: str, remote: str, base: str, relative_path: str) -> str:
    """The file as it exists on the base branch, for branch-vs-base diffs.

    Tries the remote-tracking base first (origin/main), then a merge-base, then
    the local base branch. Returns "" when the file is new on this branch.
    """
    candidates = [f"{remote}/{base}", base]
    merge_base = _git_capture(workspace_path, ["merge-base", f"{remote}/{base}", "HEAD"])
    if merge_base:
        candidates.insert(0, merge_base)
    for ref in candidates:
        content = _git_show_file(workspace_path, ref, relative_path)
        if content:
            return content
    return ""


def _git_show_file(workspace_path: str, ref: str, relative_path: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "show", f"{ref}:{relative_path}"],
            cwd=workspace_path,
            text=True,
            capture_output=True,
            check=False,
        )
    except Exception:
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _git_capture(workspace_path: str, args: list[str]) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=workspace_path, text=True, capture_output=True, check=False)
    except Exception:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def parse_ci_jobs(raw: str) -> list[dict[str, str]]:
    if not raw.strip():
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


def render_ci_context(ci_jobs: list[dict[str, str]]) -> str:
    if not ci_jobs:
        return ""
    lines = []
    for job in ci_jobs:
        lines.append(
            f"- {job.get('name', 'job')}: status={job.get('status', '')}, conclusion={job.get('conclusion', '')}, summary={job.get('summary', '')}"
        )
    return "\n".join(lines)


def phase_anchor(phase: str) -> str:
    clean = "".join(ch.lower() if ch.isalnum() else "-" for ch in phase).strip("-")
    while "--" in clean:
        clean = clean.replace("--", "-")
    return f"log-{clean or 'phase'}"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("T", " ").split(".")[0].split("+")[0].strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def run_stats(db: Database, owner: str) -> dict:
    """Aggregate run history for the stats dashboard (owner-scoped)."""
    rows = db.fetchall("SELECT state, created_at, finished_at FROM runs WHERE owner=?", (owner,))
    by_state: dict[str, int] = {}
    per_day: dict[str, int] = {}
    durations: list[float] = []
    for row in rows:
        by_state[row["state"]] = by_state.get(row["state"], 0) + 1
        created = _parse_ts(row["created_at"])
        if created:
            key = created.date().isoformat()
            per_day[key] = per_day.get(key, 0) + 1
        finished = _parse_ts(row["finished_at"])
        if created and finished and finished >= created:
            durations.append((finished - created).total_seconds())

    total = len(rows)
    success = by_state.get("done", 0) + by_state.get("pushed", 0)
    failed = by_state.get("failed", 0) + by_state.get("cancelled", 0)
    avg_seconds = int(sum(durations) / len(durations)) if durations else 0

    today = datetime.utcnow().date()
    series = []
    for offset in range(13, -1, -1):
        day = today - timedelta(days=offset)
        series.append({"label": day.strftime("%m-%d"), "count": per_day.get(day.isoformat(), 0)})
    max_count = max([point["count"] for point in series] or [0])

    recent = db.fetchall(
        """
        SELECT r.*, t.summary
        FROM runs r LEFT JOIN tickets t ON t.key = r.ticket_key AND t.owner = r.owner
        WHERE r.owner=? ORDER BY r.id DESC LIMIT 20
        """,
        (owner,),
    )
    return {
        "total": total,
        "success": success,
        "failed": failed,
        "active": total - success - failed,
        "success_rate": round(100 * success / total) if total else 0,
        "by_state": sorted(by_state.items(), key=lambda kv: -kv[1]),
        "avg_duration": _seconds_text(avg_seconds),
        "completed_count": len(durations),
        "series": series,
        "max_count": max_count,
        "recent": recent,
    }


def _seconds_text(total_seconds: int) -> str:
    if total_seconds <= 0:
        return "-"
    minutes, seconds = divmod(int(total_seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def duration_text(start: str | None, end: str | None) -> str:
    if not start or not end:
        return "-"
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return "-"
    total_seconds = int(max(0, (end_dt - start_dt).total_seconds()))
    minutes, seconds = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


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
    owner: str = DEFAULT_OWNER,
) -> asyncio.Task[object]:
    task = asyncio.create_task(coro)

    def _on_done(done_task: asyncio.Task[object]) -> None:
        try:
            done_task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            error_message = f"{message} Error: {exc}"
            db.add_notification(title, error_message, "error", run_id, owner=owner)
            print(error_message)

    task.add_done_callback(_on_done)
    return task
