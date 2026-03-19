"""
Tasks Service for Seny - Phase 5 + Phase 8 Errand Features

Provides task management with:
- CRUD operations on tasks
- Priority levels (urgent, high, medium, low)
- Due dates and overdue tracking
- Status transitions (pending, in_progress, completed, cancelled)
- Recurring tasks with pattern support
- Categories and projects for organization
- Reminders infrastructure
- Type distinction: 'task' (complex work) vs 'errand' (simple life admin)
- Fuzzy title completion for errands
- Task insights for daily digest

Usage:
    tasks = TasksService(user_id)
    task = await tasks.create_task("Buy groceries", priority="high", due_date=tomorrow)
    errand = await tasks.create_task("Pick up dry cleaning", task_type="errand")
    await tasks.complete_task(task["id"])
    await tasks.complete_task_by_title("dry cleaning")  # Fuzzy match
"""

import logging
from typing import Optional
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta

from web.core.database import get_db

logger = logging.getLogger(__name__)


# Valid values for constrained fields
VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}
VALID_PRIORITIES = {"low", "medium", "high", "urgent"}
VALID_RECURRENCE_PATTERNS = {"daily", "weekly", "monthly", "yearly"}
VALID_TASK_TYPES = {"task", "errand"}


class TasksService:
    """
    Task management service with priorities, due dates, and recurrence.

    One instance per user - do not share across users.

    Attributes:
        user_id: The user's database ID
    """

    def __init__(self, user_id: int):
        """
        Initialize Tasks service for a specific user.

        Args:
            user_id: User's database ID
        """
        self.user_id = user_id

    # =========================================================================
    # CRUD Operations
    # =========================================================================

    async def create_task(
        self,
        title: str,
        description: str = None,
        priority: str = "medium",
        due_date: datetime = None,
        category: str = None,
        project: str = None,
        task_type: str = "task",
        is_recurring: bool = False,
        recurrence_pattern: str = None,
        recurrence_interval: int = 1,
        recurrence_end_date: datetime = None
    ) -> dict:
        """
        Create a new task or errand.

        Args:
            title: Task title (required)
            description: Optional longer description
            priority: low, medium (default), high, or urgent
            due_date: When the task is due
            category: Optional category (e.g., "work", "personal")
            project: Optional project name
            task_type: 'task' (default) or 'errand' for simple life admin
            is_recurring: Whether this task repeats
            recurrence_pattern: daily, weekly, monthly, or yearly
            recurrence_interval: Every N days/weeks/etc (default 1)
            recurrence_end_date: When recurrence stops

        Returns:
            Created task dict with all fields
        """
        # Validate inputs
        priority = priority.lower() if priority else "medium"
        if priority not in VALID_PRIORITIES:
            priority = "medium"

        task_type = task_type.lower() if task_type else "task"
        if task_type not in VALID_TASK_TYPES:
            task_type = "task"

        if is_recurring and recurrence_pattern:
            recurrence_pattern = recurrence_pattern.lower()
            if recurrence_pattern not in VALID_RECURRENCE_PATTERNS:
                recurrence_pattern = None
                is_recurring = False

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO tasks (
                    user_id, title, description, priority, due_date,
                    category, project, type, is_recurring, recurrence_pattern,
                    recurrence_interval, recurrence_end_date
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                self.user_id,
                title,
                description,
                priority,
                due_date.isoformat() if due_date else None,
                category,
                project,
                task_type,
                1 if is_recurring else 0,
                recurrence_pattern if is_recurring else None,
                recurrence_interval if is_recurring else 1,
                recurrence_end_date.isoformat() if recurrence_end_date else None
            ))

            task_id = cursor.fetchone()['id']
            logger.info(f"Created {task_type} {task_id}: {title[:50]}")

        # Return fresh copy (after commit)
        return await self.get_task(task_id)

    async def get_task(self, task_id: int) -> Optional[dict]:
        """
        Get a task by ID.

        Args:
            task_id: The task's ID

        Returns:
            Task dict or None if not found/not owned by user
        """
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT id, title, description, status, priority, due_date,
                       completed_at, category, project, type, is_recurring,
                       recurrence_pattern, recurrence_interval, recurrence_end_date,
                       parent_task_id, created_at, updated_at
                FROM tasks
                WHERE id = %s AND user_id = %s
            """, (task_id, self.user_id))

            row = cursor.fetchone()
            if not row:
                return None

            return self._row_to_dict(row)

    async def update_task(
        self,
        task_id: int,
        title: str = None,
        description: str = None,
        status: str = None,
        priority: str = None,
        due_date: datetime = None,
        category: str = None,
        project: str = None
    ) -> Optional[dict]:
        """
        Update an existing task.

        For status changes, prefer using complete_task() or reopen_task().

        Args:
            task_id: The task's ID
            title: New title (optional)
            description: New description (optional)
            status: New status (optional)
            priority: New priority (optional)
            due_date: New due date (optional)
            category: New category (optional)
            project: New project (optional)

        Returns:
            Updated task dict or None if not found/not owned
        """
        # Verify ownership
        existing = await self.get_task(task_id)
        if not existing:
            return None

        # Validate inputs
        if status and status.lower() not in VALID_STATUSES:
            status = None
        if priority and priority.lower() not in VALID_PRIORITIES:
            priority = None

        # Build update dynamically
        updates = []
        params = []

        if title is not None:
            updates.append("title = %s")
            params.append(title)

        if description is not None:
            updates.append("description = %s")
            params.append(description)

        if status is not None:
            updates.append("status = %s")
            params.append(status.lower())
            # If completing, set completed_at
            if status.lower() == "completed":
                updates.append("completed_at = %s")
                params.append(datetime.now().isoformat())
            elif existing["status"] == "completed":
                # Reopening - clear completed_at
                updates.append("completed_at = NULL")

        if priority is not None:
            updates.append("priority = %s")
            params.append(priority.lower())

        if due_date is not None:
            updates.append("due_date = %s")
            params.append(due_date.isoformat())

        if category is not None:
            updates.append("category = %s")
            params.append(category if category else None)

        if project is not None:
            updates.append("project = %s")
            params.append(project if project else None)

        if not updates:
            return existing  # Nothing to update

        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.extend([task_id, self.user_id])

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute(f"""
                UPDATE tasks
                SET {', '.join(updates)}
                WHERE id = %s AND user_id = %s
            """, params)

            logger.info(f"Updated task {task_id}")

        return await self.get_task(task_id)

    async def delete_task(self, task_id: int) -> bool:
        """
        Delete a task.

        Also removes associated reminders (via CASCADE).

        Args:
            task_id: The task's ID

        Returns:
            True if deleted, False if not found/not owned
        """
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                DELETE FROM tasks WHERE id = %s AND user_id = %s
            """, (task_id, self.user_id))

            deleted = cursor.rowcount > 0

            if deleted:
                logger.info(f"Deleted task {task_id}")

            return deleted

    # =========================================================================
    # Status Transitions
    # =========================================================================

    async def complete_task(self, task_id: int) -> Optional[dict]:
        """
        Mark a task as completed.

        For recurring tasks, also generates the next occurrence.

        Args:
            task_id: The task's ID

        Returns:
            Completed task dict, or None if not found
        """
        task = await self.get_task(task_id)
        if not task:
            return None

        now = datetime.now()

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE tasks
                SET status = 'completed', completed_at = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND user_id = %s
            """, (now.isoformat(), task_id, self.user_id))

            logger.info(f"Completed task {task_id}")

        # Generate next occurrence for recurring tasks
        if task["is_recurring"]:
            await self.generate_next_occurrence(task_id)

        return await self.get_task(task_id)

    async def reopen_task(self, task_id: int) -> Optional[dict]:
        """
        Reopen a completed or cancelled task.

        Args:
            task_id: The task's ID

        Returns:
            Reopened task dict, or None if not found
        """
        task = await self.get_task(task_id)
        if not task:
            return None

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE tasks
                SET status = 'pending', completed_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND user_id = %s
            """, (task_id, self.user_id))

            logger.info(f"Reopened task {task_id}")

        return await self.get_task(task_id)

    async def start_task(self, task_id: int) -> Optional[dict]:
        """
        Mark a task as in progress.

        Args:
            task_id: The task's ID

        Returns:
            Updated task dict, or None if not found
        """
        task = await self.get_task(task_id)
        if not task:
            return None

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE tasks
                SET status = 'in_progress', updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND user_id = %s
            """, (task_id, self.user_id))

            logger.info(f"Started task {task_id}")

        return await self.get_task(task_id)

    async def cancel_task(self, task_id: int) -> Optional[dict]:
        """
        Cancel a task.

        Args:
            task_id: The task's ID

        Returns:
            Cancelled task dict, or None if not found
        """
        task = await self.get_task(task_id)
        if not task:
            return None

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE tasks
                SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND user_id = %s
            """, (task_id, self.user_id))

            logger.info(f"Cancelled task {task_id}")

        return await self.get_task(task_id)

    async def complete_task_by_title(self, title: str, task_type: str = None) -> Optional[dict]:
        """
        Mark a task as completed by title (fuzzy match).

        Useful for errands where user says "I did the dry cleaning" - finds
        matching task by title search.

        Args:
            title: Title to search for (case-insensitive)
            task_type: Optional filter by type ('task' or 'errand')

        Returns:
            Completed task dict, or None if not found
        """
        # First try exact match (case-insensitive)
        with get_db() as conn:
            cursor = conn.cursor()

            conditions = ["user_id = %s", "LOWER(title) = LOWER(%s)", "status = 'pending'"]
            params = [self.user_id, title]

            if task_type:
                conditions.append("type = %s")
                params.append(task_type.lower())

            cursor.execute(f"""
                SELECT id FROM tasks
                WHERE {' AND '.join(conditions)}
            """, params)

            row = cursor.fetchone()
            if row:
                return await self.complete_task(row['id'])

        # Fall back to FTS-like search using LIKE with wildcards
        with get_db() as conn:
            cursor = conn.cursor()

            # Normalize search term - extract key words
            search_term = f"%{title.lower()}%"

            conditions = ["user_id = %s", "title ILIKE %s", "status = 'pending'"]
            params = [self.user_id, search_term]

            if task_type:
                conditions.append("type = %s")
                params.append(task_type.lower())

            cursor.execute(f"""
                SELECT id FROM tasks
                WHERE {' AND '.join(conditions)}
                LIMIT 1
            """, params)

            row = cursor.fetchone()
            if row:
                return await self.complete_task(row['id'])

        return None

    # =========================================================================
    # Task Insights (for Daily Digest)
    # =========================================================================

    async def get_task_insights(self, task_type: str = None) -> dict:
        """
        Get task insights for daily digest.

        Useful for errands to get overdue/due today/due this week at a glance.

        Args:
            task_type: Optional filter by type ('task' or 'errand')

        Returns:
            Dict with pending_count, overdue, due_today, due_this_week
        """
        pending = await self.list_tasks(task_type=task_type)
        overdue = await self.get_overdue(task_type=task_type)
        due_today = await self.get_due_today(task_type=task_type)
        due_this_week = await self.get_upcoming(days=7, task_type=task_type)

        # Filter out items due today from due_this_week to avoid duplicates
        today = datetime.now().strftime("%Y-%m-%d")
        due_this_week = [
            task for task in due_this_week
            if task.get('due_date') and not task['due_date'].startswith(today)
        ]

        return {
            'pending_count': len(pending),
            'overdue': overdue,
            'due_today': due_today,
            'due_this_week': due_this_week
        }

    # =========================================================================
    # List & Filter
    # =========================================================================

    async def list_tasks(
        self,
        status: str = None,
        priority: str = None,
        category: str = None,
        project: str = None,
        task_type: str = None,
        due_before: datetime = None,
        due_after: datetime = None,
        include_completed: bool = False,
        limit: int = 50
    ) -> list[dict]:
        """
        List tasks with optional filters.

        Args:
            status: Filter by status (pending, in_progress, completed, cancelled)
            priority: Filter by priority (low, medium, high, urgent)
            category: Filter by category
            project: Filter by project
            task_type: Filter by type ('task' or 'errand')
            due_before: Only tasks due before this date
            due_after: Only tasks due after this date
            include_completed: Include completed tasks (default False)
            limit: Maximum number of tasks to return

        Returns:
            List of task dicts, ordered by due date (nulls last), then priority
        """
        conditions = ["user_id = %s"]
        params = [self.user_id]

        if status:
            conditions.append("status = %s")
            params.append(status.lower())
        elif not include_completed:
            conditions.append("status NOT IN ('completed', 'cancelled')")

        if priority:
            conditions.append("priority = %s")
            params.append(priority.lower())

        if category:
            conditions.append("category = %s")
            params.append(category)

        if project:
            conditions.append("project = %s")
            params.append(project)

        if task_type:
            conditions.append("type = %s")
            params.append(task_type.lower())

        if due_before:
            conditions.append("due_date <= %s")
            params.append(due_before.isoformat())

        if due_after:
            conditions.append("due_date >= %s")
            params.append(due_after.isoformat())

        params.append(limit)

        with get_db() as conn:
            cursor = conn.cursor()

            # Order by: due date (nulls last), then priority (urgent first), then created
            # Priority ordering: urgent=1, high=2, medium=3, low=4
            cursor.execute(f"""
                SELECT id, title, description, status, priority, due_date,
                       completed_at, category, project, type, is_recurring,
                       recurrence_pattern, recurrence_interval, recurrence_end_date,
                       parent_task_id, created_at, updated_at
                FROM tasks
                WHERE {' AND '.join(conditions)}
                ORDER BY
                    CASE WHEN due_date IS NULL THEN 1 ELSE 0 END,
                    due_date ASC,
                    CASE priority
                        WHEN 'urgent' THEN 1
                        WHEN 'high' THEN 2
                        WHEN 'medium' THEN 3
                        WHEN 'low' THEN 4
                    END,
                    created_at DESC
                LIMIT %s
            """, params)

            return [self._row_to_dict(row) for row in cursor.fetchall()]

    async def get_due_today(self, task_type: str = None) -> list[dict]:
        """
        Get tasks due today.

        Args:
            task_type: Filter by type ("task" or "errand"), None for all

        Returns:
            List of tasks due today, ordered by priority
        """
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)

        return await self.list_tasks(
            task_type=task_type,
            due_after=today_start,
            due_before=today_end,
            include_completed=False
        )

    async def get_overdue(self, task_type: str = None) -> list[dict]:
        """
        Get overdue tasks (due date has passed, not completed).

        Args:
            task_type: Optional filter by type ('task' or 'errand')

        Returns:
            List of overdue tasks, ordered by how overdue they are
        """
        now = datetime.now()

        conditions = ["user_id = %s", "due_date < %s", "status NOT IN ('completed', 'cancelled')"]
        params = [self.user_id, now.isoformat()]

        if task_type:
            conditions.append("type = %s")
            params.append(task_type.lower())

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute(f"""
                SELECT id, title, description, status, priority, due_date,
                       completed_at, category, project, type, is_recurring,
                       recurrence_pattern, recurrence_interval, recurrence_end_date,
                       parent_task_id, created_at, updated_at
                FROM tasks
                WHERE {' AND '.join(conditions)}
                ORDER BY due_date ASC
            """, params)

            return [self._row_to_dict(row) for row in cursor.fetchall()]

    async def get_upcoming(self, days: int = 7, task_type: str = None) -> list[dict]:
        """
        Get tasks due in the next N days.

        Args:
            days: Number of days to look ahead (default 7)
            task_type: Filter by type ("task" or "errand"), None for all

        Returns:
            List of upcoming tasks, ordered by due date
        """
        now = datetime.now()
        future = now + timedelta(days=days)

        return await self.list_tasks(
            task_type=task_type,
            due_after=now,
            due_before=future,
            include_completed=False
        )

    # =========================================================================
    # Categories & Projects
    # =========================================================================

    async def list_categories(self) -> list[dict]:
        """
        List all categories used by this user with task counts.

        Returns:
            List of dicts with category name and count
        """
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT category, COUNT(*) as count
                FROM tasks
                WHERE user_id = %s AND category IS NOT NULL
                GROUP BY category
                ORDER BY count DESC, category ASC
            """, (self.user_id,))

            return [
                {"category": row["category"], "count": row["count"]}
                for row in cursor.fetchall()
            ]

    async def list_projects(self) -> list[dict]:
        """
        List all projects used by this user with task counts.

        Returns:
            List of dicts with project name and count
        """
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT project, COUNT(*) as count
                FROM tasks
                WHERE user_id = %s AND project IS NOT NULL
                GROUP BY project
                ORDER BY count DESC, project ASC
            """, (self.user_id,))

            return [
                {"project": row["project"], "count": row["count"]}
                for row in cursor.fetchall()
            ]

    # =========================================================================
    # Reminders
    # =========================================================================

    async def add_reminder(
        self,
        task_id: int,
        remind_at: datetime,
        reminder_type: str = "notification"
    ) -> Optional[dict]:
        """
        Add a reminder for a task.

        Args:
            task_id: The task's ID
            remind_at: When to send the reminder
            reminder_type: Type of reminder (notification or email)

        Returns:
            Reminder dict, or None if task not found
        """
        # Verify task ownership
        task = await self.get_task(task_id)
        if not task:
            return None

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO task_reminders (task_id, remind_at, reminder_type)
                VALUES (%s, %s, %s)
                RETURNING id
            """, (task_id, remind_at.isoformat(), reminder_type))

            reminder_id = cursor.fetchone()['id']
            logger.info(f"Added reminder {reminder_id} for task {task_id}")

            return {
                "id": reminder_id,
                "task_id": task_id,
                "remind_at": remind_at.isoformat(),
                "reminder_type": reminder_type,
                "is_sent": False
            }

    async def get_pending_reminders(self) -> list[dict]:
        """
        Get all pending reminders that are due.

        Returns:
            List of reminder dicts with task info, ordered by remind_at
        """
        now = datetime.now()

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT r.id, r.task_id, r.remind_at, r.reminder_type,
                       t.title as task_title, t.due_date as task_due_date
                FROM task_reminders r
                JOIN tasks t ON t.id = r.task_id
                WHERE t.user_id = %s
                AND r.is_sent = 0
                AND r.remind_at <= %s
                ORDER BY r.remind_at ASC
            """, (self.user_id, now.isoformat()))

            return [
                {
                    "id": row["id"],
                    "task_id": row["task_id"],
                    "task_title": row["task_title"],
                    "task_due_date": row["task_due_date"],
                    "remind_at": row["remind_at"],
                    "reminder_type": row["reminder_type"]
                }
                for row in cursor.fetchall()
            ]

    async def mark_reminder_sent(self, reminder_id: int) -> bool:
        """
        Mark a reminder as sent.

        Args:
            reminder_id: The reminder's ID

        Returns:
            True if marked, False if not found
        """
        now = datetime.now()

        with get_db() as conn:
            cursor = conn.cursor()

            # Verify the reminder belongs to a task owned by this user
            cursor.execute("""
                UPDATE task_reminders
                SET is_sent = 1, sent_at = %s
                WHERE id = %s AND task_id IN (
                    SELECT id FROM tasks WHERE user_id = %s
                )
            """, (now.isoformat(), reminder_id, self.user_id))

            return cursor.rowcount > 0

    async def delete_reminder(self, reminder_id: int) -> bool:
        """
        Delete a reminder.

        Args:
            reminder_id: The reminder's ID

        Returns:
            True if deleted, False if not found
        """
        with get_db() as conn:
            cursor = conn.cursor()

            # Verify the reminder belongs to a task owned by this user
            cursor.execute("""
                DELETE FROM task_reminders
                WHERE id = %s AND task_id IN (
                    SELECT id FROM tasks WHERE user_id = %s
                )
            """, (reminder_id, self.user_id))

            return cursor.rowcount > 0

    # =========================================================================
    # Recurring Tasks
    # =========================================================================

    async def generate_next_occurrence(self, task_id: int) -> Optional[dict]:
        """
        Generate the next occurrence of a recurring task.

        Called automatically when completing a recurring task.

        Args:
            task_id: The parent recurring task's ID

        Returns:
            New task dict, or None if not recurring or past end date
        """
        task = await self.get_task(task_id)
        if not task or not task["is_recurring"]:
            return None

        # Calculate next due date
        current_due = task["due_date"]
        if not current_due:
            # No due date - can't calculate next occurrence
            return None

        # Parse due date if it's a string
        if isinstance(current_due, str):
            current_due = datetime.fromisoformat(current_due)

        next_due = self._calculate_next_due_date(
            current_due,
            task["recurrence_pattern"],
            task["recurrence_interval"]
        )

        # Check if past end date
        if task["recurrence_end_date"]:
            end_date = task["recurrence_end_date"]
            if isinstance(end_date, str):
                end_date = datetime.fromisoformat(end_date)
            if next_due > end_date:
                logger.info(f"Recurring task {task_id} has reached end date")
                return None

        # Create next occurrence
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO tasks (
                    user_id, title, description, priority, due_date,
                    category, project, is_recurring, recurrence_pattern,
                    recurrence_interval, recurrence_end_date, parent_task_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                self.user_id,
                task["title"],
                task["description"],
                task["priority"],
                next_due.isoformat(),
                task["category"],
                task["project"],
                1,  # is_recurring
                task["recurrence_pattern"],
                task["recurrence_interval"],
                task["recurrence_end_date"],
                task_id  # parent_task_id
            ))

            new_task_id = cursor.fetchone()['id']
            logger.info(f"Generated next occurrence {new_task_id} from task {task_id}")

        return await self.get_task(new_task_id)

    def _calculate_next_due_date(
        self,
        current_due: datetime,
        pattern: str,
        interval: int
    ) -> datetime:
        """
        Calculate the next due date based on recurrence pattern.

        Args:
            current_due: Current due date
            pattern: Recurrence pattern (daily, weekly, monthly, yearly)
            interval: Every N periods

        Returns:
            Next due date
        """
        if pattern == "daily":
            return current_due + timedelta(days=interval)
        elif pattern == "weekly":
            return current_due + timedelta(weeks=interval)
        elif pattern == "monthly":
            return current_due + relativedelta(months=interval)
        elif pattern == "yearly":
            return current_due + relativedelta(years=interval)
        else:
            # Fallback to daily
            return current_due + timedelta(days=interval)

    # =========================================================================
    # Internal Helpers
    # =========================================================================

    def _row_to_dict(self, row) -> dict:
        """
        Convert a database row to a task dictionary.

        Args:
            row: sqlite3.Row object

        Returns:
            Task dict with all fields
        """
        # Check if type column exists (for backward compatibility during migration)
        task_type = row["type"] if "type" in row.keys() else "task"

        task = {
            "id": row["id"],
            "title": row["title"],
            "description": row["description"],
            "status": row["status"],
            "priority": row["priority"],
            "due_date": row["due_date"],
            "completed_at": row["completed_at"],
            "category": row["category"],
            "project": row["project"],
            "type": task_type,
            "is_recurring": bool(row["is_recurring"]),
            "recurrence_pattern": row["recurrence_pattern"],
            "recurrence_interval": row["recurrence_interval"],
            "recurrence_end_date": row["recurrence_end_date"],
            "parent_task_id": row["parent_task_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"]
        }

        # Add overdue flag for convenience
        if task["status"] == "pending" and task["due_date"]:
            now = datetime.now()
            due = task["due_date"] if isinstance(task["due_date"], datetime) else datetime.fromisoformat(str(task["due_date"]))
            task["is_overdue"] = due < now
        else:
            task["is_overdue"] = False

        return task
