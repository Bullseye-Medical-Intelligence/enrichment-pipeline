"""
auth.py
API key authentication. Applied globally to all routes via FastAPI dependency.
"""

import logging
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import PIPELINE_API_KEY

logger = logging.getLogger(__name__)
_bearer = HTTPBearer()


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
