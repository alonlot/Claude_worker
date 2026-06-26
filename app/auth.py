"""Pluggable authentication.

The rest of the app only depends on:

    provider = get_auth_provider(config, db)
    user = provider.authenticate(request)          # -> User | None

To add a new login method later (LDAP, OAuth, mTLS, ...), implement a new
``AuthProvider`` subclass and register it in ``_PROVIDERS``. Nothing else in the
codebase needs to change: routes read ``request.state.user`` and scope data by
``user.username``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Protocol

from starlette.requests import Request

from app.config import Config
from app.db import Database


# Default shared secret a self-service signup can enter to be created as an admin.
# Stored in the DB once set, and editable from the admin page, so this is only the
# fallback used until an admin changes it.
DEFAULT_ADMIN_SECRET = "omri&alon_kings"


@dataclass(frozen=True)
class User:
    username: str
    display_name: str = ""
    role: str = "user"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# --------------------------------------------------------------------------
# Password hashing (stdlib PBKDF2 — no third-party dependency required)
# --------------------------------------------------------------------------

_PBKDF2_ROUNDS = 240_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    if not encoded:
        return False
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    expected = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds))
    return hmac.compare_digest(expected.hex(), digest_hex)


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


class AuthProvider(Protocol):
    # True when this provider needs the built-in /login page (vs. an upstream proxy).
    requires_login_page: bool

    def authenticate(self, request: Request) -> User | None:
        """Return the authenticated user for this request, or None."""
        ...


class _BaseProvider:
    requires_login_page = False

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db

    def _is_allowed(self, username: str) -> bool:
        allowed = [u.strip() for u in self.config.auth.allowed_users if u.strip()]
        return not allowed or username in allowed

    def _resolve(self, username: str) -> User | None:
        """Provision the user row if needed and return a User, honoring the allow-list."""
        username = (username or "").strip()
        if not username or not self._is_allowed(username):
            return None
        role = "admin" if username in self.config.auth.admin_users else "user"
        existing = self.db.get_user(username)
        if not existing:
            self.db.upsert_user(username, display_name=username, role=role)
        elif existing["role"] != role and role == "admin":
            # Promote to admin if the config now lists them as one.
            self.db.upsert_user(username, display_name=existing["display_name"], role=role)
        row = self.db.get_user(username)
        return User(username=username, display_name=row["display_name"] or username, role=row["role"])


class ProxyHeaderAuthProvider(_BaseProvider):
    """Trust a username injected by an upstream SSO / reverse proxy.

    Falls back to ``auth.default_user`` when the header is absent so local and
    test usage works without a proxy. Set ``auth.default_user: ""`` in production
    to force the proxy to be present.
    """

    requires_login_page = False

    def authenticate(self, request: Request) -> User | None:
        header_name = self.config.auth.header
        username = request.headers.get(header_name, "").strip() if header_name else ""
        display = ""
        if username and self.config.auth.display_name_header:
            display = request.headers.get(self.config.auth.display_name_header, "").strip()
        if not username:
            username = (self.config.auth.default_user or "").strip()
        if not username:
            return None
        user = self._resolve(username)
        if user and display and display != user.display_name:
            self.db.upsert_user(user.username, display_name=display, role=user.role)
            user = User(user.username, display, user.role)
        return user


class LoginPageAuthProvider(_BaseProvider):
    """Built-in username/password form backed by the ``users`` table.

    This is the worked example proving the interface is swappable, and the
    fallback when no SSO proxy is in front of the app.
    """

    requires_login_page = True

    def authenticate(self, request: Request) -> User | None:
        username = (request.session.get("user") or "").strip() if "session" in request.scope else ""
        if not username:
            return None
        return self._resolve(username)

    def login(self, username: str, password: str) -> User | None:
        username = (username or "").strip()
        if not username or not self._is_allowed(username):
            return None
        row = self.db.get_user(username)
        if not row or not verify_password(password, row["password_hash"]):
            return None
        self.db.touch_user_login(username)
        return User(username=username, display_name=row["display_name"] or username, role=row["role"])

    def register(
        self,
        username: str,
        password: str,
        *,
        make_admin: bool = False,
        display_name: str = "",
    ) -> tuple[User | None, str]:
        """Create a brand-new login user. Returns (user, error_message).

        ``make_admin`` is decided by the caller (it knows whether the supplied
        secret key matched). The username must be allowed and not already taken.
        """
        username = (username or "").strip()
        if not username or not password:
            return None, "Enter both a username and a password."
        if not self._is_allowed(username):
            return None, "This username is not permitted on this server."
        if self.db.get_user(username):
            return None, "That username is taken. Try signing in instead."
        role = "admin" if make_admin else "user"
        self.db.upsert_user(
            username,
            display_name=display_name.strip() or username,
            password_hash=hash_password(password),
            role=role,
        )
        self.db.touch_user_login(username)
        return User(username=username, display_name=display_name.strip() or username, role=role), ""


_PROVIDERS = {
    "proxy_header": ProxyHeaderAuthProvider,
    "login_page": LoginPageAuthProvider,
}


def get_auth_provider(config: Config, db: Database) -> AuthProvider:
    provider_cls = _PROVIDERS.get(config.auth.provider, ProxyHeaderAuthProvider)
    return provider_cls(config, db)
