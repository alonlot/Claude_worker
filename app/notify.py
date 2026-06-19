"""Outbound notifications for run events: SMTP email and HTTP webhook.

Both channels are optional and per-user (see NotifyConfig). Everything here is
best-effort: a channel that errors returns its error string rather than raising,
so a failing mail server never breaks a run. Uses only the stdlib (smtplib,
urllib) so it works on an air-gapped network with no extra dependencies.
"""
from __future__ import annotations

import json
import smtplib
import ssl
from email.message import EmailMessage
from urllib.request import Request, urlopen

from app.config import NotifyConfig


def dispatch(config: NotifyConfig, title: str, message: str, level: str = "info") -> list[str]:
    """Send the enabled notifications. Returns a list of error strings (empty = all ok)."""
    errors: list[str] = []
    if config.email_enabled:
        error = send_email(config, title, message)
        if error:
            errors.append(f"email: {error}")
    if config.webhook_enabled:
        error = send_webhook(config, title, message, level)
        if error:
            errors.append(f"webhook: {error}")
    return errors


def send_email(config: NotifyConfig, subject: str, body: str) -> str:
    """Send a plain-text email via SMTP. Returns "" on success or an error string."""
    recipients = [addr.strip() for addr in (config.email_to or "").replace(";", ",").split(",") if addr.strip()]
    sender = (config.email_from or config.smtp_user or "").strip()
    if not config.smtp_host or not recipients or not sender:
        return "smtp_host, email_from, and email_to are required"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body or subject)
    try:
        with smtplib.SMTP(config.smtp_host, int(config.smtp_port or 587), timeout=30) as server:
            if config.smtp_use_tls:
                server.starttls(context=ssl.create_default_context())
            if config.smtp_user:
                server.login(config.smtp_user, config.smtp_password)
            server.send_message(msg, from_addr=sender, to_addrs=recipients)
        return ""
    except Exception as exc:  # noqa: BLE001 - best-effort channel
        return str(exc)


def send_webhook(config: NotifyConfig, title: str, message: str, level: str = "info") -> str:
    """POST a small JSON payload to a webhook (Slack-compatible "text"). Returns "" or error."""
    url = (config.webhook_url or "").strip()
    if not url:
        return "webhook_url is required"
    payload = {
        "text": f"[{level}] {title}: {message}".strip(),
        "title": title,
        "message": message,
        "level": level,
    }
    data = json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=30) as response:
            response.read()
        return ""
    except Exception as exc:  # noqa: BLE001 - best-effort channel
        return str(exc)
