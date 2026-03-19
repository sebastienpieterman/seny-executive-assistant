"""
Slack Events Handler - Phase 21-02

Processes Slack DM message events received via the Events API webhook.
Routes messages through MultiChannelChatService and sends responses via bot.

Called by the Slack Events webhook endpoint (web/api/slack_events.py)
as a background task for each incoming DM event.
"""

import asyncio
import logging
import uuid
from typing import Optional

from web.services.slack_bot_service import SlackBotService
from web.services.multichannel_chat_service import MultiChannelChatService
from web.core.database import (
    is_slack_chat_enabled,
    get_slack_bot_conversation,
    create_slack_bot_conversation,
    update_slack_bot_conversation,
    create_conversation,
    get_seny_user_by_slack_team,
    get_last_sent_nudge,  # HF-03: nudge context injection
)

logger = logging.getLogger(__name__)


class SlackEventsHandler:
    """
    Handles incoming Slack DM message events from the Events API.

    Routes messages through MultiChannelChatService and sends responses
    back via SlackBotService. Reuses the same conversation tracking as
    the polling worker (get/create_slack_bot_conversation).
    """

    def __init__(self):
        """Initialize handler with shared chat service."""
        self.chat_service = MultiChannelChatService()

    async def handle_message_event(self, event: dict, team_id: str) -> None:
        """
        Process a Slack message event from the Events API.

        Args:
            event: Slack event object (from event_callback payload's "event" field)
            team_id: Slack workspace team ID (from event_callback payload)
        """
        # Skip bot messages to avoid response loops
        if event.get("bot_id"):
            return

        # Skip non-user message subtypes (channel_join, channel_leave, etc.)
        subtype = event.get("subtype")
        if subtype and subtype not in ("thread_broadcast",):
            logger.debug(f"Slack Events: skipping subtype={subtype}")
            return

        channel = event.get("channel")
        sender_slack_id = event.get("user")
        text = (event.get("text") or "").strip()

        if not channel or not sender_slack_id or not text:
            return

        # Look up which Seny user owns this Slack workspace
        user_id = get_seny_user_by_slack_team(team_id)
        if user_id is None:
            logger.debug(f"Slack Events: no Seny user found for team {team_id}")
            return

        # Check if Slack chat is enabled for this user
        if not is_slack_chat_enabled(user_id):
            logger.debug(f"Slack Events: chat disabled for user {user_id}")
            return

        bot_service = SlackBotService(user_id, team_id)
        if not bot_service.is_configured():
            logger.warning(f"Slack Events: bot not configured for user {user_id}, team {team_id}")
            return

        # Get sender display name for context
        user_info = await bot_service.get_user_info(sender_slack_id)
        sender_name: Optional[str] = None
        if user_info:
            sender_name = (
                user_info.get("display_name")
                or user_info.get("first_name")
                or user_info.get("real_name")
                or user_info.get("name")
            )

        # Get or create conversation link (shared with polling worker)
        conv_link = get_slack_bot_conversation(user_id, channel)
        if conv_link:
            conversation_id = conv_link["conversation_id"]
        else:
            conversation_id = str(uuid.uuid4())
            create_conversation(
                user_id=user_id,
                conversation_id=conversation_id,
                title="Slack Chat"
            )
            create_slack_bot_conversation(
                user_id=user_id,
                slack_channel_id=channel,
                slack_user_id=sender_slack_id,
                conversation_id=conversation_id
            )
            logger.info(f"Slack Events: created conversation {conversation_id} for channel {channel}")

        # Inject recent nudge context so Claude can understand replies to nudges (HF-03/HF-10)
        recent_nudge = get_last_sent_nudge(user_id, 'slack', hours=4)
        if recent_nudge:
            nudge_ts = recent_nudge.get('sent_at') or recent_nudge.get('created_at', '')
            nudge_id = recent_nudge.get('id', '')
            source_type = recent_nudge.get('source_type', '')
            source_id = recent_nudge.get('source_id', '')
            source_line = f"Source: {source_type} (id={source_id})\n" if source_type else ""
            text = (
                f"[Context: You recently sent me this nudge (ID: {nudge_id}, sent: {nudge_ts}):\n"
                f"Title: {recent_nudge['title']}\n"
                f"Content: {recent_nudge.get('body', '')}\n"
                f"{source_line}"
                f"Nudge ID {nudge_id} — use this ID with nudge_get if you need full details or to link feedback.\n"
                f"If my message below references items from this nudge, use the above to understand them.]\n\n"
                f"{text}"
            )

        # Route through MultiChannelChatService
        try:
            response, tools_used = await asyncio.wait_for(
                self.chat_service.handle_message(
                    user_id=user_id,
                    channel="slack",
                    channel_chat_id=channel,
                    message_text=text,
                    sender_name=sender_name,
                ),
                timeout=30.0
            )

            # Auto-resolve backstop: if Claude said it resolved a priority item but forgot to call the tool
            if (
                recent_nudge
                and recent_nudge.get('source_type') == 'priority_context'
                and recent_nudge.get('source_id')
                and isinstance(recent_nudge.get('source_id'), int)
                and 'priority_resolve' not in tools_used
                and 'resolv' in response.lower()
            ):
                try:
                    from web.core.database import resolve_priority_item
                    source_id = recent_nudge['source_id']
                    ok = resolve_priority_item(source_id, user_id)
                    if ok:
                        logger.info(
                            "Auto-resolved priority_context item %d for user %d (backstop)",
                            source_id, user_id
                        )
                    else:
                        logger.warning(
                            "Auto-resolve backstop: resolve_priority_item(%d, %d) returned False",
                            source_id, user_id
                        )
                except Exception as e:
                    logger.warning("Auto-resolve backstop error: %s", repr(e))

            # Send response via bot
            await bot_service.send_message(channel, response)

            # Update conversation last-message timestamp
            ts = event.get("ts")
            if ts:
                update_slack_bot_conversation(
                    user_id=user_id,
                    slack_channel_id=channel,
                    last_message_ts=ts
                )

            if tools_used:
                logger.info(
                    f"Slack Events: response for user {user_id}: "
                    f"tools={tools_used}, len={len(response)}"
                )

        except asyncio.TimeoutError:
            logger.warning(f"Slack Events: response timeout for channel {channel}")
            await bot_service.send_message(
                channel,
                "I'm taking longer than expected to process that. "
                "Please try again in a moment."
            )

        except Exception as e:
            logger.error(
                f"Slack Events: error handling message in channel {channel}: {repr(e)}"
            )
