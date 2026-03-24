"""
Authentication endpoints for Seny web application.

Provides user registration and login with JWT token generation.
"""

import re
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Request, status, Depends
from pydantic import BaseModel, EmailStr, field_validator
import bcrypt
from web.core.database import create_user, get_user_by_email, get_db
from web.auth.jwt_utils import create_access_token, verify_token, require_auth
from web.core.rate_limit import limiter


# Create auth router
router = APIRouter()



# Request/Response models
class RegisterRequest(BaseModel):
    """Request model for user registration."""
    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        """
        Validate password meets minimum security requirements.

        Requirements:
            - Minimum 8 characters

        Args:
            v: Password to validate

        Returns:
            Password if valid

        Raises:
            ValueError: If password doesn't meet requirements
        """
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long")
        return v


class LoginRequest(BaseModel):
    """Request model for user login."""
    email: EmailStr
    password: str


class AuthResponse(BaseModel):
    """Response model for successful authentication."""
    access_token: str
    token_type: str = "bearer"


class MessageResponse(BaseModel):
    """Generic message response."""
    message: str


@router.post("/register", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/hour")
async def register(request: Request, body: RegisterRequest):
    """
    Register a new user.

    Creates a new user account with hashed password.

    Args:
        body: RegisterRequest with email and password

    Returns:
        MessageResponse confirming user creation

    Raises:
        HTTPException:
            - 409: Email already registered
            - 422: Invalid input (handled by Pydantic validation)

    Security:
        - Password is hashed with bcrypt (never stored as plaintext)
        - Email uniqueness enforced by database constraint
        - Minimum password length: 8 characters
    """
    # Hash the password (bcrypt automatically generates salt)
    # SECURITY: Never store plaintext passwords!
    # bcrypt has a max password length of 72 bytes, truncate if needed
    password_bytes = body.password.encode('utf-8')[:72]
    salt = bcrypt.gensalt()
    hashed_password = bcrypt.hashpw(password_bytes, salt).decode('utf-8')

    # Attempt to create user in database
    user_id = create_user(email=body.email, hashed_password=hashed_password)

    if user_id is None:
        # Email already exists (UNIQUE constraint violation)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered"
        )

    return MessageResponse(message="User created successfully")


@router.post("/login", response_model=AuthResponse)
@limiter.limit("10/minute")
async def login(request: Request, body: LoginRequest):
    """
    Authenticate a user and generate JWT token.

    Validates credentials and returns access token for authenticated requests.

    Args:
        body: LoginRequest with email and password

    Returns:
        AuthResponse with access_token and token_type

    Raises:
        HTTPException:
            - 401: Invalid credentials (email or password incorrect)

    Security:
        - Generic error message prevents email enumeration
        - Password verified with constant-time comparison (bcrypt)
        - JWT token contains user_id claim
        - Token expires after 60 minutes (configurable)
    """
    # Retrieve user from database
    user = get_user_by_email(body.email)

    # SECURITY: Generic error message for any auth failure
    # Don't reveal whether email or password was wrong (prevents email enumeration)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Verify password with constant-time comparison
    # bcrypt.checkpw() uses secure comparison
    # bcrypt has a max password length of 72 bytes, truncate if needed
    password_bytes = body.password.encode('utf-8')[:72]
    hashed_password_bytes = user["hashed_password"].encode('utf-8')
    if not bcrypt.checkpw(password_bytes, hashed_password_bytes):
        # SECURITY: Same generic error message as above
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Authentication successful - generate JWT token
    # Token contains user_id claim for identifying the authenticated user
    access_token = create_access_token(
        user_id=str(user["id"]),
        expires_minutes=10080  # 7 days — balances security with convenience for daily-use personal assistant
    )

    return AuthResponse(access_token=access_token, token_type="bearer")


@router.post("/logout", response_model=MessageResponse)
@limiter.limit("30/hour")
async def logout(request: Request, user_id: str = Depends(require_auth)):
    """
    Log out by blocklisting the current token.

    Adds the token's JTI to the blocklist so it cannot be reused.
    Legacy tokens without a JTI are handled gracefully (success without blocklisting).

    Args:
        request: FastAPI request (required by slowapi)
        user_id: Authenticated user ID (from JWT)

    Returns:
        MessageResponse confirming logout

    Security:
        - Token is blocklisted immediately (cannot be reused)
        - Double-logout is handled gracefully (ON CONFLICT DO NOTHING)
        - Legacy tokens without jti return success without blocklisting
    """
    # Extract the raw token from the Authorization header
    token = request.headers.get("authorization", "").replace("Bearer ", "")

    # Decode token to get jti and exp claims
    payload = verify_token(token)

    # Extract jti — if None, this is a legacy token with no jti to blocklist
    jti = payload.get("jti") if payload else None
    if jti is None:
        return MessageResponse(message="Logged out successfully")

    # Determine expiry for the blocklist entry
    exp_claim = payload.get("exp")
    if exp_claim is not None:
        expires_at = datetime.utcfromtimestamp(exp_claim)
    else:
        # Legacy permanent token with jti — shouldn't happen but use safe default
        expires_at = datetime.utcnow() + timedelta(days=90)

    # Insert into blocklist — ON CONFLICT handles double-logout gracefully
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO token_blocklist (jti, user_id, expires_at) VALUES (%s, %s, %s) ON CONFLICT (jti) DO NOTHING",
            (jti, user_id, expires_at)
        )
        conn.commit()

    return MessageResponse(message="Logged out successfully")


@router.post("/desktop-token", response_model=AuthResponse)
@limiter.limit("5/hour")
async def generate_desktop_token(request: Request, user_id: str = Depends(require_auth)):
    """
    Generate a long-lived token for the desktop app.

    This token expires after 90 days and is intended for use with the Seny Desktop App.
    Requires the user to already be authenticated (logged in to the web app).

    Args:
        user_id: Authenticated user ID (from JWT)

    Returns:
        AuthResponse with long-lived access_token

    Security:
        - Requires existing authentication
        - Token is still tied to user_id
        - Token expires after 90 days — regenerate periodically
        - Can be revoked via /logout or by changing SECRET_KEY
    """
    # Generate a long-lived token for desktop app convenience
    access_token = create_access_token(
        user_id=user_id,
        expires_minutes=129600  # 90 days — long-lived for desktop app convenience, regenerate periodically
    )

    return AuthResponse(access_token=access_token, token_type="bearer")
