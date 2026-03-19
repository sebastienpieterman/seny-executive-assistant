"""
Admin Service for Second Brain errand/life admin tracking.

Manages the Admin Database for simple errands and life admin items.
Think: "pick up dry cleaning", "renew passport", "buy birthday gift for mom".

Note on Admin vs Tasks:
We already have a Tasks system (from Phase 5). Admin is for SIMPLE errands -
things without complex priorities, reminders, or recurring patterns.
If something needs reminders, priorities, or is work-related, it should go to Tasks.
Admin is for life admin that just needs a checkbox.

Usage:
    service = AdminService(user_id)
    item = await service.create_item("Pick up dry cleaning", due_date="2025-01-20")
    insights = await service.get_admin_insights()
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from web.core.database import (
    create_admin_item as db_create_admin_item,
    get_admin_item as db_get_admin_item,
    get_admin_items_by_user,
    update_admin_item as db_update_admin_item,
    complete_admin_item as db_complete_admin_item,
    search_admin_items as db_search_admin_items,
    get_db,
)

logger = logging.getLogger(__name__)


class AdminService:
    """
    Service for managing the Admin Database for simple errands.

    Provides:
    - Admin item CRUD with due dates
    - Completion tracking
    - Overdue/due-soon detection
    - Admin insights for daily digest
    """

    def __init__(self, user_id: int):
        """
        Initialize Admin service for a specific user.

        Args:
            user_id: User's database ID
        """
        self.user_id = user_id

    async def create_item(
        self,
        title: str,
        notes: str = None,
        due_date: str = None
    ) -> dict:
        """
        Create an admin/errand item.

        Args:
            title: What needs to be done
            notes: Additional context
            due_date: Optional due date (ISO format YYYY-MM-DD)

        Returns:
            Created admin item dict
        """
        item_id = db_create_admin_item(
            user_id=self.user_id,
            title=title,
            notes=notes,
            due_date=due_date
        )

        if not item_id:
            raise ValueError("Failed to create admin item")

        return db_get_admin_item(item_id)

    async def get_item(self, item_id: int) -> Optional[dict]:
        """
        Get item details.

        Args:
            item_id: Admin item's database ID

        Returns:
            Admin item dict or None if not found
        """
        item = db_get_admin_item(item_id)
        if not item:
            return None

        # Verify ownership
        if item.get('user_id') != self.user_id:
            return None

        # Add overdue flag
        if item.get('status') == 'pending' and item.get('due_date'):
            today = datetime.utcnow().strftime("%Y-%m-%d")
            item['is_overdue'] = item['due_date'] < today

        return item

    async def list_items(self, status: str = None, include_overdue: bool = True) -> list:
        """
        List admin items.

        Args:
            status: 'pending', 'done', or None for pending only (default)
            include_overdue: Whether to flag overdue items (default True)

        Returns:
            List of admin item dicts with overdue items flagged
        """
        # Default to pending only if no status specified
        filter_status = status if status else 'pending'
        items = get_admin_items_by_user(self.user_id, status=filter_status)

        if include_overdue:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            for item in items:
                if item.get('status') == 'pending' and item.get('due_date'):
                    item['is_overdue'] = item['due_date'] < today
                else:
                    item['is_overdue'] = False

        return items

    async def search_items(self, query: str) -> list:
        """
        FTS search across admin items.

        Args:
            query: Search query

        Returns:
            List of matching admin item dicts
        """
        return db_search_admin_items(self.user_id, query, limit=20)

    async def complete_item(self, item_id: int) -> Optional[dict]:
        """
        Mark item as done.

        Args:
            item_id: Admin item's database ID

        Returns:
            Updated admin item dict or None if not found
        """
        # Verify ownership first
        item = await self.get_item(item_id)
        if not item:
            return None

        success = db_complete_admin_item(item_id)
        if not success:
            return None

        return await self.get_item(item_id)

    async def complete_item_by_title(self, title: str) -> Optional[dict]:
        """
        Mark item as done by title (fuzzy match).

        Args:
            title: Title to search for

        Returns:
            Updated admin item dict or None if not found
        """
        # First try exact match (case-insensitive)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id FROM admin_items
                WHERE user_id = %s AND title ILIKE %s AND status = 'pending'
            """, (self.user_id, title))
            row = cursor.fetchone()
            if row:
                return await self.complete_item(row['id'])

        # Fall back to FTS search
        results = db_search_admin_items(self.user_id, title, limit=1)
        if results and results[0].get('status') == 'pending':
            return await self.complete_item(results[0]['id'])

        return None

    async def update_item(self, item_id: int, **fields) -> Optional[dict]:
        """
        Update item fields.

        Args:
            item_id: Admin item's database ID
            **fields: Fields to update (title, notes, due_date, status)

        Returns:
            Updated admin item dict or None if not found
        """
        # Verify ownership first
        item = await self.get_item(item_id)
        if not item:
            return None

        success = db_update_admin_item(item_id, **fields)
        if not success:
            return None

        return await self.get_item(item_id)

    async def delete_item(self, item_id: int) -> bool:
        """
        Delete an item.

        Args:
            item_id: Admin item's database ID

        Returns:
            True if deleted
        """
        # Verify ownership first
        item = await self.get_item(item_id)
        if not item:
            return False

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM admin_items WHERE id = %s", (item_id,))
            return cursor.rowcount > 0

    async def get_overdue_items(self) -> list:
        """
        Get items past their due date.

        Returns:
            List of overdue admin item dicts
        """
        today = datetime.utcnow().strftime("%Y-%m-%d")

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_id, title, notes, due_date, status,
                       created_at, completed_at
                FROM admin_items
                WHERE user_id = %s
                AND status = 'pending'
                AND due_date IS NOT NULL
                AND due_date < %s
                ORDER BY due_date ASC
            """, (self.user_id, today))

            items = [dict(row) for row in cursor.fetchall()]
            for item in items:
                item['is_overdue'] = True
            return items

    async def get_due_soon(self, days: int = 3) -> list:
        """
        Get items due within N days.

        Args:
            days: Number of days to look ahead

        Returns:
            List of admin item dicts due soon
        """
        today = datetime.utcnow().strftime("%Y-%m-%d")
        end_date = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_id, title, notes, due_date, status,
                       created_at, completed_at
                FROM admin_items
                WHERE user_id = %s
                AND status = 'pending'
                AND due_date IS NOT NULL
                AND due_date >= %s
                AND due_date <= %s
                ORDER BY due_date ASC
            """, (self.user_id, today, end_date))

            items = [dict(row) for row in cursor.fetchall()]
            for item in items:
                item['is_overdue'] = False
            return items

    async def get_due_today(self) -> list:
        """
        Get items due today.

        Returns:
            List of admin item dicts due today
        """
        today = datetime.utcnow().strftime("%Y-%m-%d")

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_id, title, notes, due_date, status,
                       created_at, completed_at
                FROM admin_items
                WHERE user_id = %s
                AND status = 'pending'
                AND due_date = %s
                ORDER BY created_at ASC
            """, (self.user_id, today))

            return [dict(row) for row in cursor.fetchall()]

    async def get_admin_insights(self) -> dict:
        """
        Get admin insights for daily digest.

        Returns:
            Dict with pending_count, overdue, due_today, due_this_week
        """
        pending = await self.list_items(status='pending')
        overdue = await self.get_overdue_items()
        due_today = await self.get_due_today()
        due_this_week = await self.get_due_soon(days=7)

        # Filter out items due today from due_this_week
        today = datetime.utcnow().strftime("%Y-%m-%d")
        due_this_week = [item for item in due_this_week if item.get('due_date') != today]

        return {
            'pending_count': len(pending),
            'overdue': overdue,
            'due_today': due_today,
            'due_this_week': due_this_week
        }
