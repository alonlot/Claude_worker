from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.config import Config, secret_values
from app.utils import ensure_child_path, inject_token_into_url, mask_secrets


class GitError(RuntimeError):
    pass


class GitOps:
    def __init__(self, config: Config):
        self.config = config

    def run(self, args: list[str], cwd: str | Path | None = None) -> str:
        proc = subprocess.run(
            args,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        output = mask_secrets((proc.stdout or "").strip(), secret_values(self.config))
        error = mask_secrets((proc.stderr or "").strip(), secret_values(self.config))
        if proc.returncode != 0:
            raise GitError(error or output or "git command failed")
        return output

    def clone_for_ticket(self, ticket_key: str, repo_url: str) -> Path:
        workspace_root = Path(self.config.app.workspace_dir)
        workspace_root.mkdir(parents=True, exist_ok=True)
        target = ensure_child_path(workspace_root, workspace_root / ticket_key)
        if target.exists():
            shutil.rmtree(target)
        clone_url = inject_token_into_url(repo_url, self.config.git.username, self.config.git.token)
        self.run(["git", "clone", clone_url, str(target)])
        return target

    def checkout_base_and_branch(self, repo_path: str | Path, base_branch: str, branch_name: str) -> None:
        repo_path = Path(repo_path)
        self.run(["git", "fetch", self.config.git.remote_name, "--prune"], cwd=repo_path)
        base = base_branch.strip()
        if not base:
            base = self.default_branch(repo_path)
        remote_ref = f"{self.config.git.remote_name}/{base}"
        self.run(["git", "checkout", "-B", base, remote_ref], cwd=repo_path)
        self.run(["git", "checkout", "-B", branch_name], cwd=repo_path)

    def default_branch(self, repo_path: str | Path) -> str:
        ref = self.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=repo_path)
        return ref.rsplit("/", 1)[-1] if ref else "main"

    def status(self, repo_path: str | Path) -> str:
        return self.run(["git", "status", "--short"], cwd=repo_path)

    def has_commits_ahead(self, repo_path: str | Path, base_branch: str) -> bool:
        base = base_branch or self.default_branch(repo_path)
        count = self.run(
            ["git", "rev-list", "--count", f"{self.config.git.remote_name}/{base}..HEAD"],
            cwd=repo_path,
        )
        return int(count or "0") > 0

    def push_branch(self, repo_path: str | Path, branch_name: str) -> str:
        return self.run(
            ["git", "push", "-u", self.config.git.remote_name, branch_name],
            cwd=repo_path,
        )

    def cleanup_old_clones(self) -> None:
        limit = max(0, int(self.config.app.clone_retention_limit))
        root = Path(self.config.app.workspace_dir)
        if limit <= 0 or not root.exists():
            return
        dirs = [path for path in root.iterdir() if path.is_dir()]
        dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        for old_dir in dirs[limit:]:
            ensure_child_path(root, old_dir)
            shutil.rmtree(old_dir, ignore_errors=True)
