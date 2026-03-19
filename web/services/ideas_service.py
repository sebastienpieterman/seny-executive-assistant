"""
Ideas Service for Second Brain idea/insight tracking.

Manages the Ideas Database for capturing insights, thoughts, and concepts.
Users can capture ideas with tags, search them, and get random ideas for review.

Usage:
    service = IdeasService(user_id)
    idea = await service.create_idea("API Design", summary="REST vs GraphQL thoughts")
    random_idea = await service.get_random_idea()
"""

import logging
from datetime import datetime, timedelta
from typing import Optional
import random

from web.core.database import (
    create_idea as db_create_idea,
    get_idea as db_get_idea,
    get_ideas_by_user,
    update_idea as db_update_idea,
    delete_idea as db_delete_idea,
    search_ideas as db_search_ideas,
    get_db,
)

logger = logging.getLogger(__name__)


class IdeasService:
    """
    Service for managing the Ideas Database.

    Provides:
    - Idea CRUD with tagging support
    - FTS search across ideas
    - Random idea surfacing for review
    - Tag-based filtering
    """

    def __init__(self, user_id: int):
        """
        Initialize Ideas service for a specific user.

        Args:
            user_id: User's database ID
        """
        self.user_id = user_id

    async def create_idea(
        self,
        title: str,
        summary: str = None,
        notes: str = None,
        tags: str = None
    ) -> dict:
        """
        Capture a new idea.

        Args:
            title: Brief title for the idea
            summary: One-liner capturing the core insight
            notes: Elaboration or context
            tags: Comma-separated tags

        Returns:
            Created idea dict
        """
        idea_id = db_create_idea(
            user_id=self.user_id,
            title=title,
            summary=summary,
            notes=notes,
            tags=tags
        )

        if not idea_id:
            raise ValueError("Failed to create idea")

        return db_get_idea(idea_id)

    async def get_idea(self, idea_id: int) -> Optional[dict]:
        """
        Get idea details.

        Args:
            idea_id: Idea's database ID

        Returns:
            Idea dict or None if not found
        """
        idea = db_get_idea(idea_id)
        if not idea:
            return None

        # Verify ownership
        if idea.get('user_id') != self.user_id:
            return None

        return idea

    async def list_ideas(self, limit: int = 50) -> list:
        """
        List all ideas, most recent first.

        Args:
            limit: Maximum number of ideas to return

        Returns:
            List of idea dicts
        """
        return get_ideas_by_user(self.user_id, limit=limit)

    async def search_ideas(self, query: str) -> list:
        """
        FTS search across ideas.

        Args:
            query: Search query

        Returns:
            List of matching idea dicts
        """
        return db_search_ideas(self.user_id, query, limit=20)

    async def list_by_tag(self, tag: str) -> list:
        """
        Get ideas with a specific tag.

        Args:
            tag: Tag to filter by (case-insensitive)

        Returns:
            List of idea dicts with that tag
        """
        tag = tag.strip().lower()
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_id, title, summary, notes, tags,
                       created_at, updated_at
                FROM ideas
                WHERE user_id = %s
                AND (
                    tags ILIKE %s
                    OR tags ILIKE %s
                    OR tags ILIKE %s
                    OR tags ILIKE %s
                )
                ORDER BY created_at DESC
            """, (
                self.user_id,
                tag,  # exact match
                f'{tag},%',  # starts with tag
                f'%,{tag},%',  # tag in middle
                f'%,{tag}'  # ends with tag
            ))
            return [dict(row) for row in cursor.fetchall()]

    async def get_all_tags(self) -> list:
        """
        Get all unique tags used across ideas.

        Returns:
            List of unique tag strings
        """
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT tags FROM ideas
                WHERE user_id = %s AND tags IS NOT NULL AND tags != ''
            """, (self.user_id,))

            # Collect all tags from comma-separated strings
            all_tags = set()
            for row in cursor.fetchall():
                tags_str = row['tags']
                if tags_str:
                    for tag in tags_str.split(','):
                        tag = tag.strip().lower()
                        if tag:
                            all_tags.add(tag)

            return sorted(list(all_tags))

    async def update_idea(self, idea_id: int, **fields) -> Optional[dict]:
        """
        Update idea fields. Auto-updates updated_at.

        Args:
            idea_id: Idea's database ID
            **fields: Fields to update (title, summary, notes, tags)

        Returns:
            Updated idea dict or None if not found
        """
        # Verify ownership first
        idea = await self.get_idea(idea_id)
        if not idea:
            return None

        success = db_update_idea(idea_id, **fields)
        if not success:
            return None

        return await self.get_idea(idea_id)

    async def delete_idea(self, idea_id: int) -> bool:
        """
        Delete an idea.

        Args:
            idea_id: Idea's database ID

        Returns:
            True if deleted
        """
        # Verify ownership first
        idea = await self.get_idea(idea_id)
        if not idea:
            return False

        return db_delete_idea(idea_id)

    async def get_random_idea(self) -> Optional[dict]:
        """
        Get a random idea for inspiration/review.

        Returns:
            Random idea dict or None if no ideas exist
        """
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_id, title, summary, notes, tags,
                       created_at, updated_at
                FROM ideas
                WHERE user_id = %s
                ORDER BY RANDOM()
                LIMIT 1
            """, (self.user_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return dict(row)

    async def get_recent_ideas(self, days: int = 7) -> list:
        """
        Get ideas from the last N days.

        Args:
            days: Look back this many days

        Returns:
            List of recent idea dicts
        """
        cutoff = datetime.utcnow() - timedelta(days=days)
        cutoff_str = cutoff.strftime("%Y-%m-%d")

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_id, title, summary, notes, tags,
                       created_at, updated_at
                FROM ideas
                WHERE user_id = %s
                AND DATE(created_at) >= %s
                ORDER BY created_at DESC
            """, (self.user_id, cutoff_str))

            return [dict(row) for row in cursor.fetchall()]
