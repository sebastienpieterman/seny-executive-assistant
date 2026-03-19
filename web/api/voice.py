"""Voice API endpoints for Seny voice hub communication."""
import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from web.auth.jwt_utils import require_auth
from web.core.database import (
    get_active_voice_session,
    create_voice_session,
    update_voice_session_activity,
    end_voice_session,
    create_conversation,
    save_message,
    get_conversation_messages
)
from web.services.claude_service import ClaudeService

logger = logging.getLogger(__name__)

router = APIRouter()

# Reuse the global Claude service
claude_service = ClaudeService()


class VoiceMessageRequest(BaseModel):
    """Request body for voice messages."""
    message: str
    conversation_id: Optional[str] = None
    satellite_id: Optional[str] = None
    source: str = "voice"


class VoiceMessageResponse(BaseModel):
    """Response for voice messages."""
    response: str
    conversation_id: str
    session_id: int


@router.post("/message", response_model=VoiceMessageResponse)
async def voice_message(
    request: VoiceMessageRequest,
    user_id: str = Depends(require_auth)
):
    """Process a voice message from the Windows hub.

    This endpoint:
    1. Finds or creates a voice session for context continuity
    2. Sends the message to Claude
    3. Returns the response for TTS synthesis

    Voice sessions maintain context for 5 minutes, allowing
    natural follow-up questions like "What about tomorrow?"
    """
    uid = int(user_id)

    # Step 1: Find or create voice session
    session = get_active_voice_session(uid, request.satellite_id)

    if session:
        # Existing session - reuse conversation for context
        conversation_id = session["conversation_id"]
        session_id = session["id"]
        update_voice_session_activity(session_id)
        logger.info(f"Resuming voice session {session_id} for user {uid}")
    else:
        # New session - create conversation
        conversation_id = request.conversation_id
        if not conversation_id:
            # Generate new conversation
            conversation_id = str(uuid.uuid4())
            create_conversation(uid, conversation_id, title="Voice Conversation")

        session_id = create_voice_session(uid, conversation_id, request.satellite_id)
        logger.info(f"Created voice session {session_id} for user {uid}")

    # Step 2: Send to Claude using the same chat flow as regular chat
    try:
        response_text, _, usage_stats, citations, tools_used, capture_info = await claude_service.chat(
            user_message=request.message,
            conversation_id=conversation_id,
            user_id=user_id,
            timezone="America/Chicago"  # Could be made configurable later
        )
    except Exception as e:
        logger.error(f"Claude error: {e!r}")
        raise HTTPException(500, "Failed to get AI response")

    # Step 3: Return response
    return VoiceMessageResponse(
        response=response_text,
        conversation_id=conversation_id,
        session_id=session_id
    )


@router.post("/end-session")
async def end_session_endpoint(
    session_id: int,
    user_id: str = Depends(require_auth)
):
    """Explicitly end a voice session.

    Called when a satellite disconnects or user says "goodbye".
    """
    end_voice_session(session_id)
    return {"status": "ok"}


@router.get("/health")
async def voice_health():
    """Health check for voice API."""
    return {"status": "ok", "service": "voice"}


class OpenAIMessage(BaseModel):
    """A single message in OpenAI chat format."""
    role: str
    content: str


class OpenAIChatRequest(BaseModel):
    """OpenAI-compatible chat completions request."""
    model: str = "seny"
    messages: List[OpenAIMessage]
    stream: bool = False


@router.get("/openai/models")
async def openai_list_models(
    user_id: str = Depends(require_auth)
):
    """OpenAI-compatible models list endpoint.

    Extended OpenAI Conversation calls this during setup to validate credentials.
    Returns a single 'seny' model entry in OpenAI format.
    """
    return {
        "object": "list",
        "data": [
            {
                "id": "seny",
                "object": "model",
                "created": 1700000000,
                "owned_by": "seny"
            }
        ]
    }


@router.post("/openai/chat/completions")
async def openai_chat_completions(
    request: OpenAIChatRequest,
    user_id: str = Depends(require_auth)
):
    """OpenAI-compatible endpoint for Home Assistant Extended OpenAI Conversation.

    HA sends transcribed speech in OpenAI chat format.
    We forward to Seny/Claude and return the response in OpenAI format.
    """
    uid = int(user_id)

    # Extract the last user message
    user_message = ""
    for msg in reversed(request.messages):
        if msg.role == "user":
            user_message = msg.content
            break

    if not user_message:
        raise HTTPException(400, "No user message found in request")

    # Find or create a voice session for conversation context
    # Use satellite_id="ha-assist" to keep HA sessions distinct from hub sessions
    session = get_active_voice_session(uid, satellite_id="ha-assist")
    if session:
        conversation_id = session["conversation_id"]
        session_id = session["id"]
        update_voice_session_activity(session_id)
    else:
        conversation_id = str(uuid.uuid4())
        create_conversation(uid, conversation_id, title="Voice (Home Assistant)")
        session_id = create_voice_session(uid, conversation_id, satellite_id="ha-assist")

    # Sync conversation history from HA's messages array into our DB.
    # HA sends the FULL conversation history on every request. If our session
    # expired or was recreated, re-seed the DB from HA's history so Claude
    # always has context — regardless of session state.
    prior_exchange = [
        msg for msg in request.messages[:-1]
        if msg.role in ("user", "assistant")
    ]
    if prior_exchange:
        existing = get_conversation_messages(conversation_id)
        existing_count = len(existing)
        if len(prior_exchange) > existing_count:
            for msg in prior_exchange[existing_count:]:
                save_message(conversation_id, msg.role, msg.content)

    # Send to Claude
    try:
        response_text, _, _, _, _, _ = await claude_service.chat(
            user_message=user_message,
            conversation_id=conversation_id,
            user_id=user_id,
            timezone="America/Chicago",
            voice_mode=True
        )
    except Exception as e:
        logger.error(f"Claude error in OpenAI endpoint: {e!r}")
        raise HTTPException(500, "Failed to get AI response")

    # Return in OpenAI format
    return {
        "id": f"seny-{session_id}",
        "object": "chat.completion",
        "model": "seny",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": response_text
            },
            "finish_reason": "stop"
        }]
    }
