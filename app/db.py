from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.config import DEFAULT_OWNER


# Owned tables keyed by an INTEGER id: adding an `owner` column is a plain ALTER.
SIMPLE_OWNED_TABLES = (
    "queue_items",
    "runs",
    "ticket_plans",
    "code_review_notes",
    "notifications",
)

# Owned tables with a natural primary key that must become composite (owner, key).
# A legacy single-column PK can't be altered in place, so these are rebuilt.
# Maps table name -> CREATE TABLE statement used when rebuilding.
REBUILD_OWNED_TABLES = {
    "tickets": """
        CREATE TABLE tickets (
            key TEXT NOT NULL,
            owner TEXT NOT NULL DEFAULT 'local',
            summary TEXT NOT NULL,
            status TEXT NOT NULL,
            url TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            labels TEXT NOT NULL DEFAULT '',
            eligibility TEXT NOT NULL DEFAULT 'discovered',
            skip_reason TEXT NOT NULL DEFAULT '',
            last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (owner, key)
        )
    """,
    "app_state": """
        CREATE TABLE app_state (
            key TEXT NOT NULL,
            owner TEXT NOT NULL DEFAULT 'local',
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (owner, key)
        )
    """,
}


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS user_configs (
    username TEXT PRIMARY KEY,
    data TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tickets (
    key TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT 'local',
    summary TEXT NOT NULL,
    status TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    labels TEXT NOT NULL DEFAULT '',
    eligibility TEXT NOT NULL DEFAULT 'discovered',
    skip_reason TEXT NOT NULL DEFAULT '',
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (owner, key)
);

CREATE TABLE IF NOT EXISTS queue_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_key TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT 'local',
    priority INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ticket_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_key TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT 'local',
    queue_item_id INTEGER,
    state TEXT NOT NULL DEFAULT 'draft',
    repo_url TEXT NOT NULL DEFAULT '',
    base_branch TEXT NOT NULL DEFAULT '',
    branch_name TEXT NOT NULL DEFAULT '',
    mission TEXT NOT NULL DEFAULT '',
    plan_text TEXT NOT NULL DEFAULT '',
    user_notes TEXT NOT NULL DEFAULT '',
    raw_output TEXT NOT NULL DEFAULT '',
    skill_ids TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ticket_plans_queue ON ticket_plans(queue_item_id);

CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'private',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS skill_likes (
    username TEXT NOT NULL,
    skill_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (username, skill_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_key TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT 'local',
    state TEXT NOT NULL DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0,
    repo_url TEXT NOT NULL DEFAULT '',
    base_branch TEXT NOT NULL DEFAULT '',
    branch_name TEXT NOT NULL DEFAULT '',
    workspace_path TEXT NOT NULL DEFAULT '',
    review_output TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    pushed_at TEXT,
    commit_sha TEXT NOT NULL DEFAULT '',
    commit_message TEXT NOT NULL DEFAULT '',
    changed_files TEXT NOT NULL DEFAULT '',
    diff_summary TEXT NOT NULL DEFAULT '',
    run_report TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    phase TEXT NOT NULL,
    line TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    agent_name TEXT NOT NULL DEFAULT 'main',
    question TEXT NOT NULL,
    option_a TEXT NOT NULL,
    option_b TEXT NOT NULL,
    option_c TEXT NOT NULL,
    free_input_enabled INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'pending',
    answer TEXT NOT NULL DEFAULT '',
    answer_source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    answered_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_inputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    consumed_at TEXT
);

CREATE TABLE IF NOT EXISTS sub_agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    task TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'starting',
    progress INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sub_agents_run_name ON sub_agents(run_id, name);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    owner TEXT NOT NULL DEFAULT 'local',
    level TEXT NOT NULL DEFAULT 'info',
    title TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT 'local',
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (owner, key)
);

CREATE TABLE IF NOT EXISTS code_review_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    owner TEXT NOT NULL DEFAULT 'local',
    provider TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    external_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'review',
    author TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL DEFAULT '',
    line INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL DEFAULT '',
    html_url TEXT NOT NULL DEFAULT '',
    response TEXT NOT NULL DEFAULT '',
    response_url TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    responded_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cr_notes_run_external ON code_review_notes(run_id, external_id, kind);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._ensure_columns(conn)

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        runs_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        for name, definition in {
            "commit_sha": "TEXT NOT NULL DEFAULT ''",
            "commit_message": "TEXT NOT NULL DEFAULT ''",
            "changed_files": "TEXT NOT NULL DEFAULT ''",
            "diff_summary": "TEXT NOT NULL DEFAULT ''",
            "run_report": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in runs_columns:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")

        plan_columns = {row["name"] for row in conn.execute("PRAGMA table_info(ticket_plans)").fetchall()}
        if "skill_ids" not in plan_columns:
            conn.execute("ALTER TABLE ticket_plans ADD COLUMN skill_ids TEXT NOT NULL DEFAULT ''")

        # Backfill the owner column on databases created before multi-user support.
        for table in SIMPLE_OWNED_TABLES:
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "owner" not in columns:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN owner TEXT NOT NULL DEFAULT '{DEFAULT_OWNER}'"
                )

        # tickets/app_state need a composite primary key, which means a rebuild
        # when migrating from the old single-column-PK schema.
        for table, create_sql in REBUILD_OWNED_TABLES.items():
            self._rebuild_owner_table(conn, table, create_sql)

        conn.commit()

    def _rebuild_owner_table(self, conn: sqlite3.Connection, table: str, create_sql: str) -> None:
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        columns = {row["name"] for row in info}
        if "owner" in columns:
            return  # already on the new schema
        legacy_cols = [row["name"] for row in info]
        shared = ", ".join(f'"{col}"' for col in legacy_cols)
        # Legacy rows may have NULL timestamps; the new schema marks them NOT NULL.
        select_exprs = ", ".join(
            f'COALESCE("{col}", CURRENT_TIMESTAMP)' if col.endswith("_at") else f'"{col}"'
            for col in legacy_cols
        )
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy")
        conn.executescript(create_sql)
        conn.execute(
            f"INSERT INTO {table} ({shared}, owner) SELECT {select_exprs}, '{DEFAULT_OWNER}' FROM {table}_legacy"
        )
        conn.execute(f"DROP TABLE {table}_legacy")

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self.connect() as conn:
            conn.execute(sql, params)
            conn.commit()

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchone()

    # ----- users & per-user config ---------------------------------------

    def upsert_user(
        self,
        username: str,
        display_name: str = "",
        password_hash: str | None = None,
        role: str = "user",
    ) -> None:
        existing = self.get_user(username)
        if existing:
            self.execute(
                "UPDATE users SET display_name=?, role=? WHERE username=?",
                (display_name or existing["display_name"], role or existing["role"], username),
            )
            if password_hash is not None:
                self.execute("UPDATE users SET password_hash=? WHERE username=?", (password_hash, username))
            return
        self.execute(
            "INSERT INTO users(username, display_name, password_hash, role) VALUES (?, ?, ?, ?)",
            (username, display_name or username, password_hash or "", role),
        )

    def get_user(self, username: str) -> sqlite3.Row | None:
        return self.fetchone("SELECT * FROM users WHERE username=?", (username,))

    def list_users(self) -> list[sqlite3.Row]:
        return self.fetchall("SELECT * FROM users ORDER BY username")

    def set_user_password(self, username: str, password_hash: str) -> None:
        self.execute("UPDATE users SET password_hash=? WHERE username=?", (password_hash, username))

    def touch_user_login(self, username: str) -> None:
        self.execute("UPDATE users SET last_login_at=CURRENT_TIMESTAMP WHERE username=?", (username,))

    def get_user_config(self, username: str) -> dict[str, Any]:
        row = self.fetchone("SELECT data FROM user_configs WHERE username=?", (username,))
        if not row or not row["data"]:
            return {}
        try:
            data = json.loads(row["data"])
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def set_user_config(self, username: str, data: dict[str, Any]) -> None:
        payload = json.dumps(data)
        self.execute(
            """
            INSERT INTO user_configs(username, data) VALUES (?, ?)
            ON CONFLICT(username) DO UPDATE SET data=excluded.data, updated_at=CURRENT_TIMESTAMP
            """,
            (username, payload),
        )

    # ----- tickets --------------------------------------------------------

    def upsert_ticket(self, ticket: dict[str, Any], owner: str = DEFAULT_OWNER) -> None:
        self.execute(
            """
            INSERT INTO tickets(owner, key, summary, status, url, description, labels, eligibility, skip_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner, key) DO UPDATE SET
                summary=excluded.summary,
                status=excluded.status,
                url=excluded.url,
                description=excluded.description,
                labels=excluded.labels,
                eligibility=excluded.eligibility,
                skip_reason=excluded.skip_reason,
                last_seen_at=CURRENT_TIMESTAMP,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                owner,
                ticket["key"],
                ticket["summary"],
                ticket["status"],
                ticket.get("url", ""),
                ticket.get("description", ""),
                ",".join(ticket.get("labels", [])),
                ticket.get("eligibility", "discovered"),
                ticket.get("skip_reason", ""),
            ),
        )

    def enqueue(self, ticket_key: str, state: str = "needs_plan", owner: str = DEFAULT_OWNER) -> None:
        existing = self.fetchone(
            """
            SELECT id FROM queue_items
            WHERE ticket_key=? AND owner=?
              AND state IN ('needs_plan', 'planning', 'plan_ready', 'queued', 'running')
            """,
            (ticket_key, owner),
        )
        if existing:
            return
        priority_row = self.fetchone(
            "SELECT COALESCE(MAX(priority), 0) + 1 AS next_priority FROM queue_items WHERE owner=?",
            (owner,),
        )
        priority = int(priority_row["next_priority"] if priority_row else 1)
        self.execute(
            "INSERT INTO queue_items(ticket_key, owner, priority, state) VALUES (?, ?, ?, ?)",
            (ticket_key, owner, priority, state),
        )

    def next_queue_item(self, owner: str = DEFAULT_OWNER) -> sqlite3.Row | None:
        return self.fetchone(
            """
            SELECT q.*, t.summary, t.description, t.status, t.url, t.labels
            FROM queue_items q
            JOIN tickets t ON t.key = q.ticket_key AND t.owner = q.owner
            WHERE q.state='queued' AND q.owner=?
            ORDER BY q.priority ASC, q.id ASC
            LIMIT 1
            """,
            (owner,),
        )

    def queue_item(self, queue_id: int) -> sqlite3.Row | None:
        return self.fetchone(
            """
            SELECT q.*, t.summary, t.description, t.status, t.url, t.labels
            FROM queue_items q
            JOIN tickets t ON t.key = q.ticket_key AND t.owner = q.owner
            WHERE q.id=?
            """,
            (queue_id,),
        )

    # ----- runs -----------------------------------------------------------

    def create_run(self, ticket_key: str, owner: str = DEFAULT_OWNER) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs(ticket_key, owner, state) VALUES (?, ?, 'queued')",
                (ticket_key, owner),
            )
            conn.commit()
            return int(cur.lastrowid)

    def update_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = tuple(fields.values()) + (run_id,)
        self.execute(f"UPDATE runs SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE id=?", values)

    def add_log(self, run_id: int, phase: str, line: str) -> None:
        self.execute("INSERT INTO logs(run_id, phase, line) VALUES (?, ?, ?)", (run_id, phase, line.rstrip()))

    # ----- agent interaction ---------------------------------------------

    def create_agent_question(
        self,
        run_id: int,
        question: str,
        options: list[str],
        agent_name: str = "main",
        free_input_enabled: bool = True,
        owner: str = DEFAULT_OWNER,
    ) -> int:
        padded = (options + ["", "", ""])[:3]
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO agent_questions(
                    run_id, agent_name, question, option_a, option_b, option_c, free_input_enabled
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, agent_name, question, padded[0], padded[1], padded[2], int(free_input_enabled)),
            )
            conn.commit()
            question_id = int(cur.lastrowid)
        self.add_notification("Agent question waiting", question, "warning", run_id, owner=owner)
        return question_id

    def answer_agent_question(self, question_id: int, answer: str, answer_source: str) -> None:
        self.execute(
            """
            UPDATE agent_questions
            SET state='answered', answer=?, answer_source=?, answered_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (answer, answer_source, question_id),
        )

    def add_agent_input(self, run_id: int, message: str) -> int:
        with self.connect() as conn:
            cur = conn.execute("INSERT INTO agent_inputs(run_id, message) VALUES (?, ?)", (run_id, message))
            conn.commit()
            return int(cur.lastrowid)

    def unconsumed_agent_inputs(self, run_id: int) -> list[sqlite3.Row]:
        return self.fetchall(
            "SELECT * FROM agent_inputs WHERE run_id=? AND consumed=0 ORDER BY id",
            (run_id,),
        )

    def mark_agent_input_consumed(self, input_id: int) -> None:
        self.execute(
            "UPDATE agent_inputs SET consumed=1, consumed_at=CURRENT_TIMESTAMP WHERE id=?",
            (input_id,),
        )

    def upsert_sub_agent(
        self,
        run_id: int,
        name: str,
        task: str = "",
        status: str = "running",
        progress: int = 0,
        summary: str = "",
    ) -> None:
        self.execute(
            """
            INSERT INTO sub_agents(run_id, name, task, status, progress, summary)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, name) DO UPDATE SET
                task=excluded.task,
                status=excluded.status,
                progress=excluded.progress,
                summary=excluded.summary,
                updated_at=CURRENT_TIMESTAMP
            """,
            (run_id, name, task, status, max(0, min(100, int(progress))), summary),
        )

    # ----- notifications --------------------------------------------------

    def add_notification(
        self,
        title: str,
        message: str = "",
        level: str = "info",
        run_id: int | None = None,
        owner: str = DEFAULT_OWNER,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO notifications(run_id, owner, level, title, message) VALUES (?, ?, ?, ?, ?)",
                (run_id, owner, level, title, message),
            )
            conn.commit()
            return int(cur.lastrowid)

    def unread_notifications(self, owner: str = DEFAULT_OWNER) -> list[sqlite3.Row]:
        return self.fetchall(
            "SELECT * FROM notifications WHERE read=0 AND owner=? ORDER BY id DESC LIMIT 20",
            (owner,),
        )

    def mark_notifications_read(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self.execute(f"UPDATE notifications SET read=1 WHERE id IN ({placeholders})", tuple(ids))

    # ----- app state (per owner) -----------------------------------------

    def get_state(self, key: str, default: str = "", owner: str = DEFAULT_OWNER) -> str:
        row = self.fetchone("SELECT value FROM app_state WHERE key=? AND owner=?", (key, owner))
        return row["value"] if row else default

    def set_state(self, key: str, value: str, owner: str = DEFAULT_OWNER) -> None:
        self.execute(
            """
            INSERT INTO app_state(key, owner, value) VALUES (?, ?, ?)
            ON CONFLICT(owner, key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
            """,
            (key, owner, value),
        )

    def queue_paused(self, owner: str = DEFAULT_OWNER) -> bool:
        return self.get_state("queue_paused", "0", owner=owner) == "1"

    def set_queue_state(self, queue_id: int, state: str) -> None:
        self.execute(
            "UPDATE queue_items SET state=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (state, queue_id),
        )

    def delete_queue_item(self, queue_id: int) -> None:
        self.execute("DELETE FROM queue_items WHERE id=? AND state!='running'", (queue_id,))

    def clear_finished_queue_items(self, owner: str = DEFAULT_OWNER) -> None:
        self.execute(
            "DELETE FROM queue_items WHERE owner=? AND state IN ('done', 'failed', 'cancelled')",
            (owner,),
        )

    # ----- ticket plans ---------------------------------------------------

    def upsert_ticket_plan(self, plan: dict[str, Any], owner: str = DEFAULT_OWNER) -> int:
        existing = self.fetchone("SELECT id FROM ticket_plans WHERE queue_item_id=?", (plan.get("queue_item_id"),))
        if existing:
            self.execute(
                """
                UPDATE ticket_plans
                SET state=?, repo_url=?, base_branch=?, branch_name=?, mission=?, plan_text=?,
                    user_notes=?, raw_output=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    plan.get("state", "draft"),
                    plan.get("repo_url", ""),
                    plan.get("base_branch", ""),
                    plan.get("branch_name", ""),
                    plan.get("mission", ""),
                    plan.get("plan_text", ""),
                    plan.get("user_notes", ""),
                    plan.get("raw_output", ""),
                    existing["id"],
                ),
            )
            return int(existing["id"])
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO ticket_plans(
                    ticket_key, owner, queue_item_id, state, repo_url, base_branch, branch_name,
                    mission, plan_text, user_notes, raw_output
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan["ticket_key"],
                    plan.get("owner", owner),
                    plan.get("queue_item_id"),
                    plan.get("state", "draft"),
                    plan.get("repo_url", ""),
                    plan.get("base_branch", ""),
                    plan.get("branch_name", ""),
                    plan.get("mission", ""),
                    plan.get("plan_text", ""),
                    plan.get("user_notes", ""),
                    plan.get("raw_output", ""),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def plan_for_queue_item(self, queue_id: int) -> sqlite3.Row | None:
        return self.fetchone("SELECT * FROM ticket_plans WHERE queue_item_id=? ORDER BY id DESC LIMIT 1", (queue_id,))

    def recover_interrupted_work(self) -> int:
        active_runs = self.fetchall(
            "SELECT id, ticket_key, owner FROM runs WHERE state IN ('preparing_git','running_claude','reviewing')"
        )
        for run in active_runs:
            self.execute(
                """
                UPDATE runs
                SET state='failed',
                    error='Recovered after app restart. Previous worker process is no longer attached.',
                    finished_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (int(run["id"]),),
            )
            self.add_notification(
                "Run recovered as failed",
                run["ticket_key"],
                "warning",
                int(run["id"]),
                owner=run["owner"],
            )
        self.execute(
            """
            UPDATE queue_items
            SET state='failed', updated_at=CURRENT_TIMESTAMP
            WHERE state IN ('planning','running')
            """
        )
        return len(active_runs)

    # ----- code review notes ---------------------------------------------

    def upsert_code_review_note(self, run_id: int, note: dict[str, Any], owner: str = DEFAULT_OWNER) -> int:
        existing = self.fetchone(
            "SELECT id FROM code_review_notes WHERE run_id=? AND external_id=? AND kind=?",
            (run_id, note.get("external_id", ""), note.get("kind", "review")),
        )
        values = (
            note.get("provider", ""),
            note.get("source_url", ""),
            note.get("external_id", ""),
            note.get("kind", "review"),
            note.get("author", ""),
            note.get("file_path", ""),
            int(note.get("line") or 0),
            note.get("body", ""),
            note.get("html_url", ""),
        )
        if existing:
            self.execute(
                """
                UPDATE code_review_notes
                SET provider=?, source_url=?, external_id=?, kind=?, author=?, file_path=?,
                    line=?, body=?, html_url=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                values + (existing["id"],),
            )
            return int(existing["id"])
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO code_review_notes(
                    run_id, owner, provider, source_url, external_id, kind, author, file_path, line, body, html_url
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, owner) + values,
            )
            conn.commit()
            return int(cur.lastrowid)

    def code_review_notes(self, run_id: int) -> list[sqlite3.Row]:
        return self.fetchall("SELECT * FROM code_review_notes WHERE run_id=? ORDER BY id", (run_id,))

    def mark_code_review_note_responded(self, note_id: int, response: str, response_url: str = "") -> None:
        self.execute(
            """
            UPDATE code_review_notes
            SET response=?, response_url=?, state='responded', responded_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (response, response_url, note_id),
        )

    # ----- skills ---------------------------------------------------------

    def create_skill(
        self,
        owner: str,
        name: str,
        description: str = "",
        content: str = "",
        visibility: str = "private",
    ) -> int:
        visibility = "public" if visibility == "public" else "private"
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO skills(owner, name, description, content, visibility) VALUES (?, ?, ?, ?, ?)",
                (owner, name, description, content, visibility),
            )
            conn.commit()
            return int(cur.lastrowid)

    def update_skill(
        self,
        skill_id: int,
        owner: str,
        name: str,
        description: str,
        content: str,
        visibility: str,
    ) -> bool:
        visibility = "public" if visibility == "public" else "private"
        existing = self.fetchone("SELECT owner FROM skills WHERE id=?", (skill_id,))
        if not existing or existing["owner"] != owner:
            return False
        self.execute(
            """
            UPDATE skills SET name=?, description=?, content=?, visibility=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND owner=?
            """,
            (name, description, content, visibility, skill_id, owner),
        )
        return True

    def delete_skill(self, skill_id: int, owner: str) -> bool:
        existing = self.fetchone("SELECT owner FROM skills WHERE id=?", (skill_id,))
        if not existing or existing["owner"] != owner:
            return False
        self.execute("DELETE FROM skills WHERE id=? AND owner=?", (skill_id, owner))
        self.execute("DELETE FROM skill_likes WHERE skill_id=?", (skill_id,))
        return True

    def get_skill(self, skill_id: int) -> sqlite3.Row | None:
        return self.fetchone("SELECT * FROM skills WHERE id=?", (skill_id,))

    def list_skills(self, owner: str) -> list[sqlite3.Row]:
        return self.fetchall("SELECT * FROM skills WHERE owner=? ORDER BY name", (owner,))

    def list_public_skills(self) -> list[sqlite3.Row]:
        return self.fetchall(
            """
            SELECT s.*, COALESCE(l.like_count, 0) AS like_count
            FROM skills s
            LEFT JOIN (SELECT skill_id, COUNT(*) AS like_count FROM skill_likes GROUP BY skill_id) l
              ON l.skill_id = s.id
            WHERE s.visibility='public'
            ORDER BY like_count DESC, s.name
            """
        )

    def like_skill(self, username: str, skill_id: int) -> None:
        self.execute(
            "INSERT OR IGNORE INTO skill_likes(username, skill_id) VALUES (?, ?)",
            (username, skill_id),
        )

    def unlike_skill(self, username: str, skill_id: int) -> None:
        self.execute("DELETE FROM skill_likes WHERE username=? AND skill_id=?", (username, skill_id))

    def liked_skill_ids(self, username: str) -> set[int]:
        rows = self.fetchall("SELECT skill_id FROM skill_likes WHERE username=?", (username,))
        return {int(row["skill_id"]) for row in rows}

    def liked_skills(self, username: str) -> list[sqlite3.Row]:
        return self.fetchall(
            """
            SELECT s.* FROM skills s
            JOIN skill_likes l ON l.skill_id = s.id
            WHERE l.username=?
            ORDER BY s.name
            """,
            (username,),
        )

    def skills_by_ids(self, skill_ids: list[int]) -> list[sqlite3.Row]:
        if not skill_ids:
            return []
        placeholders = ",".join("?" for _ in skill_ids)
        return self.fetchall(f"SELECT * FROM skills WHERE id IN ({placeholders})", tuple(skill_ids))

    def reorder_queue(self, ordered_ids: list[int]) -> None:
        with self.connect() as conn:
            for index, item_id in enumerate(ordered_ids, start=1):
                conn.execute(
                    "UPDATE queue_items SET priority=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (index, item_id),
                )
            conn.commit()
