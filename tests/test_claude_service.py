import pytest
from unittest.mock import AsyncMock

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


def test_gmail_service_has_batch_methods():
    from web.services.gmail_service import GmailService

    assert hasattr(GmailService, 'mark_read_batch')
    assert hasattr(GmailService, 'mark_unread_batch')


@pytest.mark.asyncio
async def test_gmail_mark_read_and_unread_tools_use_batch_methods(monkeypatch):
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
    messages = [{'role': 'user', 'content': 'Mark these emails read.'}]
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

    assert len(gmail_tools) == 2
    for tool in gmail_tools:
        assert tool['name'] in {'gmail_mark_read', 'gmail_mark_unread'}
        assert tool['input_schema']['type'] == 'object'
        assert 'oneOf' not in tool['input_schema']
        assert 'anyOf' not in tool['input_schema']
        assert 'allOf' not in tool['input_schema']
        assert 'message_ids' in tool['input_schema']['properties']
        assert 'query' in tool['input_schema']['properties']
        assert 'email_account' in tool['input_schema']['properties']
        assert 'user_instruction' in tool['input_schema']['properties']


@pytest.mark.asyncio
async def test_gmail_tool_handler_calls_batch_methods(monkeypatch):
    import web.services.claude_service as claude_module
    from web.services.claude_service import ClaudeService, GmailService

    monkeypatch.setattr(GmailService, 'list_connected_accounts', staticmethod(lambda user_id: [{'email': 'test@example.com'}]))
    monkeypatch.setattr(claude_module, 'list_telegram_sessions', lambda user_id: [])
    monkeypatch.setattr(claude_module, 'get_local_file_stats', lambda user_id: {'total_files': 0})

    class ToolUseBlock:
        def __init__(self, name, input_data):
            self.type = 'tool_use'
            self.name = name
            self.input = input_data
            self.id = 'tool_use_1'

    class ToolUseResponse:
        stop_reason = 'tool_use'
        content = [
            ToolUseBlock('gmail_mark_read', {'message_ids': ['abc123'], 'email_account': 'test@example.com'}),
            ToolUseBlock('gmail_mark_unread', {'message_ids': ['def456'], 'email_account': 'test@example.com'})
        ]
        usage = type('U', (), {
            'input_tokens': 0,
            'output_tokens': 0,
            'cache_creation_input_tokens': 0,
            'cache_read_input_tokens': 0
        })()

    class FinalResponse:
        stop_reason = 'stop'
        content = [type('T', (), {'type': 'text', 'text': 'done'})()]
        usage = type('U', (), {
            'input_tokens': 0,
            'output_tokens': 0,
            'cache_creation_input_tokens': 0,
            'cache_read_input_tokens': 0
        })()

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=[ToolUseResponse(), FinalResponse()])
    service = ClaudeService()
    service.client = mock_client

    mocked_read_batch = AsyncMock(return_value=1)
    mocked_unread_batch = AsyncMock(return_value=1)
    monkeypatch.setattr(GmailService, 'mark_read_batch', mocked_read_batch)
    monkeypatch.setattr(GmailService, 'mark_unread_batch', mocked_unread_batch)

    monkeypatch.setattr(claude_module.CalendarService, 'list_connected_accounts', staticmethod(lambda user_id: []))
    monkeypatch.setattr('web.services.calendar_service.list_google_tokens', lambda user_id: [])
    monkeypatch.setattr(claude_module.OutlookService, 'list_connected_accounts', staticmethod(lambda user_id: []))
    monkeypatch.setattr('web.services.outlook_service.OutlookService.list_connected_accounts', staticmethod(lambda user_id: []))
    monkeypatch.setattr(claude_module.SlackService, 'list_connected_workspaces', staticmethod(lambda user_id: []))
    monkeypatch.setattr('web.services.slack_service.SlackService.list_connected_workspaces', staticmethod(lambda user_id: []))
    monkeypatch.setattr(claude_module.OutlookCalendarService, 'list_connected_accounts', staticmethod(lambda user_id: []))
    monkeypatch.setattr('web.services.outlook_calendar_service.OutlookCalendarService.list_connected_accounts', staticmethod(lambda user_id: []))

    await service.send_message([{'role': 'user', 'content': 'Mark these as read and unread.'}], enable_web_search=False, user_id=1)

    assert mocked_read_batch.await_count == 1
    assert mocked_unread_batch.await_count == 1
