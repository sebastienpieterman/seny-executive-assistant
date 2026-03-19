"""
Pending Actions API - Phase 44-01

Provides endpoints for listing, counting, viewing, approving, dismissing,
and editing pending actions in the EA queue.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from web.auth.jwt_utils import require_auth
from web.core.database import (
    count_pending_actions,
    get_db,
    get_pending_action,
    list_google_tokens,
    list_pending_actions,
    record_feedback,
    update_pending_action_content,
    update_pending_action_status,
)
from web.services.calendar_service import CalendarService
from web.services.gmail_service import GmailService
from web.services.memory_service import MemoryService
from web.services.tasks_service import TasksService

logger = logging.getLogger(__name__)

router = APIRouter()


# ============================================================================
# Pydantic Models
# ============================================================================

class PendingActionUpdate(BaseModel):
    """Request model for editing a pending action's title and content."""
    title: str
    content_json: str  # raw JSON string — frontend sends serialized JSON


class DismissRequest(BaseModel):
    reason: Optional[str] = None


# ============================================================================
# Helpers
# ============================================================================

async def _trigger_compute_patterns(user_id: int) -> None:
    """Fire-and-forget: recompute preference scores after feedback is recorded."""
    try:
        from web.services.pattern_learning_service import PatternLearningService
        await PatternLearningService(user_id).compute_patterns()
    except Exception as e:
        logger.warning("[pending_actions] compute_patterns failed for user %d: %s", user_id, repr(e))


# ============================================================================
# Endpoints
# ============================================================================

@router.get("")
async def list_actions(
    user_id: str = Depends(require_auth),
    status: str = Query(default="pending"),
    action_type: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list:
    """
    List pending actions for the authenticated user.

    Args:
        status: Filter by status — 'pending' | 'approved' | 'dismissed' (default 'pending')
        action_type: Optional filter — 'email_draft' | 'calendar_proposal' | 'task_proposal'
        limit: Maximum number of actions to return (default 50, max 200)
    """
    uid = int(user_id)
    return list_pending_actions(uid, status=status, action_type=action_type, limit=limit)


@router.get("/count")
async def get_count(
    user_id: str = Depends(require_auth),
    status: str = Query(default="pending"),
) -> dict:
    """
    Count pending actions for sidebar badge.

    NOTE: This route is registered BEFORE /{action_id} routes so FastAPI does not
    match 'count' as an action_id.

    Args:
        status: Status to count (default 'pending')
    """
    uid = int(user_id)
    n = count_pending_actions(uid, status=status)
    return {"count": n}


@router.get("/{action_id}")
async def get_action(
    action_id: int,
    user_id: str = Depends(require_auth),
) -> dict:
    """
    Get a single pending action by ID.

    Returns 404 if not found or not owned by the authenticated user.
    """
    uid = int(user_id)
    action = get_pending_action(uid, action_id)
    if not action:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pending action not found",
        )
    return action


@router.post("/{action_id}/approve")
async def approve_action(
    action_id: int,
    user_id: str = Depends(require_auth),
    calendar_id: Optional[str] = Query(None, description="Override calendar for calendar_proposal approval"),
) -> dict:
    """
    Approve a pending action.

    For email_draft actions: validates body non-empty, sends the email via
    GmailService, then marks approved. Returns 400 if body empty, 502 if
    Gmail send fails.

    For calendar_proposal actions: creates the event in Google Calendar, then marks approved.
    For task_proposal actions: creates the task via TasksService, then marks approved.
    """
    uid = int(user_id)

    # Fetch the action first so we can read its content
    action = get_pending_action(uid, action_id)
    if not action:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pending action not found",
        )

    if action["action_type"] == "email_draft":
        # Parse content_json
        content = json.loads(action.get("content_json") or "{}")

        to = content.get("to", "")
        subject = content.get("subject", "")
        body = content.get("body", "")
        cc = content.get("cc") or None
        thread_id = content.get("reply_to_message_id") or content.get("thread_id") or None
        gmail_account = content.get("gmail_account") or None

        # Validate body is non-empty
        if not body or not body.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email body is required. Edit the draft to add your message before approving.",
            )

        # Resolve gmail_account — fall back to first connected account
        if not gmail_account:
            tokens = list_google_tokens(uid)
            if not tokens:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="No Gmail account connected. Please connect Gmail in Settings first.",
                )
            gmail_account = tokens[0]["email"]

        # Send the email
        gmail = GmailService(uid, gmail_account)
        result = await gmail.send_email(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            reply_to_message_id=thread_id,
        )

        if result is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Gmail send failed. Check that your Gmail account is still connected and try again.",
            )

        # Only mark approved after successful send
        update_pending_action_status(uid, action_id, "approved")
        try:
            action_type = action.get("action_type") or ""
            if action_type:
                record_feedback(
                    user_id=uid,
                    item_type=action_type,
                    item_id=action_id,
                    feedback_type="helpful",
                    item_context=action.get("title", ""),
                )
                asyncio.create_task(_trigger_compute_patterns(uid))
        except Exception as _fb_e:
            logger.warning("[approve] feedback recording failed: %s", repr(_fb_e))
        return {
            "status": "approved",
            "action_id": action_id,
            "message_id": result.get("id", ""),
        }

    elif action["action_type"] == "calendar_proposal":
        content = json.loads(action.get("content_json") or "{}")

        # Check Google Calendar is connected
        cal_accounts = CalendarService.list_connected_accounts(uid)
        if not cal_accounts:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Google Calendar connection required. Connect Google Calendar in Settings → Integrations.",
            )

        # Parse fields — title maps to summary (Google's term)
        summary = content.get("title", "").strip()
        if not summary:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Event title is required. Edit the proposal to add a title before approving.",
            )

        start_str = content.get("start_datetime") or ""
        if not start_str:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Start time is required. Edit the proposal to set a start time before approving.",
            )

        # Normalize datetime-local format (input may omit seconds: "2026-03-15T14:00")
        if len(start_str) == 16:
            start_str = start_str + ":00"

        end_str = content.get("end_datetime") or ""
        if not end_str:
            # Default: 1 hour after start
            try:
                start_dt = datetime.fromisoformat(start_str)
                end_str = (start_dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid start time format. Edit the proposal and try again.",
                )
        elif len(end_str) == 16:
            end_str = end_str + ":00"

        location = content.get("location") or None
        description = content.get("description") or None
        calendar_id = calendar_id or content.get("calendar_id") or "primary"

        # Fetch user timezone from user_settings (fallback: America/Chicago)
        tz = "America/Chicago"
        try:
            with get_db() as db:
                cur = db.cursor()
                cur.execute("SELECT digest_timezone FROM user_settings WHERE user_id = %s", (uid,))
                row = cur.fetchone()
                if row and row["digest_timezone"]:
                    tz = row["digest_timezone"]
        except Exception as e:
            logger.warning(f"[approve_calendar] Could not fetch timezone for user {uid}: {repr(e)}")

        # Select calendar account — prefer default_calendar_email from user_settings
        preferred_email = None
        try:
            with get_db() as _db:
                _cur = _db.cursor()
                _cur.execute("SELECT default_calendar_email FROM user_settings WHERE user_id = %s", (uid,))
                _row = _cur.fetchone()
                if _row:
                    preferred_email = _row.get("default_calendar_email")
        except Exception:
            pass

        if preferred_email:
            email = next((a["email"] for a in cal_accounts if a["email"] == preferred_email), cal_accounts[0]["email"])
        else:
            email = cal_accounts[0]["email"]
        cal = CalendarService(uid, email)
        result = await cal.create_event(
            summary=summary,
            start_time=start_str,
            end_time=end_str,
            description=description,
            location=location,
            calendar_id=calendar_id,
            timezone=tz,
        )

        if not result or result.get("error"):
            msg = result.get("message", "Unknown error") if result else "Calendar service unavailable"
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to create calendar event: {msg}. Check that Google Calendar is still connected and try again.",
            )

        update_pending_action_status(uid, action_id, "approved")
        try:
            action_type = action.get("action_type") or ""
            if action_type:
                record_feedback(
                    user_id=uid,
                    item_type=action_type,
                    item_id=action_id,
                    feedback_type="helpful",
                    item_context=action.get("title", ""),
                )
                asyncio.create_task(_trigger_compute_patterns(uid))
        except Exception as _fb_e:
            logger.warning("[approve] feedback recording failed: %s", repr(_fb_e))
        return {
            "status": "approved",
            "action_id": action_id,
            "event_id": result.get("id", ""),
            "html_link": result.get("html_link", ""),
        }

    elif action["action_type"] == "task_proposal":
        content = json.loads(action.get("content_json") or "{}")

        title = content.get("title", "").strip()
        if not title:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Task title is required. Edit the proposal to add a title before approving.",
            )

        description = content.get("description") or None
        priority = content.get("priority") or "medium"
        due_date_str = content.get("due_date") or None

        # Parse due_date string → datetime object (TasksService requires datetime, not str)
        due_date = None
        if due_date_str:
            try:
                due_date = datetime.fromisoformat(due_date_str)
            except ValueError:
                logger.warning(f"[approve_task] Invalid due_date format '{due_date_str}' for action {action_id} — ignoring")
                due_date = None  # Don't block approval over an unparseable date

        tasks_svc = TasksService(uid)
        task = await tasks_svc.create_task(
            title=title,
            description=description,
            priority=priority,
            due_date=due_date,
        )

        if not task:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to create task. Please try again.",
            )

        update_pending_action_status(uid, action_id, "approved")
        try:
            action_type = action.get("action_type") or ""
            if action_type:
                record_feedback(
                    user_id=uid,
                    item_type=action_type,
                    item_id=action_id,
                    feedback_type="helpful",
                    item_context=action.get("title", ""),
                )
                asyncio.create_task(_trigger_compute_patterns(uid))
        except Exception as _fb_e:
            logger.warning("[approve] feedback recording failed: %s", repr(_fb_e))
        return {
            "status": "approved",
            "action_id": action_id,
            "task_id": task.get("id", ""),
            "title": task.get("title", ""),
        }

    elif action["action_type"] == "research_proposal":
        content = {}
        try:
            content = json.loads(action.get("content_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            pass
        memory_rule = (content.get("memory_rule") or "").strip()
        memory_category = content.get("memory_category") or "behavior"
        if not memory_rule:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="research_proposal missing memory_rule in content_json",
            )
        memory_id = MemoryService.save_memory(uid, memory_rule, category=memory_category)
        update_pending_action_status(uid, action_id, "approved")
        logger.info(
            "[approve] research_proposal approved for user %d — saved memory id=%s: %s",
            uid, memory_id, memory_rule[:80],
        )
        return {
            "status": "approved",
            "action_id": action_id,
            "memory_saved": memory_rule,
            "memory_id": memory_id,
        }

    else:
        # Unknown action_type — should never happen with validated enum
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported action type: {action['action_type']}",
        )


@router.post("/{action_id}/dismiss")
async def dismiss_action(
    action_id: int,
    user_id: str = Depends(require_auth),
    body: Optional[DismissRequest] = None,
) -> dict:
    """
    Mark a pending action as dismissed.

    Accepts an optional body with a `reason` field. If reason is provided,
    records a less_like_this feedback signal and triggers compute_patterns.
    Neutral dismiss (no reason) removes from queue with no learning signal.
    """
    uid = int(user_id)
    # Fetch action first — need action_type and title if recording feedback
    action = get_pending_action(uid, action_id)
    if not action:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pending action not found",
        )
    update_pending_action_status(uid, action_id, "dismissed")
    # Record feedback only when reason is provided (neutral dismiss = no learning signal)
    reason = (body.reason or "").strip() if body else ""
    if reason:
        try:
            action_type = action.get("action_type") or ""
            if action_type:
                record_feedback(
                    user_id=uid,
                    item_type=action_type,
                    item_id=action_id,
                    feedback_type="less_like_this",
                    reason=reason,
                    item_context=action.get("title", ""),
                )
                asyncio.create_task(_trigger_compute_patterns(uid))
        except Exception as _fb_e:
            logger.warning("[dismiss] feedback recording failed: %s", repr(_fb_e))
    return {"status": "dismissed", "action_id": action_id}


@router.post("/{action_id}/restore")
async def restore_action(
    action_id: int,
    user_id: str = Depends(require_auth),
) -> dict:
    """
    Restore a dismissed action back to pending.
    """
    uid = int(user_id)
    ok = update_pending_action_status(uid, action_id, "pending")
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pending action not found",
        )
    return {"status": "pending", "action_id": action_id}


@router.patch("/{action_id}")
async def edit_action(
    action_id: int,
    body: PendingActionUpdate,
    user_id: str = Depends(require_auth),
) -> dict:
    """
    Edit the title and content of a pending action.

    Args:
        body: PendingActionUpdate with new title and content_json
    """
    uid = int(user_id)
    ok = update_pending_action_content(uid, action_id, body.title, body.content_json)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pending action not found",
        )
    updated = get_pending_action(uid, action_id)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pending action not found after update",
        )
    return updated
