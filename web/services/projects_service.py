"""
Projects Service for Second Brain project tracking.

Manages the Projects Database with GTD-style next actions and status management.
Users can track projects, set concrete next actions, and get project insights.

Usage:
    service = ProjectsService(user_id)
    project = await service.create_project("Website Redesign", next_action="Draft homepage wireframe")
    insights = await service.get_project_insights()
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from web.core.database import (
    create_project as db_create_project,
    get_project as db_get_project,
    get_projects_by_user,
    update_project as db_update_project,
    delete_project as db_delete_project,
    search_projects as db_search_projects,
    get_db,
)

logger = logging.getLogger(__name__)


class ProjectsService:
    """
    Service for managing the Projects Database with GTD-style next actions.

    Provides:
    - Project CRUD with status management
    - GTD-style next action tracking
    - Project insights (stuck, waiting, actionable)

    Status definitions:
    - active: Currently working on this
    - waiting: Waiting on someone/something external
    - blocked: Stuck, needs problem solved
    - someday: Want to do eventually, not now
    - done: Completed
    """

    VALID_STATUSES = {'active', 'waiting', 'blocked', 'someday', 'done'}

    def __init__(self, user_id: int):
        """
        Initialize Projects service for a specific user.

        Args:
            user_id: User's database ID
        """
        self.user_id = user_id

    async def create_project(
        self,
        name: str,
        next_action: str = None,
        notes: str = None,
        status: str = 'active'
    ) -> dict:
        """
        Create a new project. next_action should be concrete and executable.

        If a project with the same name already exists, returns the existing project
        with an 'already_existed' flag instead of creating a duplicate.

        Args:
            name: Project name
            next_action: GTD-style next executable action (physical, visible)
            notes: Project notes
            status: Initial status (default: active)

        Returns:
            Created or existing project dict (with 'already_existed' flag if duplicate)
        """
        if status not in self.VALID_STATUSES:
            status = 'active'

        # Check for existing project with same name (prevents duplicates)
        existing = await self.get_project_by_name(name)
        if existing:
            logger.info(f"Project '{name}' already exists (id={existing['id']}), returning existing instead of creating duplicate")

            # If next_action was provided, update the existing project's next_action
            if next_action:
                updated = await self.update_project(existing['id'], next_action=next_action)
                if updated:
                    return {
                        **updated,
                        'already_existed': True,
                        'next_action_updated': True,
                        'message': f"Project '{updated['name']}' already exists - updated next action"
                    }

            # Return existing project with flag
            return {
                **existing,
                'already_existed': True,
                'message': f"Project '{existing['name']}' already exists"
            }

        # Create new project
        project_id = db_create_project(
            user_id=self.user_id,
            name=name,
            next_action=next_action,
            notes=notes,
            status=status
        )

        if not project_id:
            raise ValueError("Failed to create project")

        return db_get_project(project_id)

    async def get_project(self, project_id: int) -> Optional[dict]:
        """
        Get project details.

        Args:
            project_id: Project's database ID

        Returns:
            Project dict or None if not found
        """
        project = db_get_project(project_id)
        if not project:
            return None

        # Verify ownership
        if project.get('user_id') != self.user_id:
            return None

        return project

    async def get_project_by_name(self, name: str) -> Optional[dict]:
        """
        Find project by name (fuzzy match).

        Args:
            name: Project name to look up

        Returns:
            Project dict or None if not found
        """
        # First try exact match (case-insensitive)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id FROM projects
                WHERE user_id = %s AND LOWER(name) = LOWER(%s)
            """, (self.user_id, name))
            row = cursor.fetchone()
            if row:
                return await self.get_project(row["id"])

        # Fall back to FTS search
        results = db_search_projects(self.user_id, name, limit=1)
        if results:
            return await self.get_project(results[0]["id"])

        return None

    async def list_projects(self, status: str = None) -> list:
        """
        List projects, optionally filtered by status.

        Args:
            status: Filter by status ('active', 'waiting', 'blocked', 'someday', 'done')
                    or None for all non-done projects

        Returns:
            List of project dicts
        """
        if status and status not in self.VALID_STATUSES:
            status = None

        return get_projects_by_user(self.user_id, status=status)

    async def search_projects(self, query: str) -> list:
        """
        FTS search across projects.

        Args:
            query: Search query

        Returns:
            List of matching project dicts
        """
        return db_search_projects(self.user_id, query, limit=20)

    async def update_project(self, project_id: int, **fields) -> Optional[dict]:
        """
        Update project fields. Auto-updates updated_at.

        Args:
            project_id: Project's database ID
            **fields: Fields to update (name, status, next_action, notes)

        Returns:
            Updated project dict or None if not found
        """
        # Verify ownership first
        project = await self.get_project(project_id)
        if not project:
            return None

        # Validate status if provided
        if 'status' in fields and fields['status'] not in self.VALID_STATUSES:
            del fields['status']

        success = db_update_project(project_id, **fields)
        if not success:
            return None

        return await self.get_project(project_id)

    async def set_next_action(self, project_id: int, next_action: str) -> Optional[dict]:
        """
        Set the next action for a project. Should be concrete and executable.

        Args:
            project_id: Project's database ID
            next_action: Concrete next physical action

        Returns:
            Updated project dict or None if not found
        """
        return await self.update_project(project_id, next_action=next_action)

    async def update_status(self, project_id: int, status: str) -> Optional[dict]:
        """
        Update project status.

        Valid statuses: active, waiting, blocked, someday, done

        Args:
            project_id: Project's database ID
            status: New status

        Returns:
            Updated project dict or None if not found/invalid status
        """
        if status not in self.VALID_STATUSES:
            return None

        return await self.update_project(project_id, status=status)

    async def complete_project(self, project_id: int) -> Optional[dict]:
        """
        Mark project as done.

        Args:
            project_id: Project's database ID

        Returns:
            Updated project dict or None if not found
        """
        return await self.update_status(project_id, 'done')

    async def delete_project(self, project_id: int) -> bool:
        """
        Delete a project.

        Args:
            project_id: Project's database ID

        Returns:
            True if deleted
        """
        # Verify ownership first
        project = await self.get_project(project_id)
        if not project:
            return False

        return db_delete_project(project_id)

    # =========================================================================
    # Project Insights
    # =========================================================================

    async def get_project_insights(self) -> dict:
        """
        Get project insights for daily digest.

        Returns:
            Dict with active_count, active_projects, stuck_projects,
            waiting_projects, and recently_completed
        """
        active = await self.get_actionable_projects()
        stuck = await self.get_stuck_projects()
        waiting = await self.list_projects(status='waiting')
        recent = await self.get_recently_completed(days=7)

        return {
            'active_count': len(active),
            'active_projects': active,
            'stuck_projects': stuck,
            'waiting_projects': waiting,
            'recently_completed': recent
        }

    async def get_stuck_projects(self) -> list:
        """
        Get projects that are active but have no next action or are blocked.

        Returns:
            List of stuck project dicts
        """
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_id, name, status, next_action, notes,
                       created_at, updated_at
                FROM projects
                WHERE user_id = %s
                AND (
                    (status = 'active' AND (next_action IS NULL OR next_action = ''))
                    OR status = 'blocked'
                )
                ORDER BY updated_at DESC
            """, (self.user_id,))

            return [dict(row) for row in cursor.fetchall()]

    async def get_actionable_projects(self) -> list:
        """
        Get active projects that have a next action defined.

        Returns:
            List of actionable project dicts
        """
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_id, name, status, next_action, notes,
                       created_at, updated_at
                FROM projects
                WHERE user_id = %s
                AND status = 'active'
                AND next_action IS NOT NULL
                AND next_action != ''
                ORDER BY updated_at DESC
            """, (self.user_id,))

            return [dict(row) for row in cursor.fetchall()]

    async def get_recently_completed(self, days: int = 7) -> list:
        """
        Get projects completed in the last X days.

        Args:
            days: Look back this many days

        Returns:
            List of recently completed project dicts
        """
        cutoff = datetime.utcnow() - timedelta(days=days)
        cutoff_str = cutoff.strftime("%Y-%m-%d")

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_id, name, status, next_action, notes,
                       created_at, updated_at
                FROM projects
                WHERE user_id = %s
                AND status = 'done'
                AND DATE(updated_at) >= %s
                ORDER BY updated_at DESC
            """, (self.user_id, cutoff_str))

            return [dict(row) for row in cursor.fetchall()]
