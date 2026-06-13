import pytest
from fastapi import HTTPException

from web.api.auth import RegisterRequest, register


@pytest.mark.asyncio
async def test_register_allows_first_user_when_no_users_exist(monkeypatch):
    """The first registration should succeed when there are no existing users."""
    monkeypatch.setattr("web.api.auth.get_user_count", lambda: 0)

    created = {"called": False}

    def fake_create_user(email: str, hashed_password: str):
        created["called"] = True
        assert email == "first@example.com"
        assert isinstance(hashed_password, str)
        assert len(hashed_password) > 0
        return 1

    monkeypatch.setattr("web.api.auth.create_user", fake_create_user)

    response = await register(
        None,
        RegisterRequest(email="first@example.com", password="password123"),
    )

    assert response.message == "User created successfully"
    assert created["called"] is True


@pytest.mark.asyncio
async def test_register_rejects_second_user_after_first_exists(monkeypatch):
    """Registration should be closed after the first user is created."""
    monkeypatch.setattr("web.api.auth.get_user_count", lambda: 1)

    with pytest.raises(HTTPException) as exc_info:
        await register(
            None,
            RegisterRequest(email="second@example.com", password="password123"),
        )

    assert exc_info.value.status_code == 403
    assert (
        exc_info.value.detail
        == "Registration is closed. This Seny instance already has an owner."
    )
