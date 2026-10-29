"""
Minimal auth for the POC dashboard: username/password login -> JWT.
Role-based access control: 'admin' sees every group, 'poc' sees only the
groups assigned to them (enforced in group_db.list_groups /
poc_can_access_group, not just hidden in the UI).

Kept deliberately small (no user-registration UI, no email flows) —
POC accounts are created via /api/pm/pocs by an existing admin, or via
the one-time bootstrap described in the README when the table is empty.
"""
import os
import time

import bcrypt
import jwt
from fastapi import Header, HTTPException

from server import group_db

JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_SECONDS = int(os.getenv("JWT_EXPIRY_SECONDS", str(60 * 60 * 12)))  # 12h

if not JWT_SECRET:
    # Fail loudly rather than silently signing tokens with a guessable
    # default — this protects the whole POC dashboard's auth.
    raise RuntimeError(
        "JWT_SECRET is not set. Add a long random value to your .env "
        "(e.g. `python -c \"import secrets; print(secrets.token_hex(32))\"`)."
    )


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return False


def create_token(poc_id: int, username: str, role: str) -> str:
    payload = {
        "sub": str(poc_id),
        "username": username,
        "role": role,
        "exp": int(time.time()) + JWT_EXPIRY_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired, please log in again")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid session")


async def get_current_poc(authorization: str | None = Header(default=None)) -> dict:
    """FastAPI dependency — require a valid Bearer token, return the
    caller's {id, username, role}."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    payload = decode_token(token)
    poc = await group_db.get_poc_by_id(int(payload["sub"]))
    if not poc:
        raise HTTPException(status_code=401, detail="Account no longer exists")
    return poc


def require_role(current_poc: dict, roles: tuple[str, ...]):
    if current_poc["role"] not in roles:
        raise HTTPException(status_code=403, detail="Not authorized for this action")
