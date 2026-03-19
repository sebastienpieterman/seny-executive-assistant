"""
Activity API endpoints for People Auto-Tracker transparency.

Provides endpoints for viewing and managing automated activity:
- GET /api/activity/feed - Global activity feed for user
- GET /api/activity/person/{person_id} - Activity for specific person
- DELETE /api/activity/{activity_id} - Soft delete an activity entry
- POST /api/activity/{activity_id}/restore - Restore soft-deleted activity

Phase 19-02 - Activity Feed & Override Controls
"""

import logging
from fastapi import APIRouter, HTTPException, status, Depends, Query

from web.auth.jwt_utils import require_auth
from web.services.activity_log_service import ActivityLogService

logger = logging.getLogger(__name__)

# Create activity router
router = APIRouter()


@router.get("/feed")
async def get_global_feed(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    include_deleted: bool = Query(default=False),
    user_id: str = Depends(require_auth)
):
    """
    Get all automated activity for current user.

    Returns activity entries sorted by most recent first,
    with person names included for display.

    Args:
        limit: Maximum entries to return (1-100)
        offset: Number of entries to skip
        include_deleted: If true, include soft-deleted entries

    Returns:
        List of activity entries with person_name
    """
    try:
        service = ActivityLogService(int(user_id))
        activities = await service.get_global_feed(
            limit=limit,
            offset=offset,
            include_deleted=include_deleted
        )
        return {"activities": activities, "count": len(activities)}
    except Exception as e:
        logger.error("Activity feed error: %r", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve activity feed"
        )


@router.get("/person/{person_id}")
async def get_person_feed(
    person_id: int,
    limit: int = Query(default=50, ge=1, le=100),
    include_deleted: bool = Query(default=False),
    user_id: str = Depends(require_auth)
):
    """
    Get activity history for a specific person.

    Returns all automated updates for the specified person,
    sorted by most recent first.

    Args:
        person_id: ID of the person
        limit: Maximum entries to return (1-100)
        include_deleted: If true, include soft-deleted entries

    Returns:
        List of activity entries for the person
    """
    try:
        service = ActivityLogService(int(user_id))
        activities = await service.get_person_feed(
            person_id=person_id,
            limit=limit,
            include_deleted=include_deleted
        )
        return {"activities": activities, "count": len(activities)}
    except Exception as e:
        logger.error("Person activity feed error: %r", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve person activity"
        )


@router.delete("/{activity_id}")
async def delete_activity(
    activity_id: int,
    user_id: str = Depends(require_auth)
):
    """
    Soft delete an activity entry (user override).

    Marks the activity as deleted but preserves it for
    potential restoration. Does not undo the actual change
    to the People record.

    Args:
        activity_id: ID of the activity to delete

    Returns:
        Status message
    """
    try:
        service = ActivityLogService(int(user_id))
        success = await service.soft_delete(activity_id)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Activity not found"
            )
        return {"status": "deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Activity delete error: %r", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete activity"
        )


@router.post("/{activity_id}/restore")
async def restore_activity(
    activity_id: int,
    user_id: str = Depends(require_auth)
):
    """
    Restore a soft-deleted activity.

    Removes the deleted_at timestamp, making the activity
    visible in the normal feed again.

    Args:
        activity_id: ID of the activity to restore

    Returns:
        Status message
    """
    try:
        service = ActivityLogService(int(user_id))
        success = await service.restore(activity_id)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Activity not found"
            )
        return {"status": "restored"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Activity restore error: %r", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to restore activity"
        )


@router.post("/{activity_id}/undo")
async def undo_activity(
    activity_id: int,
    user_id: str = Depends(require_auth)
):
    """
    Undo an activity - reverts the change AND removes added context.

    This is different from DELETE which just hides the activity entry.
    UNDO actually reverts the person's last_contact_date to the old value
    AND removes any context that was added to their notes.

    Args:
        activity_id: ID of the activity to undo

    Returns:
        Status message
    """
    try:
        service = ActivityLogService(int(user_id))
        success = await service.undo_activity(activity_id)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Activity not found or cannot undo"
            )
        return {"status": "undone"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Activity undo error: %r", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to undo activity"
        )
