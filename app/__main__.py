from __future__ import annotations

import argparse
import asyncio
import getpass

import uvicorn

from app.auth import hash_password
from app.config import DEFAULT_OWNER, load_config
from app.db import Database
from app.demo import seed_demo
from app.runner import WorkerRegistry
from app.web import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Jira to Claude worker")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="Start the local website")
    sub.add_parser("init-db", help="Create or migrate the SQLite database")

    run_once = sub.add_parser("run-once", help="Scan Jira and run one queued ticket")
    run_once.add_argument("--user", default=DEFAULT_OWNER, help="Owner to run as")
    run_interval = sub.add_parser("run-interval", help="Run forever on the configured interval")
    run_interval.add_argument("--user", default=DEFAULT_OWNER, help="Owner to run as")

    sub.add_parser("seed-demo", help="Create a fake ticket/run/code-review demo")

    add_user = sub.add_parser("add-user", help="Create/update a login-page user")
    add_user.add_argument("username")
    add_user.add_argument("--display", default="", help="Display name")
    add_user.add_argument("--admin", action="store_true", help="Grant admin role")
    add_user.add_argument("--password", default="", help="Password (prompted if omitted)")

    args = parser.parse_args()

    config = load_config()
    db = Database(config.app.database_path)
    db.init()

    if args.command == "init-db":
        print(f"Initialized {config.app.database_path}")
        return

    if args.command == "serve":
        app = create_app(config, db)
        uvicorn.run(app, host=config.app.host, port=config.app.port)
        return

    if args.command == "seed-demo":
        run_id = seed_demo(db)
        print(f"Seeded demo run #{run_id}")
        return

    if args.command == "add-user":
        password = args.password or getpass.getpass("Password: ")
        db.upsert_user(
            args.username,
            display_name=args.display or args.username,
            password_hash=hash_password(password),
            role="admin" if args.admin else "user",
        )
        print(f"Saved user {args.username}{' (admin)' if args.admin else ''}")
        return

    registry = WorkerRegistry(config, db)
    worker = registry.for_user(args.user)
    if args.command == "run-once":
        asyncio.run(worker.scan_and_run_once())
    elif args.command == "run-interval":
        asyncio.run(worker.run_interval_forever())


if __name__ == "__main__":
    main()
