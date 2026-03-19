"""
Slack Bot Service for Seny (Phase 20-02).

Handles bot-based Slack DM operations using the bot token.
Used for two-way chat with users who DM the Seny bot.

This is separate from SlackService (user token) which handles:
- Scanning workspace messages
- Search functionality
- Sending to channels on behalf of the user

SlackBotService uses the bot token for:
- Receiving DMs to the bot
- Sending responses from the bot

Usage:
    service = SlackBotService(user_id)
    if service.is_configured():
        channel = await service.get_bot_dm_channel()
        messages = await service.get_new_messages(channel, since_ts="1234567890.123456")
        await service.send_message(channel, "Hello from Seny!")
"""

import asyncio
import logging
import time
from typing import Optional

import httpx

from web.core.database import get_slack_bot_token

logger = logging.getLogger(__name__)

# Slack API base URL
SLACK_API_BASE = "https://slack.com/api"

# Circuit breaker state for bot tokens (separate from user token circuit)
# Each entry: {"state": "closed"|"open"|"half_open", "failure_count": 0, "opened_at": None}
_bot_circuit_state: dict[str, dict] = {}
_BOT_CIRCUIT_FAILURE_THRESHOLD = 3
_BOT_CIRCUIT_RECOVERY_SECONDS = 900  # 15 minutes


class SlackBotCircuitOpenError(Exception):
    """Raised when the circuit breaker is open for a Slack bot."""
    pass


class SlackBotService:
    """
    Slack Bot API wrapper for DM chat operations.

    Uses bot token (xoxb-...) for receiving and sending DMs to users.
    One instance per user.

    Attributes:
        user_id: The Seny user's database ID
        team_id: Slack workspace ID (optional - uses first configured if not provided)
    """

    def __init__(self, user_id: int, team_id: str = None):
        """
        Initialize Slack bot service for a specific user.

        Args:
            user_id: User's database ID
            team_id: Slack workspace ID (optional)
        """
        self.user_id = user_id
        self.team_id = team_id
        self._token_data: Optional[dict] = None

    def _load_token(self) -> Optional[dict]:
        """Load bot token from database, caching the result."""
        if self._token_data is not None:
            return self._token_data

        self._token_data = get_slack_bot_token(self.user_id, self.team_id)
        if self._token_data:
            self.team_id = self._token_data["team_id"]

        return self._token_data

    def is_configured(self) -> bool:
        """
        Check if this user has a Slack bot token configured.

        Returns:
            True if bot token exists
        """
        token_data = self._load_token()
        return token_data is not None and token_data.get("bot_token") is not None

    def get_bot_user_id(self) -> Optional[str]:
        """
        Get the bot's Slack user ID.

        Returns:
            Bot user ID or None if not configured
        """
        token_data = self._load_token()
        return token_data.get("bot_user_id") if token_data else None

    def _check_circuit(self) -> None:
        """Check circuit breaker state; raise if open."""
        team_id = self.team_id or "unknown"
        state = _bot_circuit_state.get(team_id)
        if not state or state["state"] == "closed":
            return
        if state["state"] == "open":
            elapsed = time.time() - (state["opened_at"] or 0)
            if elapsed < _BOT_CIRCUIT_RECOVERY_SECONDS:
                raise SlackBotCircuitOpenError(
                    f"Bot circuit open for team {team_id}, "
                    f"{int(_BOT_CIRCUIT_RECOVERY_SECONDS - elapsed)}s until retry"
                )
            # Enough time passed - try half-open
            state["state"] = "half_open"
            logger.info(f"Slack bot circuit half-open for team {team_id}, allowing probe request")

    def _circuit_success(self) -> None:
        """Record a successful API call - reset circuit to closed."""
        team_id = self.team_id or "unknown"
        _bot_circuit_state[team_id] = {"state": "closed", "failure_count": 0, "opened_at": None}

    def _circuit_failure(self) -> None:
        """Record a failed API call - maybe open circuit."""
        team_id = self.team_id or "unknown"
        state = _bot_circuit_state.setdefault(
            team_id, {"state": "closed", "failure_count": 0, "opened_at": None}
        )
        state["failure_count"] += 1
        if state["failure_count"] >= _BOT_CIRCUIT_FAILURE_THRESHOLD:
            state["state"] = "open"
            state["opened_at"] = time.time()
            logger.warning(
                f"Slack bot circuit OPEN for team {team_id} after "
                f"{state['failure_count']} consecutive failures"
            )

    async def _api_call(
        self,
        method: str,
        params: dict = None,
        json_body: dict = None,
        max_retries: int = 3
    ) -> dict:
        """
        Make a Slack Web API call using bot token with exponential backoff.

        Args:
            method: Slack API method (e.g., "conversations.list")
            params: Query parameters
            json_body: JSON body for POST requests
            max_retries: Maximum retry attempts for rate limits

        Returns:
            API response dict, or {"ok": False, "error": "..."} on failure
        """
        token_data = self._load_token()
        bot_token = token_data.get("bot_token") if token_data else None

        # Validate bot token - must exist and start with xoxb- (not xoxp- user token)
        if not bot_token or not bot_token.startswith("xoxb-"):
            if bot_token and bot_token.startswith("xoxp-"):
                logger.warning(f"Slack bot: user token stored as bot_token for team {self.team_id}, skipping")
            return {"ok": False, "error": "bot_not_configured"}

        # Circuit breaker check
        try:
            self._check_circuit()
        except SlackBotCircuitOpenError as e:
            logger.warning(f"Slack bot API call blocked by circuit breaker: {repr(e)}")
            return {"ok": False, "error": f"circuit_open: {e}"}

        headers = {
            "Authorization": f"Bearer {token_data['bot_token']}",
            "Content-Type": "application/json; charset=utf-8"
        }

        url = f"{SLACK_API_BASE}/{method}"

        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient() as client:
                    if json_body:
                        response = await client.post(url, headers=headers, json=json_body, params=params)
                    else:
                        response = await client.get(url, headers=headers, params=params)

                    data = response.json()

                    # Log non-ok responses for debugging
                    if not data.get("ok"):
                        logger.warning(f"Slack bot API {method} failed: {data.get('error')} (status={response.status_code})")

                    # Check for rate limiting - don't block, just fail fast
                    if response.status_code == 429 or data.get("error") == "ratelimited":
                        retry_after = int(response.headers.get("Retry-After", 60))
                        logger.warning(f"Slack bot rate limit hit (retry after {retry_after}s)")
                        self._circuit_failure()  # Trigger circuit breaker
                        return {"ok": False, "error": "ratelimited", "retry_after": retry_after}

                    self._circuit_success()
                    return data

            except Exception as e:
                logger.error(f"Slack bot API error ({method}): {repr(e)}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                self._circuit_failure()
                return {"ok": False, "error": repr(e)}

        self._circuit_failure()
        return {"ok": False, "error": "max_retries_exceeded"}

    async def get_bot_dm_channel(self, user_slack_id: str) -> Optional[str]:
        """
        Get or create DM channel between bot and a Slack user.

        Args:
            user_slack_id: The Slack user ID to DM with

        Returns:
            DM channel ID or None on failure
        """
        result = await self._api_call("conversations.open", json_body={
            "users": user_slack_id
        })

        if not result.get("ok"):
            logger.error(f"Failed to open DM channel: {result.get('error')}")
            return None

        return result.get("channel", {}).get("id")

    async def list_bot_dm_channels(self, limit: int = 100) -> list[dict]:
        """
        List all DM channels with the bot.

        Returns:
            List of DM channel dicts with: id, user_id
        """
        result = await self._api_call("conversations.list", params={
            "types": "im",
            "limit": min(limit, 200)
        })

        if not result.get("ok"):
            logger.error(f"Failed to list bot DMs: {result.get('error')}")
            return []

        dms = []
        for dm in result.get("channels", []):
            dms.append({
                "id": dm.get("id"),
                "user_id": dm.get("user")
            })

        return dms

    async def get_new_messages(
        self,
        channel_id: str,
        since_ts: str = None,
        limit: int = 20
    ) -> list[dict]:
        """
        Get messages newer than since_ts from a DM channel.

        Filters out bot's own messages.

        Args:
            channel_id: Slack DM channel ID
            since_ts: Unix timestamp string to get messages after
            limit: Maximum messages to return

        Returns:
            List of message dicts with: ts, user, text
        """
        params = {
            "channel": channel_id,
            "limit": min(limit, 100)
        }
        if since_ts:
            params["oldest"] = since_ts

        result = await self._api_call("conversations.history", params=params)

        if not result.get("ok"):
            logger.error(f"Failed to get bot DM messages: {result.get('error')}")
            return []

        bot_user_id = self.get_bot_user_id()
        messages = []

        for msg in result.get("messages", []):
            # Filter out bot's own messages
            if msg.get("user") == bot_user_id:
                continue
            # Filter out bot messages (subtype)
            if msg.get("subtype") == "bot_message":
                continue

            messages.append({
                "ts": msg.get("ts"),
                "user": msg.get("user"),
                "text": msg.get("text", "")
            })

        # Return in chronological order (API returns reverse chronological)
        return list(reversed(messages))

    async def send_message(self, channel_id: str, text: str) -> Optional[dict]:
        """
        Send a message to a DM channel using bot token.

        Args:
            channel_id: Slack DM channel ID
            text: Message text

        Returns:
            Dict with message info (ts, channel) or None on failure
        """
        result = await self._api_call("chat.postMessage", json_body={
            "channel": channel_id,
            "text": text
        })

        if not result.get("ok"):
            logger.error(f"Failed to send bot message: {result.get('error')}")
            return None

        return {
            "ts": result.get("ts"),
            "channel": result.get("channel")
        }

    async def send_typing(self, channel_id: str) -> bool:
        """
        Indicate typing in channel.

        Note: Slack API doesn't support typing indicators for bots.
        This method exists for API consistency but always returns True.

        Args:
            channel_id: Slack DM channel ID

        Returns:
            True (always, as typing indicators are not supported)
        """
        # Slack doesn't have typing indicators for bots
        # Just return True for API consistency
        return True

    async def get_user_info(self, user_id: str) -> Optional[dict]:
        """
        Get info about a Slack user.

        Args:
            user_id: Slack user ID

        Returns:
            Dict with user info or None on failure
        """
        result = await self._api_call("users.info", params={
            "user": user_id
        })

        if not result.get("ok"):
            logger.error(f"Failed to get user info: {result.get('error')}")
            return None

        user = result.get("user", {})
        profile = user.get("profile", {})

        return {
            "id": user.get("id"),
            "name": user.get("name"),
            "real_name": user.get("real_name"),
            "display_name": profile.get("display_name"),
            "first_name": profile.get("first_name"),
            "email": profile.get("email")
        }
