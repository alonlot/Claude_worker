# Jira Claude Worker

Local web app for controlling a Jira-to-Claude automation worker.

The website is the control surface. The Python worker scans Jira, asks Claude for ticket routing JSON, clones and checks out the target Git branch itself, runs Claude for implementation and review, records logs/progress in SQLite, and lets the user push when ready.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp config.example.yaml config.yaml
python -m app init-db
python -m app serve
```

Open `http://127.0.0.1:8000`.

## Commands

```bash
python -m app serve
python -m app run-once
python -m app run-interval
python -m app init-db
python -m app seed-demo
pytest
```

## Notes

- Secrets are stored in `config.yaml` as requested. The UI and logs mask known secret values.
- Python owns all Git operations. Claude prompts explicitly forbid Git commands.
- Set `git.default_repo_url` and `git.default_base_branch` when most tickets should use the same repository.
- The Claude executable is configurable at `claude.command`; use an absolute path if `claude` is not on `PATH`.
- See `CONFIG_GUIDE.md` for setup, GitHub/GitLab review notes, and demo data instructions.
- See `WORKER_INTEGRATION_CONTRACT.txt` for the UI/database contract the worker follows.
