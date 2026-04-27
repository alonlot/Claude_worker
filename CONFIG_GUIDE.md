# Configuration Guide

This app is meant to run locally on your machine. Put real secrets only in `config.yaml`; keep `config.example.yaml` safe to commit.

## 1. Start With The Example

```powershell
Copy-Item config.example.yaml config.yaml
python -m app init-db
python -m app serve
```

Open `http://127.0.0.1:8000`, then use Settings to edit YAML or the visual form.

## 2. Jira

Required fields:

```yaml
jira:
  url: https://your-domain.atlassian.net
  email: you@example.com
  token: your-jira-api-token
  jql: assignee = currentUser() ORDER BY updated DESC
  excluded_statuses:
    - Review
    - Done
  required_text: ""
  max_results: 25
```

Create a Jira API token from your Atlassian account security page. The scan button uses the configured JQL, then skips excluded statuses. `required_text` is optional; when set, tickets must contain that text in title, description, or labels.

## 3. Git

Recommended SSH setup:

```yaml
git:
  remote_name: origin
  default_repo_url: git@github.com:alonlot/Claude_worker.git
  default_base_branch: main
```

For GitHub SSH, make sure your public key is added to GitHub and this works:

```powershell
ssh -T git@github.com
git ls-remote git@github.com:alonlot/Claude_worker.git HEAD
```

If you use HTTPS tokens instead, set `git.username` and `git.token`. Secrets are masked in logs.

## 4. Claude

Default:

```yaml
claude:
  command: claude
  args: []
  model: ""
  api_key: ""
  timeout_seconds: 7200
  allow_cr_fix: true
  auto_cr_fix: false
```

`claude.command` must be available on PATH, or use an absolute path. The worker sends prompts through stdin and streams output into the run logs. Python owns all Git commands; Claude is told not to run Git.

## 5. Code Review Notes

GitHub PR review scanning uses the GitHub CLI:

```powershell
gh auth login
gh pr view --repo owner/repo 123
```

GitLab MR review scanning uses `GITLAB_TOKEN`:

```powershell
$env:GITLAB_TOKEN = "your-token"
```

The code review menu only reads notes and posts comments. It does not resolve notes, approve, merge, close, or change review state.

## 6. Optional IDE Panel

If you run code-server or another local web IDE, set:

```yaml
ui:
  title: Jira Claude Worker
  ide_url_template: "http://127.0.0.1:8080/?folder={workspace_path}"
```

Supported placeholders:

`{run_id}`, `{ticket_key}`, `{workspace_path}`, `{workspace_path_raw}`, `{branch_name}`.

## 7. Demo Data

Seed a fake ticket, completed run, and code-review notes:

```powershell
python -m app seed-demo
```

Then refresh the dashboard. You can open the fake run, inspect Push Preview, and open the Code Review menu without real Jira or Claude.

## 8. Safe First Real Run

1. Fill Jira, Git, and Claude settings.
2. Use Settings test buttons.
3. Scan Jira.
4. Pick one ticket.
5. Optional: Ask Claude for a plan and revise it.
6. Click Build now.
7. Inspect the run report and Push Preview.
8. Push only after the commit and diff look right.
