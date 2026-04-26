from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS tickets (
    key TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    status TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    labels TEXT NOT NULL DEFAULT '',
    eligibility TEXT NOT NULL DEFAULT 'discovered',
    skip_reason TEXT NOT NULL DEFAULT '',
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS queue_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_key TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_key TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0,
    repo_url TEXT NOT NULL DEFAULT '',
    base_branch TEXT NOT NULL DEFAULT '',
    branch_name TEXT NOT NULL DEFAULT '',
    workspace_path TEXT NOT NULL DEFAULT '',
    review_output TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    pushed_at TEXT,
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

    def upsert_ticket(self, ticket: dict[str, Any]) -> None:
        self.execute(
            """
            INSERT INTO tickets(key, summary, status, url, description, labels, eligibility, skip_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
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

    def enqueue(self, ticket_key: str) -> None:
        existing = self.fetchone(
            "SELECT id FROM queue_items WHERE ticket_key=? AND state IN ('queued', 'running')",
            (ticket_key,),
        )
        if existing:
            return
        priority_row = self.fetchone("SELECT COALESCE(MAX(priority), 0) + 1 AS next_priority FROM queue_items")
        priority = int(priority_row["next_priority"] if priority_row else 1)
        self.execute(
            "INSERT INTO queue_items(ticket_key, priority, state) VALUES (?, ?, 'queued')",
            (ticket_key, priority),
        )

    def next_queue_item(self) -> sqlite3.Row | None:
        return self.fetchone(
            """
            SELECT q.*, t.summary, t.description, t.status, t.url, t.labels
            FROM queue_items q
            JOIN tickets t ON t.key = q.ticket_key
            WHERE q.state='queued'
            ORDER BY q.priority ASC, q.id ASC
            LIMIT 1
            """
        )

    def create_run(self, ticket_key: str) -> int:
        with self.connect() as conn:
            cur = conn.execute("INSERT INTO runs(ticket_key, state) VALUES (?, 'queued')", (ticket_key,))
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

    def create_agent_question(
        self,
        run_id: int,
        question: str,
        options: list[str],
        agent_name: str = "main",
        free_input_enabled: bool = True,
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
            return int(cur.lastrowid)

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

    def set_queue_state(self, queue_id: int, state: str) -> None:
        self.execute(
            "UPDATE queue_items SET state=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (state, queue_id),
        )

    def reorder_queue(self, ordered_ids: list[int]) -> None:
        with self.connect() as conn:
            for index, item_id in enumerate(ordered_ids, start=1):
                conn.execute(
                    "UPDATE queue_items SET priority=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (index, item_id),
                )
            conn.commit()
