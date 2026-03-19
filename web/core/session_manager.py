"""
Database-backed session management for conversation state.

Refactored from in-memory storage to use SQLite persistence.
"""

import uuid
from typing import List
from web.core import database as db


def generate_title(content: str, max_length: int = 50) -> str:
    """
    Generate a conversation title from the first user message.

    Args:
        content: The first user message content
        max_length: Maximum title length

    Returns:
        str: Truncated title
    """
    if not content:
        return "New Conversation"
    title = content.replace('\n', ' ').strip()
    if len(title) > max_length:
        return title[:max_length - 3] + "..."
    return title or "New Conversation"


class SessionManager:
    """Manages conversation sessions with database persistence."""

    def __init__(self):
        """Initialize the session manager."""
        pass

    def create_session(self, user_id: int = None) -> str:
        """
        Create a new conversation session.

        Args:
            user_id: User ID to associate with this session (required for persistence)

        Returns:
            str: New conversation ID (UUID)

        Note:
            If user_id is not provided, creates conversation without user association.
            This maintains backwards compatibility but won't persist properly.
        """
        conversation_id = str(uuid.uuid4())

        # Create conversation in database if user_id provided
        if user_id is not None:
            db.create_conversation(user_id, conversation_id)

        return conversation_id

    def get_history(self, conversation_id: str) -> List[dict]:
        """
        Get conversation history for a session.

        Args:
            conversation_id: The conversation ID

        Returns:
            list: List of messages in the conversation (empty if new session)
        """
        # Get messages from database
        messages = db.get_conversation_messages(conversation_id)
        return messages

    def save_message(self, conversation_id: str, role: str, content: str) -> None:
        """
        Save a message to a conversation.

        Args:
            conversation_id: The conversation ID
            role: Message role ('user' or 'assistant')
            content: Message content
        """
        # Save message to database
        db.save_message(conversation_id, role, content)

    def update_history(self, conversation_id: str, messages: List[dict]) -> None:
        """
        Update the entire conversation history.

        Args:
            conversation_id: The conversation ID
            messages: Complete list of messages for the conversation

        Note:
            This implementation saves only NEW messages that aren't already in the database.
            The claude_service.py calls this with the full history after adding new messages,
            so we need to identify and save only the messages added since last call.
        """
        # Get existing messages from database
        existing_messages = db.get_conversation_messages(conversation_id)

        # Find new messages (those not in existing_messages)
        # Compare by counting - new messages are at the end
        existing_count = len(existing_messages)
        new_messages = messages[existing_count:]

        # Save only the new messages (strip internal tool metadata before persisting)
        import re
        for msg in new_messages:
            content = msg['content']
            # Multimodal messages (image + text) have content as a list of blocks.
            # Extract just the text for storage — we don't persist base64 image data.
            if isinstance(content, list):
                content = ' '.join(
                    block.get('text', '') for block in content
                    if isinstance(block, dict) and block.get('type') == 'text'
                )
            if msg['role'] == 'assistant' and '<tool_calls_made>' in content:
                content = re.sub(r'\n*\s*<tool_calls_made>[\s\S]*?</tool_calls_made>\s*', '', content).strip()
            db.save_message(conversation_id, msg['role'], content)

        # Set title from first user message if this is a new conversation
        # Guard: only auto-set if no title exists yet (user may have renamed it)
        if existing_count == 0 and len(new_messages) > 0:
            existing_conv = db.get_conversation(conversation_id)
            if not (existing_conv and existing_conv.get('title')):
                first_user_msg = next((m for m in new_messages if m['role'] == 'user'), None)
                if first_user_msg:
                    title_content = first_user_msg['content']
                    if isinstance(title_content, list):
                        title_content = ' '.join(
                            block.get('text', '') for block in title_content
                            if isinstance(block, dict) and block.get('type') == 'text'
                        )
                    title = generate_title(title_content)
                    db.update_conversation_title(conversation_id, title)

    def clear_session(self, conversation_id: str) -> None:
        """
        Clear a conversation session.

        Args:
            conversation_id: The conversation ID to clear

        Note:
            Currently not implemented for database storage.
            Messages are kept for history. Consider soft-delete in future.
        """
        # TODO: Implement soft delete or archive functionality
        pass

    def session_exists(self, conversation_id: str) -> bool:
        """
        Check if a session exists.

        Args:
            conversation_id: The conversation ID to check

        Returns:
            bool: True if session exists, False otherwise
        """
        conversation = db.get_conversation(conversation_id)
        return conversation is not None

    def get_session_count(self) -> int:
        """
        Get the total number of active sessions.

        Returns:
            int: Number of sessions

        Note:
            This is no longer meaningful for database storage.
            Returns 0 to maintain interface compatibility.
        """
        # Not meaningful for database storage
        # Would need to count all conversations across all users
        return 0
