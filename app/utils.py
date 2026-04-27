from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit


def slugify(value: str, max_length: int = 48) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return (value or "work")[:max_length].strip("_") or "work"


def branch_name(ticket_key: str, summary: str) -> str:
    return f"{ticket_key}/by_claude_{slugify(summary)}"


def mask_secrets(text: str, secrets: list[str]) -> str:
    masked = text
    for secret in secrets:
        if secret:
            masked = masked.replace(secret, "***")
    masked = re.sub(r"(https?://)[^/\s@]+:[^/\s@]+@", r"\1***@", masked)
    return masked


def ensure_child_path(parent: str | Path, child: str | Path) -> Path:
    parent_path = Path(parent).resolve()
    child_path = Path(child).resolve()
    child_path.relative_to(parent_path)
    return child_path


def inject_token_into_url(url: str, username: str, token: str) -> str:
    if not token or not url.startswith(("http://", "https://")):
        return url
    parts = urlsplit(url)
    if "@" in parts.netloc:
        return url
    user = quote(username or "x-token-auth", safe="")
    encoded_token = quote(token, safe="")
    return urlunsplit((parts.scheme, f"{user}:{encoded_token}@{parts.netloc}", parts.path, parts.query, parts.fragment))
