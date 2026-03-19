"""
Memory service for Seny's persistent memory system (Phase 02-04-01).

Stores and retrieves memories that Seny learns across conversations.
Memories are injected into the system prompt on every chat so Seny
can apply corrections and preferences from previous sessions.
"""

import logging
from web.core.database import get_db

logger = logging.getLogger(__name__)


class MemoryService:

    @staticmethod
    def get_memories(user_id: int) -> list[dict]:
        """Load all active memories for a user. Called on every chat to inject into system prompt."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, memory, category, created_at FROM user_memories WHERE user_id = %s ORDER BY created_at DESC",
                (user_id,)
            )
            rows = cursor.fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def save_memory(user_id: int, memory: str, category: str = 'general', conversation_id: str = None) -> int:
        """Save a new memory. Returns the new memory ID."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO user_memories (user_id, memory, category, conversation_id) VALUES (%s, %s, %s, %s) RETURNING id",
                (user_id, memory.strip(), category, conversation_id)
            )
            return cursor.fetchone()['id']

    @staticmethod
    def update_memory(user_id: int, memory_id: int, memory: str, category: str = None) -> bool:
        """Update an existing memory's text (and optionally category). Returns True if updated, False if not found."""
        with get_db() as conn:
            cursor = conn.cursor()
            if category:
                cursor.execute(
                    "UPDATE user_memories SET memory = %s, category = %s WHERE id = %s AND user_id = %s",
                    (memory.strip(), category, memory_id, user_id)
                )
            else:
                cursor.execute(
                    "UPDATE user_memories SET memory = %s WHERE id = %s AND user_id = %s",
                    (memory.strip(), memory_id, user_id)
                )
            return cursor.rowcount > 0

    @staticmethod
    def delete_memory(user_id: int, memory_id: int) -> bool:
        """Delete a memory. Returns True if deleted, False if not found."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM user_memories WHERE id = %s AND user_id = %s",
                (memory_id, user_id)
            )
            return cursor.rowcount > 0
