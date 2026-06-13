import pytest
from fastapi import HTTPException

from web.api.routes import ChatRequest, chat
from web.services.claude_service import ClaudeServiceError


@pytest.mark.asyncio
async def test_chat_returns_503_for_connection_error(monkeypatch):
    """It should map a Claude connection failure to 503 Service Unavailable."""

    async def fake_chat(*args, **kwargs):
        raise ClaudeServiceError("Connection error: network unreachable")

    monkeypatch.setattr("web.api.routes.claude_service.chat", fake_chat)
    monkeypatch.setattr("web.api.routes.get_user_settings", lambda user_id: {"claude_model": "claude-sonnet-4-5-20250929"})
    monkeypatch.setattr("web.api.routes.get_priority_items", lambda user_id, status='active', limit=15: [])
    monkeypatch.setattr("web.api.routes.get_recent_nudges", lambda user_id, hours=24, limit=3: [])
    monkeypatch.setattr("web.api.routes.get_user_profile", lambda user_id: {
        'user_name': 'Test User',
        'user_pronouns_subject': 'they',
        'user_pronouns_object': 'them',
        'user_pronouns_possessive': 'their',
        'user_context': '',
        'key_people': '[]',
        'key_projects': '[]',
        'priorities': '',
        'setup_complete': False
    })

    request = ChatRequest(message="Hello")

    with pytest.raises(HTTPException) as exc_info:
        await chat(request, user_id="1")

    assert exc_info.value.status_code == 503
    assert "Connection error" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_chat_returns_500_for_unexpected_claude_error(monkeypatch):
    """It should map unexpected Claude service failures to 500."""

    async def fake_chat(*args, **kwargs):
        raise ClaudeServiceError("Unexpected error: 0")

    monkeypatch.setattr("web.api.routes.claude_service.chat", fake_chat)
    monkeypatch.setattr("web.api.routes.get_user_settings", lambda user_id: {"claude_model": "claude-sonnet-4-5-20250929"})
    monkeypatch.setattr("web.api.routes.get_priority_items", lambda user_id, status='active', limit=15: [])
    monkeypatch.setattr("web.api.routes.get_recent_nudges", lambda user_id, hours=24, limit=3: [])
    monkeypatch.setattr("web.api.routes.get_user_profile", lambda user_id: {
        'user_name': 'Test User',
        'user_pronouns_subject': 'they',
        'user_pronouns_object': 'them',
        'user_pronouns_possessive': 'their',
        'user_context': '',
        'key_people': '[]',
        'key_projects': '[]',
        'priorities': '',
        'setup_complete': False
    })

    request = ChatRequest(message="Hello")

    with pytest.raises(HTTPException) as exc_info:
        await chat(request, user_id="1")

    assert exc_info.value.status_code == 500
    assert "Service error: Unexpected error: 0" == str(exc_info.value.detail)
