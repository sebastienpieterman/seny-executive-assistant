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
