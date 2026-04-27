from __future__ import annotations

from datetime import datetime

from app.db import Database


def seed_demo(db: Database) -> int:
    ticket_key = "DEMO-101"
    db.upsert_ticket(
        {
            "key": ticket_key,
            "summary": "Polish dashboard empty states",
            "status": "Ready for Claude",
            "url": "https://example.atlassian.net/browse/DEMO-101",
            "description": (
                "Demo ticket: improve dashboard empty states, make queue actions clearer, "
                "and verify that code review notes can be handled from the app."
            ),
            "labels": ["demo", "ui", "claude"],
            "eligibility": "eligible",
            "skip_reason": "",
        }
    )
    db.enqueue(ticket_key)
    queue = db.fetchone("SELECT id FROM queue_items WHERE ticket_key=? ORDER BY id DESC LIMIT 1", (ticket_key,))
    if queue:
        db.set_queue_state(int(queue["id"]), "plan_ready")
        db.upsert_ticket_plan(
            {
                "ticket_key": ticket_key,
                "queue_item_id": int(queue["id"]),
                "state": "draft",
                "repo_url": "git@github.com:alonlot/Claude_worker.git",
                "base_branch": "main",
                "branch_name": "DEMO-101/by_claude_polish_dashboard_empty_states",
                "mission": "Make the dashboard feel ready even when there is no real Jira data yet.",
                "plan_text": (
                    "Update empty states, keep the queue actions easy to scan, run tests, "
                    "and produce a clean run report before push."
                ),
                "user_notes": "Demo seed data.",
                "raw_output": "{}",
            }
        )

    run_id = db.create_run(ticket_key)
    now = datetime.utcnow().isoformat()
    db.update_run(
        run_id,
        state="done",
        progress=100,
        repo_url="git@github.com:alonlot/Claude_worker.git",
        base_branch="main",
        branch_name="DEMO-101/by_claude_polish_dashboard_empty_states",
        workspace_path="workspaces/DEMO-101",
        review_output="REVIEW_RESULT: pass\nNo actionable findings in this demo run.",
        commit_sha="demoabc1234567890",
        commit_message="DEMO-101: Polish dashboard empty states",
        changed_files="app/templates/index.html\napp/static/styles.css",
        diff_summary=" app/templates/index.html | 12 +++++++-----\n app/static/styles.css    | 18 +++++++++++++-----",
        run_report=(
            "Ticket: DEMO-101\n"
            "State: done\n"
            "Repo: git@github.com:alonlot/Claude_worker.git\n"
            "Base branch: main\n"
            "Work branch: DEMO-101/by_claude_polish_dashboard_empty_states\n"
            "Commit: demoabc1234567890\n\n"
            "Review:\nREVIEW_RESULT: pass\nNo actionable findings in this demo run."
        ),
        finished_at=now,
    )
    for phase, line in [
        ("plan", "Approved mission: Make the dashboard feel ready with demo data."),
        ("git", "checked out DEMO-101/by_claude_polish_dashboard_empty_states"),
        ("claude", "PROGRESS 40%"),
        ("claude", "Updated dashboard empty state copy and queue affordances."),
        ("review", "REVIEW_RESULT: pass"),
        ("git", "Commit created: demoabc1234567890"),
    ]:
        db.add_log(run_id, phase, line)

    notes = [
        {
            "provider": "github",
            "source_url": "https://github.com/alonlot/Claude_worker/pull/101",
            "external_id": "9001",
            "kind": "review",
            "author": "reviewer-a",
            "file_path": "app/templates/index.html",
            "line": 42,
            "body": "Can the empty queue state explain the next action more clearly?",
            "html_url": "https://github.com/alonlot/Claude_worker/pull/101#discussion_r9001",
        },
        {
            "provider": "github",
            "source_url": "https://github.com/alonlot/Claude_worker/pull/101",
            "external_id": "9002",
            "kind": "conversation",
            "author": "reviewer-b",
            "file_path": "",
            "line": 0,
            "body": "Please confirm this does not auto-resolve review threads.",
            "html_url": "https://github.com/alonlot/Claude_worker/pull/101#issuecomment-9002",
        },
    ]
    for note in notes:
        db.upsert_code_review_note(run_id, note)
    db.add_notification("Demo run ready", f"Open run #{run_id} to inspect Push Preview and Code Review.", "info", run_id)
    return run_id
