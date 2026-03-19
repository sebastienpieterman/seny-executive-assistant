"""
Telegram API endpoints for Seny.

Handles Telegram authentication and message operations.
Uses phone-based authentication with SMS verification codes.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from web.auth.jwt_utils import require_auth
from web.services.telegram_service import TelegramService


router = APIRouter()


# ============================================================================
# Request/Response Models
# ============================================================================

class StartAuthRequest(BaseModel):
    """Request to start Telegram authentication."""
    phone_number: str  # With country code, e.g., "+1234567890"


class StartAuthResponse(BaseModel):
    """Response from starting authentication."""
    phone_code_hash: Optional[str] = None
    phone_number: Optional[str] = None
    error: Optional[str] = None


class CompleteAuthRequest(BaseModel):
    """Request to complete Telegram authentication."""
    phone_number: str
    code: str
    phone_code_hash: str
    password: Optional[str] = None  # For 2FA


class CompleteAuthResponse(BaseModel):
    """Response from completing authentication."""
    success: bool = False
    username: Optional[str] = None
    display_name: Optional[str] = None
    phone_number: Optional[str] = None
    error: Optional[str] = None
    requires_2fa: bool = False


class TelegramStatusResponse(BaseModel):
    """Response with Telegram connection status."""
    configured: bool
    connected: bool
    accounts: list[dict] = []


class ChatsResponse(BaseModel):
    """Response with list of chats."""
    chats: list[dict]
    phone_number: str


class MessagesResponse(BaseModel):
    """Response with messages from a chat."""
    messages: list[dict]
    chat_id: int
    phone_number: str


class SendMessageRequest(BaseModel):
    """Request to send a message."""
    chat_id: int
    text: str
    phone_number: Optional[str] = None
    reply_to_msg_id: Optional[int] = None


class SendGifRequest(BaseModel):
    """Request to send a GIF."""
    chat_id: int
    gif_url: str
    phone_number: Optional[str] = None
    reply_to_msg_id: Optional[int] = None


class SendReactionRequest(BaseModel):
    """Request to send a reaction to a message."""
    chat_id: int
    message_id: int
    emoji: str
    phone_number: Optional[str] = None


class SendReactionResponse(BaseModel):
    """Response from sending a reaction."""
    success: bool
    error: Optional[str] = None
    emoji: Optional[str] = None


class SendMessageResponse(BaseModel):
    """Response from sending a message."""
    success: bool
    message: Optional[dict] = None
    error: Optional[str] = None


class SearchRequest(BaseModel):
    """Request to search messages."""
    query: str
    limit: int = 20
    chat_id: Optional[int] = None  # None = global search
    phone_number: Optional[str] = None


class SearchResponse(BaseModel):
    """Response from message search."""
    results: list[dict]
    query: str


# ============================================================================
# Status Endpoint
# ============================================================================

@router.get("/status", response_model=TelegramStatusResponse)
async def get_telegram_status(user_id: str = Depends(require_auth)):
    """Check Telegram connection status and list connected accounts."""
    service = TelegramService(int(user_id))

    return TelegramStatusResponse(
        configured=service.is_configured(),
        connected=service.is_connected(),
        accounts=service.list_accounts()
    )


# ============================================================================
# Authentication Endpoints
# ============================================================================

# Store pending auth sessions (phone_number -> TelegramService instance)
# In production, consider using Redis or similar for multi-instance deployment
_pending_auth_sessions: dict[str, TelegramService] = {}


@router.post("/start-auth", response_model=StartAuthResponse)
async def start_auth(
    request: StartAuthRequest,
    user_id: str = Depends(require_auth)
):
    """
    Start Telegram authentication.

    This will send a verification code to the user's Telegram app.
    """
    service = TelegramService(int(user_id))

    if not service.is_configured():
        return StartAuthResponse(error="Telegram API credentials not configured on server")

    result = await service.start_auth(request.phone_number)

    if "error" in result:
        return StartAuthResponse(error=result["error"])

    # Store service for complete_auth
    _pending_auth_sessions[f"{user_id}:{request.phone_number}"] = service

    return StartAuthResponse(
        phone_code_hash=result["phone_code_hash"],
        phone_number=result["phone_number"]
    )


@router.post("/complete-auth", response_model=CompleteAuthResponse)
async def complete_auth(
    request: CompleteAuthRequest,
    user_id: str = Depends(require_auth)
):
    """
    Complete Telegram authentication with verification code.

    If 2FA is enabled, include the password in the request.
    """
    session_key = f"{user_id}:{request.phone_number}"

    # Get pending auth session
    service = _pending_auth_sessions.get(session_key)

    if not service:
        # Try creating a new service and reconnecting
        service = TelegramService(int(user_id))
        if not service.is_configured():
            return CompleteAuthResponse(error="No pending authentication found")

    result = await service.complete_auth(
        phone_number=request.phone_number,
        code=request.code,
        phone_code_hash=request.phone_code_hash,
        password=request.password
    )

    # Clean up pending session
    if session_key in _pending_auth_sessions:
        del _pending_auth_sessions[session_key]

    if "error" in result:
        return CompleteAuthResponse(
            error=result["error"],
            requires_2fa=result.get("requires_2fa", False)
        )

    return CompleteAuthResponse(
        success=True,
        username=result.get("username"),
        display_name=result.get("display_name"),
        phone_number=result.get("phone_number")
    )


@router.delete("/disconnect/{phone_number}")
async def disconnect_account(
    phone_number: str,
    user_id: str = Depends(require_auth)
):
    """Disconnect a Telegram account."""
    service = TelegramService(int(user_id), phone_number)

    await service.disconnect(remove_session=True)

    return {"success": True, "phone_number": phone_number}


# ============================================================================
# Chat Endpoints
# ============================================================================

@router.get("/chats", response_model=ChatsResponse)
async def get_chats(
    phone_number: Optional[str] = None,
    limit: int = 50,
    user_id: str = Depends(require_auth)
):
    """Get list of all chats (DMs, groups, channels)."""
    service = TelegramService(int(user_id), phone_number)

    if not await service.connect():
        raise HTTPException(status_code=400, detail="Not connected to Telegram")

    chats = await service.list_dialogs(limit=limit)

    return ChatsResponse(
        chats=chats,
        phone_number=service.phone_number
    )


@router.get("/messages/{chat_id}", response_model=MessagesResponse)
async def get_messages(
    chat_id: int,
    phone_number: Optional[str] = None,
    limit: int = 20,
    offset_id: int = 0,
    user_id: str = Depends(require_auth)
):
    """Get messages from a specific chat."""
    service = TelegramService(int(user_id), phone_number)

    if not await service.connect():
        raise HTTPException(status_code=400, detail="Not connected to Telegram")

    messages = await service.get_messages(
        chat_id=chat_id,
        limit=limit,
        offset_id=offset_id
    )

    return MessagesResponse(
        messages=messages,
        chat_id=chat_id,
        phone_number=service.phone_number
    )


@router.post("/send", response_model=SendMessageResponse)
async def send_message(
    request: SendMessageRequest,
    user_id: str = Depends(require_auth)
):
    """Send a message to a chat."""
    service = TelegramService(int(user_id), request.phone_number)

    if not await service.connect():
        return SendMessageResponse(error="Not connected to Telegram")

    result = await service.send_message(
        chat_id=request.chat_id,
        text=request.text,
        reply_to=request.reply_to_msg_id
    )

    if "error" in result:
        return SendMessageResponse(error=result["error"])

    return SendMessageResponse(
        success=True,
        message=result
    )


@router.post("/send-gif", response_model=SendMessageResponse)
async def send_gif(
    request: SendGifRequest,
    user_id: str = Depends(require_auth)
):
    """Send a GIF to a chat."""
    service = TelegramService(int(user_id), request.phone_number)

    if not await service.connect():
        return SendMessageResponse(error="Not connected to Telegram")

    result = await service.send_gif(
        chat_id=request.chat_id,
        gif_url=request.gif_url,
        reply_to=request.reply_to_msg_id
    )

    if "error" in result:
        return SendMessageResponse(error=result["error"])

    return SendMessageResponse(
        success=True,
        message=result
    )


@router.post("/react", response_model=SendReactionResponse)
async def send_reaction(
    request: SendReactionRequest,
    user_id: str = Depends(require_auth)
):
    """Send a reaction to a message."""
    service = TelegramService(int(user_id), request.phone_number)

    if not await service.connect():
        return SendReactionResponse(success=False, error="Not connected to Telegram")

    result = await service.send_reaction(
        chat_id=request.chat_id,
        message_id=request.message_id,
        emoji=request.emoji
    )

    if "error" in result:
        return SendReactionResponse(success=False, error=result["error"])

    return SendReactionResponse(
        success=True,
        emoji=result["emoji"]
    )


@router.post("/mark-read/{chat_id}")
async def mark_chat_as_read(
    chat_id: int,
    phone_number: Optional[str] = None,
    user_id: str = Depends(require_auth)
):
    """Mark all messages in a Telegram chat as read."""
    service = TelegramService(int(user_id), phone_number)

    if not await service.connect():
        raise HTTPException(status_code=400, detail="Not connected to Telegram")

    result = await service.mark_as_read(chat_id)

    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["error"])

    return result


@router.post("/search", response_model=SearchResponse)
async def search_messages(
    request: SearchRequest,
    user_id: str = Depends(require_auth)
):
    """Search messages globally or in a specific chat."""
    service = TelegramService(int(user_id), request.phone_number)

    if not await service.connect():
        raise HTTPException(status_code=400, detail="Not connected to Telegram")

    results = await service.search_messages(
        query=request.query,
        limit=request.limit,
        chat_id=request.chat_id
    )

    return SearchResponse(
        results=results,
        query=request.query
    )


@router.get("/chat/{chat_id}")
async def get_chat_info(
    chat_id: int,
    phone_number: Optional[str] = None,
    user_id: str = Depends(require_auth)
):
    """Get information about a specific chat."""
    service = TelegramService(int(user_id), phone_number)

    if not await service.connect():
        raise HTTPException(status_code=400, detail="Not connected to Telegram")

    info = await service.get_chat_info(chat_id)

    if not info:
        raise HTTPException(status_code=404, detail="Chat not found")

    return info


# ============================================================================
# Giphy API Proxy (for GIF picker)
# ============================================================================

import httpx
import os

GIPHY_API_KEY = os.getenv("GIPHY_API_KEY", "")


class GiphySearchResponse(BaseModel):
    """Response from Giphy search."""
    gifs: list[dict]


@router.get("/giphy/search", response_model=GiphySearchResponse)
async def search_giphy(
    q: str,
    limit: int = 20,
    user_id: str = Depends(require_auth)
):
    """Search Giphy for GIFs."""
    if not GIPHY_API_KEY:
        raise HTTPException(status_code=500, detail="Giphy API key not configured")

    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.giphy.com/v1/gifs/search",
            params={
                "api_key": GIPHY_API_KEY,
                "q": q,
                "limit": min(limit, 50),
                "rating": "pg-13"
            }
        )

        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail="Giphy API error")

        data = response.json()

        # Extract just the data we need
        gifs = []
        for gif in data.get("data", []):
            gifs.append({
                "id": gif.get("id"),
                "title": gif.get("title"),
                "url": gif.get("images", {}).get("fixed_height", {}).get("url"),
                "preview": gif.get("images", {}).get("fixed_height_small", {}).get("url"),
                "width": gif.get("images", {}).get("fixed_height", {}).get("width"),
                "height": gif.get("images", {}).get("fixed_height", {}).get("height"),
            })

        return GiphySearchResponse(gifs=gifs)


@router.get("/giphy/trending", response_model=GiphySearchResponse)
async def trending_giphy(
    limit: int = 20,
    user_id: str = Depends(require_auth)
):
    """Get trending GIFs from Giphy."""
    if not GIPHY_API_KEY:
        raise HTTPException(status_code=500, detail="Giphy API key not configured")

    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.giphy.com/v1/gifs/trending",
            params={
                "api_key": GIPHY_API_KEY,
                "limit": min(limit, 50),
                "rating": "pg-13"
            }
        )

        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail="Giphy API error")

        data = response.json()

        # Extract just the data we need
        gifs = []
        for gif in data.get("data", []):
            gifs.append({
                "id": gif.get("id"),
                "title": gif.get("title"),
                "url": gif.get("images", {}).get("fixed_height", {}).get("url"),
                "preview": gif.get("images", {}).get("fixed_height_small", {}).get("url"),
                "width": gif.get("images", {}).get("fixed_height", {}).get("width"),
                "height": gif.get("images", {}).get("fixed_height", {}).get("height"),
            })

        return GiphySearchResponse(gifs=gifs)
