"""
Slack Bot Polling Worker for Seny (Phase 20-02).

Polls for new DM messages to the Seny Slack bot and routes them through
MultiChannelChatService for responses.

Similar to TelegramBotWorker but for Slack bot DMs.

Usage:
    worker = SlackBotWorker()
    await worker.poll_and_respond()  # Single iteration, called by scheduler
"""

import asyncio
import logging
import uuid
from typing import Optional

from web.services.slack_bot_service import SlackBotService
from web.services.multichannel_chat_service import MultiChannelChatService
from web.core.database import (
    list_users_with_slack_bot_token,
    get_slack_bot_conversation,
    create_slack_bot_conversation,
    update_slack_bot_conversation,
    create_conversation,
    is_slack_chat_enabled,
    get_last_sent_nudge,
    get_nudge_by_slack_ts,
)

logger = logging.getLogger(__name__)


class SlackBotWorker:
    """
    Worker that polls Slack Bot API for DM messages and handles responses.

    Designed to be called periodically by scheduler.
    Processes all users with configured Slack bot tokens.
    """

    def __init__(self):
        """Initialize the Slack bot worker."""
        self.chat_service = MultiChannelChatService()

    async def poll_and_respond(self) -> dict:
        """
        Poll for new messages across all users and send responses.

        This is a single iteration designed to be called by scheduler.
        Returns stats about what was processed.

        Returns:
            Dict with processing stats
        """
        try:
            # Get all users with Slack bot tokens
            users = list_users_with_slack_bot_token()

            if not users:
                logger.debug("Slack bot: no users with bot token configured")
                return {"status": "ok", "users_checked": 0, "messages_processed": 0}

            logger.info(f"Slack bot: checking {len(users)} user(s) with bot tokens")

            total_processed = 0
            total_errors = 0
            users_checked = 0

            for user_data in users:
                user_id = user_data["user_id"]
                team_id = user_data["team_id"]

                try:
                    processed, errors = await self._process_user(
                        user_id=user_id,
                        team_id=team_id,
                        bot_user_id=user_data.get("bot_user_id")
                    )
                    total_processed += processed
                    total_errors += errors
                    users_checked += 1

                except Exception as e:
                    logger.error(
                        f"Slack bot worker error for user {user_id}: {repr(e)}"
                    )
                    total_errors += 1

            return {
                "status": "ok",
                "users_checked": users_checked,
                "messages_processed": total_processed,
                "errors": total_errors
            }

        except Exception as e:
            logger.error(f"Slack bot poll error: {repr(e)}")
            return {"status": "error", "error": repr(e)}

    async def _process_user(
        self,
        user_id: int,
        team_id: str,
        bot_user_id: Optional[str]
    ) -> tuple[int, int]:
        """
        Process messages for a single user's Slack workspace.

        Args:
            user_id: Seny user ID
            team_id: Slack workspace ID
            bot_user_id: Bot's Slack user ID

        Returns:
            Tuple of (messages_processed, errors)
        """
        # Check if Slack chat is enabled for this user
        if not is_slack_chat_enabled(user_id):
            logger.debug(f"Slack chat disabled for user {user_id}, skipping")
            return 0, 0

        bot_service = SlackBotService(user_id, team_id)

        if not bot_service.is_configured():
            return 0, 0

        # List all DM channels with the bot
        dm_channels = await bot_service.list_bot_dm_channels()

        if not dm_channels:
            return 0, 0

        processed = 0
        errors = 0

        for dm in dm_channels:
            channel_id = dm["id"]
            slack_user_id = dm.get("user_id")

            if not slack_user_id:
                continue

            try:
                msgs, errs = await self._process_channel(
                    user_id=user_id,
                    bot_service=bot_service,
                    channel_id=channel_id,
                    slack_user_id=slack_user_id
                )
                processed += msgs
                errors += errs

            except Exception as e:
                logger.error(
                    f"Error processing Slack channel {channel_id} for user {user_id}: {repr(e)}"
                )
                errors += 1

        return processed, errors

    async def _process_channel(
        self,
        user_id: int,
        bot_service: SlackBotService,
        channel_id: str,
        slack_user_id: str
    ) -> tuple[int, int]:
        """
        Process messages from a single DM channel.

        Args:
            user_id: Seny user ID
            bot_service: SlackBotService instance
            channel_id: Slack DM channel ID
            slack_user_id: Slack user ID chatting with bot

        Returns:
            Tuple of (messages_processed, errors)
        """
        # Get or create conversation link
        conv_link = get_slack_bot_conversation(user_id, channel_id)

        if conv_link:
            last_ts = conv_link.get("last_message_ts")
            conversation_id = conv_link["conversation_id"]
        else:
            last_ts = None
            conversation_id = None

        # Get new messages since last_ts
        messages = await bot_service.get_new_messages(
            channel_id=channel_id,
            since_ts=last_ts,
            limit=20
        )

        if not messages:
            return 0, 0

        processed = 0
        errors = 0
        last_processed_ts = last_ts

        # Get user display name for context
        user_info = await bot_service.get_user_info(slack_user_id)
        sender_name = None
        if user_info:
            sender_name = (
                user_info.get("display_name") or
                user_info.get("first_name") or
                user_info.get("real_name") or
                user_info.get("name")
            )

        # Create conversation if needed
        if not conversation_id:
            conversation_id = str(uuid.uuid4())
            create_conversation(
                user_id=user_id,
                conversation_id=conversation_id,
                title=f"Slack Chat"
            )
            create_slack_bot_conversation(
                user_id=user_id,
                slack_channel_id=channel_id,
                slack_user_id=slack_user_id,
                conversation_id=conversation_id
            )
            logger.info(
                f"Created new Slack bot conversation {conversation_id} "
                f"for channel {channel_id}"
            )

        for msg in messages:
            msg_ts = msg.get("ts")
            text = msg.get("text", "").strip()

            if not text:
                continue

            # Skip if we've already processed this message
            if last_ts and msg_ts and float(msg_ts) <= float(last_ts):
                continue

            try:
                # Inject nudge context so Claude can understand replies to nudges
                # Try exact thread reply lookup first (user replied to a specific message thread)
                thread_ts = msg.get("thread_ts")
                msg_ts = msg.get("ts")

                matched_nudge = None
                # thread_ts is only set on replies; if thread_ts != ts, this IS a reply
                if thread_ts and thread_ts != msg_ts:
                    matched_nudge = get_nudge_by_slack_ts(user_id, thread_ts)

                # Fall back to recency if no exact match
                if not matched_nudge:
                    matched_nudge = get_last_sent_nudge(user_id, 'slack', hours=4)

                user_message = text  # Preserve original before nudge context injection

                if matched_nudge:
                    nudge_ts = matched_nudge.get('sent_at') or matched_nudge.get('created_at', '')
                    nudge_id = matched_nudge.get('id', '')
                    match_type = "You replied directly to" if (thread_ts and thread_ts != msg_ts and get_nudge_by_slack_ts(user_id, thread_ts)) else "You recently received"
                    source_type = matched_nudge.get('source_type', '')
                    source_id = matched_nudge.get('source_id', '')
                    source_line = f"Source: {source_type} (id={source_id})\n" if source_type else ""
                    text = (
                        f"[Context: {match_type} this nudge (ID: {nudge_id}, sent: {nudge_ts}):\n"
                        f"Title: {matched_nudge['title']}\n"
                        f"Content: {matched_nudge.get('body', '')}\n"
                        f"{source_line}"
                        f"Nudge ID {nudge_id} — use this ID with nudge_get if you need full details or to link feedback.\n"
                        f"If my message below references this nudge, use the above to understand it.]\n\n"
                        f"{text}"
                    )

                # Process message through MultiChannelChatService
                response, tools_used = await asyncio.wait_for(
                    self.chat_service.handle_message(
                        user_id=user_id,
                        channel="slack",
                        channel_chat_id=channel_id,
                        message_text=text,
                        sender_name=sender_name
                    ),
                    timeout=30.0
                )

                # Auto-dismiss backstop for detected_action nudges
                DISMISS_SIGNALS = ('not my department', "don't nudge", 'ignore this', 'forget it',
                                   'not relevant', 'not for me', "doesn't apply", 'skip this')
                if (
                    matched_nudge
                    and matched_nudge.get('source_type') == 'detected_action'
                    and matched_nudge.get('source_id')
                    and isinstance(matched_nudge.get('source_id'), int)
                    and any(sig in user_message.lower() for sig in DISMISS_SIGNALS)
                ):
                    try:
                        from web.core.database import update_detected_action_status
                        source_id = matched_nudge['source_id']
                        update_detected_action_status(source_id, 'dismissed')
                        logger.info(
                            "Auto-dismissed detected_action %d for user %d via backstop",
                            source_id, user_id
                        )
                    except Exception as _e:
                        logger.warning("Auto-dismiss backstop error: %s", repr(_e))

                # Memory safety net — auto-save corrections Claude missed
                try:
                    from web.services.memory_safety_net import check_and_save_missed_correction
                    from web.core.database import get_db

                    # Check if a memory was saved during this response cycle
                    # (new user_memory with created_at in the last 5 seconds)
                    _memory_was_saved = False
                    try:
                        with get_db() as _conn:
                            _cur = _conn.cursor()
                            _cur.execute(
                                "SELECT 1 FROM user_memories WHERE user_id = %s "
                                "AND created_at >= NOW() - INTERVAL '5 seconds' LIMIT 1",
                                (user_id,)
                            )
                            _memory_was_saved = _cur.fetchone() is not None
                    except Exception:
                        _memory_was_saved = False

                    _saved_rule = await check_and_save_missed_correction(
                        user_message=user_message,
                        memory_was_saved=_memory_was_saved,
                        user_id=user_id,
                    )
                    if _saved_rule:
                        _footnote = f'\n\n_(Saved to memory: "{_saved_rule[:80]}{"..." if len(_saved_rule) > 80 else ""}")_'
                        response = response + _footnote
                except Exception as _snex:
                    logger.warning("[safety_net] wiring error: %s", repr(_snex))

                # Send response via bot
                await bot_service.send_message(channel_id, response)

                processed += 1
                if msg_ts:
                    last_processed_ts = msg_ts

                if tools_used:
                    logger.info(
                        f"Slack bot response for user {user_id}: "
                        f"tools={tools_used}, len={len(response)}"
                    )

            except asyncio.TimeoutError:
                logger.warning(
                    f"Slack bot response timeout for channel {channel_id}"
                )
                await bot_service.send_message(
                    channel_id,
                    "I'm taking longer than expected to process that. "
                    "Please try again in a moment."
                )
                errors += 1

            except Exception as e:
                logger.error(
                    f"Error processing Slack message in channel {channel_id}: {repr(e)}"
                )
                errors += 1

        # Update last_message_ts for next poll
        if last_processed_ts and last_processed_ts != last_ts:
            update_slack_bot_conversation(
                user_id=user_id,
                slack_channel_id=channel_id,
                last_message_ts=last_processed_ts
            )

        return processed, errors


def get_slack_bot_worker() -> SlackBotWorker:
    """Get a SlackBotWorker instance."""
    return SlackBotWorker()


# Scheduler-compatible async function
async def process_slack_bot_messages():
    """
    Process Slack bot messages.

    Called by scheduler every 10 seconds.
    """
    worker = SlackBotWorker()
    result = await worker.poll_and_respond()

    if result.get("messages_processed", 0) > 0:
        logger.info(
            f"Slack bot: processed {result['messages_processed']} messages "
            f"across {result.get('users_checked', 0)} users, "
            f"errors={result.get('errors', 0)}"
        )
