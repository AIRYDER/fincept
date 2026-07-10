"""
api.auth — JWT bearer authentication.

Simple HS256 JWT for v1.  ``FINCEPT_JWT_SECRET`` from Settings is the
signing key; the dev default is intentionally unsafe so production
deploys must override it (see ``Settings.JWT_SECRET`` docstring).

For OAuth flow / refresh tokens / scopes / per-user permissions, see
the Phase H roadmap.  v1 covers a single internal operator who logs
into the dashboard; multi-user is out of scope until we have multiple
operators or external API consumers.
"""

from __future__ import annotations

from typing import Any

import jwt
from fastapi import Header, HTTPException, status
from fincept_core.config import get_settings


def encode_token(claims: dict[str, Any]) -> str:
    """Sign *claims* into a JWT.  Useful for tests and bootstrap scripts."""
    return jwt.encode(claims, get_settings().JWT_SECRET, algorithm="HS256")


def require_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """FastAPI dependency: parse and verify a Bearer JWT.

    Raises ``401`` if the header is missing entirely, lacks the
    ``Bearer `` prefix, or if the token doesn't decode under our HS256
    secret.  The decoded claims are returned to the route so handlers
    can read ``sub`` (user id) etc.
    """
    if authorization is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="empty bearer token",
        )
    try:
        return jwt.decode(token, get_settings().JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {exc}",
        ) from exc
