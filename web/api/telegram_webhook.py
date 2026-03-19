"""
Telegram Webhook API - Phase 21-01

Receives webhook callbacks from Telegram Bot API for real-time message processing.
Replaces polling when configured, providing instant message detection.

Security:
- Verifies X-Telegram-Bot-Api-Secret-Token header matches TELEGRAM_WEBHOOK_SECRET
- Returns 401 for unauthorized requests
- Returns 200 quickly, processes messages in background
"""

import logging
import os
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_webhook_secret() -> Optional[str]:
    """Get the webhook secret from environment."""
    return os.getenv("TELEGRAM_WEBHOOK_SECRET")


def is_webhook_mode() -> bool:
    """Check if webhook mode is configured (env vars present)."""
    return bool(_get_webhook_secret()) and bool(os.getenv("APP_URL"))


async def _process_telegram_update(update: dict) -> None:
    """
    Process a Telegram update in the background.

    Routes message updates to TelegramBotWorker for handling.

    Args:
        update: Telegram update object from webhook
    """
    from web.services.telegram_bot_worker import TelegramBotWorker

    try:
        worker = TelegramBotWorker()
        await worker.handle_webhook_update(update)
    except Exception as e:
        logger.error(f"Error processing Telegram webhook update: {repr(e)}")


@router.post("/telegram")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: Optional[str] = Header(
        default=None,
        alias="X-Telegram-Bot-Api-Secret-Token"
    )
):
    """
    Telegram Bot webhook endpoint.

    Receives updates from Telegram when a message is sent to the bot.
    Verifies the secret token and processes messages in the background.

    Security: Rejects requests without valid secret token.
    Performance: Returns 200 immediately, processes in background.
    """
    # Get expected secret
    expected_secret = _get_webhook_secret()

    # Verify secret token
    if not expected_secret:
        logger.warning("Telegram webhook called but TELEGRAM_WEBHOOK_SECRET not configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook not configured"
        )

    if not x_telegram_bot_api_secret_token:
        logger.warning("Telegram webhook called without secret token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing secret token"
        )

    if x_telegram_bot_api_secret_token != expected_secret:
        logger.warning("Telegram webhook called with invalid secret token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid secret token"
        )

    # Parse update JSON
    try:
        update = await request.json()
    except Exception as e:
        logger.warning(f"Telegram webhook received malformed JSON: {repr(e)}")
        # Return 200 to acknowledge receipt (Telegram will retry on non-200)
        return {"ok": True, "error": "malformed_json"}

    # Log update receipt (debug level to avoid noise)
    update_id = update.get("update_id")
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id") if message else None

    logger.debug(f"Telegram webhook received update {update_id} from chat {chat_id}")

    # Process in background (return 200 quickly)
    background_tasks.add_task(_process_telegram_update, update)

    # Return success immediately (Telegram expects response within seconds)
    return {"ok": True}


@router.get("/telegram/health")
async def telegram_webhook_health():
    """
    Health check for Telegram webhook.

    Returns webhook configuration status.
    """
    from web.services.telegram_bot_service import TelegramBotService

    bot = TelegramBotService()

    # Check if webhook is configured
    webhook_secret_set = bool(_get_webhook_secret())
    app_url_set = bool(os.getenv("APP_URL"))
    bot_configured = bot.is_configured()

    result = {
        "webhook_mode": is_webhook_mode(),
        "webhook_secret_configured": webhook_secret_set,
        "app_url_configured": app_url_set,
        "bot_token_configured": bot_configured
    }

    # Get webhook info from Telegram if fully configured
    if is_webhook_mode() and bot_configured:
        try:
            webhook_info = await bot.get_webhook_info()
            result["telegram_webhook"] = {
                "url": webhook_info.get("url", ""),
                "has_custom_certificate": webhook_info.get("has_custom_certificate", False),
                "pending_update_count": webhook_info.get("pending_update_count", 0),
                "last_error_date": webhook_info.get("last_error_date"),
                "last_error_message": webhook_info.get("last_error_message"),
            }
            result["healthy"] = await bot.is_webhook_healthy()
        except Exception as e:
            result["error"] = repr(e)
            result["healthy"] = False
    else:
        result["healthy"] = False
        result["reason"] = "Webhook not fully configured"

    return result
