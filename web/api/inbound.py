"""
Inbound Classification API - Phase 14-04

Provides endpoints for viewing classification results, managing detected actions,
querying cross-references, and accessing actionable items for Phase 15 (digest).
"""

import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from web.auth.jwt_utils import require_auth
from web.core.database import (
    get_db,
    get_pending_actions,
    get_cross_references_for_entity,
    get_actionable_items,
    update_detected_action_status,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ============================================================================
# Endpoints
# ============================================================================

@router.get("/status")
async def get_inbound_status(user_id: str = Depends(require_auth)):
    """
    Get inbound processing stats: unprocessed count, classified count,
    actionable count, pending actions, total cross-references.
    """
    from web.services.inbound_processor import InboundProcessor

    uid = int(user_id)
    processor = InboundProcessor(uid)
    stats = await processor.get_processing_stats()
    return stats


@router.post("/process-now")
async def trigger_processing(user_id: str = Depends(require_auth)):
    """Manually trigger inbound classification for all unprocessed items."""
    from web.services.inbound_processor import InboundProcessor
    uid = int(user_id)
    processor = InboundProcessor(uid)
    result = await processor.process_all_pending(max_batches=10)
    return result


@router.get("/actions")
async def get_actions(
    user_id: str = Depends(require_auth),
    limit: int = Query(default=20, ge=1, le=100),
    include_dismissed: bool = Query(default=False),
):
    """
    Get detected actions with context from scanned items and classifications.
    """
    uid = int(user_id)

    if include_dismissed:
        # Custom query that includes dismissed actions
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT da.id, da.user_id, da.scanned_item_id, da.action_text,
                           da.action_type, da.person_name, da.person_id, da.deadline,
                           da.status, da.promoted_task_id, da.detected_at,
                           si.source, si.source_id, si.source_metadata, si.item_type
                    FROM detected_actions da
                    JOIN scanned_items si ON da.scanned_item_id = si.id
                    WHERE da.user_id = %s
                    ORDER BY da.detected_at DESC
                    LIMIT %s
                """, (uid, limit))
                actions = [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error("Failed to fetch actions: %s", repr(e))
            actions = []
    else:
        actions = get_pending_actions(uid, limit=limit)

    # Enrich with classification summary
    enriched = []
    for action in actions:
        item = dict(action)
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT summary, relevance, urgency
                    FROM item_classifications
                    WHERE scanned_item_id = %s AND user_id = %s
                    LIMIT 1
                """, (item.get("scanned_item_id"), uid))
                cls_row = cursor.fetchone()
                if cls_row:
                    item["classification_summary"] = cls_row["summary"]
                    item["classification_relevance"] = cls_row["relevance"]
                    item["classification_urgency"] = cls_row["urgency"]
        except Exception:
            pass
        enriched.append(item)

    return {"actions": enriched, "count": len(enriched)}


@router.post("/actions/{action_id}/promote")
async def promote_action(action_id: int, user_id: str = Depends(require_auth)):
    """
    Promote a detected action to a Task.

    Creates a task via TasksService, updates detected_action status to 'promoted'
    with promoted_task_id link.
    """
    from web.services.tasks_service import TasksService

    uid = int(user_id)

    # Fetch the action
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, action_text, action_type, person_name, deadline, status, user_id
                FROM detected_actions
                WHERE id = %s AND user_id = %s
            """, (action_id, uid))
            action = cursor.fetchone()
    except Exception as e:
        logger.error("Failed to fetch action %d: %s", action_id, repr(e))
        raise HTTPException(status_code=500, detail="Failed to fetch action")

    if not action:
        raise HTTPException(status_code=404, detail="Action not found")

    action = dict(action)
    if action["status"] != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Action is already {action['status']}"
        )

    # Create a task from the action
    tasks_service = TasksService(uid)
    try:
        due_date = None
        if action.get("deadline"):
            try:
                due_date = datetime.fromisoformat(action["deadline"])
            except (ValueError, TypeError):
                pass

        task = await tasks_service.create_task(
            title=action["action_text"],
            description=f"Auto-created from detected {action['action_type']} action"
                        + (f" (related to {action['person_name']})" if action.get("person_name") else ""),
            priority="normal" if action["action_type"] != "deadline" else "high",
            due_date=due_date,
        )

        # Update action status
        update_detected_action_status(action_id, "promoted", task["id"])

        return {"status": "promoted", "task": task}

    except Exception as e:
        logger.error("Failed to promote action %d: %s", action_id, repr(e))
        raise HTTPException(status_code=500, detail="Failed to create task")


@router.post("/actions/{action_id}/dismiss")
async def dismiss_action(action_id: int, user_id: str = Depends(require_auth)):
    """
    Dismiss a detected action (mark as 'dismissed').
    """
    uid = int(user_id)

    # Verify ownership
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, status FROM detected_actions WHERE id = %s AND user_id = %s",
                (action_id, uid)
            )
            action = cursor.fetchone()
    except Exception as e:
        logger.error("Failed to fetch action %d: %s", action_id, repr(e))
        raise HTTPException(status_code=500, detail="Failed to fetch action")

    if not action:
        raise HTTPException(status_code=404, detail="Action not found")

    if dict(action)["status"] != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Action is already {dict(action)['status']}"
        )

    success = update_detected_action_status(action_id, "dismissed")
    if not success:
        raise HTTPException(status_code=500, detail="Failed to dismiss action")

    return {"status": "dismissed", "action_id": action_id}


@router.get("/cross-references")
async def get_cross_references(
    user_id: str = Depends(require_auth),
    entity_type: str = Query(..., pattern="^(person|project|idea|task)$"),
    entity_id: int = Query(...),
    limit: int = Query(default=20, ge=1, le=100),
):
    """
    Get cross-references for a specific entity (person, project, idea, task).
    """
    uid = int(user_id)

    refs = get_cross_references_for_entity(uid, entity_type, entity_id, limit=limit)

    # Enrich with classification summary
    enriched = []
    for ref in refs:
        item = dict(ref)
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT summary, relevance, urgency
                    FROM item_classifications
                    WHERE scanned_item_id = %s AND user_id = %s
                    LIMIT 1
                """, (item.get("scanned_item_id"), uid))
                cls_row = cursor.fetchone()
                if cls_row:
                    item["classification_summary"] = cls_row["summary"]
                    item["classification_relevance"] = cls_row["relevance"]
                    item["classification_urgency"] = cls_row["urgency"]
        except Exception:
            pass
        enriched.append(item)

    return {"cross_references": enriched, "count": len(enriched)}


@router.get("/recent")
async def get_recent_actionable(
    user_id: str = Depends(require_auth),
    since: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
):
    """
    Get recently classified actionable items.
    This is what Phase 15's digest will call.
    """
    uid = int(user_id)
    items = get_actionable_items(uid, since=since, limit=limit)
    return {"items": items, "count": len(items)}
