"""
API route definitions for Seny.
"""

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, status, Depends
from pydantic import BaseModel
from anthropic import APIError
from web.services.claude_service import ClaudeService, ClaudeServiceError
from web.auth.jwt_utils import require_auth
from web.core.database import (
    get_user_conversations, get_conversation_with_messages, delete_conversation,
    get_conversation, update_conversation_model, log_usage, get_usage_summary,
    get_recent_nudges, update_conversation_title, get_priority_items,
    get_user_profile
)
from web.api.settings import get_user_settings

logger = logging.getLogger(__name__)

# Create API router
router = APIRouter()

# Initialize Claude service
claude_service = ClaudeService()

# Retry marker for hallucination detection (prevents infinite loops)
HALLUCINATION_RETRY_MARKER = "[TOOL_RETRY]"
# Legacy marker kept for backward compatibility with in-flight conversations
PROJECT_RETRY_MARKER = "[PROJECT_TOOL_RETRY]"


# Request/Response models
class ChatRequest(BaseModel):
    """Request model for chat endpoint."""
    message: str
    conversation_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None  # Gmail message ID for email replies
    timezone: Optional[str] = "UTC"  # IANA timezone (e.g., "America/New_York")
    slack_workspace: Optional[str] = None  # Slack team_id for workspace context
    image_b64: Optional[str] = None         # base64 encoded image bytes
    image_media_type: Optional[str] = None  # e.g. "image/png"
    image_file_name: Optional[str] = None   # original filename for context
    model: Optional[str] = None             # model override from frontend (used on first message of new conversations)


class Citation(BaseModel):
    """Citation from web search results."""
    url: str
    title: Optional[str] = None
    cited_text: Optional[str] = None


class CaptureInfo(BaseModel):
    """Info about a Second Brain capture."""
    type: str  # people, project, idea, admin
    confidence: float
    table: str
    id: int
    text_preview: str


class ChatResponse(BaseModel):
    """Response model for chat endpoint."""
    response: str
    conversation_id: str
    tokens_used: int
    citations: list[Citation] = []
    tools_used: list[str] = []  # List of tool names called during this response
    capture_info: Optional[CaptureInfo] = None  # Second Brain capture info if something was captured


class ConversationSummary(BaseModel):
    """Summary of a conversation for list view."""
    id: str
    title: Optional[str] = None
    model: Optional[str] = None  # Per-conversation Claude model
    created_at: str
    updated_at: str


class Message(BaseModel):
    """A message in a conversation."""
    role: str
    content: str
    created_at: str


class ConversationDetail(BaseModel):
    """Full conversation with messages."""
    id: str
    title: Optional[str] = None
    model: Optional[str] = None  # Per-conversation Claude model
    created_at: str
    updated_at: str
    messages: list[Message]


class ConversationsListResponse(BaseModel):
    """Response model for conversations list endpoint."""
    conversations: list[ConversationSummary]


class UpdateConversationModelRequest(BaseModel):
    """Request model for updating a conversation's model."""
    model: str


@router.get("/conversations", response_model=ConversationsListResponse)
async def list_conversations(user_id: str = Depends(require_auth)):
    """
    List all conversations for the authenticated user.

    Protected endpoint - requires valid JWT token.

    Returns:
        List of conversation summaries, most recent first
    """
    conversations = get_user_conversations(int(user_id))
    return ConversationsListResponse(
        conversations=[ConversationSummary(**c) for c in conversations]
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation_endpoint(conversation_id: str, user_id: str = Depends(require_auth)):
    """
    Get a specific conversation with all messages.

    Protected endpoint - requires valid JWT token.
    Only returns conversations owned by the authenticated user.

    Args:
        conversation_id: The conversation's unique identifier

    Returns:
        Conversation with all messages

    Raises:
        HTTPException 404: Conversation not found or not owned by user
    """
    conversation = get_conversation_with_messages(conversation_id, int(user_id))

    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found"
        )

    return ConversationDetail(
        id=conversation["id"],
        title=conversation["title"],
        model=conversation.get("model"),
        created_at=conversation["created_at"],
        updated_at=conversation["updated_at"],
        messages=[Message(**m) for m in conversation["messages"]]
    )


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation_endpoint(conversation_id: str, user_id: str = Depends(require_auth)):
    """
    Delete a conversation and all its messages.

    Protected endpoint - requires valid JWT token.
    Only allows deletion of conversations owned by the authenticated user.

    Args:
        conversation_id: The conversation's unique identifier

    Returns:
        204 No Content on success

    Raises:
        HTTPException 404: Conversation not found or not owned by user

    Security:
        - Never reveals whether a conversation exists to non-owners
        - Returns 404 for both "not found" and "not owned" cases
    """
    deleted = delete_conversation(conversation_id, int(user_id))

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found"
        )

    # 204 No Content - no body needed
    return None


@router.patch("/conversations/{conversation_id}/model")
async def update_conversation_model_endpoint(
    conversation_id: str,
    request: UpdateConversationModelRequest,
    user_id: str = Depends(require_auth)
):
    """
    Update the Claude model for a specific conversation.

    Protected endpoint - requires valid JWT token.
    Only allows updates to conversations owned by the authenticated user.

    Args:
        conversation_id: The conversation's unique identifier
        request: Contains the new model ID

    Returns:
        Success message with the new model

    Raises:
        HTTPException 404: Conversation not found or not owned by user
    """
    updated = update_conversation_model(conversation_id, request.model, int(user_id))

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found"
        )

    return {"message": "Model updated", "model": request.model}


class UpdateConversationTitleRequest(BaseModel):
    """Request model for updating a conversation's title."""
    title: str


@router.patch("/conversations/{conversation_id}/title")
async def update_conversation_title_endpoint(
    conversation_id: str,
    request: UpdateConversationTitleRequest,
    user_id: str = Depends(require_auth)
):
    """
    Update the user-facing title of a conversation.

    Protected endpoint - requires valid JWT token.
    Only allows updates to conversations owned by the authenticated user.
    Empty or whitespace-only titles are rejected with 400.
    """
    title = request.title.strip()
    if not title:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Title cannot be empty"
        )

    # Verify ownership first
    conversation = get_conversation(conversation_id)
    if not conversation or conversation.get("user_id") != int(user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found"
        )

    updated = update_conversation_title(conversation_id, title)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update title"
        )

    return {"conversation_id": conversation_id, "title": title}


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, user_id: str = Depends(require_auth)):
    """
    Chat endpoint for conversing with Claude.

    Protected endpoint - requires valid JWT token.
    Conversations are associated with the authenticated user.

    Args:
        request: ChatRequest with message and optional conversation_id
        user_id: User ID from JWT token (injected by require_auth dependency)

    Returns:
        ChatResponse with Claude's response, conversation_id, and token usage

    Raises:
        HTTPException:
            - 401: Unauthorized (missing or invalid token)
            - 429: Rate limit exceeded
            - 503: Service unavailable
            - 500: API or unexpected error
    """
    try:
        # Determine which model to use:
        # 1. If request includes a model override (frontend selection on new conversation), use that
        # 2. If conversation exists and has a model set, use that
        # 3. Otherwise, fall back to user's default preference
        conversation_model = None
        if request.conversation_id:
            existing_conv = get_conversation(request.conversation_id)
            if existing_conv:
                conversation_model = existing_conv.get("model")

        # Get user's default model as fallback
        user_settings = get_user_settings(int(user_id))
        user_default_model = user_settings.get("claude_model")

        # Honor the model the frontend sends on every message, enabling mid-conversation
        # upgrades (e.g. Haiku → Sonnet). Fall back to stored conversation model or user default.
        if request.model:
            model_to_use = request.model
        else:
            model_to_use = conversation_model or user_default_model

        # Inject priority stack + recent nudge context at session start (HF-03 web equivalent)
        # Passed as system_context so it goes into the system prompt — never shown in UI
        system_context = None
        if not request.conversation_id and not request.image_b64:
            context_parts = []

            # Priority stack: all active items (no timing filter — Claude reasons from timestamps)
            priority_items = get_priority_items(int(user_id), status='active', limit=15)
            if priority_items:
                level_labels = {2: "critical", 1: "high", 0: "normal"}
                priority_lines = ["[Priority Stack: Your active unresolved items (most urgent first):"]
                for item in priority_items:
                    level_label = level_labels.get(item.get('priority_level', 0), "normal")
                    created = (item.get('created_at') or '')[:10]
                    due_raw = item.get('due_at')
                    due = due_raw[:10] if due_raw else "none"
                    priority_lines.append(
                        f"- (ID: {item.get('id', '?')}, level: {item.get('priority_level', 0)}/{level_label},"
                        f" type: {item.get('item_type', '?')}, created: {created}, due: {due}) {item.get('title', '(no title)')}"
                    )
                priority_lines.append("Call priority_list for full details. Call priority_resolve when something is done.]")
                context_parts.append("\n".join(priority_lines))

            # Recent nudges: last 24h sent/delivered nudges
            recent_nudges = get_recent_nudges(int(user_id), hours=24, limit=3)
            sent_nudges = [n for n in recent_nudges if n.get('status') in ('sent', 'delivered')]
            if sent_nudges:
                nudge_lines = ["[Context: Recent nudges Seny sent me in the last 24h:"]
                for n in sent_nudges:
                    ts = n.get('sent_at') or n.get('created_at', '')
                    nudge_lines.append(f"- (ID: {n.get('id', '?')}, sent: {ts}) {n.get('title') or '(no title)'}: {(n.get('body') or '')[:200]}")
                nudge_lines.append("Use nudge_get with an ID above if you need full details.]")
                context_parts.append("\n".join(nudge_lines))

            # Dismissed nudges: any auto-dismissed as stale in last 24h
            # Surface these so user can correct wrong calls
            def _get_row_value(row, key, index=0):
                if row is None:
                    return None
                if isinstance(row, dict):
                    return row.get(key)
                if hasattr(row, 'get'):
                    try:
                        return row.get(key)
                    except Exception:
                        pass
                try:
                    return row[index]
                except Exception:
                    return None

            try:
                from web.core.database import get_db
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT title, dismiss_reason FROM nudges"
                        " WHERE user_id=%s AND status='dismissed' AND dismiss_reason IS NOT NULL"
                        " AND created_at > NOW() - INTERVAL '24 hours'"
                        " ORDER BY created_at DESC LIMIT 5",
                        (int(user_id),)
                    )
                    dismissed_rows = cursor.fetchall()
                if dismissed_rows:
                    dismissed_count = len(dismissed_rows)
                    dismissed_titles = ", ".join(
                        (_get_row_value(row, 'title') or '(no title)')
                        for row in dismissed_rows
                    )
                    context_parts.append(
                        f"[Dismissed as stale in last 24h: {dismissed_count} nudge(s)"
                        f" — {dismissed_titles}. Mention these if relevant and the user can correct any wrong calls.]"
                    )
            except Exception as _dismissed_err:
                logger.warning("Failed to load dismissed nudges for context: %s", repr(_dismissed_err))

            # LCD Layer 2: recent narrations synthesized into current state
            try:
                from web.services.lcd_service import LCDService
                import asyncio
                lcd_layer2 = await LCDService(int(user_id))._get_layer2_for_context()
                if lcd_layer2:
                    profile = get_user_profile(int(user_id))
                    user_name = profile['user_name']
                    context_parts.insert(0, f"[What {user_name} has told you recently: {lcd_layer2}]")
            except Exception as _lcd_err:
                pass  # Fail-open

            system_context = "\n\n".join(context_parts) if context_parts else None

        # Build user content — multimodal if image present, plain string otherwise
        if request.image_b64:
            user_content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": request.image_media_type or "image/jpeg",
                        "data": request.image_b64,
                    },
                },
                {
                    "type": "text",
                    "text": f"[Attached image: {request.image_file_name or 'image'}]\n\n{request.message}",
                },
            ]
        else:
            user_content = request.message

        # Chat with conversation state management
        # Associate conversation with authenticated user
        response, conversation_id, usage_stats, citations, tools_used, capture_info = await claude_service.chat(
            user_message=user_content,
            conversation_id=request.conversation_id,
            user_id=user_id,
            reply_to_message_id=request.reply_to_message_id,
            timezone=request.timezone,
            slack_workspace=request.slack_workspace,
            model=model_to_use,
            system_context=system_context  # Injected into system prompt, never shown in UI
        )

        # Log usage for cost tracking (includes cache stats)
        log_usage(
            user_id=int(user_id),
            conversation_id=conversation_id,
            input_tokens=usage_stats.get('input_tokens', 0),
            output_tokens=usage_stats.get('output_tokens', 0),
            cache_creation_tokens=usage_stats.get('cache_creation_tokens', 0),
            cache_read_tokens=usage_stats.get('cache_read_tokens', 0),
            model=model_to_use or "claude-sonnet-4-5-20250929",
            tools_used=tools_used
        )

        # For backward compatibility, extract total tokens
        tokens_used = usage_stats.get('total_tokens', 0)

        # Strip any tool-summary tags Claude may have mimicked from history context
        import re
        response = re.sub(r'\n*\s*<tool_calls_made>[\s\S]*?</tool_calls_made>\s*', '', response).strip()
        response = re.sub(r'\n*\s*\[Tools used:[^\]]*\]\s*', '', response).strip()

        # ========================================================
        # Hallucination Detection: Catch when Claude claims to have
        # done something but didn't actually call the tool
        # ========================================================

        # --- AI-powered action hallucination detection (Haiku) ---
        # Detects when Claude claims ANY action (project, task, note, email, etc.)
        # without calling the corresponding tool. Replaces brittle phrase matching.
        is_retry = HALLUCINATION_RETRY_MARKER in request.message or PROJECT_RETRY_MARKER in request.message

        if not is_retry:
            try:
                from web.services.hallucination_detector import HallucinationDetector
                detector = HallucinationDetector()
                detection = await detector.check_response(response, tools_used)

                if detection.get("claimed_action") and detection.get("confidence", 0) >= 0.65:
                    expected_tool = detection.get("expected_tool", "unknown")
                    action_desc = detection.get("action_description", "an action")
                    print(f"[HALLUCINATION DETECTED] Haiku detected: '{action_desc}', expected_tool={expected_tool}, tools_used={tools_used}")

                    # Retry with forced tool instruction
                    retry_message = f"""{HALLUCINATION_RETRY_MARKER}
IMPORTANT: You just claimed to perform an action ({action_desc}) but did NOT call the required tool ({expected_tool}).

Original request: {request.message}

YOU MUST NOW CALL THE APPROPRIATE TOOL. Do NOT respond with text only. Call the tool first, then respond based on the tool result.
"""
                    try:
                        retry_response, _, retry_usage, retry_citations, retry_tools_used, retry_capture = await claude_service.chat(
                            user_message=retry_message,
                            conversation_id=conversation_id,
                            user_id=user_id,
                            timezone=request.timezone,
                            model=model_to_use
                        )

                        if retry_tools_used:
                            print(f"[HALLUCINATION FIX] Retry succeeded, tools_used={retry_tools_used}")
                            response = retry_response
                            tools_used = retry_tools_used
                            citations = retry_citations
                            capture_info = retry_capture
                        else:
                            print(f"[HALLUCINATION PERSIST] Retry also failed, tools_used={retry_tools_used}")
                            response += "\n\n⚠️ *I may not have completed this action — please try asking again.*"
                    except Exception as retry_error:
                        print(f"[HALLUCINATION RETRY ERROR] {retry_error}")
                        response += "\n\n⚠️ *I may not have completed this action — please try asking again.*"

            except Exception as detector_error:
                # Detection failure should never break the chat
                print(f"[HALLUCINATION DETECTOR ERROR] {detector_error}")

        elif is_retry:
            # Already a retry — if still no tools, warn user
            response_lower_check = response.lower()
            response_words_check = set(response_lower_check.split())
            action_verbs = {"created", "updated", "deleted", "removed", "completed", "finished", "marked", "changed", "set", "sent", "added"}
            if not tools_used and bool(response_words_check & action_verbs):
                print(f"[HALLUCINATION DETECTED] Already a retry, adding warning. tools_used={tools_used}")
                response += "\n\n⚠️ *I may not have completed this action — please try asking again.*"

        # --- Data fabrication detection (phrase-based, kept as-is) ---
        response_lower = response.lower()

        # Slack hallucination detection: Claude shows messages without calling slack_read
        slack_message_indicators = [
            "here are the messages", "here's what", "recent messages",
            "from the channel", "in #", "said:", "wrote:", "posted:",
            "[", "]", "@"  # Message formatting patterns
        ]
        # Check if response looks like it contains Slack messages
        has_slack_context = "slack" in response_lower or "#" in response
        looks_like_messages = sum(1 for p in slack_message_indicators if p in response_lower) >= 3
        used_slack_read = 'slack_read' in tools_used

        if has_slack_context and looks_like_messages and not used_slack_read and "message" in response_lower:
            print(f"[HALLUCINATION DETECTED] Claude appears to show Slack messages but didn't call slack_read, tools_used={tools_used}")
            response += "\n\n⚠️ *I may have made up these messages — please ask me again to actually fetch them from Slack.*"

        # Slack hallucination detection: Claude claims a channel exists without verifying
        channel_exists_phrases = [
            "there is a #", "there is a channel", "yes, there is", "yes! there is",
            "channel in your", "workspace. it has", "members."
        ]
        claims_channel_exists = sum(1 for p in channel_exists_phrases if p in response_lower) >= 2
        used_slack_list = 'slack_list_channels' in tools_used

        if claims_channel_exists and not used_slack_list and "#" in response:
            print(f"[HALLUCINATION DETECTED] Claude claims channel exists without calling slack_list_channels, tools_used={tools_used}")
            response += "\n\n⚠️ *I didn't actually verify this channel exists — please ask me to list your channels to confirm.*"

        # Slack hallucination detection: Claude claims a channel DOESN'T exist without verifying
        channel_not_exists_phrases = [
            "don't see a channel", "don't see that channel", "don't see a #",
            "i don't see", "couldn't find", "can't find", "cannot find",
            "channel not found", "channel doesn't exist", "channel does not exist",
            "no channel called", "no channel named", "there is no #", "there isn't a #",
            "not a channel", "isn't a channel"
        ]
        slack_tools = {'slack_list_channels', 'slack_read', 'slack_search', 'slack_list_dms'}
        claims_channel_not_exists = any(p in response_lower for p in channel_not_exists_phrases)
        used_any_slack_tool = bool(set(tools_used) & slack_tools)
        has_channel_context = "channel" in response_lower or "#" in response

        if claims_channel_not_exists and not used_any_slack_tool and has_channel_context:
            print(f"[HALLUCINATION DETECTED] Claude claims channel doesn't exist without calling any Slack tool, tools_used={tools_used}")
            response += "\n\n⚠️ *I didn't actually check for this channel — please ask me to search or list your channels to verify.*"

        # Slack hallucination detection: Claude claims to send/will send message without calling tool (Issue #10)
        send_message_phrases = [
            "i'll send", "i will send", "i've sent", "i have sent",
            "sending a message", "send the message", "message sent",
            "i'll post", "i will post", "i've posted", "posting to"
        ]
        claims_send_message = any(p in response_lower for p in send_message_phrases)
        used_slack_send = 'slack_send' in tools_used
        has_slack_send_context = "slack" in response_lower or "#" in response or "channel" in response_lower

        if claims_send_message and not used_slack_send and has_slack_send_context:
            print(f"[HALLUCINATION DETECTED] Claude claims to send Slack message without calling slack_send, tools_used={tools_used}")
            response += "\n\n⚠️ *I didn't actually send this message — please ask me again and I'll verify the channel first.*"

        # Telegram hallucination detection: Claude claims to read/search without calling tools
        telegram_tools = {'telegram_read', 'telegram_search', 'telegram_send', 'telegram_list_chats'}
        telegram_read_phrases = [
            "i looked for", "i searched", "i checked", "couldn't find",
            "can't find", "cannot find", "no messages", "don't see any",
            "i read", "i found", "here are the messages", "recent messages from"
        ]
        claims_telegram_read = any(p in response_lower for p in telegram_read_phrases)
        has_telegram_context = "telegram" in response_lower
        used_telegram_tool = bool(set(tools_used) & telegram_tools)

        if claims_telegram_read and has_telegram_context and not used_telegram_tool:
            print(f"[HALLUCINATION DETECTED] Claude claims Telegram action without calling any Telegram tool, tools_used={tools_used}")
            response += "\n\n⚠️ *I didn't actually check Telegram — please ask me again and I'll fetch the real data.*"

        # Nudge hallucination detection: Claude claims nudge history without calling nudge_list
        nudge_state_phrases = [
            "i sent you", "i've sent", "i have sent", "i sent a nudge", "i sent a reminder",
            "i nudged you", "haven't sent", "haven't nudged", "didn't send a nudge",
            "no nudges", "i haven't reminded", "i've been reminding", "last nudge i sent",
            "nudge i sent", "reminder i sent"
        ]
        nudge_tools = {'nudge_list', 'nudge_get'}
        claims_nudge_history = any(p in response_lower for p in nudge_state_phrases)
        used_nudge_tool = bool(set(tools_used) & nudge_tools)
        has_nudge_context = "nudge" in response_lower or "reminder" in response_lower

        if claims_nudge_history and not used_nudge_tool and has_nudge_context:
            print(f"[HALLUCINATION DETECTED] Claude claims nudge history without calling nudge_list/nudge_get, tools_used={tools_used}")
            response += "\n\n⚠️ *I didn't actually check my nudge history — please ask me to use nudge_list to verify what I've sent.*"

        # Contacts hallucination detection: Claude claims full details without calling contacts_get
        full_details_phrases = [
            "all details", "full details", "complete details", "all information",
            "here's everything", "all the info", "full contact", "complete info"
        ]
        address_phrases = ["address", "lives at", "located at", "street", "city", "zip"]
        birthday_phrases = ["birthday", "born on", "birth date", "date of birth"]
        notes_phrases = ["notes:", "note:", "additional notes"]

        claims_full_details = any(p in response_lower for p in full_details_phrases)
        claims_address = any(p in response_lower for p in address_phrases)
        claims_birthday = any(p in response_lower for p in birthday_phrases)
        claims_notes = any(p in response_lower for p in notes_phrases)
        used_contacts_get = 'contacts_get' in tools_used
        has_contact_context = 'contacts_search' in tools_used or "contact" in response_lower

        # If claiming full details or specific fields that require contacts_get
        if has_contact_context and not used_contacts_get:
            if claims_full_details or claims_address or claims_birthday or claims_notes:
                print(f"[HALLUCINATION DETECTED] Claude claims contact details without calling contacts_get, tools_used={tools_used}")
                response += "\n\n⚠️ *I should have fetched the full contact details — please ask again and I'll call the right tool.*"

        # YouTube hallucination detection
        youtube_tools = {'youtube_subscriptions', 'youtube_playlists', 'youtube_liked'}
        youtube_subscription_phrases = [
            "subscribed to", "your subscriptions", "channels you follow",
            "you're subscribed", "you are subscribed", "subscription list"
        ]
        youtube_playlist_phrases = [
            "your playlists", "you have these playlists", "playlist called",
            "playlists include", "playlists are", "here are your playlists",
            "playlist:", "playlists:", "don't have any playlists", "no playlists"
        ]
        youtube_liked_phrases = [
            "liked videos", "videos you've liked", "videos you liked",
            "your likes", "recently liked", "favorite videos"
        ]

        has_youtube_context = "youtube" in response_lower
        claims_subscriptions = any(p in response_lower for p in youtube_subscription_phrases)
        claims_playlists = any(p in response_lower for p in youtube_playlist_phrases)
        claims_liked = any(p in response_lower for p in youtube_liked_phrases)
        used_youtube_tool = bool(set(tools_used) & youtube_tools)

        if has_youtube_context and not used_youtube_tool:
            if claims_subscriptions or claims_playlists or claims_liked:
                print(f"[HALLUCINATION DETECTED] Claude claims YouTube data without calling YouTube tool, tools_used={tools_used}")
                response += "\n\n⚠️ *I didn't actually check your YouTube data — please ask again and I'll fetch the real information.*"

        # Note: Project/task action hallucination is now handled by the Haiku-based
        # detector above. The data-fabrication detectors below remain phrase-based.

        return ChatResponse(
            response=response,
            conversation_id=conversation_id,
            tokens_used=tokens_used,
            citations=[Citation(**c) for c in (citations or [])],
            tools_used=tools_used or [],
            capture_info=CaptureInfo(**capture_info) if (capture_info and isinstance(capture_info, dict)) else None
        )

    except ClaudeServiceError as e:
        logger.exception("ClaudeServiceError while handling /api/chat")
        error_msg = str(e)
        if "rate limit" in error_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Please wait a moment and try again."
            )
        elif "connection" in error_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Connection error. Please check your internet connection."
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Service error: {error_msg}"
            )

    except APIError as e:
        logger.exception("Anthropic APIError while handling /api/chat")
        # Handle Anthropic API errors
        status_code = getattr(e, 'status_code', 500)

        if status_code == 429:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Please wait a moment and try again."
            )
        elif status_code == 503:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Connection error. Please check your internet connection."
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"API error: {str(e)}"
            )

    except Exception as e:
        # Catch any unexpected errors
        import traceback
        logger.exception("Unexpected error while handling /api/chat")
        print(f"[CHAT ERROR] {type(e).__name__}: {str(e)}\n{traceback.format_exc()}", flush=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error: {str(e)}"
        )


# ============================================================================
# Usage Tracking Endpoints
# ============================================================================

class UsageSummary(BaseModel):
    """Usage summary response model."""
    total_requests: int
    total_input_tokens: int
    total_output_tokens: int
    total_cache_write_tokens: int
    total_cache_read_tokens: int
    total_cost_usd: float
    cache_hit_rate: float
    daily_breakdown: list


@router.get("/usage", response_model=UsageSummary)
async def get_usage(days: int = 7, user_id: str = Depends(require_auth)):
    """
    Get usage summary for the authenticated user.

    Args:
        days: Number of days to look back (default 7)
        user_id: User ID from JWT token

    Returns:
        UsageSummary with token usage, costs, and cache hit rate
    """
    try:
        summary = get_usage_summary(int(user_id), days)
        return UsageSummary(**summary)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error fetching usage: {str(e)}"
        )


# ============================================================================
# Memory Endpoints
# ============================================================================

@router.get("/memories")
async def list_memories(user_id: str = Depends(require_auth)):
    """
    List all memories for the authenticated user.

    Returns:
        Dict with list of memory objects
    """
    from web.services.memory_service import MemoryService
    memories = MemoryService.get_memories(int(user_id))
    return {"memories": memories}


@router.delete("/memories/{memory_id}")
async def delete_memory(memory_id: int, user_id: str = Depends(require_auth)):
    """
    Delete a specific memory for the authenticated user.

    Args:
        memory_id: The memory's unique identifier

    Returns:
        Success confirmation

    Raises:
        HTTPException 404: Memory not found or not owned by user
    """
    from web.services.memory_service import MemoryService
    deleted = MemoryService.delete_memory(int(user_id), memory_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"success": True}
