"""
Slack API endpoints for Seny.

Provides:
- OAuth 2.0 flow for connecting Slack workspaces
- Workspace management (list, disconnect)
- Channel and message listing for UI
"""

import logging
import os
import secrets
from typing import Optional

logger = logging.getLogger(__name__)
from fastapi import APIRouter, HTTPException, status, Depends, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from web.auth.jwt_utils import require_auth
from web.services.slack_service import SlackService
from web.core.database import save_slack_token, delete_slack_token

router = APIRouter()


# ============================================================================
# Response Models
# ============================================================================

class SlackWorkspace(BaseModel):
    """Connected Slack workspace info."""
    team_id: str
    team_name: str
    authed_user_name: Optional[str] = None
    created_at: str


class WorkspacesResponse(BaseModel):
    """List of connected workspaces."""
    workspaces: list[SlackWorkspace]
    count: int


class SlackChannel(BaseModel):
    """Slack channel info."""
    id: str
    name: str
    is_private: bool = False
    is_im: bool = False
    is_mpim: bool = False
    num_members: int = 0
    topic: str = ""
    purpose: str = ""


class ChannelsResponse(BaseModel):
    """List of channels."""
    channels: list[SlackChannel]
    team_id: str
    team_name: str


class SlackMessage(BaseModel):
    """Slack message info."""
    ts: str
    user: Optional[str] = None
    user_name: Optional[str] = None  # Resolved display name
    text: str
    thread_ts: Optional[str] = None
    reply_count: int = 0


class MessagesResponse(BaseModel):
    """List of messages."""
    messages: list[SlackMessage]
    channel_id: str


class SlackSearchResult(BaseModel):
    """Slack search result."""
    channel_id: str
    channel_name: Optional[str] = None
    ts: str
    user: Optional[str] = None
    username: Optional[str] = None
    text: str
    permalink: Optional[str] = None


class SearchResponse(BaseModel):
    """Search results."""
    results: list[SlackSearchResult]
    query: str


class AuthUrlResponse(BaseModel):
    """OAuth auth URL response."""
    auth_url: str
    state: str


class StatusResponse(BaseModel):
    """Connection status response."""
    connected: bool
    workspaces: list[SlackWorkspace] = []


class HealthResponse(BaseModel):
    """Health check response."""
    connected: bool
    healthy: bool
    workspaces: list[SlackWorkspace] = []


# ============================================================================
# OAuth Endpoints
# ============================================================================

# Store OAuth states temporarily (in production, use Redis or database)
_oauth_states: dict[str, int] = {}


@router.get("/auth-url", response_model=AuthUrlResponse)
async def get_auth_url(
    request: Request,
    user_id: str = Depends(require_auth)
):
    """
    Generate Slack OAuth authorization URL.

    Returns URL to redirect user to for Slack authorization.
    """
    # Generate random state for CSRF protection
    state = secrets.token_urlsafe(32)

    # Store state -> user_id mapping (expires after use)
    _oauth_states[state] = int(user_id)

    # Determine redirect URI based on request (supports custom domains)
    base_url = str(request.base_url).rstrip("/")
    is_localhost = "localhost" in base_url or "127.0.0.1" in base_url
    if not is_localhost:
        base_url = base_url.replace("http://", "https://")
    redirect_uri = f"{base_url}/api/slack/oauth/callback"

    auth_url = SlackService.get_auth_url(redirect_uri, state)

    return AuthUrlResponse(auth_url=auth_url, state=state)


@router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...)
):
    """
    OAuth callback from Slack.

    Exchanges authorization code for access token and stores it.
    Redirects user back to main app.
    """
    # Verify state and get user_id
    user_id = _oauth_states.pop(state, None)
    if user_id is None:
        # Invalid or expired state
        return RedirectResponse(
            url="/?error=invalid_state&message=OAuth+session+expired.+Please+try+again.",
            status_code=302
        )

    # Determine redirect URI (must match auth request)
    base_url = str(request.base_url).rstrip("/")
    is_localhost = "localhost" in base_url or "127.0.0.1" in base_url
    if not is_localhost:
        base_url = base_url.replace("http://", "https://")
    redirect_uri = f"{base_url}/api/slack/oauth/callback"

    # Exchange code for token
    token_data = await SlackService.exchange_code(code, redirect_uri)

    if "error" in token_data:
        error = token_data.get("error", "unknown")
        return RedirectResponse(
            url=f"/?error=slack_oauth_failed&message={error}",
            status_code=302
        )

    # Fetch user info to get username
    temp_service = SlackService.__new__(SlackService)
    temp_service.user_id = user_id
    temp_service.team_id = token_data["team_id"]
    temp_service._token_data = {
        "access_token": token_data["access_token"],
        "team_id": token_data["team_id"],
        "team_name": token_data["team_name"]
    }

    user_info = await temp_service.get_user(token_data["authed_user_id"])
    authed_user_name = user_info.get("name") if user_info else None

    # Save token to database
    bot_token = token_data.get("bot_token")
    bot_user_id = token_data.get("bot_user_id")

    # Log bot token status for debugging
    if bot_token:
        logger.info(f"Slack OAuth: Got bot token for {token_data['team_name']} (bot_user_id={bot_user_id})")
    else:
        logger.info(f"Slack OAuth: No bot token for {token_data['team_name']} (user-only scopes)")

    save_slack_token(
        user_id=user_id,
        team_id=token_data["team_id"],
        team_name=token_data["team_name"],
        access_token=token_data["access_token"],
        scope=token_data["scope"],
        authed_user_id=token_data["authed_user_id"],
        authed_user_name=authed_user_name,
        token_type=token_data.get("token_type", "user"),
        bot_token=bot_token,
        bot_user_id=bot_user_id
    )

    # Redirect back to main app with success message
    team_name = token_data.get("team_name", "workspace")
    return RedirectResponse(
        url=f"/?slack_connected=true&team={team_name}",
        status_code=302
    )


# ============================================================================
# Workspace Management Endpoints
# ============================================================================

@router.get("/status", response_model=StatusResponse)
async def get_status(user_id: str = Depends(require_auth)):
    """
    Check Slack connection status.

    Returns whether user has any connected workspaces and their info.
    """
    workspaces = SlackService.list_connected_workspaces(int(user_id))

    return StatusResponse(
        connected=len(workspaces) > 0,
        workspaces=[SlackWorkspace(**w) for w in workspaces]
    )


@router.get("/health", response_model=HealthResponse)
async def get_health(user_id: str = Depends(require_auth)):
    """
    Check if Slack tokens are valid and working.

    Unlike /status which only checks if tokens exist, this endpoint
    actually tests the tokens by making an auth.test API call.

    Returns:
        connected: True if tokens exist
        healthy: True if tokens are valid and working
        workspaces: List of connected workspaces
    """
    workspaces = SlackService.list_connected_workspaces(int(user_id))

    if not workspaces:
        return HealthResponse(connected=False, healthy=False)

    # Test the first workspace's token with auth.test
    first_workspace = workspaces[0]
    slack = SlackService(int(user_id), first_workspace["team_id"])

    # auth.test is a lightweight call that verifies the token
    result = await slack._api_call("auth.test")

    healthy = result.get("ok", False)

    return HealthResponse(
        connected=True,
        healthy=healthy,
        workspaces=[SlackWorkspace(**w) for w in workspaces]
    )


@router.get("/workspaces", response_model=WorkspacesResponse)
async def list_workspaces(user_id: str = Depends(require_auth)):
    """
    List all connected Slack workspaces.
    """
    workspaces = SlackService.list_connected_workspaces(int(user_id))

    return WorkspacesResponse(
        workspaces=[SlackWorkspace(**w) for w in workspaces],
        count=len(workspaces)
    )


@router.delete("/disconnect/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_workspace(
    team_id: str,
    user_id: str = Depends(require_auth)
):
    """
    Disconnect a Slack workspace.

    Removes stored tokens for the specified workspace.
    """
    deleted = delete_slack_token(int(user_id), team_id)

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found or not connected"
        )

    return None


# ============================================================================
# Channel & Message Endpoints
# ============================================================================

@router.get("/channels", response_model=ChannelsResponse)
async def list_channels(
    team_id: Optional[str] = None,
    types: str = "public_channel,private_channel",
    user_id: str = Depends(require_auth)
):
    """
    List channels in a Slack workspace.

    Args:
        team_id: Workspace ID (optional - uses first connected if not specified)
        types: Comma-separated channel types (public_channel, private_channel, mpim, im)
    """
    slack = SlackService(int(user_id), team_id)

    if not slack.is_connected():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Slack workspace connected"
        )

    channels = await slack.list_channels(types=types)

    # Get workspace info
    token_data = slack._load_token()

    return ChannelsResponse(
        channels=[SlackChannel(**ch) for ch in channels],
        team_id=token_data["team_id"],
        team_name=token_data["team_name"]
    )


@router.get("/messages/{channel_id}", response_model=MessagesResponse)
async def get_messages(
    channel_id: str,
    team_id: Optional[str] = None,
    limit: int = 20,
    user_id: str = Depends(require_auth)
):
    """
    Get messages from a Slack channel or DM.

    Args:
        channel_id: Slack channel ID
        team_id: Workspace ID (optional)
        limit: Maximum messages to return (max 100)
    """
    slack = SlackService(int(user_id), team_id)

    if not slack.is_connected():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Slack workspace connected"
        )

    raw_messages = await slack.get_messages(channel_id, limit=min(limit, 100))

    # Resolve user IDs to display names
    slack_users_map = await slack.get_users_map()
    resolved_messages = []
    for msg in raw_messages:
        msg_with_name = dict(msg)
        msg_user_id = msg.get('user')
        if msg_user_id:
            msg_with_name['user_name'] = slack_users_map.get(msg_user_id, msg_user_id)
        resolved_messages.append(msg_with_name)

    return MessagesResponse(
        messages=[SlackMessage(**msg) for msg in resolved_messages],
        channel_id=channel_id
    )


@router.get("/search", response_model=SearchResponse)
async def search_messages(
    q: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    count: int = 20,
    user_id: str = Depends(require_auth)
):
    """
    Search messages in Slack.

    Query syntax:
    - "from:@username" - messages from specific user
    - "in:#channel" - messages in specific channel
    - "has:link" - messages with links
    - "after:2025-01-01" - messages after date

    Args:
        q: Search query
        team_id: Workspace ID (optional)
        count: Maximum results to return
    """
    slack = SlackService(int(user_id), team_id)

    if not slack.is_connected():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Slack workspace connected"
        )

    results = await slack.search_messages(q, count=min(count, 100))

    return SearchResponse(
        results=[SlackSearchResult(**r) for r in results],
        query=q
    )


@router.post("/send/{channel_id}")
async def send_message(
    channel_id: str,
    text: str = Query(..., min_length=1),
    thread_ts: Optional[str] = None,
    team_id: Optional[str] = None,
    user_id: str = Depends(require_auth)
):
    """
    Send a message to a Slack channel or DM.

    Args:
        channel_id: Slack channel ID
        text: Message text
        thread_ts: Optional thread timestamp to reply in thread
        team_id: Workspace ID (optional)
    """
    slack = SlackService(int(user_id), team_id)

    if not slack.is_connected():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Slack workspace connected"
        )

    result = await slack.send_message(channel_id, text, thread_ts)

    if not result:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to send message"
        )

    return {
        "success": True,
        "ts": result.get("ts"),
        "channel": result.get("channel")
    }


# ============================================================================
# UI-Specific Endpoints
# ============================================================================

class DMInfo(BaseModel):
    """Direct message info."""
    id: str
    user_id: str
    user_name: Optional[str] = None
    display_name: Optional[str] = None
    is_mpim: bool = False
    member_names: list[str] = []


class DMsResponse(BaseModel):
    """List of direct messages."""
    dms: list[DMInfo]
    group_dms: list[DMInfo]
    team_id: str


class SidebarChannel(BaseModel):
    """Channel info for sidebar."""
    id: str
    name: str
    type: str  # 'public', 'private', 'dm', 'group_dm'
    is_private: bool = False
    user_id: Optional[str] = None  # For DMs
    user_name: Optional[str] = None  # For DMs
    member_names: list[str] = []  # For group DMs
    topic: str = ""


class SidebarResponse(BaseModel):
    """Combined sidebar data."""
    channels: list[SidebarChannel]
    dms: list[SidebarChannel]
    group_dms: list[SidebarChannel]
    team_id: str
    team_name: str


class UserInfo(BaseModel):
    """User info response."""
    id: str
    name: str
    display_name: Optional[str] = None
    real_name: Optional[str] = None
    email: Optional[str] = None
    avatar_url: Optional[str] = None


@router.get("/dms", response_model=DMsResponse)
async def list_dms(
    team_id: Optional[str] = None,
    user_id: str = Depends(require_auth)
):
    """
    List direct messages and group DMs.

    Returns DMs with user names resolved.
    """
    slack = SlackService(int(user_id), team_id)

    if not slack.is_connected():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Slack workspace connected"
        )

    # Get DMs and group DMs
    dms = await slack.list_dms()
    group_dms = await slack.list_group_dms()

    token_data = slack._load_token()

    return DMsResponse(
        dms=[DMInfo(**dm) for dm in dms],
        group_dms=[DMInfo(**gdm, is_mpim=True) for gdm in group_dms],
        team_id=token_data["team_id"]
    )


@router.get("/sidebar", response_model=SidebarResponse)
async def get_sidebar_data(
    team_id: Optional[str] = None,
    user_id: str = Depends(require_auth)
):
    """
    Get combined sidebar data (channels + DMs + group DMs).

    Optimized endpoint that returns all data needed for sidebar rendering.
    """
    slack = SlackService(int(user_id), team_id)

    if not slack.is_connected():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Slack workspace connected"
        )

    # Fetch all data in parallel-ish manner
    channels = await slack.list_channels(types="public_channel,private_channel")
    dms = await slack.list_dms()
    group_dms = await slack.list_group_dms()

    token_data = slack._load_token()

    # Transform channels
    sidebar_channels = [
        SidebarChannel(
            id=ch["id"],
            name=ch["name"],
            type="private" if ch.get("is_private") else "public",
            is_private=ch.get("is_private", False),
            topic=ch.get("topic", "") or ch.get("purpose", "")
        )
        for ch in channels
    ]

    # Transform DMs
    sidebar_dms = [
        SidebarChannel(
            id=dm["id"],
            name=dm.get("user_name") or dm.get("display_name") or dm.get("user_id", "Unknown"),
            type="dm",
            is_private=True,
            user_id=dm.get("user_id"),
            user_name=dm.get("user_name") or dm.get("display_name")
        )
        for dm in dms
    ]

    # Transform group DMs
    sidebar_group_dms = [
        SidebarChannel(
            id=gdm["id"],
            name=", ".join(gdm.get("member_names", [])) or gdm.get("name", "Group DM"),
            type="group_dm",
            is_private=True,
            member_names=gdm.get("member_names", [])
        )
        for gdm in group_dms
    ]

    return SidebarResponse(
        channels=sidebar_channels,
        dms=sidebar_dms,
        group_dms=sidebar_group_dms,
        team_id=token_data["team_id"],
        team_name=token_data["team_name"]
    )


@router.get("/user/{slack_user_id}", response_model=UserInfo)
async def get_user_info(
    slack_user_id: str,
    team_id: Optional[str] = None,
    user_id: str = Depends(require_auth)
):
    """
    Get info about a Slack user.

    Used for displaying names and avatars.
    """
    slack = SlackService(int(user_id), team_id)

    if not slack.is_connected():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Slack workspace connected"
        )

    user_info = await slack.get_user(slack_user_id)

    if not user_info:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    return UserInfo(
        id=user_info.get("id", slack_user_id),
        name=user_info.get("name", "Unknown"),
        display_name=user_info.get("display_name"),
        real_name=user_info.get("real_name"),
        email=user_info.get("email"),
        avatar_url=user_info.get("avatar_url") or user_info.get("image_72")
    )
