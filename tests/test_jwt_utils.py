"""
Test suite for JWT authentication utilities.

Tests cover:
- Token creation with proper payload
- Token validation and decoding
- Expired token handling
- Invalid signature detection
- User extraction from tokens
- FastAPI dependency integration
"""

import pytest
from datetime import datetime, timedelta
from fastapi import HTTPException
import time


def test_create_access_token_returns_non_empty_string():
    """Test that create_access_token generates a non-empty token string."""
    from web.auth.jwt_utils import create_access_token

    token = create_access_token(user_id="test_user_123")

    assert isinstance(token, str)
    assert len(token) > 0


def test_create_access_token_includes_user_id_and_expiry():
    """Test that created tokens contain user_id and exp in their payload."""
    from web.auth.jwt_utils import create_access_token, verify_token

    user_id = "test_user_456"
    token = create_access_token(user_id=user_id)
    payload = verify_token(token)

    assert payload is not None
    assert "user_id" in payload
    assert payload["user_id"] == user_id
    assert "exp" in payload
    assert "iat" in payload  # issued at time


def test_create_access_token_custom_expiry():
    """Test that custom expiry time is respected."""
    from web.auth.jwt_utils import create_access_token, verify_token

    token = create_access_token(user_id="test_user", expires_minutes=120)
    payload = verify_token(token)

    assert payload is not None
    # Verify expiry is roughly 120 minutes from now (allow 5 second variance)
    # Use utcfromtimestamp since we're using utcnow() in jwt_utils
    exp_time = datetime.utcfromtimestamp(payload["exp"])
    expected_exp = datetime.utcnow() + timedelta(minutes=120)
    time_diff = abs((exp_time - expected_exp).total_seconds())
    assert time_diff < 5


def test_verify_token_returns_payload_for_valid_token():
    """Test that verify_token decodes valid tokens correctly."""
    from web.auth.jwt_utils import create_access_token, verify_token

    user_id = "valid_user"
    token = create_access_token(user_id=user_id)
    payload = verify_token(token)

    assert payload is not None
    assert isinstance(payload, dict)
    assert payload["user_id"] == user_id


def test_verify_token_returns_none_for_expired_token():
    """Test that expired tokens are rejected and return None."""
    from web.auth.jwt_utils import create_access_token, verify_token

    # Create token with negative expiry (already expired)
    token = create_access_token(user_id="test_user", expires_minutes=-1)

    # Small delay to ensure token is definitely expired
    time.sleep(0.1)

    payload = verify_token(token)
    assert payload is None


def test_verify_token_returns_none_for_invalid_signature():
    """Test that tokens with invalid signatures are rejected."""
    from web.auth.jwt_utils import verify_token

    # Manually craft an invalid token (this is not a properly signed JWT)
    invalid_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyX2lkIjoidGVzdCJ9.invalid_signature"

    payload = verify_token(invalid_token)
    assert payload is None


def test_verify_token_returns_none_for_malformed_token():
    """Test that malformed tokens are rejected."""
    from web.auth.jwt_utils import verify_token

    malformed_token = "not.a.valid.jwt.token"
    payload = verify_token(malformed_token)
    assert payload is None


def test_get_current_user_returns_user_id_for_valid_token():
    """Test that get_current_user extracts user_id from valid tokens."""
    from web.auth.jwt_utils import create_access_token, get_current_user

    user_id = "current_user_789"
    token = create_access_token(user_id=user_id)
    extracted_user_id = get_current_user(token)

    assert extracted_user_id == user_id


def test_get_current_user_returns_none_for_invalid_token():
    """Test that get_current_user returns None for invalid tokens."""
    from web.auth.jwt_utils import get_current_user

    invalid_token = "invalid.token.here"
    user_id = get_current_user(invalid_token)

    assert user_id is None


def test_get_current_user_returns_none_for_expired_token():
    """Test that get_current_user returns None for expired tokens."""
    from web.auth.jwt_utils import create_access_token, get_current_user

    # Create expired token
    token = create_access_token(user_id="test_user", expires_minutes=-1)
    time.sleep(0.1)

    user_id = get_current_user(token)
    assert user_id is None


def test_get_current_user_returns_none_for_empty_token():
    """Test that get_current_user handles empty/None tokens."""
    from web.auth.jwt_utils import get_current_user

    assert get_current_user("") is None
    assert get_current_user(None) is None


def test_require_auth_raises_401_for_missing_token():
    """Test that require_auth dependency raises 401 when no token provided."""
    from web.auth.jwt_utils import require_auth

    with pytest.raises(HTTPException) as exc_info:
        # Call dependency with no Authorization header
        require_auth(authorization=None)

    assert exc_info.value.status_code == 401
    assert "missing" in exc_info.value.detail.lower() or "required" in exc_info.value.detail.lower()


def test_require_auth_raises_401_for_invalid_format():
    """Test that require_auth raises 401 for malformed Authorization header."""
    from web.auth.jwt_utils import require_auth

    with pytest.raises(HTTPException) as exc_info:
        # Missing "Bearer " prefix
        require_auth(authorization="just_a_token")

    assert exc_info.value.status_code == 401


def test_require_auth_raises_401_for_invalid_token():
    """Test that require_auth raises 401 for invalid tokens."""
    from web.auth.jwt_utils import require_auth

    with pytest.raises(HTTPException) as exc_info:
        require_auth(authorization="Bearer invalid.token.here")

    assert exc_info.value.status_code == 401


def test_require_auth_returns_user_id_for_valid_token():
    """Test that require_auth returns user_id for valid Bearer tokens."""
    from web.auth.jwt_utils import create_access_token, require_auth

    user_id = "authenticated_user"
    token = create_access_token(user_id=user_id)

    extracted_user_id = require_auth(authorization=f"Bearer {token}")

    assert extracted_user_id == user_id


def test_require_auth_raises_401_for_expired_token():
    """Test that require_auth rejects expired tokens."""
    from web.auth.jwt_utils import create_access_token, require_auth

    token = create_access_token(user_id="test_user", expires_minutes=-1)
    time.sleep(0.1)

    with pytest.raises(HTTPException) as exc_info:
        require_auth(authorization=f"Bearer {token}")

    assert exc_info.value.status_code == 401
