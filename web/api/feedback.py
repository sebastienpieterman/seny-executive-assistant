"""
Feedback API - Phase 17-01, updated Phase 18-01

Provides endpoints for recording and retrieving user feedback on intelligence items.
Enables pattern learning from explicit user reactions to nudges, detected actions,
and other intelligence system outputs.

Phase 18-01 adds email-based feedback via secure tokens for digest email links.
"""

import json
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, field_validator

from web.auth.jwt_utils import require_auth
from web.core.database import (
    record_feedback,
    get_recent_feedback,
    get_feedback_stats,
    validate_and_consume_email_feedback_token,
    peek_email_feedback_token,
    add_ignored_sender,
    get_ignored_senders,
    remove_ignored_sender,
    get_pattern_preferences,
    get_suppression_overrides,
    reset_suppression_override,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ============================================================================
# Constants
# ============================================================================

VALID_ITEM_TYPES = {
    'nudge',
    'detected_action',
    'needs_reply',
    'unfulfilled_commitment',
    'cross_source_connection',
    'open_loop',
}

VALID_FEEDBACK_TYPES = {
    'helpful',
    'not_helpful',
    'too_much',
    'snooze',
    'accurate',
    'inaccurate',
    'more_like_this',
    'less_like_this',
    'ignore_sender', # : Used for email digest feedback
    'already_handled', # : Loop closure signal — user acted before nudge fired
}

# Plain-English labels for all known nudge item types
# NOTE: Do not import from claude_service.py — circular dependency risk. Keep this in sync manually.
_NUDGE_TYPE_LABELS = {
    'priority_context':            'Priority reminders',
    'detected_action':             'Detected tasks',
    'relationship_check':          'Relationship check-ins',
    'open_followup':               'Open follow-ups',
    'meeting_prep':                'Meeting prep',
    'overdue_task':                'Overdue tasks',
    'urgent_item':                 'Urgent items',
    'relationship_checkin_prompt': 'Family check-ins',
    'nudge_followup':              'Nudge follow-ups',
    'needs_reply':                 'Needs reply',
    'unfulfilled_commitment':      'Unfulfilled commitments',
    'cross_source_connection':     'Cross-source connections',
    'open_loop':                   'Open loops',
    'nudge':                       'General nudges (legacy)',
    'email_draft':               'Email drafts',
    'calendar_proposal':         'Calendar proposals',
    'task_proposal':             'Task proposals',
}

_VALID_ITEM_TYPES = set(_NUDGE_TYPE_LABELS.keys())


def _score_label(score: float) -> str:
    """Convert a numeric preference score to a plain-English description."""
    if score >= 0.5:
        return 'Well received'
    if score >= 0.1:
        return 'Mostly positive'
    if score > -0.1:
        return 'Neutral'
    if score > -0.5:
        return 'Sometimes dismissed'
    return 'Frequently dismissed'


# ============================================================================
# Pydantic Models
# ============================================================================

class FeedbackRequest(BaseModel):
    """Request model for submitting feedback."""
    item_type: str
    item_id: Optional[int] = None
    feedback_type: str
    context: Optional[dict] = None
    reason: Optional[str] = None
    item_context: Optional[str] = None

    @field_validator('item_type')
    @classmethod
    def validate_item_type(cls, v: str) -> str:
        if v not in VALID_ITEM_TYPES:
            raise ValueError(
                f'Invalid item_type "{v}". Must be one of: {", ".join(sorted(VALID_ITEM_TYPES))}'
            )
        return v

    @field_validator('feedback_type')
    @classmethod
    def validate_feedback_type(cls, v: str) -> str:
        if v not in VALID_FEEDBACK_TYPES:
            raise ValueError(
                f'Invalid feedback_type "{v}". Must be one of: {", ".join(sorted(VALID_FEEDBACK_TYPES))}'
            )
        return v


class FeedbackResponse(BaseModel):
    """Response model for a single feedback record."""
    id: int
    item_type: str
    item_id: Optional[int]
    feedback_type: str
    feedback_context: Optional[str]
    created_at: str


class FeedbackStatsResponse(BaseModel):
    """Response model for aggregated feedback statistics."""
    by_item_type: dict
    by_feedback_type: dict
    total: int


class FeedbackListResponse(BaseModel):
    """Response model for list of feedback records."""
    feedback: List[dict]
    count: int


# ============================================================================
# Endpoints
# ============================================================================

@router.post("/react")
async def react_to_item(
    request: FeedbackRequest,
    user_id: str = Depends(require_auth),
) -> dict:
    """
    Record user feedback on an intelligence item.

    Args:
        request: FeedbackRequest with item_type, item_id, feedback_type, context

    Returns:
        { success: true, feedback_id: int }
    """
    uid = int(user_id)

    # Convert context dict to JSON string if provided
    feedback_context = None
    if request.context:
        try:
            feedback_context = json.dumps(request.context)
        except (TypeError, ValueError) as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid context format: {str(e)}"
            )

    feedback_id = record_feedback(
        user_id=uid,
        item_type=request.item_type,
        item_id=request.item_id,
        feedback_type=request.feedback_type,
        feedback_context=feedback_context,
        reason=request.reason,
        item_context=request.item_context,
    )

    if feedback_id is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to record feedback"
        )

    logger.info(
        "Recorded feedback for user %d: %s on %s (id=%s)",
        uid, request.feedback_type, request.item_type, request.item_id
    )

    return {"success": True, "feedback_id": feedback_id}


@router.get("/stats")
async def get_stats(
    user_id: str = Depends(require_auth),
) -> FeedbackStatsResponse:
    """
    Get aggregated feedback statistics for the authenticated user.

    Returns counts grouped by item_type and feedback_type.
    """
    uid = int(user_id)
    stats = get_feedback_stats(uid)

    return FeedbackStatsResponse(
        by_item_type=stats.get('by_item_type', {}),
        by_feedback_type=stats.get('by_feedback_type', {}),
        total=stats.get('total', 0)
    )


@router.get("/recent")
async def get_recent(
    user_id: str = Depends(require_auth),
    days: int = Query(default=30, ge=1, le=365),
    item_type: Optional[str] = Query(default=None),
) -> FeedbackListResponse:
    """
    Get recent feedback for the authenticated user.

    Args:
        days: Number of days to look back (default 30, max 365)
        item_type: Optional filter by item type

    Returns:
        List of feedback records with timestamps
    """
    uid = int(user_id)

    # Validate item_type if provided
    if item_type and item_type not in VALID_ITEM_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Invalid item_type "{item_type}". Must be one of: {", ".join(sorted(VALID_ITEM_TYPES))}'
        )

    feedback_list = get_recent_feedback(uid, days=days, item_type=item_type)

    return FeedbackListResponse(
        feedback=feedback_list,
        count=len(feedback_list)
    )


@router.get("/history")
async def get_history(
    user_id: str = Depends(require_auth),
    days: int = Query(default=30, ge=1, le=365),
    item_type: Optional[str] = Query(default=None),
) -> FeedbackListResponse:
    """
    Get feedback history for the authenticated user, including reason field.

    Returns recent feedback records with reason and item_context fields
    to support pattern inspection and lessons-learned aggregation.

    Args:
        days: Number of days to look back (default 30, max 365)
        item_type: Optional filter by item type

    Returns:
        List of feedback records with timestamps, reason, and item_context
    """
    uid = int(user_id)

    # Validate item_type if provided
    if item_type and item_type not in VALID_ITEM_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Invalid item_type "{item_type}". Must be one of: {", ".join(sorted(VALID_ITEM_TYPES))}'
        )

    feedback_list = get_recent_feedback(uid, days=days, item_type=item_type)

    return FeedbackListResponse(
        feedback=feedback_list,
        count=len(feedback_list)
    )


# ============================================================================
# Email Feedback Endpoint
# ============================================================================

def _generate_feedback_html(success: bool, message: str, action: str = "") -> str:
    """Generate a simple HTML page for email feedback response."""
    if success:
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Feedback Received - Seny</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    min-height: 100vh;
                    margin: 0;
                    background: #f5f5f5;
                }}
                .container {{
                    text-align: center;
                    padding: 40px;
                    background: white;
                    border-radius: 12px;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                    max-width: 400px;
                }}
                .icon {{
                    font-size: 48px;
                    margin-bottom: 16px;
                }}
                h1 {{
                    color: #27ae60;
                    font-size: 24px;
                    margin-bottom: 8px;
                }}
                p {{
                    color: #666;
                    font-size: 14px;
                    margin: 0;
                }}
                .action {{
                    color: #888;
                    font-size: 12px;
                    margin-top: 12px;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="icon">&#10004;</div>
                <h1>Thanks!</h1>
                <p>{message}</p>
                {f'<p class="action">{action}</p>' if action else ''}
            </div>
        </body>
        </html>
        """
    else:
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Feedback Error - Seny</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    min-height: 100vh;
                    margin: 0;
                    background: #f5f5f5;
                }}
                .container {{
                    text-align: center;
                    padding: 40px;
                    background: white;
                    border-radius: 12px;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                    max-width: 400px;
                }}
                .icon {{
                    font-size: 48px;
                    margin-bottom: 16px;
                }}
                h1 {{
                    color: #e74c3c;
                    font-size: 24px;
                    margin-bottom: 8px;
                }}
                p {{
                    color: #666;
                    font-size: 14px;
                    margin: 0;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="icon">&#10060;</div>
                <h1>Oops!</h1>
                <p>{message}</p>
            </div>
        </body>
        </html>
        """


def _generate_reason_form_html(token: str) -> str:
    """Generate an HTML form page asking the user why an item wasn't helpful."""
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Feedback - Seny</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                min-height: 100vh;
                margin: 0;
                background: #f5f5f5;
            }}
            .container {{
                text-align: center;
                padding: 40px;
                background: white;
                border-radius: 12px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                max-width: 480px;
                width: 90%;
            }}
            h1 {{
                color: #333;
                font-size: 22px;
                margin-bottom: 8px;
            }}
            p.note {{
                color: #888;
                font-size: 13px;
                margin-bottom: 16px;
            }}
            button {{
                background: #3498db;
                color: white;
                border: none;
                padding: 10px 24px;
                font-size: 15px;
                border-radius: 6px;
                cursor: pointer;
            }}
            button:hover {{
                background: #2980b9;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Why wasn't this helpful?</h1>
            <form method="POST" action="/api/feedback/email/{token}">
                <textarea name="reason" rows="4" placeholder="Tell us why..." style="width:100%;margin:12px 0;padding:8px;font-size:14px;border:1px solid #ddd;border-radius:4px;box-sizing:border-box;"></textarea>
                <br>
                <button type="submit">Submit Feedback</button>
            </form>
            <p class="note">Your feedback helps Seny learn what to surface.</p>
        </div>
    </body>
    </html>
    """


@router.get("/email/{token}", response_class=HTMLResponse)
async def process_email_feedback(token: str) -> HTMLResponse:
    """
    Process feedback from email link click.

    This endpoint is called when a user clicks a feedback link in their
    digest email. The token contains all the information needed to record
    the feedback securely.

    No authentication required - the token IS the authentication.

    Args:
        token: Secure feedback token from email link

    Returns:
        HTML page with success or error message
    """
    # For not_helpful: show form first (don't consume token yet)
    peeked = peek_email_feedback_token(token)
    if peeked and peeked.get('feedback_action') == 'not_helpful':
        return HTMLResponse(content=_generate_reason_form_html(token))

    # Validate and consume the token
    token_data = validate_and_consume_email_feedback_token(token)

    if not token_data:
        logger.warning("Invalid or expired email feedback token: %s...", token[:8] if len(token) >= 8 else token)
        return HTMLResponse(
            content=_generate_feedback_html(
                success=False,
                message="This feedback link is invalid, expired, or has already been used."
            ),
            status_code=400
        )

    user_id = token_data['user_id']
    item_type = token_data['item_type']
    item_id = token_data.get('item_id')
    feedback_action = token_data['feedback_action']
    sender_identifier = token_data.get('sender_identifier')
    source_type = token_data.get('source_type')

    # Build context for feedback record
    feedback_context = json.dumps({
        'from_email': True,
        'sender': sender_identifier,
        'source_type': source_type,
        'scanned_item_id': token_data.get('scanned_item_id'),
    })

    # Record the feedback
    feedback_id = record_feedback(
        user_id=user_id,
        item_type=item_type,
        item_id=item_id,
        feedback_type=feedback_action,
        feedback_context=feedback_context,
    )

    if feedback_id is None:
        logger.error("Failed to record email feedback for user %d", user_id)
        return HTMLResponse(
            content=_generate_feedback_html(
                success=False,
                message="Something went wrong recording your feedback. Please try again."
            ),
            status_code=500
        )

    # If this is an already_handled action, check if it's a screen agent nudge
    # and set cooldown to prevent the screen agent from re-firing.
    if feedback_action == 'already_handled':
        if item_id:
            from web.core.database import get_nudge_by_id
            nudge = get_nudge_by_id(user_id, item_id)
            if nudge and nudge.get('nudge_type') == 'screen_agent':
                from web.api.screen import dismiss_screen_nudge
                dismiss_screen_nudge(str(user_id))

    # If this is an ignore_sender action, also add to ignore list
    action_message = ""
    if feedback_action == 'ignore_sender' and sender_identifier and source_type:
        if add_ignored_sender(user_id, source_type, sender_identifier):
            action_message = f"Messages from {sender_identifier} will be excluded from future digests."
            logger.info(
                "User %d ignored sender: %s/%s via email feedback",
                user_id, source_type, sender_identifier
            )
        else:
            logger.error("Failed to add ignored sender for user %d: %s", user_id, sender_identifier)

    logger.info(
        "Recorded email feedback for user %d: %s on %s (id=%s)",
        user_id, feedback_action, item_type, item_id
    )

    # Return success page
    if feedback_action == 'ignore_sender':
        message = "Your feedback has been recorded."
    elif feedback_action == 'already_handled':
        message = "Got it — marked as already handled. I'll learn from this."
    else:
        message = "Your feedback helps Seny learn what's useful to you."

    return HTMLResponse(
        content=_generate_feedback_html(
            success=True,
            message=message,
            action=action_message
        ),
        status_code=200
    )


@router.post("/email/{token}", response_class=HTMLResponse)
async def submit_email_feedback_reason(token: str, reason: str = Form("")) -> HTMLResponse:
    """
    Accept the "Why wasn't this helpful?" form submission.

    Consumes the token, records feedback with the submitted reason text.
    No authentication required — the token IS the authentication.

    Args:
        token: Secure feedback token from the form action URL
        reason: Optional free-text reason submitted via the form

    Returns:
        HTML page with success or error message
    """
    token_data = validate_and_consume_email_feedback_token(token)
    if not token_data:
        return HTMLResponse(
            content=_generate_feedback_html(False, "This feedback link is invalid, expired, or has already been used."),
            status_code=400
        )
    feedback_id = record_feedback(
        user_id=token_data['user_id'],
        item_type=token_data['item_type'],
        item_id=token_data.get('item_id'),
        feedback_type=token_data['feedback_action'],
        feedback_context=json.dumps({
            'from_email': True,
            'sender': token_data.get('sender_identifier'),
            'source_type': token_data.get('source_type'),
            'scanned_item_id': token_data.get('scanned_item_id'),
        }),
        reason=reason.strip() or None,
    )
    if feedback_id is None:
        return HTMLResponse(
            content=_generate_feedback_html(False, "Something went wrong recording your feedback. Please try again."),
            status_code=500
        )
    logger.info("Recorded email feedback with reason for user %d: %s", token_data['user_id'], token_data['feedback_action'])
    return HTMLResponse(content=_generate_feedback_html(True, "Thanks for your feedback! This helps Seny learn."))


# ============================================================================
# Ignored Senders Endpoints
# ============================================================================

class IgnoredSenderResponse(BaseModel):
    """Response model for an ignored sender."""
    id: int
    source_type: str
    sender_identifier: str
    ignored_at: str


class IgnoredSendersListResponse(BaseModel):
    """Response model for list of ignored senders."""
    senders: List[IgnoredSenderResponse]
    count: int


@router.get("/ignored-senders")
async def list_ignored_senders(
    user_id: str = Depends(require_auth),
    source_type: Optional[str] = Query(default=None),
) -> IgnoredSendersListResponse:
    """
    Get list of ignored senders for the authenticated user.

    Args:
        source_type: Optional filter by source type (gmail, slack, telegram)

    Returns:
        List of ignored senders with source and identifier
    """
    uid = int(user_id)
    senders = get_ignored_senders(uid, source_type=source_type)

    return IgnoredSendersListResponse(
        senders=[IgnoredSenderResponse(**s) for s in senders],
        count=len(senders)
    )


class RemoveIgnoredSenderRequest(BaseModel):
    """Request model for removing an ignored sender."""
    source_type: str
    sender_identifier: str


@router.delete("/ignored-senders")
async def delete_ignored_sender(
    request: RemoveIgnoredSenderRequest,
    user_id: str = Depends(require_auth),
) -> dict:
    """
    Remove a sender from the user's ignore list.

    Args:
        request: RemoveIgnoredSenderRequest with source_type and sender_identifier

    Returns:
        { success: true } or raises 404 if not found
    """
    uid = int(user_id)

    removed = remove_ignored_sender(uid, request.source_type, request.sender_identifier)

    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ignored sender not found"
        )

    logger.info(
        "User %d removed ignored sender: %s/%s",
        uid, request.source_type, request.sender_identifier
    )

    return {"success": True}


# ============================================================================
# Patterns Endpoints
# ============================================================================

@router.get("/patterns")
async def get_patterns(user_id: str = Depends(require_auth)) -> dict:
    """
    Get the user's learned feedback patterns in plain English.

    Returns per-item-type preference scores, suppression status, override flags,
    responsive hours, and lessons learned — all formatted for display in the
    Settings > "What Seny Learned" tab.
    """
    uid = int(user_id)
    prefs = get_pattern_preferences(uid)
    overrides = get_suppression_overrides(uid)
    stats = get_feedback_stats(uid)

    # Parse item_type_preferences JSON blob
    type_prefs = {}
    last_computed = None
    if prefs:
        last_computed = str(prefs.get('last_computed_at', '')) or None
        raw = prefs.get('item_type_preferences')
        if raw:
            type_prefs = json.loads(raw) if isinstance(raw, str) else raw

    # Parse lessons_learned
    lessons = {}
    if prefs and prefs.get('lessons_learned'):
        raw_lessons = prefs['lessons_learned']
        lessons = json.loads(raw_lessons) if isinstance(raw_lessons, str) else raw_lessons

    # Parse responsive_hours
    responsive_hours = []
    if prefs and prefs.get('responsive_hours'):
        raw_hours = prefs['responsive_hours']
        responsive_hours = json.loads(raw_hours) if isinstance(raw_hours, str) else raw_hours

    # Build preference list sorted by score ascending (worst first for easy suppression scanning)
    preferences = []
    for item_type, score in sorted(type_prefs.items(), key=lambda x: x[1]):
        is_overridden = overrides.get(item_type) is True
        is_suppressed = score < -0.5 and not is_overridden
        preferences.append({
            'item_type': item_type,
            'label': _NUDGE_TYPE_LABELS.get(item_type, item_type),
            'score': round(score, 2),
            'score_label': _score_label(score),
            'suppressed': is_suppressed,
            'override_active': is_overridden,
        })

    # Data quality flag — scores computed before 49-02 shipped used generic labels
    data_quality_note = None
    if not last_computed or last_computed < '2026-03-05':
        data_quality_note = (
            "Feedback accuracy improved on 2026-03-05. Preferences computed before this date "
            "may be less precise because older feedback used a generic label instead of the "
            "specific nudge type. Scores will improve as new feedback accumulates."
        )

    return {
        'preferences': preferences,
        'suppressed_count': sum(1 for p in preferences if p['suppressed']),
        'overridden_count': sum(1 for p in preferences if p['override_active']),
        'feedback_stats': stats,
        'responsive_hours': responsive_hours,
        'lessons_learned': lessons,
        'last_computed_at': last_computed,
        'data_quality_note': data_quality_note,
        'has_data': bool(prefs or stats.get('total', 0) > 0),
    }


@router.delete("/patterns/{item_type}", status_code=204)
async def reset_pattern_suppression(
    item_type: str,
    user_id: str = Depends(require_auth),
):
    """
    Reset suppression for a specific item type by setting a persistent override.

    After calling this endpoint the suppression flag for the given item_type
    is cleared and Seny will resume sending that nudge category regardless of
    the current score.

    Args:
        item_type: Must be one of the known nudge item types.

    Returns:
        204 No Content on success.
        400 if item_type is unknown.
        500 if the DB write fails.
    """
    if item_type not in _VALID_ITEM_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown item_type: {item_type}"
        )
    uid = int(user_id)
    success = reset_suppression_override(uid, item_type)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reset suppression"
        )
    logger.info("User %d reset suppression override for item_type=%s", uid, item_type)
    # 204 No Content
