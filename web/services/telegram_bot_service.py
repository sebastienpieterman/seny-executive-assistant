"""
Telegram Bot API service for Seny.

Uses Telegram Bot HTTP API to receive and respond to DMs.
Unlike TelegramService (MTProto), this uses a simple bot token for two-way chat.

This is for multi-channel chat - users message Seny's bot directly to interact.
Separate from TelegramService which scans user's personal chats.

Usage:
    bot = TelegramBotService()
    if bot.is_configured():
        updates = await bot.get_updates()
        await bot.send_message(chat_id, "Hello!")
"""

import os
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Telegram Bot API base URL
TELEGRAM_BOT_API = "https://api.telegram.org"


class TelegramBotService:
    """
    Telegram Bot API wrapper for two-way chat with Seny.

    Uses TELEGRAM_BOT_TOKEN environment variable (from @BotFather).
    Provides polling-based message retrieval and response sending.
    """

    def __init__(self):
        """Initialize Telegram Bot service."""
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._bot_info: Optional[dict] = None

    def is_configured(self) -> bool:
        """Check if Telegram Bot token is configured."""
        return bool(self.token)

    def _api_url(self, method: str) -> str:
        """Build API URL for a given method."""
        return f"{TELEGRAM_BOT_API}/bot{self.token}/{method}"

    async def _api_call(
        self,
        method: str,
        params: dict = None,
        json_body: dict = None,
        timeout: float = 35.0
    ) -> dict:
        """
        Make a Telegram Bot API call.

        Args:
            method: API method name (e.g., "getUpdates", "sendMessage")
            params: Query parameters for GET requests
            json_body: JSON body for POST requests
            timeout: Request timeout in seconds

        Returns:
            API response dict, or {"ok": False, "error": "..."} on failure
        """
        if not self.is_configured():
            return {"ok": False, "error": "bot_not_configured"}

        url = self._api_url(method)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if json_body:
                    response = await client.post(url, json=json_body)
                else:
                    response = await client.get(url, params=params)

                data = response.json()

                if not data.get("ok"):
                    error_desc = data.get("description", "Unknown error")
                    logger.error(f"Telegram Bot API error ({method}): {error_desc}")
                    return {"ok": False, "error": error_desc}

                return data

        except httpx.TimeoutException:
            logger.warning(f"Telegram Bot API timeout ({method})")
            return {"ok": False, "error": "timeout"}
        except Exception as e:
            logger.error(f"Telegram Bot API error ({method}): {repr(e)}")
            return {"ok": False, "error": repr(e)}

    async def get_bot_info(self) -> dict:
        """
        Get information about the bot.

        Returns:
            Dict with bot info (id, username, first_name) or empty dict on error
        """
        if self._bot_info:
            return self._bot_info

        result = await self._api_call("getMe")
        if result.get("ok"):
            self._bot_info = result.get("result", {})
            return self._bot_info

        return {}

    async def get_updates(
        self,
        offset: int = 0,
        timeout: int = 30,
        allowed_updates: list = None
    ) -> list[dict]:
        """
        Long-poll for new messages/updates.

        Uses Telegram's long-polling - blocks up to `timeout` seconds
        waiting for new updates.

        Args:
            offset: Update ID offset (use last_update_id + 1 to avoid reprocessing)
            timeout: Long-poll timeout in seconds (max 50)
            allowed_updates: List of update types to receive (default: messages only)

        Returns:
            List of update objects, empty list on error
        """
        params = {
            "offset": offset,
            "timeout": min(timeout, 50),  # Telegram max is 50
            "allowed_updates": allowed_updates or ["message"]
        }

        # Use longer HTTP timeout than Telegram timeout
        result = await self._api_call(
            "getUpdates",
            params=params,
            timeout=timeout + 5
        )

        if result.get("ok"):
            return result.get("result", [])

        return []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "Markdown",
        disable_notification: bool = False,
        reply_to_message_id: int = None,
        disable_web_page_preview: bool = True
    ) -> dict:
        """
        Send a text message to a chat.

        Args:
            chat_id: Target chat ID
            text: Message text (supports Markdown or HTML based on parse_mode)
            parse_mode: "Markdown", "MarkdownV2", "HTML", or None for plain text
            disable_notification: If True, sends silently
            reply_to_message_id: Message ID to reply to (optional)
            disable_web_page_preview: If True (default), prevents Telegram from
                pre-fetching URLs in the message

        Returns:
            Sent message object on success, error dict on failure
        """
        body = {
            "chat_id": chat_id,
            "text": text
        }

        if parse_mode:
            body["parse_mode"] = parse_mode

        if disable_web_page_preview:
            body["disable_web_page_preview"] = True

        if disable_notification:
            body["disable_notification"] = True

        if reply_to_message_id:
            body["reply_to_message_id"] = reply_to_message_id

        result = await self._api_call("sendMessage", json_body=body)

        if result.get("ok"):
            return result.get("result", {})

        # If Markdown fails, retry without parse_mode
        if "parse_mode" in body and "can't parse" in result.get("error", "").lower():
            logger.warning(f"Markdown parse failed, retrying as plain text")
            del body["parse_mode"]
            result = await self._api_call("sendMessage", json_body=body)
            if result.get("ok"):
                return result.get("result", {})

        return {"error": result.get("error", "send_failed")}

    async def send_typing(self, chat_id: int) -> bool:
        """
        Send "typing" indicator to show bot is processing.

        Telegram shows "typing..." for up to 5 seconds.
        Call repeatedly for longer processing.

        Args:
            chat_id: Target chat ID

        Returns:
            True if successful, False otherwise
        """
        result = await self._api_call(
            "sendChatAction",
            json_body={
                "chat_id": chat_id,
                "action": "typing"
            }
        )

        return result.get("ok", False)

    async def send_photo(
        self,
        chat_id: int,
        photo_url: str,
        caption: str = None
    ) -> dict:
        """
        Send a photo to a chat.

        Args:
            chat_id: Target chat ID
            photo_url: URL of the photo
            caption: Optional caption

        Returns:
            Sent message object on success, error dict on failure
        """
        body = {
            "chat_id": chat_id,
            "photo": photo_url
        }

        if caption:
            body["caption"] = caption

        result = await self._api_call("sendPhoto", json_body=body)

        if result.get("ok"):
            return result.get("result", {})

        return {"error": result.get("error", "send_failed")}

    async def get_chat(self, chat_id: int) -> Optional[dict]:
        """
        Get information about a chat.

        Args:
            chat_id: Chat ID to look up

        Returns:
            Chat info dict or None on error
        """
        result = await self._api_call("getChat", params={"chat_id": chat_id})

        if result.get("ok"):
            return result.get("result")

        return None

    async def delete_webhook(self, drop_pending_updates: bool = False) -> bool:
        """
        Delete any existing webhook to enable polling mode.

        Call this before using get_updates() if a webhook was previously set.

        Args:
            drop_pending_updates: If True, clear any pending updates when deleting webhook

        Returns:
            True if successful
        """
        result = await self._api_call(
            "deleteWebhook",
            json_body={"drop_pending_updates": drop_pending_updates}
        )

        if result.get("ok"):
            logger.info("Telegram webhook deleted")

        return result.get("ok", False)

    async def set_webhook(self, url: str, secret_token: str) -> bool:
        """
        Set webhook URL for receiving updates.

        When a webhook is set, Telegram will POST updates to this URL
        instead of requiring polling via getUpdates().

        Args:
            url: HTTPS URL for Telegram to POST updates to
            secret_token: Secret token for X-Telegram-Bot-Api-Secret-Token header

        Returns:
            True if webhook was set successfully
        """
        result = await self._api_call(
            "setWebhook",
            json_body={
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": ["message"],
                "drop_pending_updates": False
            }
        )

        if result.get("ok"):
            logger.info(f"Telegram webhook set to {url}")
            return True

        logger.error(f"Failed to set Telegram webhook: {result.get('error')}")
        return False

    async def get_webhook_info(self) -> dict:
        """
        Get current webhook configuration from Telegram.

        Returns:
            Dict with webhook info including:
            - url: Webhook URL (empty if not set)
            - has_custom_certificate: If a custom SSL certificate was provided
            - pending_update_count: Number of pending updates
            - last_error_date: Unix timestamp of last error (if any)
            - last_error_message: Error description (if any)
            - max_connections: Max concurrent connections
            - allowed_updates: List of update types being sent
        """
        result = await self._api_call("getWebhookInfo")

        if result.get("ok"):
            return result.get("result", {})

        return {}

    async def is_webhook_healthy(self) -> bool:
        """
        Check if webhook is healthy (configured and no recent errors).

        A webhook is considered healthy if:
        - URL is set
        - No last_error_message, or error is older than 1 hour

        Returns:
            True if webhook appears healthy
        """
        import time

        info = await self.get_webhook_info()

        # No URL means webhook isn't set
        if not info.get("url"):
            return False

        # Check for recent errors (within last hour)
        last_error_date = info.get("last_error_date")
        if last_error_date:
            # Error within last hour is considered unhealthy
            one_hour_ago = time.time() - 3600
            if last_error_date > one_hour_ago:
                logger.warning(
                    f"Telegram webhook has recent error: {info.get('last_error_message')}"
                )
                return False

        return True
