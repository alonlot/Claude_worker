# Jira Claude Worker

Multi-user web app for controlling a Jira-to-Claude automation worker. Designed to run on a server on your network: users sign in, each gets their own config, tickets, and isolated agent runs.

The website is the control surface. The Python worker scans Jira, asks Claude for ticket routing JSON, clones and checks out the target Git branch itself, runs Claude for implementation and review, records logs/progress in SQLite, and lets the user push when ready.

## Multi-user model

- **Authentication is pluggable** (`app/auth.py`). Ships with two providers, chosen by `auth.provider`:
  - `proxy_header` (default/production): trusts a username header set by your SSO/reverse proxy (`auth.header`, default `X-Forwarded-User`).
  - `login_page`: built-in username/password sign-in form. Create accounts with `python -m app add-user <name>`.
  - Adding LDAP/OAuth/etc. later = one new `AuthProvider` subclass; nothing else changes.
- **Per-user data separation**: one SQLite DB, every row scoped by `owner`. Users never see each other's tickets, runs, queue, or notifications.
- **Per-user config**: each user's Jira/Git/Claude credentials are saved under their username (Settings page). Server-level config (auth, Docker, host/port) is admin-only.
- **Isolated execution**: with `docker.enabled: true`, each run executes the Claude agent inside a throwaway, locked-down container (build the image from `docker/Dockerfile`). Git still runs on the host. With Docker off, runs use host subprocesses (dev/Windows).

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
python -m app run-once --user <username>
python -m app run-interval --user <username>
python -m app init-db
python -m app seed-demo
python -m app add-user <username> [--admin] [--display "Name"]   # login_page accounts
pytest
```

## Deploying on a network server

1. `cp config.example.yaml config.yaml` and set a strong `auth.session_secret`.
2. Choose auth:
   - Behind SSO: keep `auth.provider: proxy_header`, set `auth.default_user: ""`, and make the proxy send `auth.header` (e.g. `X-Forwarded-User`). Optionally restrict with `auth.allowed_users` and grant admins via `auth.admin_users`.
   - No SSO yet: set `auth.provider: login_page` and create accounts with `python -m app add-user`.
3. For isolation, set `docker.enabled: true` and build the agent image: `docker build -t claude-worker-agent:latest ./docker`. Set `claude.command: claude` (it runs inside the image).
4. `python -m app init-db` then `python -m app serve` (front it with your proxy/TLS).

## Notes

- Secrets are stored in `config.yaml` as requested. The UI and logs mask known secret values.
- Python owns all Git operations. Claude prompts explicitly forbid Git commands.
- Set `git.default_repo_url` and `git.default_base_branch` when most tickets should use the same repository.
- The Claude executable is configurable at `claude.command`; use an absolute path if `claude` is not on `PATH`.
- See `CONFIG_GUIDE.md` for setup, GitHub/GitLab review notes, and demo data instructions.
- See `WORKER_INTEGRATION_CONTRACT.txt` for the UI/database contract the worker follows.
