# Jira Claude Worker

Linux-first local web app for controlling a Jira-to-Claude automation worker.

The website and its interaction model are the important part of this project. The included Python worker is a functional placeholder so the UI has something to call. If you already have the real automation code, wire it to the routes/database contract described in `WORKER_INTEGRATION_CONTRACT.txt`.

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
pytest
```

## Notes

- Secrets are stored in `config.yaml` as requested. The UI and logs mask known secret values.
- Python owns all Git operations. Claude prompts explicitly forbid Git commands.
- The Claude executable is configurable at `claude.command`; use an absolute path if `claude` is not on `PATH`.
- See `WORKER_INTEGRATION_CONTRACT.txt` for exactly what your real Python worker should export/update for the website.
