"""
Multi-Channel Chat Service for Seny.

Routes messages from external channels (Telegram, Slack) to ClaudeService
and manages conversation context per channel.

This service is channel-agnostic - handlers for each channel call it.

Usage:
    service = MultiChannelChatService()
    response, tools_used = await service.handle_message(
        user_id=1,
        channel="telegram",
        channel_chat_id="12345",
        message_text="Hello!"
    )
"""

import logging
import uuid
from typing import Optional

from web.core.cache import response_cache
from web.services.claude_service import ClaudeService
from web.core.database import (
    get_telegram_bot_conversation,
    create_telegram_bot_conversation,
    update_telegram_bot_conversation_activity,
    delete_telegram_bot_conversation,
    get_slack_bot_conversation,
    update_slack_bot_conversation,
    delete_slack_bot_conversation,
    create_conversation,
    get_conversation,
    get_user_profile,
)
from web.api.settings import get_user_settings

logger = logging.getLogger(__name__)


class MultiChannelChatService:
    """
    Service for handling chat messages from external channels.

    Manages conversation context and routes to ClaudeService.
    Supports Telegram and future channels (Slack).
    """

    def __init__(self):
        """Initialize the multi-channel chat service."""
        self.claude_service = ClaudeService()

    async def handle_message(
        self,
        user_id: int,
        channel: str,
        channel_chat_id: str,
        message_text: str,
        sender_name: Optional[str] = None,
        timezone: str = "America/Chicago"
    ) -> tuple[str, list[str]]:
        """
        Handle an incoming message from an external channel.

        Routes to ClaudeService with proper conversation context.

        Args:
            user_id: Seny user ID
            channel: Channel identifier ("telegram", "slack")
            channel_chat_id: Channel-specific chat ID
            message_text: The message content
            sender_name: Optional display name of sender
            timezone: User's timezone for context

        Returns:
            Tuple of (response_text, tools_used)
        """
        try:
            # Get or create conversation for this channel+chat
            conversation_id = await self._get_or_create_conversation(
                user_id=user_id,
                channel=channel,
                channel_chat_id=channel_chat_id
            )

            # Get user's model preference
            user_settings = get_user_settings(user_id)
            model = user_settings.get("claude_model") if user_settings else None
            if user_settings:
                timezone = user_settings.get('digest_timezone', timezone)

            # LCD Layer 2: inject current state context — matches routes.py pattern
            system_context = None
            try:
                from web.services.lcd_service import LCDService
                lcd_layer2 = await LCDService(user_id)._get_layer2_for_context()
                if lcd_layer2:
                    profile = get_user_profile(user_id)
                    user_name = profile['user_name']
                    system_context = f"[What {user_name} has told you recently: {lcd_layer2}]"
            except Exception:
                pass  # Fail-open — bot chat continues without LCD

            # Call ClaudeService.chat() for the actual response
            response, conv_id, usage_stats, citations, tools_used, capture_info = \
                await self.claude_service.chat(
                    user_message=message_text,
                    conversation_id=conversation_id,
                    user_id=str(user_id),
                    timezone=timezone,
                    model=model,
                    system_context=system_context
                )

            # Update activity timestamp
            self._update_activity(user_id, channel, channel_chat_id)

            # Clean up response (remove any internal tags Claude may echo)
            import re
            response = re.sub(
                r'\n*\s*<tool_calls_made>[\s\S]*?</tool_calls_made>\s*',
                '', response
            ).strip()
            response = re.sub(
                r'\n*\s*\[Tools used:[^\]]*\]\s*',
                '', response
            ).strip()

            return response, tools_used

        except Exception as e:
            logger.error(
                f"MultiChannelChat error for user {user_id}, {channel}/{channel_chat_id}: {repr(e)}"
            )
            # Return user-friendly error
            return (
                "I'm having trouble processing that right now. Please try again in a moment.",
                []
            )

    async def _get_or_create_conversation(
        self,
        user_id: int,
        channel: str,
        channel_chat_id: str
    ) -> str:
        """
        Get existing or create new conversation for channel chat.

        Checks the in-memory TTL cache before hitting the database.
        Cache TTL is 10 minutes to cover active multi-message conversations.

        Args:
            user_id: Seny user ID
            channel: Channel type
            channel_chat_id: Channel-specific chat ID

        Returns:
            Conversation ID
        """
        cache_key = f"chat_context:{user_id}:{channel}:{channel_chat_id}"
        cached_id = response_cache.get(cache_key)
        if cached_id:
            logger.debug(f"Conversation cache hit for {channel}/{channel_chat_id}")
            return cached_id

        if channel == "telegram":
            conversation_id = self._get_or_create_telegram_conversation(
                user_id, int(channel_chat_id)
            )
        elif channel == "slack":
            conversation_id = self._get_or_create_slack_conversation(
                user_id, channel_chat_id
            )
        else:
            conversation_id = self._create_new_conversation(user_id, channel, channel_chat_id)

        response_cache.set(cache_key, conversation_id, ttl_seconds=600)
        return conversation_id

    def clear_conversation_cache(self, user_id: int, channel: str, channel_chat_id: str) -> None:
        """
        Invalidate the cached conversation ID for a given channel chat.

        Call this when a conversation is explicitly reset or deleted,
        so the next message will re-query the database.

        Args:
            user_id: Seny user ID
            channel: Channel type
            channel_chat_id: Channel-specific chat ID
        """
        cache_key = f"chat_context:{user_id}:{channel}:{channel_chat_id}"
        response_cache.invalidate(cache_key)

    def _get_or_create_telegram_conversation(
        self,
        user_id: int,
        telegram_chat_id: int
    ) -> str:
        """
        Get or create conversation for Telegram chat.

        Args:
            user_id: Seny user ID
            telegram_chat_id: Telegram chat ID

        Returns:
            Conversation ID
        """
        # Check for existing conversation
        existing = get_telegram_bot_conversation(user_id, telegram_chat_id)
        if existing:
            conv_id = existing["conversation_id"]
            # Verify the conversation actually exists (guard against stale migration links)
            if get_conversation(conv_id):
                return conv_id
            # Stale link — delete it so we can create a fresh one below
            logger.warning(
                f"Stale telegram_bot_conversations link for user {user_id}, "
                f"chat {telegram_chat_id}: conversation {conv_id} missing. Recreating."
            )
            delete_telegram_bot_conversation(user_id, telegram_chat_id)

        # Create new conversation
        conversation_id = str(uuid.uuid4())
        create_conversation(
            user_id=user_id,
            conversation_id=conversation_id,
            title=f"Telegram Chat"
        )

        # Link to Telegram chat
        create_telegram_bot_conversation(
            user_id=user_id,
            telegram_chat_id=telegram_chat_id,
            conversation_id=conversation_id
        )

        logger.info(
            f"Created new conversation {conversation_id} for Telegram chat {telegram_chat_id}"
        )
        return conversation_id

    def _get_or_create_slack_conversation(
        self,
        user_id: int,
        slack_channel_id: str
    ) -> str:
        """
        Get or create conversation for Slack bot DM.

        Args:
            user_id: Seny user ID
            slack_channel_id: Slack DM channel ID

        Returns:
            Conversation ID

        Note: The conversation link is created by SlackBotWorker which has
        access to the slack_user_id. This method just looks up existing links
        or creates a new conversation if none exists.
        """
        # Check for existing conversation
        existing = get_slack_bot_conversation(user_id, slack_channel_id)
        if existing:
            conv_id = existing["conversation_id"]
            # Verify the conversation actually exists (guard against stale migration links)
            if get_conversation(conv_id):
                return conv_id
            # Stale link — delete it so we can create a fresh one below
            logger.warning(
                f"Stale slack_bot_conversations link for user {user_id}, "
                f"channel {slack_channel_id}: conversation {conv_id} missing. Recreating."
            )
            delete_slack_bot_conversation(user_id, slack_channel_id)

        # If no conversation link exists, create a new conversation
        # The worker will link it to the Slack channel
        conversation_id = str(uuid.uuid4())
        create_conversation(
            user_id=user_id,
            conversation_id=conversation_id,
            title="Slack Chat"
        )

        logger.info(
            f"Created new conversation {conversation_id} for Slack channel {slack_channel_id}"
        )
        return conversation_id

    def _create_new_conversation(
        self,
        user_id: int,
        channel: str,
        channel_chat_id: str
    ) -> str:
        """
        Create a new conversation for a channel chat.

        Args:
            user_id: Seny user ID
            channel: Channel type
            channel_chat_id: Channel-specific chat ID

        Returns:
            Conversation ID
        """
        conversation_id = str(uuid.uuid4())
        create_conversation(
            user_id=user_id,
            conversation_id=conversation_id,
            title=f"{channel.title()} Chat"
        )
        return conversation_id

    def _update_activity(
        self,
        user_id: int,
        channel: str,
        channel_chat_id: str
    ) -> None:
        """
        Update activity timestamp for channel conversation.

        Args:
            user_id: Seny user ID
            channel: Channel type
            channel_chat_id: Channel-specific chat ID
        """
        if channel == "telegram":
            update_telegram_bot_conversation_activity(
                user_id, int(channel_chat_id)
            )
        elif channel == "slack":
            update_slack_bot_conversation(
                user_id, channel_chat_id
            )
