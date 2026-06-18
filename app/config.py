from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml


CONFIG_PATH = Path("config.yaml")
EXAMPLE_PATH = Path("config.example.yaml")

# The owner key used for single-user / local / dev contexts. Auth resolves to this
# username when no proxy header and no session are present, so the local experience
# and the existing test-suite keep working without a login step.
DEFAULT_OWNER = "local"

# Config sections that are stored per user (editable on the per-user Settings page).
# Everything else (app, auth, docker) is server-level and only an admin edits it.
USER_SECTIONS = ("jira", "git", "claude", "ui")


class IndentedSafeDumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


@dataclass
class AppConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    database_path: str = "data/worker.sqlite3"
    workspace_dir: str = "workspaces"
    interval_seconds: int = 300
    clone_retention_limit: int = 8


@dataclass
class JiraConfig:
    url: str = ""
    email: str = ""
    token: str = ""
    jql: str = "assignee = currentUser() ORDER BY updated DESC"
    excluded_statuses: list[str] = field(default_factory=lambda: ["Review", "Done"])
    required_text: str = ""
    max_results: int = 25


@dataclass
class GitConfig:
    username: str = ""
    token: str = ""
    remote_name: str = "origin"
    default_repo_url: str = ""
    default_base_branch: str = "main"


@dataclass
class ClaudeConfig:
    command: str = "claude"
    args: list[str] = field(default_factory=list)
    model: str = ""
    api_key: str = ""
    timeout_seconds: int = 7200
    allow_cr_fix: bool = True
    auto_cr_fix: bool = False


@dataclass
class UiConfig:
    title: str = "Jira Claude Worker"


@dataclass
class AuthConfig:
    # Which AuthProvider to use. "proxy_header" trusts a reverse-proxy/SSO header;
    # "login_page" shows the built-in /login form. New providers can be registered
    # in app/auth.py without touching the rest of the app.
    provider: str = "proxy_header"
    # Header the SSO/reverse proxy sets with the authenticated username.
    header: str = "X-Forwarded-User"
    # Optional header carrying a friendly display name.
    display_name_header: str = "X-Forwarded-Preferred-Username"
    # When the proxy header (or session) is absent, fall back to this username.
    # Set to "" in production to force real authentication. Defaults to the local
    # user so dev and the test-suite work with no proxy in front.
    default_user: str = DEFAULT_OWNER
    # Allow-list of usernames. Empty means any authenticated user is allowed.
    allowed_users: list[str] = field(default_factory=list)
    # Usernames with access to the server-level (admin) settings.
    admin_users: list[str] = field(default_factory=list)
    # Secret used to sign session cookies. Override in production.
    session_secret: str = "change-me-dev-session-secret"


@dataclass
class DockerConfig:
    # When enabled, each run executes the Claude agent inside a throwaway container.
    # When disabled (dev / Windows), the agent runs as a host subprocess.
    enabled: bool = False
    image: str = "claude-worker-agent:latest"
    # Path the per-run workspace is mounted to inside the container.
    workspace_mount: str = "/workspace"
    network: str = "bridge"
    memory: str = ""  # e.g. "2g"; empty means no limit
    cpus: str = ""  # e.g. "2"; empty means no limit
    # Extra raw flags appended to `docker run` (advanced).
    extra_args: list[str] = field(default_factory=list)


@dataclass
class Config:
    app: AppConfig = field(default_factory=AppConfig)
    jira: JiraConfig = field(default_factory=JiraConfig)
    git: GitConfig = field(default_factory=GitConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    docker: DockerConfig = field(default_factory=DockerConfig)


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key) or {}
    return value if isinstance(value, dict) else {}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _config_section(cls: type, data: dict[str, Any], key: str) -> Any:
    section = _section(data, key)
    spec = {item.name: item for item in fields(cls)}
    values = {name: value for name, value in section.items() if name in spec}
    for name, value in list(values.items()):
        if spec[name].type in (bool, "bool"):
            values[name] = _as_bool(value)
    return cls(**values)


def load_config(path: Path | str = CONFIG_PATH) -> Config:
    path = Path(path)
    if not path.exists():
        if EXAMPLE_PATH.exists():
            path.write_text(EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            path.write_text("", encoding="utf-8")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _config_from_data(raw)


def _config_from_data(raw: dict[str, Any]) -> Config:
    return Config(
        app=_config_section(AppConfig, raw, "app"),
        jira=_config_section(JiraConfig, raw, "jira"),
        git=_config_section(GitConfig, raw, "git"),
        claude=_config_section(ClaudeConfig, raw, "claude"),
        ui=_config_section(UiConfig, raw, "ui"),
        auth=_config_section(AuthConfig, raw, "auth"),
        docker=_config_section(DockerConfig, raw, "docker"),
    )


def _section_to_dict(section: Any) -> dict[str, Any]:
    return {item.name: getattr(section, item.name) for item in fields(section)}


def config_to_user_sections(config: Config) -> dict[str, Any]:
    """Extract the per-user editable sections (jira/git/claude/ui) as plain data."""
    return {name: _section_to_dict(getattr(config, name)) for name in USER_SECTIONS}


def apply_user_sections(base: Config, sections: dict[str, Any] | None) -> Config:
    """Overlay a user's stored sections on top of the server base config.

    The result keeps server-level app/auth/docker untouched and replaces the
    user-owned jira/git/claude/ui sections with the user's saved values.
    """
    data: dict[str, Any] = {
        "app": _section_to_dict(base.app),
        "auth": _section_to_dict(base.auth),
        "docker": _section_to_dict(base.docker),
        "jira": _section_to_dict(base.jira),
        "git": _section_to_dict(base.git),
        "claude": _section_to_dict(base.claude),
        "ui": _section_to_dict(base.ui),
    }
    for name in USER_SECTIONS:
        incoming = (sections or {}).get(name)
        if isinstance(incoming, dict):
            data[name] = {**data[name], **incoming}
    return _config_from_data(data)


def load_config_data(path: Path | str = CONFIG_PATH) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        load_config(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("config.yaml must contain a YAML mapping")
    return raw


def write_config_data(data: dict[str, Any], path: Path | str = CONFIG_PATH) -> Config:
    path = Path(path)
    normalized = yaml.dump(data, Dumper=IndentedSafeDumper, sort_keys=False, default_flow_style=False)
    path.write_text(normalized, encoding="utf-8", newline="\n")
    return load_config(path)


def save_config_text(text: str, path: Path | str = CONFIG_PATH) -> Config:
    parsed = yaml.safe_load(text) or {}
    if not isinstance(parsed, dict):
        raise ValueError("config.yaml must contain a YAML mapping")
    return write_config_data(parsed, path)


def load_config_text(path: Path | str = CONFIG_PATH) -> str:
    path = Path(path)
    if not path.exists():
        load_config(path)
    return path.read_text(encoding="utf-8")


def secret_values(config: Config) -> list[str]:
    return [
        value
        for value in [config.jira.token, config.git.token, config.claude.api_key]
        if value and len(value) >= 4
    ]
