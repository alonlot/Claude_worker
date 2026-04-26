from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml


CONFIG_PATH = Path("config.yaml")
EXAMPLE_PATH = Path("config.example.yaml")


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
class Config:
    app: AppConfig = field(default_factory=AppConfig)
    jira: JiraConfig = field(default_factory=JiraConfig)
    git: GitConfig = field(default_factory=GitConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    ui: UiConfig = field(default_factory=UiConfig)


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
    names = {item.name for item in fields(cls)}
    values = {name: value for name, value in section.items() if name in names}
    if cls is ClaudeConfig:
        for bool_key in ["allow_cr_fix", "auto_cr_fix"]:
            if bool_key in values:
                values[bool_key] = _as_bool(values[bool_key])
    return cls(**values)


def load_config(path: Path | str = CONFIG_PATH) -> Config:
    path = Path(path)
    if not path.exists():
        if EXAMPLE_PATH.exists():
            path.write_text(EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            path.write_text("", encoding="utf-8")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config(
        app=_config_section(AppConfig, raw, "app"),
        jira=_config_section(JiraConfig, raw, "jira"),
        git=_config_section(GitConfig, raw, "git"),
        claude=_config_section(ClaudeConfig, raw, "claude"),
        ui=_config_section(UiConfig, raw, "ui"),
    )


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
