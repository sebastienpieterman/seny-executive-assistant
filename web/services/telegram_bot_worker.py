"""
Telegram Bot Worker for Seny.

Handles messages from Telegram Bot API via polling or webhook and routes
them through MultiChannelChatService for responses.

Usage (polling mode - scheduler):
    worker = TelegramBotWorker()
    await worker.poll_and_respond()  # Single iteration, called by scheduler

Usage (webhook mode - endpoint):
    worker = TelegramBotWorker()
    await worker.handle_webhook_update(update)  # Called by webhook endpoint
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from anthropic import AsyncAnthropic

from web.services.telegram_bot_service import TelegramBotService
from web.services.multichannel_chat_service import MultiChannelChatService
from web.core.database import (
    get_telegram_bot_user_link,
    create_telegram_bot_user_link,
    is_telegram_chat_enabled,
    get_last_sent_nudge,
    get_nudge_by_telegram_message_id,
    get_priority_items,
    get_screen_dismissal_patterns,
    list_google_tokens,
    record_screen_dismissal,
    get_user_profile,
)
from src.core.config import Config

logger = logging.getLogger(__name__)

# Module-level state for update tracking
_last_update_id = 0


def _is_today(dt_value) -> bool:
    """Check if a datetime value (string or datetime) is from today."""
    if dt_value is None:
        return False
    try:
        if isinstance(dt_value, str):
            dt_value = datetime.fromisoformat(dt_value.replace('Z', '+00:00'))
        return dt_value.date() == datetime.now().date()
    except Exception:
        return False


class TelegramBotWorker:
    """
    Worker that handles Telegram Bot messages via polling or webhook.

    In polling mode: called periodically by scheduler (poll_and_respond).
    In webhook mode: called by webhook endpoint (handle_webhook_update).

    Both modes share the same message handling logic.
    """

    def __init__(self):
        """Initialize the Telegram bot worker."""
        self.bot_service = TelegramBotService()
        self.chat_service = MultiChannelChatService()
        # Get app URL for linking instructions
        self.app_url = os.getenv("APP_URL", "http://localhost:8000")

    async def poll_and_respond(self) -> dict:
        """
        Poll for new messages and send responses.

        This is a single iteration designed to be called by scheduler.
        Returns stats about what was processed.

        Returns:
            Dict with processing stats
        """
        global _last_update_id

        if not self.bot_service.is_configured():
            logger.debug("Telegram bot not configured, skipping poll")
            return {"status": "skipped", "reason": "not_configured"}

        try:
            # Poll for updates (short timeout since scheduler calls frequently)
            updates = await self.bot_service.get_updates(
                offset=_last_update_id + 1,
                timeout=5
            )

            if not updates:
                return {"status": "ok", "messages_processed": 0}

            processed = 0
            errors = 0

            for update in updates:
                # Track update ID to avoid reprocessing
                update_id = update.get("update_id", 0)
                if update_id > _last_update_id:
                    _last_update_id = update_id

                # Process message updates
                message = update.get("message")
                if not message:
                    continue

                try:
                    await self._handle_message(message)
                    processed += 1
                except Exception as e:
                    logger.error(f"Error handling Telegram message: {repr(e)}")
                    errors += 1

            return {
                "status": "ok",
                "messages_processed": processed,
                "errors": errors
            }

        except Exception as e:
            logger.error(f"Telegram bot poll error: {repr(e)}")
            return {"status": "error", "error": repr(e)}

    async def _handle_message(self, message: dict) -> None:
        """
        Handle a single incoming Telegram message.

        Args:
            message: Telegram message object
        """
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        text = message.get("text", "")
        sender = message.get("from", {})

        if not chat_id or not text:
            return

        # Check if this Telegram chat is linked to a Seny user
        user_link = get_telegram_bot_user_link(chat_id)

        if not user_link:
            # User not linked - send welcome/linking message
            await self._send_link_instructions(
                chat_id=chat_id,
                telegram_username=sender.get("username"),
                telegram_first_name=sender.get("first_name")
            )
            return

        user_id = user_link["user_id"]

        # Check if Telegram chat is enabled for this user
        if not is_telegram_chat_enabled(user_id):
            logger.debug(f"Telegram chat disabled for user {user_id}, ignoring message")
            return

        # Check if this is a reply to a screen agent nudge — route through Claude with context
        # Two detection paths:
        #   1. Swipe-reply: user explicitly replied to the screen nudge message
        #   2. Recency: screen nudge was sent within last 5 min and user just typed a message
        reply_to = message.get("reply_to_message", {})
        reply_message_id = reply_to.get("message_id") if reply_to else None

        is_screen_reply = False
        if reply_message_id:
            from web.api.screen import is_screen_nudge_message
            if is_screen_nudge_message(reply_message_id):
                is_screen_reply = True

        if not is_screen_reply:
            from web.api.screen import has_recent_screen_nudge
            if has_recent_screen_nudge(str(user_id)):
                is_screen_reply = True

        if is_screen_reply:
            await self._handle_screen_nudge_reply(
                user_id=user_id,
                chat_id=chat_id,
                user_message=text,
            )
            return

        # Send typing indicator
        await self.bot_service.send_typing(chat_id)

        matched_nudge = None
        if reply_message_id:
            matched_nudge = get_nudge_by_telegram_message_id(user_id, str(reply_message_id))

        # Fall back to recency if no exact match
        if not matched_nudge:
            matched_nudge = get_last_sent_nudge(user_id, 'telegram', hours=4)

        user_message = text  # Preserve original before nudge context injection

        if matched_nudge:
            nudge_ts = matched_nudge.get('sent_at') or matched_nudge.get('created_at', '')
            nudge_id = matched_nudge.get('id', '')
            match_type = "You replied directly to" if (reply_message_id and get_nudge_by_telegram_message_id(user_id, str(reply_message_id))) else "You recently received"
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

        # Create task with timeout for Claude processing
        try:
            # Process message through MultiChannelChatService
            response, tools_used = await asyncio.wait_for(
                self.chat_service.handle_message(
                    user_id=user_id,
                    channel="telegram",
                    channel_chat_id=str(chat_id),
                    message_text=text,
                    sender_name=sender.get("first_name")
                ),
                timeout=30.0
            )

            # Auto-resolve backstop: if Claude said it resolved a priority item but forgot to call the tool
            if (
                matched_nudge
                and matched_nudge.get('source_type') == 'priority_context'
                and matched_nudge.get('source_id')
                and isinstance(matched_nudge.get('source_id'), int)
                and 'priority_resolve' not in tools_used
                and 'resolv' in response.lower()
            ):
                try:
                    from web.core.database import resolve_priority_item
                    source_id = matched_nudge['source_id']
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

            # Send response — guard against empty string (Telegram rejects blank messages)
            if not response or not response.strip():
                response = "Got it."
            await self.bot_service.send_message(chat_id, response)

            if tools_used:
                logger.info(
                    f"Telegram response for user {user_id}: "
                    f"tools={tools_used}, len={len(response)}"
                )

        except asyncio.TimeoutError:
            logger.warning(f"Telegram response timeout for chat {chat_id}")
            await self.bot_service.send_message(
                chat_id,
                "I'm taking longer than expected to process that. "
                "Please try again in a moment."
            )

    async def _handle_screen_nudge_reply(
        self,
        user_id: int,
        chat_id: int,
        user_message: str,
    ) -> None:
        """
        Handle a reply to a screen agent nudge with contextual intelligence.

        Instead of blindly backing off, gathers calendar/task/priority context
        and makes a one-shot Claude call to evaluate the user's explanation.

        Args:
            user_id: Seny user ID
            chat_id: Telegram chat ID for sending response
            user_message: What the user replied to the nudge
        """
        from web.api.screen import dismiss_screen_nudge, set_short_cooldown
        from web.core.database import get_db

        try:
            # Send typing indicator while we gather context
            await self.bot_service.send_typing(chat_id)

            profile = get_user_profile(user_id)
            user_name = profile['user_name']

            # ------------------------------------------------------------------
            # 1. Gather context
            # ------------------------------------------------------------------

            # LCD Layer 2: fetch current context to inform dismissal judgment
            lcd_layer2 = None
            try:
                from web.services.lcd_service import LCDService
                lcd_layer2 = await LCDService(user_id)._get_layer2_for_context()
            except Exception:
                pass  # Fail-open

            # Calendar: today's events
            calendar_summary = "no calendar connected"
            try:
                google_accounts = list_google_tokens(user_id)
                if google_accounts:
                    from web.services.calendar_service import CalendarService
                    email = google_accounts[0].get('email')
                    if email:
                        cal = CalendarService(user_id, email)
                        if cal.is_connected():
                            events = await cal.get_events(
                                days_ahead=1,
                                max_results=10,
                                timezone="America/Chicago",
                            )
                            if events:
                                event_strs = []
                                for ev in events[:5]:
                                    start = ev.get('start', {})
                                    time_str = start.get('dateTime', start.get('date', ''))
                                    event_strs.append(f"- {ev.get('summary', 'Untitled')} ({time_str})")
                                calendar_summary = "\n".join(event_strs)
                            else:
                                calendar_summary = "nothing scheduled today"
            except Exception as e:
                logger.warning(f"Screen nudge context: calendar fetch failed: {repr(e)}")
                calendar_summary = "calendar unavailable"

            # Priorities
            priority_items = get_priority_items(user_id, status="active")
            if priority_items:
                priority_strs = [f"- {p['title']} (priority {p['priority_level']})" for p in priority_items[:5]]
                priorities_summary = "\n".join(priority_strs)
            else:
                priorities_summary = "none"

            # Overdue tasks
            overdue_summary = "none"
            try:
                now = datetime.now()
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT title, due_date FROM tasks
                        WHERE user_id = %s
                          AND due_date < %s
                          AND status NOT IN ('completed', 'cancelled')
                        ORDER BY due_date ASC
                        LIMIT 5
                    """, (user_id, now.isoformat()))
                    overdue_rows = [dict(r) for r in cursor.fetchall()]
                    if overdue_rows:
                        overdue_strs = [f"- {t['title']} (due {t['due_date']})" for t in overdue_rows]
                        overdue_summary = "\n".join(overdue_strs)
            except Exception as e:
                logger.warning(f"Screen nudge context: overdue tasks fetch failed: {repr(e)}")

            # Dismissal patterns (last 30 days)
            dismissals = get_screen_dismissal_patterns(user_id, days=30)
            today_dismissals = [d for d in dismissals if _is_today(d.get('dismissed_at'))]
            if dismissals:
                recent_reasons = [d['user_reason'] for d in dismissals[:5] if d.get('user_reason')]
                if recent_reasons:
                    history_summary = f"{len(today_dismissals)} today, {len(dismissals)} in last 30 days. Recent reasons: {', '.join(recent_reasons)}"
                else:
                    history_summary = f"{len(today_dismissals)} today, {len(dismissals)} in last 30 days"
            else:
                history_summary = "first time"

            # ------------------------------------------------------------------
            # 2. One-shot Claude call
            # ------------------------------------------------------------------
            lcd_line = f"- What {user_name} is currently focused on: {lcd_layer2}" if lcd_layer2 else ""

            prompt = f"""The user was flagged as drifting by the screen agent and received a nudge.
They replied: "{user_message}"

Their current context (use ONLY to judge whether their dismissal is reasonable):
- Calendar today: {calendar_summary}
- Active priorities: {priorities_summary}
- Overdue tasks: {overdue_summary}
- Recent dismissal history: {history_summary}
{lcd_line}

Evaluate their reply:
1. If they have a reasonable explanation (watching a tutorial, taking a planned break, done for the day), accept it. Reply warmly and back off. If the LCD context shows {user_name} is in a focused work sprint relevant to their explanation, weight that in their favor.
2. If their calendar is empty, they have overdue tasks, and they're clearly just browsing — gently push back. Don't be harsh, but don't just accept it either.
3. If they've dismissed 3+ times today, note the pattern without being preachy.

CRITICAL RULES:
- Your ONLY job is to evaluate the dismissal. Accept it or push back.
- Do NOT mention specific tasks, priorities, or calendar events by name.
- Do NOT suggest actions, remind them about people, or surface to-dos. That's handled by the nudge system, not you.
- Keep it to ONE sentence. Two max if pushing back.

You MUST include exactly one of these tags at the end of your response (after your message):
[ACCEPT] — if you're accepting the dismissal
[PUSHBACK] — if you're pushing back

Be direct, warm, not robotic. You're a cool uncle, not a productivity cop."""

            client = AsyncAnthropic(api_key=Config.ANTHROPIC_API_KEY)
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )

            claude_reply = response.content[0].text.strip()

            # ------------------------------------------------------------------
            # 3. Parse accept/pushback decision
            # ------------------------------------------------------------------
            accepted = "[ACCEPT]" in claude_reply
            # Remove the tag before sending to user
            clean_reply = claude_reply.replace("[ACCEPT]", "").replace("[PUSHBACK]", "").strip()

            # ------------------------------------------------------------------
            # 4. Send response and set appropriate cooldown
            # ------------------------------------------------------------------
            await self.bot_service.send_message(chat_id, clean_reply, parse_mode=None)

            if accepted:
                dismiss_screen_nudge(str(user_id))
                logger.info(f"Screen nudge accepted by Claude for user {user_id}, cooldown 2h")
            else:
                set_short_cooldown(str(user_id))
                logger.info(f"Screen nudge pushback by Claude for user {user_id}, cooldown 10m")

            # ------------------------------------------------------------------
            # 5. Record dismissal for pattern learning
            # ------------------------------------------------------------------
            record_screen_dismissal(
                user_id=user_id,
                vision_status="drifting",
                user_reason=user_message,
                calendar_context=calendar_summary[:500],
                accepted=accepted,
            )

        except Exception as e:
            logger.error(f"Screen nudge reply handling failed for user {user_id}: {repr(e)}")
            # Fail gracefully — fall back to dumb dismiss
            from web.api.screen import dismiss_screen_nudge
            dismiss_screen_nudge(str(user_id))
            await self.bot_service.send_message(
                chat_id,
                "Got it — backing off for 2 hours.",
                parse_mode=None,
            )

    async def handle_webhook_update(self, update: dict) -> None:
        """
        Handle a Telegram update received via webhook.

        Called by the webhook endpoint for real-time message processing.

        Args:
            update: Telegram update object from webhook POST
        """
        # Extract message from update
        message = update.get("message")
        if not message:
            logger.debug(f"Webhook update {update.get('update_id')} has no message, skipping")
            return

        try:
            await self._handle_message(message)
            logger.debug(f"Processed webhook update {update.get('update_id')}")
        except Exception as e:
            logger.error(f"Error handling webhook update {update.get('update_id')}: {repr(e)}")

    async def _send_link_instructions(
        self,
        chat_id: int,
        telegram_username: Optional[str],
        telegram_first_name: Optional[str]
    ) -> None:
        """
        Send account linking instructions to an unlinked user.

        Args:
            chat_id: Telegram chat ID
            telegram_username: Telegram username (optional)
            telegram_first_name: Telegram first name (optional)
        """
        name = telegram_first_name or "there"

        # Check if this is the /start command (new chat)
        # We'll still send instructions but note it differently
        message = (
            f"Hi {name}! I'm Seny, your personal AI assistant.\n\n"
            f"To chat with me here, please link your Telegram account:\n\n"
            f"1. Go to {self.app_url}\n"
            f"2. Log in to your Seny account\n"
            f"3. Go to Settings > Integrations > Telegram Bot\n"
            f"4. Click 'Link this chat' and enter code: `{chat_id}`\n\n"
            f"Once linked, you can chat with me just like on the web!"
        )

        await self.bot_service.send_message(chat_id, message)

        logger.info(
            f"Sent link instructions to unlinked Telegram chat {chat_id} "
            f"(@{telegram_username or 'no_username'})"
        )


def get_telegram_bot_worker() -> TelegramBotWorker:
    """Get a TelegramBotWorker instance."""
    return TelegramBotWorker()


# Scheduler-compatible async function
async def process_telegram_bot_messages():
    """
    Process Telegram bot messages.

    Called by scheduler every 5 seconds.
    """
    worker = TelegramBotWorker()
    result = await worker.poll_and_respond()

    if result.get("messages_processed", 0) > 0:
        logger.info(
            f"Telegram bot: processed {result['messages_processed']} messages, "
            f"errors={result.get('errors', 0)}"
        )
