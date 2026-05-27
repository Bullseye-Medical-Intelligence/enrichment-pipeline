"""
auth.py
Authentication for both API routes (Bearer token) and UI routes (session cookie).
These two auth paths are entirely separate — no coupling between them.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import PIPELINE_API_KEY, SESSION_MAX_AGE_HOURS, SESSION_SECRET_KEY, get_valid_users

logger = logging.getLogger(__name__)

_bearer = HTTPBearer()
_COOKIE_NAME = "bemi_session"


# ---------------------------------------------------------------------------
# API key auth (Bearer token) — used by all /runs/* API routes
# ---------------------------------------------------------------------------

def verify_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> None:
    """
    Validate the Bearer token against PIPELINE_API_KEY.

    Raises HTTP 401 if missing or incorrect.
    Logs every failed attempt with timestamp and client IP.
    """
    if not PIPELINE_API_KEY:
        logger.error("PIPELINE_API_KEY is not configured — all requests will be rejected")
        raise HTTPException(status_code=500, detail="API key not configured on server")

    if credentials.credentials != PIPELINE_API_KEY:
        client_ip = request.client.host if request.client else "unknown"
        logger.warning(
            "Auth failure from %s at %s",
            client_ip,
            datetime.now(timezone.utc).isoformat(),
        )
        raise HTTPException(status_code=401, detail="Unauthorized")


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
    """Return True if username/password match the configured UI users."""
    users = get_valid_users()
    return users.get(username) == password


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
