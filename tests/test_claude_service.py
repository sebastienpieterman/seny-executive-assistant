import pytest

from web.services.claude_service import _extract_user_instruction_text, _find_last_user_message


def test_extract_user_instruction_text_returns_string_for_text_message():
    result = _extract_user_instruction_text('Mark thread as read')
    assert result == 'Mark thread as read'


def test_extract_user_instruction_text_extracts_text_from_content_blocks():
    user_message = [
        {'type': 'image', 'url': 'https://example.com/img.png'},
        {'type': 'text', 'text': 'Please mark all unread messages in inbox as read.'},
    ]

    result = _extract_user_instruction_text(user_message)
    assert result == 'Please mark all unread messages in inbox as read.'


def test_extract_user_instruction_text_returns_empty_for_none():
    assert _extract_user_instruction_text(None) == ''


def test_find_last_user_message_returns_last_user_content():
    messages = [
        {'role': 'assistant', 'content': 'Hello'},
        {'role': 'user', 'content': 'First user message'},
        {'role': 'assistant', 'content': 'Ok'},
        {'role': 'user', 'content': 'Second user message'},
    ]

    assert _find_last_user_message(messages) == 'Second user message'


def test_find_last_user_message_returns_empty_when_no_user_messages():
    messages = [
        {'role': 'assistant', 'content': 'Hello'},
        {'role': 'tool', 'content': 'Result'},
    ]

    assert _find_last_user_message(messages) == ''


@pytest.mark.asyncio
async def test_gmail_tool_schemas_do_not_use_top_level_oneof_anyof_allof(monkeypatch):
    import web.services.claude_service as claude_module
    from web.services.claude_service import ClaudeService, GmailService, CalendarService, SlackService, OutlookService, OutlookCalendarService

    monkeypatch.setattr(GmailService, 'list_connected_accounts', staticmethod(lambda user_id: [{'email': 'test@example.com'}]))
    monkeypatch.setattr(claude_module, 'list_telegram_sessions', lambda user_id: [])
    monkeypatch.setattr(claude_module, 'get_local_file_stats', lambda user_id: {'total_files': 0})
    monkeypatch.setattr(OutlookCalendarService, 'list_connected_accounts', staticmethod(lambda user_id: []))
    monkeypatch.setattr(OutlookService, 'list_connected_accounts', staticmethod(lambda user_id: []))
    monkeypatch.setattr(CalendarService, 'list_connected_accounts', staticmethod(lambda user_id: []))
    monkeypatch.setattr(SlackService, 'list_connected_workspaces', staticmethod(lambda user_id: []))

    service = ClaudeService()
    messages = [{'role': 'user', 'content': 'Test'}]
    captured = {}

    async def fake_create(**params):
        captured['params'] = params
        class Usage:
            input_tokens = 0
            output_tokens = 0
            cache_creation_input_tokens = 0
            cache_read_input_tokens = 0

        class Response:
            stop_reason = 'stop'
            content = 'ok'
            usage = Usage()

        return Response()

    service.client.messages.create = fake_create
    await service.send_message(messages, enable_web_search=False, user_id=1)

    tools = captured['params'].get('tools', [])
    gmail_tools = [t for t in tools if t['name'] in {'gmail_mark_read', 'gmail_mark_unread'}]

    assert gmail_tools, 'Expected gmail_mark_read and gmail_mark_unread tools to be present when a Gmail account is connected.'

    for tool in gmail_tools:
        schema = tool['input_schema']
        assert isinstance(schema, dict)
        assert 'oneOf' not in schema
        assert 'anyOf' not in schema
        assert 'allOf' not in schema
        assert schema.get('type') == 'object'
        assert schema.get('required') == []
