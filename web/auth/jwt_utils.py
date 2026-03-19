"""
JWT authentication utilities for Seny web application.

Provides secure token creation, validation, and FastAPI dependency
for protecting routes with JWT authentication.
"""

from datetime import datetime, timedelta
from typing import Optional
from jose import jwt, JWTError, ExpiredSignatureError
from fastapi import HTTPException, Header
from src.core.config import Config


def create_access_token(user_id: str, expires_minutes: Optional[int] = 60) -> str:
    """
    Create a JWT access token for a user.

    Args:
        user_id: Unique identifier for the user
        expires_minutes: Token expiry time in minutes (default: 60).
                        Pass None for a non-expiring token.

    Returns:
        Encoded JWT token string

    Example:
        >>> token = create_access_token("user123", expires_minutes=30)
        >>> print(token)
        'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...'
        >>> permanent_token = create_access_token("user123", expires_minutes=None)
    """
    # Get current time once for consistency
    now = datetime.utcnow()

    # Create payload with user_id and issued-at time
    payload = {
        "user_id": user_id,
        "iat": now,
    }

    # Add expiry only if specified (None = never expires)
    if expires_minutes is not None:
        payload["exp"] = now + timedelta(minutes=expires_minutes)

    # Encode and sign the token
    token = jwt.encode(
        payload,
        Config.SECRET_KEY,
        algorithm=Config.ALGORITHM
    )

    return token


def verify_token(token: str) -> Optional[dict]:
    """
    Verify and decode a JWT token.

    Args:
        token: JWT token string to verify

    Returns:
        Decoded payload dict if valid, None if invalid or expired

    Example:
        >>> token = create_access_token("user123")
        >>> payload = verify_token(token)
        >>> print(payload["user_id"])
        'user123'
    """
    if not token:
        return None

    try:
        # Decode and verify the token
        payload = jwt.decode(
            token,
            Config.SECRET_KEY,
            algorithms=[Config.ALGORITHM]
        )
        return payload

    except ExpiredSignatureError:
        # Token has expired
        return None

    except JWTError:
        # Invalid signature, malformed token, or other JWT errors
        return None


def get_current_user(token: Optional[str]) -> Optional[str]:
    """
    Extract user_id from a valid JWT token.

    Args:
        token: JWT token string or None

    Returns:
        user_id if token is valid, None otherwise

    Example:
        >>> token = create_access_token("user456")
        >>> user_id = get_current_user(token)
        >>> print(user_id)
        'user456'
    """
    if not token:
        return None

    payload = verify_token(token)

    if payload is None:
        return None

    return payload.get("user_id")


def require_auth(authorization: Optional[str] = Header(None)) -> str:
    """
    FastAPI dependency for protecting routes with JWT authentication.

    Extracts and validates JWT from Authorization header (Bearer token).
    Raises 401 HTTPException if authentication fails.

    Args:
        authorization: Authorization header value (injected by FastAPI)

    Returns:
        user_id from validated token

    Raises:
        HTTPException: 401 if token is missing, invalid, or expired

    Example:
        >>> from fastapi import FastAPI, Depends
        >>> app = FastAPI()
        >>>
        >>> @app.get("/protected")
        >>> async def protected_route(user_id: str = Depends(require_auth)):
        >>>     return {"message": f"Hello {user_id}"}
    """
    # Check if Authorization header is present
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Authorization header is missing. Please provide a valid Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Check for Bearer token format
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Invalid authorization header format. Use: Authorization: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = parts[1]

    # Validate token and extract user_id
    user_id = get_current_user(token)

    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token. Please authenticate again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user_id


def require_screen_agent(x_screen_agent_key: Optional[str] = Header(None)) -> str:
    """
    FastAPI dependency for screen agent requests.

    Validates the X-Screen-Agent-Key header against user_settings.
    Returns the user_id associated with the key.

    Raises:
        HTTPException: 401 if header is missing or key is invalid
    """
    if not x_screen_agent_key:
        raise HTTPException(status_code=401, detail="Missing X-Screen-Agent-Key header")
    from web.core.database import get_db
    with get_db() as conn:
        _cur = conn.cursor()

        _cur.execute(
            "SELECT user_id FROM user_settings WHERE screen_agent_api_key = %s",
            (x_screen_agent_key,)
        )

        row = _cur.fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid screen agent key")
    return str(row["user_id"])
