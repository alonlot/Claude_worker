from app import notify
from app.config import Config, NotifyConfig, _config_from_data
from app.db import Database
from app.runner import Worker


class _FakeSMTP:
    instances: list = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.tls = False
        self.logged = None
        self.sent: list = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
        self.logged = (user, password)

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.sent.append((from_addr, to_addrs, msg["Subject"], msg.get_content()))


class _FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"ok"


def test_notify_config_parses():
    cfg = _config_from_data({"notify": {"email_enabled": "yes", "smtp_port": 25, "smtp_use_tls": "false", "webhook_enabled": "on"}})
    assert cfg.notify.email_enabled is True
    assert cfg.notify.smtp_port == 25
    assert cfg.notify.smtp_use_tls is False
    assert cfg.notify.webhook_enabled is True


def test_send_email_uses_smtp(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr(notify.smtplib, "SMTP", _FakeSMTP)
    cfg = NotifyConfig(
        email_enabled=True,
        smtp_host="smtp.example.com",
        smtp_user="u",
        smtp_password="p",
        email_from="worker@example.com",
        email_to="a@example.com, b@example.com",
    )
    error = notify.send_email(cfg, "Run finished", "A-1 is done")
    assert error == ""
    smtp = _FakeSMTP.instances[0]
    assert smtp.tls is True
    assert smtp.logged == ("u", "p")
    from_addr, to_addrs, subject, _ = smtp.sent[0]
    assert from_addr == "worker@example.com"
    assert to_addrs == ["a@example.com", "b@example.com"]
    assert subject == "Run finished"


def test_send_email_missing_fields_returns_error():
    assert notify.send_email(NotifyConfig(email_enabled=True), "x", "y") != ""


def test_send_webhook_posts_json(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["method"] = request.method
        return _FakeResp()

    monkeypatch.setattr(notify, "urlopen", fake_urlopen)
    cfg = NotifyConfig(webhook_enabled=True, webhook_url="https://hooks.example.com/x")
    error = notify.send_webhook(cfg, "Run failed", "A-1 broke", "error")
    assert error == ""
    assert captured["url"] == "https://hooks.example.com/x"
    assert captured["method"] == "POST"
    assert b"Run failed" in captured["data"]
    assert b'"level": "error"' in captured["data"]


def test_dispatch_runs_enabled_channels(monkeypatch):
    monkeypatch.setattr(notify, "send_email", lambda *a, **k: "")
    monkeypatch.setattr(notify, "send_webhook", lambda *a, **k: "boom")
    cfg = NotifyConfig(email_enabled=True, webhook_enabled=True)
    errors = notify.dispatch(cfg, "t", "m", "info")
    assert errors == ["webhook: boom"]


def test_dispatch_skips_disabled(monkeypatch):
    monkeypatch.setattr(notify, "send_email", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")))
    assert notify.dispatch(NotifyConfig(), "t", "m") == []


def test_runner_notify_external_logs_failures(tmp_path, monkeypatch):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    db = Database(config.app.database_path)
    db.init()
    db.upsert_ticket({"key": "A-1", "summary": "One", "status": "To Do", "eligibility": "eligible"})
    run_id = db.create_run("A-1")
    monkeypatch.setattr(notify, "dispatch", lambda *a, **k: ["smtp down"])
    Worker(config, db, "local")._notify_external("Run finished", "A-1", "success", run_id)
    logs = db.fetchall("SELECT line FROM logs WHERE run_id=? AND phase='notify'", (run_id,))
    assert any("smtp down" in row["line"] for row in logs)
