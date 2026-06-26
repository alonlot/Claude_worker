"""Minimal Confluence client: read a page and turn it into a skill.

Reuses the same Atlassian auth pattern as JiraClient (httpx + HTTP basic auth
with email + API token). Credentials fall back to the Jira config so a user who
already configured Jira gets Confluence for free on the same Atlassian account.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import ConfluenceConfig, JiraConfig


class ConfluenceError(RuntimeError):
    pass


@dataclass
class ConfluencePage:
    id: str
    title: str
    text: str
    url: str


def resolve_confluence(confluence: ConfluenceConfig, jira: JiraConfig) -> tuple[str, str, str]:
    """Return (base_url, email, token), falling back to Jira creds and deriving
    the wiki base from the Jira site URL when confluence.base_url is empty."""
    base = (confluence.base_url or "").strip().rstrip("/")
    if not base and jira and jira.url:
        base = jira.url.strip().rstrip("/") + "/wiki"
    email = (confluence.email or (jira.email if jira else "") or "").strip()
    token = (confluence.token or (jira.token if jira else "") or "").strip()
    if not base or not email or not token:
        raise ConfluenceError(
            "Confluence needs a base URL, email, and API token (set them in Settings, "
            "or configure Jira — the same Atlassian credentials work)."
        )
    return base, email, token


def page_id_from_url(url: str) -> str:
    """Extract the numeric page id from a Confluence page URL.

    Handles modern URLs (.../wiki/spaces/SPACE/pages/123456/Title) and the
    legacy viewpage form (...?pageId=123456).
    """
    match = re.search(r"/pages/(\d+)", url or "")
    if match:
        return match.group(1)
    match = re.search(r"[?&]pageId=(\d+)", url or "")
    if match:
        return match.group(1)
    if (url or "").strip().isdigit():
        return url.strip()
    raise ConfluenceError("Could not find a page id in that Confluence URL.")


def storage_to_text(storage_html: str) -> str:
    """Turn Confluence 'storage format' (XHTML) into readable plain text."""
    text = storage_html or ""
    # Keep some structure: list items and block breaks become newlines.
    text = re.sub(r"(?i)</(p|li|h[1-6]|tr|div|br\s*/?)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", "", text)  # strip remaining tags
    text = html.unescape(text)
    lines = [line.rstrip() for line in text.splitlines()]
    cleaned: list[str] = []
    blank = False
    for line in lines:
        if line.strip():
            cleaned.append(line.strip())
            blank = False
        elif not blank:
            cleaned.append("")
            blank = True
    return "\n".join(cleaned).strip()


class ConfluenceClient:
    def __init__(self, confluence: ConfluenceConfig, jira: JiraConfig | None = None):
        self.base, self.email, self.token = resolve_confluence(confluence, jira or JiraConfig())

    async def fetch_page(self, url: str) -> ConfluencePage:
        page_id = page_id_from_url(url)
        api = f"{self.base}/rest/api/content/{page_id}"
        params = {"expand": "body.storage"}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(api, params=params, auth=(self.email, self.token))
            if resp.status_code == 404:
                raise ConfluenceError(f"Confluence page {page_id} not found (check the URL and access).")
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
        title = str(data.get("title") or f"Confluence page {page_id}")
        body = ((data.get("body") or {}).get("storage") or {}).get("value") or ""
        text = storage_to_text(body)
        webui = (((data.get("_links") or {}).get("webui")) or "")
        full_url = f"{self.base}{webui}" if webui else url
        return ConfluencePage(id=page_id, title=title, text=text, url=full_url)
