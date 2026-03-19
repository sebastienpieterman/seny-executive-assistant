"""
Activity Log Service for People Auto-Tracker.

Provides CRUD operations for activity_log entries, enabling:
- Transparency: Users see what automated updates were made
- Undo capability: Soft delete allows reverting unwanted changes
- Audit trail: Full history of automated People tracker updates

Phase 19-02 - Activity Feed & Override Controls
"""

import json
import logging
from typing import Optional

from web.core.database import get_db

logger = logging.getLogger(__name__)


class ActivityLogService:
    """
    Service for managing activity log entries.

    Activity logs track automated updates to the People tracker,
    including last_contact_date changes and context additions.
    """

    def __init__(self, user_id: int):
        self.user_id = user_id

    async def log_activity(
        self,
        person_id: int,
        action_type: str,
        old_value: Optional[str],
        new_value: str,
        context_added: Optional[str],
        source: str,
        source_context: dict
    ) -> int:
        """
        Create a new activity log entry.

        Args:
            person_id: ID of the person being updated
            action_type: Type of action (e.g., 'auto_update_contact')
            old_value: Previous last_contact_date (for undo)
            new_value: New last_contact_date
            context_added: AI-extracted context note (if noteworthy), None otherwise
            source: Communication source ('gmail', 'slack', 'telegram')
            source_context: Metadata dict with sender, snippet, timestamp, message_id

        Returns:
            ID of the created activity log entry
        """
        source_context_json = json.dumps(source_context) if source_context else None

        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO activity_log (
                        user_id, person_id, action_type, old_value, new_value,
                        context_added, source, source_context
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    self.user_id,
                    person_id,
                    action_type,
                    old_value,
                    new_value,
                    context_added,
                    source,
                    source_context_json
                ))

                activity_id = cursor.fetchone()['id']
                logger.debug(
                    "ActivityLogService: Created activity %d for person %d",
                    activity_id, person_id
                )
                return activity_id

        except Exception as e:
            logger.error("ActivityLogService: Failed to log activity: %r", e)
            raise

    async def get_global_feed(
        self,
        limit: int = 50,
        offset: int = 0,
        include_deleted: bool = False
    ) -> list[dict]:
        """
        Get all activity for user, newest first.

        JOINs with people table to include person_name.

        Args:
            limit: Maximum number of entries to return
            offset: Number of entries to skip (for pagination)
            include_deleted: If True, include soft-deleted entries

        Returns:
            List of activity dicts with person_name included
        """
        try:
            with get_db() as conn:
                cursor = conn.cursor()

                if include_deleted:
                    cursor.execute("""
                        SELECT
                            al.id,
                            al.person_id,
                            p.name as person_name,
                            al.action_type,
                            al.old_value,
                            al.new_value,
                            al.context_added,
                            al.source,
                            al.source_context,
                            al.created_at,
                            al.deleted_at
                        FROM activity_log al
                        JOIN people p ON al.person_id = p.id
                        WHERE al.user_id = %s
                        ORDER BY al.created_at DESC
                        LIMIT %s OFFSET %s
                    """, (self.user_id, limit, offset))
                else:
                    cursor.execute("""
                        SELECT
                            al.id,
                            al.person_id,
                            p.name as person_name,
                            al.action_type,
                            al.old_value,
                            al.new_value,
                            al.context_added,
                            al.source,
                            al.source_context,
                            al.created_at,
                            al.deleted_at
                        FROM activity_log al
                        JOIN people p ON al.person_id = p.id
                        WHERE al.user_id = %s AND al.deleted_at IS NULL
                        ORDER BY al.created_at DESC
                        LIMIT %s OFFSET %s
                    """, (self.user_id, limit, offset))

                return [self._row_to_dict(row) for row in cursor.fetchall()]

        except Exception as e:
            logger.error("ActivityLogService: Failed to get global feed: %r", e)
            return []

    async def get_person_feed(
        self,
        person_id: int,
        limit: int = 50,
        include_deleted: bool = False
    ) -> list[dict]:
        """
        Get activity history for a specific person.

        Args:
            person_id: ID of the person
            limit: Maximum number of entries to return
            include_deleted: If True, include soft-deleted entries

        Returns:
            List of activity dicts for the person
        """
        try:
            with get_db() as conn:
                cursor = conn.cursor()

                if include_deleted:
                    cursor.execute("""
                        SELECT
                            al.id,
                            al.person_id,
                            p.name as person_name,
                            al.action_type,
                            al.old_value,
                            al.new_value,
                            al.context_added,
                            al.source,
                            al.source_context,
                            al.created_at,
                            al.deleted_at
                        FROM activity_log al
                        JOIN people p ON al.person_id = p.id
                        WHERE al.user_id = %s AND al.person_id = %s
                        ORDER BY al.created_at DESC
                        LIMIT %s
                    """, (self.user_id, person_id, limit))
                else:
                    cursor.execute("""
                        SELECT
                            al.id,
                            al.person_id,
                            p.name as person_name,
                            al.action_type,
                            al.old_value,
                            al.new_value,
                            al.context_added,
                            al.source,
                            al.source_context,
                            al.created_at,
                            al.deleted_at
                        FROM activity_log al
                        JOIN people p ON al.person_id = p.id
                        WHERE al.user_id = %s AND al.person_id = %s AND al.deleted_at IS NULL
                        ORDER BY al.created_at DESC
                        LIMIT %s
                    """, (self.user_id, person_id, limit))

                return [self._row_to_dict(row) for row in cursor.fetchall()]

        except Exception as e:
            logger.error("ActivityLogService: Failed to get person feed: %r", e)
            return []

    async def soft_delete(self, activity_id: int) -> bool:
        """
        Mark activity as deleted (user override).

        Soft delete preserves the record for potential undo/restore.

        Args:
            activity_id: ID of the activity to delete

        Returns:
            True if deleted, False if not found or already deleted
        """
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE activity_log
                    SET deleted_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND user_id = %s AND deleted_at IS NULL
                """, (activity_id, self.user_id))

                if cursor.rowcount > 0:
                    logger.debug("ActivityLogService: Soft deleted activity %d", activity_id)
                    return True
                return False

        except Exception as e:
            logger.error("ActivityLogService: Failed to soft delete: %r", e)
            return False

    async def restore(self, activity_id: int) -> bool:
        """
        Restore a soft-deleted activity.

        Args:
            activity_id: ID of the activity to restore

        Returns:
            True if restored, False if not found or not deleted
        """
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE activity_log
                    SET deleted_at = NULL
                    WHERE id = %s AND user_id = %s AND deleted_at IS NOT NULL
                """, (activity_id, self.user_id))

                if cursor.rowcount > 0:
                    logger.debug("ActivityLogService: Restored activity %d", activity_id)
                    return True
                return False

        except Exception as e:
            logger.error("ActivityLogService: Failed to restore: %r", e)
            return False

    async def get_by_id(self, activity_id: int) -> Optional[dict]:
        """
        Get a single activity entry by ID.

        Args:
            activity_id: ID of the activity

        Returns:
            Activity dict or None if not found
        """
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT
                        al.id,
                        al.user_id,
                        al.person_id,
                        p.name as person_name,
                        al.action_type,
                        al.old_value,
                        al.new_value,
                        al.context_added,
                        al.source,
                        al.source_context,
                        al.created_at,
                        al.deleted_at
                    FROM activity_log al
                    JOIN people p ON al.person_id = p.id
                    WHERE al.id = %s AND al.user_id = %s
                """, (activity_id, self.user_id))

                row = cursor.fetchone()
                if row:
                    result = self._row_to_dict(row)
                    result['user_id'] = row['user_id']
                    return result
                return None

        except Exception as e:
            logger.error("ActivityLogService: Failed to get activity by id: %r", e)
            return None

    async def undo_activity(self, activity_id: int) -> bool:
        """
        Undo an activity by reverting the person's fields to old state.

        This reverts the last_contact_date to the old value, removes any
        context that was added to notes, and soft-deletes the activity entry.

        Args:
            activity_id: ID of the activity to undo

        Returns:
            True if undone successfully, False otherwise
        """
        from web.core.database import (
            get_person as db_get_person,
            update_person as db_update_person,
        )

        try:
            # Get the activity
            activity = await self.get_by_id(activity_id)
            if not activity:
                logger.warning("ActivityLogService: Activity %d not found", activity_id)
                return False

            if activity.get('deleted_at'):
                logger.warning("ActivityLogService: Activity %d already deleted", activity_id)
                return False

            person_id = activity['person_id']

            # Revert last_contact_date to old value
            db_update_person(person_id, last_contact_date=activity['old_value'])
            logger.debug(
                "ActivityLogService: Reverted person %d last_contact_date to %s",
                person_id, activity['old_value']
            )

            # If context was added, remove it from notes
            context_added = activity.get('context_added')
            if context_added:
                person = db_get_person(person_id)
                if person and person.get('notes'):
                    # The context was added as: "[YYYY-MM-DD] context_added"
                    # The date used is the new_value (the date of contact)
                    date_str = activity['new_value']
                    line_to_remove = f"[{date_str}] {context_added}"

                    notes = person['notes']
                    # Remove the line (handle both with and without preceding newlines)
                    new_notes = notes.replace(f"\n\n{line_to_remove}", "")
                    new_notes = new_notes.replace(f"\n{line_to_remove}", "")
                    new_notes = new_notes.replace(line_to_remove, "")
                    new_notes = new_notes.strip()

                    if new_notes != notes:
                        db_update_person(person_id, notes=new_notes if new_notes else None)
                        logger.debug(
                            "ActivityLogService: Removed context from person %d notes",
                            person_id
                        )

            # Mark activity as deleted
            await self.soft_delete(activity_id)

            logger.info(
                "ActivityLogService: Successfully undone activity %d for person %d",
                activity_id, person_id
            )
            return True

        except Exception as e:
            logger.error("ActivityLogService: Failed to undo activity: %r", e)
            return False

    def _row_to_dict(self, row) -> dict:
        """Convert a database row to a dictionary."""
        source_context = None
        if row['source_context']:
            try:
                source_context = json.loads(row['source_context'])
            except (json.JSONDecodeError, TypeError):
                source_context = None

        # Append 'Z' to timestamps to indicate UTC (SQLite stores UTC without timezone)
        created_at = row['created_at']
        if created_at and not created_at.endswith('Z'):
            created_at = created_at.replace(' ', 'T') + 'Z'

        deleted_at = row['deleted_at']
        if deleted_at and not deleted_at.endswith('Z'):
            deleted_at = deleted_at.replace(' ', 'T') + 'Z'

        return {
            'id': row['id'],
            'person_id': row['person_id'],
            'person_name': row['person_name'],
            'action_type': row['action_type'],
            'old_value': row['old_value'],
            'new_value': row['new_value'],
            'context_added': row['context_added'],
            'source': row['source'],
            'source_context': source_context,
            'created_at': created_at,
            'deleted_at': deleted_at,
        }
