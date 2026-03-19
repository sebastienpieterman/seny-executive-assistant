"""
Webhook Health Service - Phase 21-03

Monitors health status of webhook integrations (Telegram and Slack).
Used by the health endpoint to report webhook configuration and status.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# In-memory tracking of last successful Slack event (module-level)
_last_slack_event_at: Optional[datetime] = None


def record_slack_event_received() -> None:
    """Record that a Slack event was successfully received. Call this from slack_events.py."""
    global _last_slack_event_at
    _last_slack_event_at = datetime.now(timezone.utc)


async def check_telegram_webhook() -> dict:
    """
    Check Telegram webhook health.

    Returns:
        dict with healthy, mode, url, pending, last_error
    """
    from web.api.telegram_webhook import is_webhook_mode
    from web.services.telegram_bot_service import TelegramBotService

    mode = "webhook" if is_webhook_mode() else "polling"

    if not is_webhook_mode():
        return {"healthy": True, "mode": mode}

    bot = TelegramBotService()
    if not bot.is_configured():
        return {"healthy": False, "mode": mode, "reason": "Bot token not configured"}

    try:
        webhook_info = await bot.get_webhook_info()
        last_error = webhook_info.get("last_error_message")
        pending = webhook_info.get("pending_update_count", 0)
        url = webhook_info.get("url", "")

        healthy = bool(url) and last_error is None

        result = {"healthy": healthy, "mode": mode, "url": url, "pending": pending}
        if last_error:
            result["last_error"] = last_error
        return result
    except Exception as e:
        logger.error(f"Failed to check Telegram webhook health: {repr(e)}")
        return {"healthy": False, "mode": mode, "error": repr(e)}


def check_slack_events() -> dict:
    """
    Check Slack Events API health.

    Returns:
        dict with configured, mode, last_event_at
    """
    from web.api.slack_events import is_events_mode

    configured = is_events_mode()
    mode = "events" if configured else "polling"

    result = {"configured": configured, "mode": mode}
    if _last_slack_event_at is not None:
        result["last_event_at"] = _last_slack_event_at.isoformat()
    else:
        result["last_event_at"] = None

    return result
