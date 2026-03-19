"""
Slack Events API Webhook - Phase 21-02

Receives Events API callbacks from Slack for real-time DM message processing.
Replaces polling when SLACK_SIGNING_SECRET is configured.

Security:
- HMAC-SHA256 signature verification using SLACK_SIGNING_SECRET
- Timestamp validation prevents replay attacks (5-minute window)
- Returns 200 quickly, processes messages in background (Slack requires < 3s response)
"""

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_signing_secret() -> Optional[str]:
    """Get the Slack signing secret from environment."""
    return os.getenv("SLACK_SIGNING_SECRET")


def is_events_mode() -> bool:
    """Check if Slack Events API mode is configured."""
    return bool(_get_signing_secret())


async def _verify_slack_signature(request: Request) -> bytes:
    """
    Verify Slack HMAC-SHA256 request signature.

    Raises HTTPException 401 if signature is missing, invalid, or timestamp is too old.

    Returns:
        Request body bytes if signature is valid.
    """
    signing_secret = _get_signing_secret()
    if not signing_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Slack Events API not configured"
        )

    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    signature = request.headers.get("X-Slack-Signature")

    if not timestamp or not signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Slack signature headers"
        )

    # Reject requests older than 5 minutes (replay attack prevention)
    try:
        req_timestamp = int(timestamp)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid timestamp format"
        )

    if abs(time.time() - req_timestamp) > 300:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Request timestamp too old (replay attack prevention)"
        )

    body = await request.body()

    # Compute expected HMAC-SHA256 signature
    sig_basestring = f"v0:{timestamp}:{body.decode()}"
    computed = "v0=" + hmac.new(
        signing_secret.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()

    # Constant-time comparison prevents timing attacks
    if not hmac.compare_digest(computed, signature):
        logger.warning("Slack Events: invalid HMAC signature")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature"
        )

    return body


async def _process_slack_event(payload: dict) -> None:
    """
    Process a Slack event payload in the background.

    Routes DM message events to SlackEventsHandler.

    Args:
        payload: Slack Events API event_callback payload
    """
    from web.services.slack_events_handler import SlackEventsHandler

    try:
        event = payload.get("event", {})
        team_id = payload.get("team_id")

        if not team_id:
            logger.warning("Slack Events: payload missing team_id")
            return

        handler = SlackEventsHandler()
        await handler.handle_message_event(event, team_id)
    except Exception as e:
        logger.error(f"Error processing Slack event: {repr(e)}")


@router.post("/slack/events")
async def slack_events(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """
    Slack Events API webhook endpoint.

    Receives events from Slack (DM messages, etc.) via Events API.
    Verifies HMAC-SHA256 signature and processes messages in background.

    Security: Rejects requests with invalid signatures or old timestamps.
    Performance: Returns 200 immediately, processes events in background.
    """
    # Verify HMAC signature and get body
    body = await _verify_slack_signature(request)

    try:
        payload = json.loads(body)
    except Exception as e:
        logger.warning(f"Slack Events: malformed JSON: {repr(e)}")
        return {"ok": True, "error": "malformed_json"}

    event_type = payload.get("type")

    # Handle URL verification challenge (Slack sends this during initial app setup)
    if event_type == "url_verification":
        challenge = payload.get("challenge")
        logger.info("Slack Events: responding to URL verification challenge")
        return {"challenge": challenge}

    # Handle event callbacks
    if event_type == "event_callback":
        # Record that a valid Slack event was received (used by health monitoring)
        from web.services.webhook_health_service import record_slack_event_received
        record_slack_event_received()

        event = payload.get("event", {})
        if event.get("type") == "message" and event.get("channel_type") == "im":
            logger.debug(f"Slack Events: queuing DM from channel {event.get('channel')}")
            background_tasks.add_task(_process_slack_event, payload)

    # Return 200 immediately (Slack requires < 3 second response)
    return {"ok": True}
