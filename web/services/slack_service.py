"""
Slack API service wrapper for Seny.

Provides Slack workspace access:
- OAuth 2.0 flow for user token authorization
- Channel and DM listing
- Message retrieval and sending
- Message search

Usage:
    slack = SlackService(user_id, team_id)
    if slack.is_connected():
        channels = await slack.list_channels()
        messages = await slack.get_messages(channel_id)
"""

import asyncio
import os
import logging
import time
import httpx
from typing import Optional
from urllib.parse import urlencode

from web.core.database import (
    get_slack_token, save_slack_token, list_slack_tokens,
    delete_slack_token, get_first_slack_token
)

logger = logging.getLogger(__name__)


# Workspace-level circuit breaker constants
# State is persisted to slack_channel_cursors with channel_id="__workspace__"
# (replaced in-memory _circuit_state dict — was lost on every Railway deploy)
_CIRCUIT_FAILURE_THRESHOLD = 3
_CIRCUIT_RECOVERY_SECONDS = 900  # 15 minutes


class SlackCircuitOpenError(Exception):
    """Raised when the circuit breaker is open for a Slack workspace."""
    pass


class SlackScanAbortError(Exception):
    """Raised when a scan has been rate-limited for too long and should abort cleanly."""
    pass


# Maximum cumulative sleep time (seconds) before aborting a scan.
_SLACK_SCAN_MAX_SLEEP_SECONDS = 180  # 3 minutes

# Delay between successful API calls to stay under Slack Tier 2 rate limit (20 req/min).
# At 1.0s per call the effective rate is ~10 req/min — well under the 20 req/min cap.
_SLACK_CALL_DELAY_SECONDS = 1.0


# OAuth Configuration
SLACK_CLIENT_ID = os.getenv("SLACK_CLIENT_ID")
SLACK_CLIENT_SECRET = os.getenv("SLACK_CLIENT_SECRET")

# Slack API base URL
SLACK_API_BASE = "https://slack.com/api"

# Required scopes for user token
# User tokens required for search:read (bots cannot search)
SLACK_USER_SCOPES = [
    "channels:history",   # Read messages in public channels
    "channels:read",      # List public channels
    "groups:history",     # Read messages in private channels
    "groups:read",        # List private channels
    "im:history",         # Read direct messages
    "im:read",            # List DMs
    "mpim:history",       # Read group DMs
    "mpim:read",          # List group DMs
    "search:read",        # Search messages (user token only!)
    "chat:write",         # Send messages
    "users:read",         # Get user info (names, avatars)
]

# Bot scopes for DM chat functionality
# Bot token allows receiving DMs to the Seny bot
SLACK_BOT_SCOPES = [
    "im:history",   # Read DMs to bot
    "im:read",      # List DMs
    "im:write",     # Send DMs
    "chat:write",   # Send messages
    "users:read",   # Get user info for display names
]

# Module-level user cache: {team_id: {"users": {user_id: display_name}, "fetched_at": timestamp}}
# Cache persists across SlackService instances (which are created per-request)
_user_cache: dict[str, dict] = {}
_USER_CACHE_TTL = 3600  # 1 hour — display names change rarely; reduces users.list calls


class SlackService:
    """
    Slack API wrapper with OAuth flow support.

    Handles OAuth authorization and Slack Web API calls.
    One instance per user/team combination.

    Attributes:
        user_id: The user's database ID
        team_id: Slack workspace ID (optional - uses first connected if not provided)
    """

    def __init__(self, user_id: int, team_id: str = None):
        """
        Initialize Slack service for a specific user and optional workspace.

        Args:
            user_id: User's database ID
            team_id: Slack workspace ID (if None, uses first connected workspace)
        """
        self.user_id = user_id
        self.team_id = team_id
        self._token_data: Optional[dict] = None
        self._scan_start_time: Optional[float] = None  # Set by scanner before scan begins

    def _load_token(self) -> Optional[dict]:
        """Load token from database, caching the result."""
        if self._token_data is not None:
            return self._token_data

        if self.team_id:
            self._token_data = get_slack_token(self.user_id, self.team_id)
        else:
            self._token_data = get_first_slack_token(self.user_id)
            if self._token_data:
                self.team_id = self._token_data["team_id"]

        return self._token_data

    def is_connected(self) -> bool:
        """
        Check if this user has any Slack workspace connected.

        Returns:
            True if at least one workspace is connected
        """
        token_data = self._load_token()
        return token_data is not None

    @staticmethod
    def list_connected_workspaces(user_id: int) -> list[dict]:
        """
        List all Slack workspaces connected for a user.

        Args:
            user_id: User's database ID

        Returns:
            List of connected workspace info (team_id, team_name, authed_user_name)
        """
        return list_slack_tokens(user_id)

    @staticmethod
    def get_auth_url(redirect_uri: str, state: str) -> str:
        """
        Generate Slack OAuth authorization URL.

        Args:
            redirect_uri: OAuth callback URL
            state: Random state string for CSRF protection

        Returns:
            Full Slack authorization URL
        """
        params = {
            "client_id": SLACK_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "user_scope": ",".join(SLACK_USER_SCOPES),  # User token scopes
            "scope": ",".join(SLACK_BOT_SCOPES), # Bot token scopes
            "state": state,
        }
        return f"https://slack.com/oauth/v2/authorize?{urlencode(params)}"

    @staticmethod
    async def exchange_code(code: str, redirect_uri: str) -> dict:
        """
        Exchange OAuth authorization code for access token.

        Args:
            code: Authorization code from OAuth callback
            redirect_uri: Same redirect_uri used in authorization

        Returns:
            Dict with token info on success, or error dict on failure:
            - success: {team_id, team_name, access_token, scope, authed_user_id, authed_user_name,
                       bot_token, bot_user_id}
            - error: {error: str, error_description: str}
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://slack.com/api/oauth.v2.access",
                data={
                    "client_id": SLACK_CLIENT_ID,
                    "client_secret": SLACK_CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": redirect_uri,
                }
            )

            data = response.json()

            if not data.get("ok"):
                logger.error(f"Slack OAuth error: {data}")
                return {
                    "error": data.get("error", "unknown_error"),
                    "error_description": data.get("error", "OAuth exchange failed")
                }

            # Extract user token info
            # For user tokens, the token is in authed_user.access_token
            authed_user = data.get("authed_user", {})

            # Extract bot token info
            # Top-level access_token is the bot token when bot scopes are requested
            bot_token = data.get("access_token")  # Bot token (xoxb-...)
            bot_user_id = data.get("bot_user_id")  # Bot's Slack user ID

            return {
                "team_id": data.get("team", {}).get("id"),
                "team_name": data.get("team", {}).get("name"),
                "access_token": authed_user.get("access_token"),  # User token (xoxp-...)
                "scope": authed_user.get("scope", ""),
                "authed_user_id": authed_user.get("id"),
                "authed_user_name": None,  # Will fetch via users.info
                "token_type": "user",
                "bot_token": bot_token, # Bot token for DM chat
                "bot_user_id": bot_user_id # Bot's user ID
            }

    def _check_circuit(self) -> None:
        """Check workspace-level circuit breaker state; raise SlackCircuitOpenError if open.

        State is persisted to slack_channel_cursors with channel_id='__workspace__'
        so it survives Railway deploys. Falls back gracefully if DB is unavailable.
        """
        team_id = self.team_id or "unknown"
        try:
            from web.core.database import get_slack_channel_cursor, upsert_slack_channel_cursor
            from datetime import datetime, timezone as tz
            state = get_slack_channel_cursor(self.user_id, team_id, "__workspace__")
            if not state:
                return  # No record = closed circuit

            circuit_state = state.get('circuit_state', 'closed')
            if circuit_state == 'closed':
                return

            if circuit_state == 'open':
                opened_at_str = state.get('circuit_opened_at')
                if opened_at_str:
                    opened_at = datetime.fromisoformat(opened_at_str.replace('Z', '+00:00'))
                    elapsed = (datetime.now(tz.utc) - opened_at).total_seconds()
                    if elapsed < _CIRCUIT_RECOVERY_SECONDS:
                        raise SlackCircuitOpenError(
                            f"Slack circuit open for workspace {team_id} "
                            f"({elapsed:.0f}s / {_CIRCUIT_RECOVERY_SECONDS}s elapsed)"
                        )
                # Recovery time elapsed — transition to half_open
                upsert_slack_channel_cursor(
                    user_id=self.user_id,
                    team_id=team_id,
                    channel_id="__workspace__",
                    circuit_state='half_open',
                )
                logger.info(
                    "Slack circuit half-open for workspace %s, allowing probe request", team_id
                )
            # half_open: allow the request through
        except SlackCircuitOpenError:
            raise  # Re-raise — never swallow
        except Exception as e:
            logger.debug("Circuit check DB error (treating as closed): %r", e)

    def _circuit_success(self) -> None:
        """Record a successful API call — reset workspace circuit to closed."""
        team_id = self.team_id or "unknown"
        try:
            from web.core.database import upsert_slack_channel_cursor
            upsert_slack_channel_cursor(
                user_id=self.user_id,
                team_id=team_id,
                channel_id="__workspace__",
                consecutive_failures=0,
                circuit_state='closed',
                circuit_opened_at=None,
            )
        except Exception as e:
            logger.debug("Circuit success DB write failed: %r", e)

    def _circuit_failure(self) -> None:
        """Record a failed API call — increment failure count, maybe open workspace circuit."""
        team_id = self.team_id or "unknown"
        try:
            from web.core.database import get_slack_channel_cursor, upsert_slack_channel_cursor
            from datetime import datetime, timezone as tz
            current = get_slack_channel_cursor(self.user_id, team_id, "__workspace__")
            failures = (current.get('consecutive_failures', 0) if current else 0) + 1
            new_state = 'closed'
            opened_at = None
            if failures >= _CIRCUIT_FAILURE_THRESHOLD:
                new_state = 'open'
                opened_at = datetime.now(tz.utc).isoformat()
                logger.warning(
                    "Slack circuit OPEN for workspace %s after %d consecutive failures",
                    team_id, failures
                )
            upsert_slack_channel_cursor(
                user_id=self.user_id,
                team_id=team_id,
                channel_id="__workspace__",
                consecutive_failures=failures,
                circuit_state=new_state,
                circuit_opened_at=opened_at,
            )
        except Exception as e:
            logger.debug("Circuit failure DB write failed: %r", e)

    async def _api_call(
        self,
        method: str,
        params: dict = None,
        json_body: dict = None,
        max_retries: int = 3
    ) -> dict:
        """
        Make a Slack Web API call with exponential backoff for rate limits.

        Includes circuit breaker: after 3 consecutive failures the circuit opens
        for 15 minutes, preventing further calls (and the death-spiral they cause).

        Args:
            method: Slack API method (e.g., "conversations.list")
            params: Query parameters
            json_body: JSON body for POST requests
            max_retries: Maximum retry attempts for rate limits

        Returns:
            API response dict, or {"ok": False, "error": "..."} on failure
        """
        token_data = self._load_token()
        if not token_data:
            return {"ok": False, "error": "not_connected"}

        # Circuit breaker check
        try:
            self._check_circuit()
        except SlackCircuitOpenError as e:
            logger.warning(f"Slack API call blocked by circuit breaker: {repr(e)}")
            return {"ok": False, "error": f"circuit_open: {e}"}

        headers = {
            "Authorization": f"Bearer {token_data['access_token']}",
            "Content-Type": "application/json; charset=utf-8"
        }

        url = f"{SLACK_API_BASE}/{method}"

        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    if json_body:
                        response = await client.post(url, headers=headers, json=json_body, params=params)
                    else:
                        response = await client.get(url, headers=headers, params=params)

                    data = response.json()

                    # Check for rate limiting
                    if response.status_code == 429 or data.get("error") == "ratelimited":
                        retry_after = int(response.headers.get("Retry-After", 2 ** attempt))
                        self._circuit_failure()
                        # In drip context (_scan_start_time is None), fail fast — never block
                        # the drip loop with long sleeps. The caller skips this channel/call
                        # and tries again on the next tick.
                        if self._scan_start_time is None:
                            logger.warning(f"Slack rate limit hit (drip context) — skipping, retry next tick")
                            return {"ok": False, "error": "ratelimited"}
                        # Batch scanner context: sleep and retry, abort after 3 min total.
                        logger.warning(f"Slack rate limit hit, retrying in {retry_after}s (status={response.status_code})")
                        await asyncio.sleep(retry_after)
                        elapsed = time.time() - self._scan_start_time
                        if elapsed > _SLACK_SCAN_MAX_SLEEP_SECONDS:
                            raise SlackScanAbortError(
                                f"Slack scan aborted after {int(elapsed)}s of rate limiting "
                                f"(threshold: {_SLACK_SCAN_MAX_SLEEP_SECONDS}s)"
                            )
                        continue

                    self._circuit_success()
                    # Throttle to stay under Slack Tier 2 rate limit (20 req/min).
                    await asyncio.sleep(_SLACK_CALL_DELAY_SECONDS)
                    return data

            except SlackScanAbortError:
                raise  # propagate abort signal out of _api_call so scan exits cleanly
            except Exception as e:
                logger.error(f"Slack API error ({method}): {repr(e)}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                self._circuit_failure()
                return {"ok": False, "error": repr(e)}

        self._circuit_failure()
        return {"ok": False, "error": "max_retries_exceeded"}

    async def list_channels(
        self,
        types: str = "public_channel,private_channel",
        limit: int = 500
    ) -> list[dict]:
        """
        List channels the user is a member of (with pagination).

        Args:
            types: Comma-separated channel types
                   Options: public_channel, private_channel, mpim, im
            limit: Maximum channels to return (will paginate to get all up to this limit)

        Returns:
            List of channel dicts with: id, name, is_private, num_members
        """
        channels = []
        cursor = None

        while len(channels) < limit:
            params = {
                "types": types,
                "limit": min(200, limit - len(channels)),  # Fetch up to 200 per page
                "exclude_archived": True
            }
            if cursor:
                params["cursor"] = cursor

            result = await self._api_call("conversations.list", params=params)

            if not result.get("ok"):
                logger.error(f"Failed to list channels: {result.get('error')}")
                break

            for ch in result.get("channels", []):
                channels.append({
                    "id": ch.get("id"),
                    "name": ch.get("name", ch.get("id")),
                    "is_private": ch.get("is_private", False),
                    "is_im": ch.get("is_im", False),
                    "is_mpim": ch.get("is_mpim", False),
                    "num_members": ch.get("num_members", 0),
                    "topic": ch.get("topic", {}).get("value", ""),
                    "purpose": ch.get("purpose", {}).get("value", "")
                })

            # Check for more pages
            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break  # No more pages

        return channels

    async def list_channels_including_archived(
        self,
        types: str = "public_channel,private_channel",
        limit: int = 500
    ) -> list[dict]:
        """
        List channels including archived ones (for fallback lookup, with pagination).

        Args:
            types: Comma-separated channel types
            limit: Maximum channels to return

        Returns:
            List of channel dicts including archived channels
        """
        channels = []
        cursor = None

        while len(channels) < limit:
            params = {
                "types": types,
                "limit": min(200, limit - len(channels)),
                "exclude_archived": False  # Include archived channels
            }
            if cursor:
                params["cursor"] = cursor

            result = await self._api_call("conversations.list", params=params)

            if not result.get("ok"):
                logger.error(f"Failed to list channels (including archived): {result.get('error')}")
                break

            for ch in result.get("channels", []):
                channels.append({
                    "id": ch.get("id"),
                    "name": ch.get("name", ch.get("id")),
                    "is_private": ch.get("is_private", False),
                    "is_archived": ch.get("is_archived", False),
                    "is_im": ch.get("is_im", False),
                    "is_mpim": ch.get("is_mpim", False)
                })

            # Check for more pages
            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        return channels

    async def list_dms(self, limit: int = 100) -> list[dict]:
        """
        List direct message conversations.

        Args:
            limit: Maximum DMs to return

        Returns:
            List of DM dicts with: id, user_id, user_name
        """
        result = await self._api_call("conversations.list", params={
            "types": "im",
            "limit": min(limit, 1000)
        })

        if not result.get("ok"):
            logger.error(f"Failed to list DMs: {result.get('error')}")
            return []

        dms = []
        for dm in result.get("channels", []):
            user_id = dm.get("user")
            # Fetch display name (uses cache to avoid repeated API calls)
            user_name = await self.get_user_display_name(user_id) if user_id else None
            dms.append({
                "id": dm.get("id"),
                "user_id": user_id,
                "user_name": user_name
            })

        return dms

    async def list_group_dms(self, limit: int = 100) -> list[dict]:
        """
        List group direct message conversations.

        Args:
            limit: Maximum group DMs to return

        Returns:
            List of group DM dicts with: id, name, members
        """
        result = await self._api_call("conversations.list", params={
            "types": "mpim",
            "limit": min(limit, 1000)
        })

        if not result.get("ok"):
            logger.error(f"Failed to list group DMs: {result.get('error')}")
            return []

        mpims = []
        for mpim in result.get("channels", []):
            mpims.append({
                "id": mpim.get("id"),
                "name": mpim.get("name"),
                "num_members": len(mpim.get("members", []))
            })

        return mpims

    async def get_messages(
        self,
        channel_id: str,
        limit: int = 20,
        oldest: str = None,
        latest: str = None
    ) -> list[dict]:
        """
        Get messages from a channel or DM.

        Args:
            channel_id: Slack channel/DM ID
            limit: Maximum messages to return (max 100 for non-Marketplace apps)
            oldest: Only messages after this Unix timestamp
            latest: Only messages before this Unix timestamp

        Returns:
            List of message dicts with: ts, user, text, thread_ts, reactions
        """
        params = {
            "channel": channel_id,
            "limit": min(limit, 100)  # Stricter limit for non-Marketplace apps
        }
        if oldest:
            params["oldest"] = oldest
        if latest:
            params["latest"] = latest

        result = await self._api_call("conversations.history", params=params)

        if not result.get("ok"):
            error = result.get('error', 'unknown_error')
            logger.error(f"Failed to get messages: {error}")
            # Permanent errors: raise so the drip service circuit breaker can open
            # for this channel. Returning [] would silently reset consecutive_failures
            # and cause the channel to be retried every 10 seconds forever.
            _PERMANENT_ERRORS = {
                'channel_not_found', 'not_in_channel', 'channel_deleted',
                'is_archived', 'missing_scope',
            }
            if error in _PERMANENT_ERRORS:
                raise Exception(f"Slack permanent error for channel: {error}")
            return []

        messages = []
        for msg in result.get("messages", []):
            messages.append({
                "ts": msg.get("ts"),
                "user": msg.get("user"),
                "text": msg.get("text", ""),
                "thread_ts": msg.get("thread_ts"),
                "reply_count": msg.get("reply_count", 0),
                "reactions": msg.get("reactions", []),
                "attachments": msg.get("attachments", []),
                "files": msg.get("files", [])
            })

        return messages

    async def get_thread_replies(
        self, channel_id: str, thread_ts: str, limit: int = 10
    ) -> list[dict]:
        """
        Fetch replies in a Slack thread.
        Returns list of {user, text, ts} dicts, excluding the parent message.
        Empty list if thread not found or error.
        """
        params = {
            "channel": channel_id,
            "ts": thread_ts,
            "limit": min(limit, 20),
        }
        result = await self._api_call("conversations.replies", params=params)
        if not result.get("ok"):
            return []

        messages = result.get("messages", [])
        # First message is the parent — skip it, return only replies
        return [
            {
                "user": m.get("user", "unknown"),
                "text": (m.get("text", "") or "")[:300],
                "ts": m.get("ts", ""),
            }
            for m in messages[1:]
            if m.get("user")  # skip bot/system messages
        ]

    async def send_message(
        self,
        channel_id: str,
        text: str,
        thread_ts: str = None
    ) -> Optional[dict]:
        """
        Send a message to a channel or DM.

        Args:
            channel_id: Slack channel/DM ID
            text: Message text
            thread_ts: Optional thread timestamp to reply in thread

        Returns:
            Dict with message info (ts, channel) or None on failure
        """
        body = {
            "channel": channel_id,
            "text": text
        }
        if thread_ts:
            body["thread_ts"] = thread_ts

        result = await self._api_call("chat.postMessage", json_body=body)

        if not result.get("ok"):
            logger.error(f"Failed to send message: {result.get('error')}")
            return None

        return {
            "ts": result.get("ts"),
            "channel": result.get("channel"),
            "message": result.get("message", {})
        }

    async def search_messages(
        self,
        query: str,
        count: int = 20,
        sort: str = "timestamp",
        sort_dir: str = "desc"
    ) -> list[dict]:
        """
        Search messages across the workspace.

        Note: Requires user token (bot tokens cannot search).

        Query syntax examples:
        - "from:@username" - messages from specific user
        - "in:#channel" - messages in specific channel
        - "has:link" - messages with links
        - "after:2025-01-01" - messages after date
        - "during:today" - messages from today

        Args:
            query: Slack search query
            count: Maximum results to return
            sort: Sort field ("timestamp" or "score")
            sort_dir: Sort direction ("asc" or "desc")

        Returns:
            List of message dicts with: channel, ts, user, text, permalink
        """
        result = await self._api_call("search.messages", params={
            "query": query,
            "count": min(count, 100),
            "sort": sort,
            "sort_dir": sort_dir
        })

        if not result.get("ok"):
            logger.error(f"Failed to search messages: {result.get('error')}")
            return []

        messages = []
        for match in result.get("messages", {}).get("matches", []):
            messages.append({
                "channel_id": match.get("channel", {}).get("id"),
                "channel_name": match.get("channel", {}).get("name"),
                "ts": match.get("ts"),
                "user": match.get("user"),
                "username": match.get("username"),
                "text": match.get("text", ""),
                "permalink": match.get("permalink")
            })

        return messages

    async def get_user(self, slack_user_id: str) -> Optional[dict]:
        """
        Get user info by Slack user ID.

        Args:
            slack_user_id: Slack user ID (e.g., "U12345678")

        Returns:
            Dict with user info (id, name, real_name, avatar) or None
        """
        result = await self._api_call("users.info", params={
            "user": slack_user_id
        })

        if not result.get("ok"):
            logger.error(f"Failed to get user: {result.get('error')}")
            return None

        user = result.get("user", {})
        profile = user.get("profile", {})

        return {
            "id": user.get("id"),
            "name": user.get("name"),
            "real_name": user.get("real_name"),
            "display_name": profile.get("display_name"),
            "avatar": profile.get("image_72"),
            "email": profile.get("email"),
            "is_bot": user.get("is_bot", False)
        }

    async def list_users(self, limit: int = 100) -> list[dict]:
        """
        List users in the workspace.

        Args:
            limit: Maximum users to return

        Returns:
            List of user dicts with: id, name, real_name, avatar
        """
        result = await self._api_call("users.list", params={
            "limit": min(limit, 1000)
        })

        if not result.get("ok"):
            logger.error(f"Failed to list users: {result.get('error')}")
            return []

        users = []
        for user in result.get("members", []):
            if user.get("deleted"):
                continue  # Skip deleted users

            profile = user.get("profile", {})
            users.append({
                "id": user.get("id"),
                "name": user.get("name"),
                "real_name": user.get("real_name"),
                "display_name": profile.get("display_name"),
                "avatar": profile.get("image_72"),
                "is_bot": user.get("is_bot", False)
            })

        return users

    async def get_user_display_name(self, slack_user_id: str) -> str:
        """
        Get display name for a user ID, using cache.

        Returns display_name > real_name > username > user_id as fallback.
        Uses module-level cache with 5-minute TTL to avoid repeated API calls.

        Args:
            slack_user_id: Slack user ID (e.g., "U12345678")

        Returns:
            Human-readable display name
        """
        global _user_cache

        # Get current team_id
        token_data = self._load_token()
        if not token_data:
            return slack_user_id  # Can't look up without token

        team_id = token_data.get("team_id", "unknown")
        current_time = time.time()

        # Check if cache exists and is fresh
        if team_id in _user_cache:
            cache_entry = _user_cache[team_id]
            if current_time - cache_entry.get("fetched_at", 0) < _USER_CACHE_TTL:
                # Cache hit
                user_map = cache_entry.get("users", {})
                if slack_user_id in user_map:
                    return user_map[slack_user_id]

        # Cache miss or stale - fetch all users
        logger.info(f"Fetching users for workspace {team_id} (cache miss/stale)")
        user_list = await self.list_users(limit=1000)

        # Build user map: user_id -> best display name
        user_map = {}
        for user_info in user_list:
            uid = user_info.get("id")
            # Priority: display_name > real_name > name > id
            display = (
                user_info.get("display_name") or
                user_info.get("real_name") or
                user_info.get("name") or
                uid
            )
            user_map[uid] = display

        # Store in cache
        _user_cache[team_id] = {
            "users": user_map,
            "fetched_at": current_time
        }

        # Return the requested user's name (or ID if not found)
        return user_map.get(slack_user_id, slack_user_id)

    async def get_users_map(self) -> dict[str, str]:
        """
        Get a mapping of all user IDs to display names for the workspace.
        Uses cache - call get_user_display_name first to populate if needed.

        Returns:
            Dict of {user_id: display_name}
        """
        global _user_cache

        token_data = self._load_token()
        if not token_data:
            return {}

        team_id = token_data.get("team_id", "unknown")
        current_time = time.time()

        # Check if cache exists and is fresh
        if team_id in _user_cache:
            cache_entry = _user_cache[team_id]
            if current_time - cache_entry.get("fetched_at", 0) < _USER_CACHE_TTL:
                return cache_entry.get("users", {})

        # Need to populate cache - call list_users
        logger.info(f"Fetching users for workspace {team_id} (cache miss/stale)")
        user_list = await self.list_users(limit=1000)

        user_map = {}
        for user_info in user_list:
            uid = user_info.get("id")
            display = (
                user_info.get("display_name") or
                user_info.get("real_name") or
                user_info.get("name") or
                uid
            )
            user_map[uid] = display

        _user_cache[team_id] = {
            "users": user_map,
            "fetched_at": current_time
        }

        return user_map

    async def get_channel_info(self, channel_id: str) -> Optional[dict]:
        """
        Get detailed info about a channel.

        Args:
            channel_id: Slack channel ID

        Returns:
            Dict with channel info or None
        """
        result = await self._api_call("conversations.info", params={
            "channel": channel_id
        })

        if not result.get("ok"):
            logger.error(f"Failed to get channel info: {result.get('error')}")
            return None

        ch = result.get("channel", {})
        return {
            "id": ch.get("id"),
            "name": ch.get("name"),
            "is_private": ch.get("is_private", False),
            "is_im": ch.get("is_im", False),
            "is_mpim": ch.get("is_mpim", False),
            "topic": ch.get("topic", {}).get("value", ""),
            "purpose": ch.get("purpose", {}).get("value", ""),
            "num_members": ch.get("num_members", 0),
            "created": ch.get("created")
        }
