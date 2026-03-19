"""
Dashboard endpoint for Seny.

Aggregates priority tasks, people follow-ups, and recent activity
into a single response for the Home Dashboard page.

- GET /api/dashboard - Get dashboard data
"""

import logging
from datetime import datetime, date
from fastapi import APIRouter, Depends

from web.auth.jwt_utils import require_auth
from web.core.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_priority_tasks(user_id: int, limit: int = 10) -> list[dict]:
    """
    Get priority tasks: overdue first, then due today, then active with no due date.
    Returns up to `limit` tasks.
    """
    today_str = date.today().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, due_date, priority, type, status
            FROM tasks
            WHERE user_id = %s AND status != 'completed'
            ORDER BY
                CASE
                    WHEN due_date IS NOT NULL AND date(due_date) < date(%s) THEN 0
                    WHEN due_date IS NOT NULL AND date(due_date) = date(%s) THEN 1
                    ELSE 2
                END,
                due_date ASC NULLS LAST,
                CASE priority
                    WHEN 'urgent' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 3
                    ELSE 4
                END
            LIMIT %s
        """, (user_id, today_str, today_str, limit))
        return [dict(row) for row in cursor.fetchall()]


def _get_people_followups(user_id: int, limit: int = 5) -> list[dict]:
    """
    Get pending people follow-ups ordered by creation date.
    Joins with people table for person name and context.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                pf.id AS followup_id,
                p.name AS person_name,
                pf.content AS followup_text,
                pf.created_at AS due_date,
                p.id AS person_id
            FROM people_followups pf
            JOIN people p ON pf.person_id = p.id
            WHERE p.user_id = %s AND pf.status = 'active'
            ORDER BY pf.created_at ASC
            LIMIT %s
        """, (user_id, limit))
        return [dict(row) for row in cursor.fetchall()]


def _get_recent_activity(user_id: int) -> dict:
    """
    Get activity counts since midnight today.
    Each source is wrapped in try/except for graceful degradation.
    """
    today_str = date.today().isoformat()
    activity = {
        "new_emails": None,
        "new_slack_messages": None,
        "new_telegram_messages": None,
        "captures_today": None,
        "tasks_completed_today": None,
    }

    # Captures today
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM inbox_log
                WHERE user_id = %s AND date(created_at) = date(%s)
                AND classification != 'none'
            """, (user_id, today_str))
            row = cursor.fetchone()
            activity["captures_today"] = row["cnt"] if row else 0
    except Exception as e:
        logger.warning(f"Dashboard: failed to get captures count: {e}")

    # Tasks completed today
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM tasks
                WHERE user_id = %s AND status = 'completed'
                AND date(completed_at) = date(%s)
            """, (user_id, today_str))
            row = cursor.fetchone()
            activity["tasks_completed_today"] = row["cnt"] if row else 0
    except Exception as e:
        logger.warning(f"Dashboard: failed to get completed tasks count: {e}")

    # Email, Slack, Telegram counts are expensive (require API calls).
    # Return null — frontend will show "—" or skip.
    # These can be added later with lightweight caching.

    return activity


@router.get("")
async def get_dashboard(user_id: str = Depends(require_auth)):
    """
    Get aggregated dashboard data for the Home page.

    Returns priority tasks, people follow-ups, and recent activity counts.
    Each data source degrades gracefully — a failure in one does not break the others.
    """
    uid = int(user_id)

    # Priority tasks
    priority_tasks = None
    try:
        priority_tasks = _get_priority_tasks(uid)
    except Exception as e:
        logger.warning(f"Dashboard: failed to get priority tasks: {e}")

    # People follow-ups
    people_followups = None
    try:
        people_followups = _get_people_followups(uid)
    except Exception as e:
        logger.warning(f"Dashboard: failed to get people followups: {e}")

    # Recent activity
    recent_activity = None
    try:
        recent_activity = _get_recent_activity(uid)
    except Exception as e:
        logger.warning(f"Dashboard: failed to get recent activity: {e}")

    return {
        "priority_tasks": priority_tasks,
        "people_followups": people_followups,
        "recent_activity": recent_activity,
    }
