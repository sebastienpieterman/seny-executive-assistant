"""
Nudge API - Phase 16-03

Provides endpoints for viewing nudge history, managing preferences,
checking nudge system status, and dismissing nudges.
"""

import logging
import re
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, field_validator

from web.auth.jwt_utils import require_auth
from web.core.database import (
    get_db,
    get_recent_nudges,
    get_nudge_preferences,
    update_nudge_preferences,
    update_nudge_status,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ============================================================================
# Pydantic Models
# ============================================================================

class NudgePreferencesUpdate(BaseModel):
    """Request model for updating nudge preferences."""
    nudge_enabled: Optional[bool] = None
    nudge_quiet_start: Optional[str] = None
    nudge_quiet_end: Optional[str] = None
    nudge_max_urgent_per_hour: Optional[int] = None
    nudge_batch_interval_minutes: Optional[int] = None
    nudge_channels: Optional[List[str]] = None
    nudge_batch_channel: Optional[str] = None
    nudge_quiet_skip_weekend: Optional[bool] = None
    nudge_smart_dedup: Optional[bool] = None
    pending_action_notification_channel: Optional[str] = None

    @field_validator('pending_action_notification_channel')
    @classmethod
    def validate_pending_action_channel(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        valid = {'telegram', 'slack', 'none'}
        if v not in valid:
            return None  # Ignore invalid values
        return v

    @field_validator('nudge_quiet_start', 'nudge_quiet_end')
    @classmethod
    def validate_time_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not re.match(r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$', v):
            raise ValueError('Time must be in HH:MM format (e.g., "22:00")')
        return v

    @field_validator('nudge_max_urgent_per_hour')
    @classmethod
    def validate_max_urgent(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return v
        if v < 1 or v > 20:
            raise ValueError('nudge_max_urgent_per_hour must be between 1 and 20')
        return v

    @field_validator('nudge_batch_interval_minutes')
    @classmethod
    def validate_batch_interval(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return v
        # Allow 60 to 720 minutes (1 to 12 hours)
        if v < 60 or v > 720:
            raise ValueError('nudge_batch_interval_minutes must be between 60 and 720 (1-12 hours)')
        return v

    @field_validator('nudge_channels', 'nudge_batch_channel')
    @classmethod
    def validate_channels(cls, v):
        if v is None:
            return v
        valid_channels = {'telegram', 'slack', 'push', 'email'}
        # Handle both list (nudge_channels) and string (nudge_batch_channel)
        channels_to_check = v if isinstance(v, list) else [v]
        for channel in channels_to_check:
            if channel not in valid_channels:
                raise ValueError(f'Invalid channel "{channel}". Must be one of: {", ".join(sorted(valid_channels))}')
        return v


class NudgeResponse(BaseModel):
    """Response model for a single nudge."""
    id: int
    nudge_type: str
    channel: str
    title: str
    body: Optional[str]
    urgency: str
    status: str
    source_type: Optional[str]
    source_id: Optional[int]
    batch_id: Optional[str]
    created_at: str
    sent_at: Optional[str]
    delivered_at: Optional[str]
    acted_at: Optional[str]


class NudgeListResponse(BaseModel):
    """Response model for nudge list."""
    nudges: List[dict]
    count: int


class NudgePreferencesResponse(BaseModel):
    """Response model for nudge preferences."""
    nudge_enabled: bool
    nudge_quiet_start: str
    nudge_quiet_end: str
    nudge_max_urgent_per_hour: int
    nudge_batch_interval_minutes: int
    nudge_channels: List[str]
    nudge_last_batch_at: Optional[str]


class NudgeStatusResponse(BaseModel):
    """Response model for nudge system status."""
    enabled: bool
    last_urgent_run: Optional[str]
    last_batch_run: Optional[str]
    nudges_today: int
    preferred_channels: dict


# ============================================================================
# Endpoints
# ============================================================================

@router.get("")
async def get_nudges(
    user_id: str = Depends(require_auth),
    hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=50, ge=1, le=200),
    status_filter: Optional[str] = Query(default=None, alias="status"),
) -> NudgeListResponse:
    """
    Get recent nudge history for the authenticated user.

    Args:
        hours: How far back to look (default 24, max 168 = 1 week)
        limit: Maximum number of nudges to return (default 50, max 200)
        status_filter: Optional filter by status (pending, sent, delivered, acted, dismissed, failed)
    """
    uid = int(user_id)

    nudges = get_recent_nudges(uid, hours=hours, limit=limit)

    # Apply optional status filter
    if status_filter:
        nudges = [n for n in nudges if n.get('status') == status_filter]

    return NudgeListResponse(nudges=nudges, count=len(nudges))


@router.get("/preferences")
async def get_preferences(
    user_id: str = Depends(require_auth),
) -> dict:
    """
    Get nudge preferences for the authenticated user.
    """
    uid = int(user_id)
    prefs = get_nudge_preferences(uid)

    # Parse channels JSON if stored as string
    channels = prefs.get('nudge_channels', '["push"]')
    if isinstance(channels, str):
        import json
        try:
            channels = json.loads(channels)
        except json.JSONDecodeError:
            channels = ['push']

    return {
        "nudge_enabled": bool(prefs.get('nudge_enabled', True)),
        "nudge_quiet_start": prefs.get('nudge_quiet_start', '22:00'),
        "nudge_quiet_end": prefs.get('nudge_quiet_end', '08:00'),
        "nudge_max_urgent_per_hour": prefs.get('nudge_max_urgent_per_hour', 3),
        "nudge_batch_interval_minutes": prefs.get('nudge_batch_interval_minutes', 60),
        "nudge_channels": channels,
        "nudge_batch_channel": prefs.get('nudge_batch_channel', 'push'),
        "nudge_last_batch_at": prefs.get('nudge_last_batch_at'),
        "pending_action_notification_channel": prefs.get('pending_action_notification_channel', 'none'),
        "nudge_quiet_skip_weekend": bool(prefs.get('nudge_quiet_skip_weekend', False)),
        "nudge_smart_dedup": bool(prefs.get('nudge_smart_dedup', True)),
    }


@router.put("/preferences")
async def update_preferences(
    prefs_update: NudgePreferencesUpdate,
    user_id: str = Depends(require_auth),
) -> dict:
    """
    Update nudge preferences for the authenticated user.

    All fields are optional - only provided fields will be updated.
    """
    uid = int(user_id)

    # Build kwargs from non-None fields
    update_kwargs = {}

    if prefs_update.nudge_enabled is not None:
        update_kwargs['nudge_enabled'] = int(prefs_update.nudge_enabled)

    if prefs_update.nudge_quiet_start is not None:
        update_kwargs['nudge_quiet_start'] = prefs_update.nudge_quiet_start

    if prefs_update.nudge_quiet_end is not None:
        update_kwargs['nudge_quiet_end'] = prefs_update.nudge_quiet_end

    if prefs_update.nudge_max_urgent_per_hour is not None:
        update_kwargs['nudge_max_urgent_per_hour'] = prefs_update.nudge_max_urgent_per_hour

    if prefs_update.nudge_batch_interval_minutes is not None:
        update_kwargs['nudge_batch_interval_minutes'] = prefs_update.nudge_batch_interval_minutes

    if prefs_update.nudge_channels is not None:
        import json
        update_kwargs['nudge_channels'] = json.dumps(prefs_update.nudge_channels)

    if prefs_update.nudge_batch_channel is not None:
        update_kwargs['nudge_batch_channel'] = prefs_update.nudge_batch_channel

    if prefs_update.nudge_quiet_skip_weekend is not None:
        update_kwargs['nudge_quiet_skip_weekend'] = int(prefs_update.nudge_quiet_skip_weekend)

    if prefs_update.nudge_smart_dedup is not None:
        update_kwargs['nudge_smart_dedup'] = int(prefs_update.nudge_smart_dedup)

    if prefs_update.pending_action_notification_channel is not None:
        valid = {'telegram', 'slack', 'none'}
        if prefs_update.pending_action_notification_channel in valid:
            update_kwargs['pending_action_notification_channel'] = prefs_update.pending_action_notification_channel

    if not update_kwargs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No preferences provided to update"
        )

    try:
        update_nudge_preferences(uid, **update_kwargs)
    except Exception as e:
        logger.error("Failed to update nudge preferences for user %d: %s", uid, repr(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update preferences"
        )

    # Return updated preferences
    return await get_preferences(user_id=user_id)


@router.get("/status")
async def get_nudge_status(
    user_id: str = Depends(require_auth),
) -> dict:
    """
    Get nudge system status for the authenticated user.

    Returns:
        - enabled: Whether nudges are enabled
        - last_urgent_run: When urgent nudges were last processed
        - last_batch_run: When batch nudges were last sent
        - nudges_today: Count of nudges sent today
        - preferred_channels: {urgent: str, batch: str}
    """
    import json
    uid = int(user_id)

    prefs = get_nudge_preferences(uid)

    # Get nudges from today
    nudges_today = get_recent_nudges(uid, hours=24, limit=1000)
    today = datetime.now().date()
    nudges_today = [
        n for n in nudges_today
        if n.get('created_at') and
        datetime.fromisoformat(n['created_at'].replace('Z', '+00:00')).date() == today
    ]

    # Parse channels
    channels = prefs.get('nudge_channels', '["push"]')
    if isinstance(channels, str):
        try:
            channels = json.loads(channels)
        except json.JSONDecodeError:
            channels = ['push']

    # Determine preferred channels
    urgent_channel = channels[0] if channels else 'push'
    batch_channel = 'push'  # Batch always uses push by default

    return {
        "enabled": bool(prefs.get('nudge_enabled', True)),
        "last_urgent_run": None,  # Would need scheduler state to track this
        "last_batch_run": prefs.get('nudge_last_batch_at'),
        "nudges_today": len(nudges_today),
        "preferred_channels": {
            "urgent": urgent_channel,
            "batch": batch_channel,
        },
    }


@router.post("/{nudge_id}/dismiss")
async def dismiss_nudge(
    nudge_id: int,
    user_id: str = Depends(require_auth),
) -> dict:
    """
    Mark a nudge as dismissed.

    Args:
        nudge_id: ID of the nudge to dismiss
    """
    uid = int(user_id)

    # Verify the nudge belongs to this user
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, status, user_id FROM nudges WHERE id = %s AND user_id = %s",
                (nudge_id, uid)
            )
            nudge = cursor.fetchone()
    except Exception as e:
        logger.error("Failed to fetch nudge %d: %s", nudge_id, repr(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch nudge"
        )

    if not nudge:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Nudge not found"
        )

    nudge_dict = dict(nudge)
    if nudge_dict.get('status') == 'dismissed':
        return {"status": "already_dismissed", "nudge_id": nudge_id}

    # Update status to dismissed
    try:
        update_nudge_status(nudge_id, 'dismissed')
    except Exception as e:
        logger.error("Failed to dismiss nudge %d: %s", nudge_id, repr(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to dismiss nudge"
        )

    return {"status": "dismissed", "nudge_id": nudge_id}


# ============================================================================
# User Response Tracking
# ============================================================================

class NudgeRespondRequest(BaseModel):
    """Request model for responding to a nudge."""
    response: str
    snooze_minutes: Optional[int] = None

    @field_validator('response')
    @classmethod
    def validate_response(cls, v: str) -> str:
        valid_responses = {'helpful', 'dismissed', 'snoozed'}
        if v not in valid_responses:
            raise ValueError(f'Invalid response "{v}". Must be one of: {", ".join(sorted(valid_responses))}')
        return v

    @field_validator('snooze_minutes')
    @classmethod
    def validate_snooze_minutes(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            if v < 5 or v > 10080:  # 5 minutes to 7 days
                raise ValueError('snooze_minutes must be between 5 and 10080 (7 days)')
        return v


@router.post("/{nudge_id}/respond")
async def respond_to_nudge(
    nudge_id: int,
    request: NudgeRespondRequest,
    user_id: str = Depends(require_auth),
) -> dict:
    """
    Record user's response to a nudge.

    This endpoint updates the nudge's user_response field and also records
    the feedback to the user_feedback table for pattern learning.

    Args:
        nudge_id: ID of the nudge to respond to
        request: NudgeRespondRequest with response type and optional snooze_minutes

    Returns:
        { success: true, nudge_id: int, response: str }
    """
    from web.services.nudge_service import NudgeService

    uid = int(user_id)

    # Calculate snooze_until if snoozed
    snooze_until = None
    if request.response == 'snoozed' and request.snooze_minutes:
        snooze_until = (
            datetime.now() + timedelta(minutes=request.snooze_minutes)
        ).isoformat()

    nudge_service = NudgeService(uid)
    result = nudge_service.record_response(
        nudge_id=nudge_id,
        response_type=request.response,
        snooze_until=snooze_until,
    )

    if not result.get('success'):
        error_msg = result.get('error', 'Unknown error')
        if 'not found' in error_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error_msg
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_msg
        )

    return {
        "success": True,
        "nudge_id": nudge_id,
        "response": request.response,
    }
