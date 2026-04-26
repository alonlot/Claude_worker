from __future__ import annotations

import argparse
import asyncio

import uvicorn

from app.config import load_config
from app.db import Database
from app.runner import Worker
from app.web import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Jira to Claude worker")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="Start the local website")
    sub.add_parser("init-db", help="Create or migrate the SQLite database")
    sub.add_parser("run-once", help="Scan Jira and run one queued ticket")
    sub.add_parser("run-interval", help="Run forever on the configured interval")
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

    worker = Worker(config, db)
    if args.command == "run-once":
        asyncio.run(worker.scan_and_run_once())
    elif args.command == "run-interval":
        asyncio.run(worker.run_interval_forever())


if __name__ == "__main__":
    main()
