"""
auth.py
Session-cookie authentication for all UI routes.
"""

import hmac
import logging
from typing import Optional

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import (
    SESSION_COOKIE_SECURE,
    SESSION_MAX_AGE_HOURS,
    SESSION_SECRET_KEY,
    get_valid_users,
)

logger = logging.getLogger(__name__)

_COOKIE_NAME = "bemi_session"


# ---------------------------------------------------------------------------
# Session cookie auth — used by all UI routes
# ---------------------------------------------------------------------------

def _get_serializer() -> URLSafeTimedSerializer:
    """Return a serializer keyed to SESSION_SECRET_KEY."""
    if not SESSION_SECRET_KEY:
        raise RuntimeError(
            "SESSION_SECRET_KEY is not set. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    return URLSafeTimedSerializer(SESSION_SECRET_KEY)


def validate_credentials(username: str, password: str) -> bool:
    """Return True if username/password match the configured UI users.

    Uses a constant-time comparison to avoid leaking password length/content
    via response timing.
    """
    users = get_valid_users()
    expected = users.get(username)
    if expected is None:
        return False
    return hmac.compare_digest(expected, password)


def get_session_username(request: Request) -> Optional[str]:
    """
    Read the signed session cookie and return the username, or None if absent/invalid.

    Returns None (not raise) so callers can decide to redirect vs. raise.
    """
    token = request.cookies.get(_COOKIE_NAME)
    if not token:
        return None
    try:
        serializer = _get_serializer()
        max_age = SESSION_MAX_AGE_HOURS * 3600
        username = serializer.loads(token, max_age=max_age)
        return str(username)
    except SignatureExpired:
        logger.debug("Session cookie expired")
        return None
    except BadSignature:
        logger.warning("Invalid session cookie signature from %s", request.client)
        return None


def require_session(request: Request) -> str:
    """
    FastAPI dependency for UI routes. Returns username or redirects to /login.

    Use as: username: str = Depends(auth.require_session)
    """
    username = get_session_username(request)
    if not username:
        raise _redirect_to_login()
    return username


def create_session_cookie(response, username: str) -> None:
    """Set a signed session cookie on the response."""
    serializer = _get_serializer()
    token = serializer.dumps(username)
    response.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=SESSION_COOKIE_SECURE,
    )


def clear_session_cookie(response) -> None:
    """Delete the session cookie from the response."""
    response.delete_cookie(key=_COOKIE_NAME)


def _redirect_to_login():
    """Return an HTTPException that redirects to /login."""
    return HTTPException(
        status_code=302,
        headers={"Location": "/login"},
    )
