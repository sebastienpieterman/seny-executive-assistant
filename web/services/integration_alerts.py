"""
Proactive alerts for expired/revoked integration tokens.

Called by each service's circuit breaker when it opens (3 consecutive
refresh failures). Sends a Telegram bot message (falling back to Slack)
so the user knows to reconnect before the outage causes missed nudges.
"""
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_SERVICE_DISPLAY = {
    "gmail": "Gmail",
    "calendar": "Google Calendar",
    "drive": "Google Drive",
    "contacts": "Google Contacts",
    "outlook": "Outlook",
}


async def _send_token_alert(user_id: int, service_key: str, email: str) -> None:
    """Send reconnect alert via Telegram bot, falling back to Slack."""
    service_name = _SERVICE_DISPLAY.get(service_key, service_key)
    message = (
        f"⚠️ Your {service_name} connection for {email} has stopped working "
        f"and Seny can't access it.\n\n"
        f"Go to Settings → Integrations to reconnect."
    )

    try:
        from web.core.database import get_telegram_bot_user_links_for_user
        from web.services.telegram_bot_service import TelegramBotService

        links = get_telegram_bot_user_links_for_user(user_id)
        if links:
            chat_id = links[0]["telegram_chat_id"]
            bot = TelegramBotService()
            if bot.is_configured():
                result = await bot.send_message(chat_id, message)
                if result and not result.get("error"):
                    logger.info(
                        "Sent token expiry alert via Telegram for user %d (%s/%s)",
                        user_id, service_key, email,
                    )
                    return
    except Exception as e:
        logger.warning("Telegram token alert failed for user %d: %s", user_id, repr(e))

    # Fallback: Slack DM
    try:
        from web.core.database import get_first_slack_token
        from web.services.slack_service import SlackService, SlackCircuitOpenError

        token_data = get_first_slack_token(user_id)
        if token_data:
            slack = SlackService(user_id, token_data["workspace_id"])
            authed_user_id = token_data.get("authed_user_id")
            if authed_user_id:
                channel = await slack.open_dm(authed_user_id)
                if channel:
                    await slack.send_message(channel, message)
                    logger.info(
                        "Sent token expiry alert via Slack for user %d (%s/%s)",
                        user_id, service_key, email,
                    )
    except Exception as e:
        logger.warning("Slack token alert failed for user %d: %s", user_id, repr(e))


def schedule_token_alert(user_id: int, service_key: str, email: str) -> None:
    """
    Schedule a token expiry alert to fire asynchronously.
    Safe to call from synchronous code running inside an async context.
    Only fires once when the circuit first opens (caller's responsibility).
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(_send_token_alert(user_id, service_key, email))
    except Exception as e:
        logger.error("Failed to schedule token alert for user %d: %s", user_id, repr(e))
