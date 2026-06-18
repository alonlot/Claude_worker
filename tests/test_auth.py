from fastapi.testclient import TestClient

from app.auth import LoginPageAuthProvider, ProxyHeaderAuthProvider, get_auth_provider, hash_password, verify_password
from app.config import Config
from app.db import Database
from app.web import create_app


def _make(tmp_path, **auth):
    config = Config()
    config.app.database_path = str(tmp_path / "worker.sqlite3")
    for key, value in auth.items():
        setattr(config.auth, key, value)
    db = Database(config.app.database_path)
    db.init()
    return config, db


def test_password_hash_roundtrip():
    encoded = hash_password("hunter2")
    assert verify_password("hunter2", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("hunter2", "")


def test_provider_factory_selects_implementation():
    config = Config()
    db = Database(":memory:")
    config.auth.provider = "proxy_header"
    assert isinstance(get_auth_provider(config, db), ProxyHeaderAuthProvider)
    config.auth.provider = "login_page"
    assert isinstance(get_auth_provider(config, db), LoginPageAuthProvider)


def test_proxy_header_default_user_allows_local(tmp_path):
    config, db = _make(tmp_path)
    client = TestClient(create_app(config, db))
    # No header -> falls back to default_user "local".
    assert client.get("/").status_code == 200


def test_proxy_header_required_when_no_default(tmp_path):
    config, db = _make(tmp_path, default_user="")
    client = TestClient(create_app(config, db))
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 401


def test_proxy_header_identifies_user(tmp_path):
    config, db = _make(tmp_path, default_user="")
    client = TestClient(create_app(config, db))
    resp = client.get(
        "/",
        headers={"X-Forwarded-User": "alice", "X-Forwarded-Preferred-Username": "Alice A"},
    )
    assert resp.status_code == 200
    user = db.get_user("alice")
    assert user is not None
    assert user["display_name"] == "Alice A"


def test_allowed_users_blocks_others(tmp_path):
    config, db = _make(tmp_path, default_user="", allowed_users=["alice"])
    client = TestClient(create_app(config, db))
    assert client.get("/", headers={"X-Forwarded-User": "alice"}).status_code == 200
    assert client.get("/", headers={"X-Forwarded-User": "mallory"}, follow_redirects=False).status_code == 401


def test_admin_promotion_from_config(tmp_path):
    config, db = _make(tmp_path, default_user="", admin_users=["root"])
    client = TestClient(create_app(config, db))
    client.get("/", headers={"X-Forwarded-User": "root"})
    assert db.get_user("root")["role"] == "admin"


def test_login_page_flow(tmp_path):
    config, db = _make(tmp_path, provider="login_page")
    db.upsert_user("carol", display_name="Carol", password_hash=hash_password("pw"), role="user")
    client = TestClient(create_app(config, db))

    # Unauthenticated requests are redirected to the login page.
    unauth = client.get("/", follow_redirects=False)
    assert unauth.status_code == 303
    assert unauth.headers["location"] == "/login"

    bad = client.post("/login", data={"username": "carol", "password": "nope"}, follow_redirects=False)
    assert bad.status_code == 303
    assert bad.headers["location"] == "/login"

    ok = client.post("/login", data={"username": "carol", "password": "pw"}, follow_redirects=False)
    assert ok.status_code == 303
    assert ok.headers["location"] == "/"

    # The session cookie now grants access.
    assert client.get("/").status_code == 200

    out = client.post("/logout", follow_redirects=False)
    assert out.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303


def test_admin_only_server_yaml_visible(tmp_path):
    config, db = _make(tmp_path, default_user="", admin_users=["root"])
    client = TestClient(create_app(config, db))
    admin_page = client.get("/settings", headers={"X-Forwarded-User": "root"})
    assert "Server YAML (admin)" in admin_page.text
    user_page = client.get("/settings", headers={"X-Forwarded-User": "joe"})
    assert "Server YAML (admin)" not in user_page.text
