"""
leonCAST - Auth
Login-only authentication: no public signup. The first time the app
boots with zero users in the DB, the API reports `needs_setup: true`
and allows exactly one call to create the first admin account. After
that, only an existing admin can create further accounts.

Sessions are opaque random tokens stored server-side (SQLite), not JWTs --
simpler to reason about and to revoke (just delete the row). The frontend
holds the token in localStorage and sends it as `Authorization: Bearer <token>`.
"""

import hashlib
import hmac
import secrets
import time

from fastapi import Header, HTTPException

from data import db

SESSION_LIFETIME_SECONDS = 30 * 24 * 3600  # 30 days
PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ITERATIONS)
    return digest.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, password_hash)


def issue_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    db.create_session(token, user_id, time.time() + SESSION_LIFETIME_SECONDS)
    return token


def get_current_user(authorization: str | None = Header(default=None)) -> dict:
    """FastAPI dependency: resolves the bearer token to a user row, or 401s."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")

    token = authorization.removeprefix("Bearer ").strip()
    session = db.get_session(token)
    if not session or session["expires_at"] < time.time():
        raise HTTPException(401, "Session expired or invalid")

    user = db.get_user_by_id(session["user_id"])
    if not user:
        raise HTTPException(401, "User no longer exists")
    return user


def require_admin(user: dict) -> dict:
    if not user["is_admin"]:
        raise HTTPException(403, "Admin access required")
    return user
