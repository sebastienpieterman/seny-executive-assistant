"""
Notes Service for Seny - Phase 5

Provides note management with wiki-style links and tag support:
- CRUD operations on notes
- FTS5 full-text search with stemming
- #tag parsing and filtering
- [[wiki-link]] parsing and bi-directional link tracking

Usage:
    notes = NotesService(user_id)
    note = await notes.create_note("My Note", "Content with #tag and [[Link]]")
    results = await notes.search_notes("keyword")
"""

import re
import logging
from typing import Optional
from datetime import datetime

from web.core.database import get_db, extract_snippet

logger = logging.getLogger(__name__)


class NotesService:
    """
    Notes management service with tag and wiki-link support.

    One instance per user - do not share across users.

    Attributes:
        user_id: The user's database ID
    """

    # Regex patterns for parsing
    TAG_PATTERN = re.compile(r'#([a-zA-Z][a-zA-Z0-9_-]*)', re.UNICODE)
    LINK_PATTERN = re.compile(r'\[\[([^\]]+)\]\]', re.UNICODE)

    def __init__(self, user_id: int):
        """
        Initialize Notes service for a specific user.

        Args:
            user_id: User's database ID
        """
        self.user_id = user_id

    # =========================================================================
    # CRUD Operations
    # =========================================================================

    async def create_note(
        self,
        title: str,
        content: str,
        tags: list[str] = None
    ) -> dict:
        """
        Create a new note.

        Automatically extracts #tags from content and adds them to the tags list.
        Automatically parses [[wiki-links]] and creates link relationships.

        Args:
            title: Note title
            content: Note content (may contain #tags and [[links]])
            tags: Additional tags to add (beyond those in content)

        Returns:
            Created note dict with id, title, content, tags, created_at, updated_at
        """
        with get_db() as conn:
            cursor = conn.cursor()

            # Insert the note
            cursor.execute("""
                INSERT INTO notes (user_id, title, content)
                VALUES (%s, %s, %s)
                RETURNING id
            """, (self.user_id, title, content))

            note_id = cursor.fetchone()['id']

            # Parse and save tags
            content_tags = self._parse_tags(content)
            all_tags = set(content_tags)
            if tags:
                all_tags.update(t.lower().strip() for t in tags if t.strip())

            for tag in all_tags:
                try:
                    cursor.execute("""
                        INSERT INTO note_tags (note_id, tag) VALUES (%s, %s)
                    """, (note_id, tag.lower()))
                except Exception as e:
                    if "unique" not in str(e).lower() and "duplicate" not in str(e).lower():
                        raise

            # Parse and save links
            self._sync_links(cursor, note_id, content)

            # Sync reverse links - find other notes that reference this new note's title
            self._sync_reverse_links(cursor, note_id, title)

            logger.info(f"Created note {note_id}: {title[:50]}")

            return {
                "id": note_id,
                "title": title,
                "content": content,
                "tags": list(all_tags),
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat()
            }

    async def get_note(self, note_id: int) -> Optional[dict]:
        """
        Get a note by ID.

        Args:
            note_id: The note's ID

        Returns:
            Note dict or None if not found/not owned by user
        """
        with get_db() as conn:
            cursor = conn.cursor()

            # Get note (with user_id check for security)
            cursor.execute("""
                SELECT id, title, content, created_at, updated_at
                FROM notes
                WHERE id = %s AND user_id = %s
            """, (note_id, self.user_id))

            row = cursor.fetchone()
            if not row:
                return None

            # Get tags for this note
            cursor.execute("""
                SELECT tag FROM note_tags WHERE note_id = %s
            """, (note_id,))
            tags = [r["tag"] for r in cursor.fetchall()]

            return {
                "id": row["id"],
                "title": row["title"],
                "content": row["content"],
                "tags": tags,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"]
            }

    async def update_note(
        self,
        note_id: int,
        title: str = None,
        content: str = None,
        tags: list[str] = None
    ) -> Optional[dict]:
        """
        Update an existing note.

        Args:
            note_id: The note's ID
            title: New title (optional)
            content: New content (optional)
            tags: Replace all tags with this list (optional)

        Returns:
            Updated note dict or None if not found/not owned
        """
        # First verify ownership
        existing = await self.get_note(note_id)
        if not existing:
            return None

        with get_db() as conn:
            cursor = conn.cursor()

            # Build update query dynamically
            updates = []
            params = []

            if title is not None:
                updates.append("title = %s")
                params.append(title)

            if content is not None:
                updates.append("content = %s")
                params.append(content)

            if updates:
                updates.append("updated_at = CURRENT_TIMESTAMP")
                params.append(note_id)
                params.append(self.user_id)

                cursor.execute(f"""
                    UPDATE notes
                    SET {', '.join(updates)}
                    WHERE id = %s AND user_id = %s
                """, params)

            # Handle tags
            final_content = content if content is not None else existing["content"]

            if tags is not None or content is not None:
                # Delete existing tags
                cursor.execute("DELETE FROM note_tags WHERE note_id = %s", (note_id,))

                # Combine explicit tags with content tags
                content_tags = self._parse_tags(final_content)
                all_tags = set(content_tags)
                if tags:
                    all_tags.update(t.lower().strip() for t in tags if t.strip())

                # Insert new tags
                for tag in all_tags:
                    try:
                        cursor.execute("""
                            INSERT INTO note_tags (note_id, tag) VALUES (%s, %s)
                        """, (note_id, tag.lower()))
                    except Exception as e:
                        if "unique" not in str(e).lower() and "duplicate" not in str(e).lower():
                            raise

            # Re-sync links if content changed
            if content is not None:
                self._sync_links(cursor, note_id, content)

            # Re-sync reverse links if title changed (other notes may reference new title)
            if title is not None:
                self._sync_reverse_links(cursor, note_id, title)

            logger.info(f"Updated note {note_id}")

        # Return fresh copy
        return await self.get_note(note_id)

    async def delete_note(self, note_id: int) -> bool:
        """
        Delete a note.

        Also removes associated tags and links (via CASCADE).

        Args:
            note_id: The note's ID

        Returns:
            True if deleted, False if not found/not owned
        """
        with get_db() as conn:
            cursor = conn.cursor()

            # Delete with user_id check for security
            cursor.execute("""
                DELETE FROM notes WHERE id = %s AND user_id = %s
            """, (note_id, self.user_id))

            deleted = cursor.rowcount > 0

            if deleted:
                logger.info(f"Deleted note {note_id}")

            return deleted

    async def list_notes(
        self,
        limit: int = 50,
        offset: int = 0
    ) -> list[dict]:
        """
        List user's notes, most recently updated first.

        Args:
            limit: Maximum number of notes to return
            offset: Number of notes to skip (for pagination)

        Returns:
            List of note dicts (without full content, just preview)
        """
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT id, title, content, created_at, updated_at
                FROM notes
                WHERE user_id = %s
                ORDER BY updated_at DESC
                LIMIT %s OFFSET %s
            """, (self.user_id, limit, offset))

            notes = []
            for row in cursor.fetchall():
                # Get tags for each note
                cursor.execute("""
                    SELECT tag FROM note_tags WHERE note_id = %s
                """, (row["id"],))
                tags = [r["tag"] for r in cursor.fetchall()]

                # Create preview (first 200 chars)
                content = row["content"]
                preview = content[:200] + "..." if len(content) > 200 else content

                notes.append({
                    "id": row["id"],
                    "title": row["title"],
                    "preview": preview,
                    "tags": tags,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"]
                })

            return notes

    # =========================================================================
    # Search
    # =========================================================================

    async def search_notes(self, query: str, limit: int = 20) -> list[dict]:
        """
        Search notes using FTS5 full-text search.

        Uses porter stemming, so "running" matches "run", etc.

        Args:
            query: Search query
            limit: Maximum results to return

        Returns:
            List of matching notes with highlighted snippets
        """
        if not query or not query.strip():
            return []

        # Sanitize query for FTS5
        clean_query = query.strip()
        for char in ['"', "'", '(', ')', '*', ':', '^', '-', 'AND', 'OR', 'NOT', 'NEAR']:
            clean_query = clean_query.replace(char, ' ')

        words = [w.strip() for w in clean_query.split() if w.strip()]
        if not words:
            return []

        fts_query = ' '.join(words)

        pattern = f'%{fts_query}%'

        with get_db() as conn:
            cursor = conn.cursor()

            try:
                cursor.execute("""
                    SELECT
                        n.id,
                        n.title,
                        n.content,
                        n.created_at,
                        n.updated_at
                    FROM notes n
                    WHERE (n.title ILIKE %s OR n.content ILIKE %s)
                    AND n.user_id = %s
                    ORDER BY n.updated_at DESC
                    LIMIT %s
                """, (pattern, pattern, self.user_id, limit))

                rows = cursor.fetchall()
                results = []
                for row in rows:
                    row_dict = dict(row)
                    row_dict['snippet'] = extract_snippet(
                        (row_dict.get('content') or '') + ' ' + (row_dict.get('title') or ''),
                        query
                    )
                    # Get tags
                    cursor.execute("""
                        SELECT tag FROM note_tags WHERE note_id = %s
                    """, (row_dict["id"],))
                    tags = [r["tag"] for r in cursor.fetchall()]

                    results.append({
                        "id": row_dict["id"],
                        "title": row_dict["title"],
                        "snippet": row_dict["snippet"],
                        "tags": tags,
                        "created_at": row_dict["created_at"],
                        "updated_at": row_dict["updated_at"]
                    })

                return results

            except Exception as e:
                logger.error(f"Notes search error: {e}")
                return []

    async def get_notes_by_tag(self, tag: str, limit: int = 50) -> list[dict]:
        """
        Get all notes with a specific tag.

        Args:
            tag: Tag to filter by (without # prefix)
            limit: Maximum number of notes to return

        Returns:
            List of notes with that tag
        """
        tag = tag.lower().strip().lstrip('#')

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT n.id, n.title, n.content, n.created_at, n.updated_at
                FROM notes n
                JOIN note_tags nt ON n.id = nt.note_id
                WHERE nt.tag = %s AND n.user_id = %s
                ORDER BY n.updated_at DESC
                LIMIT %s
            """, (tag, self.user_id, limit))

            notes = []
            for row in cursor.fetchall():
                # Get all tags for this note
                cursor.execute("""
                    SELECT tag FROM note_tags WHERE note_id = %s
                """, (row["id"],))
                tags = [r["tag"] for r in cursor.fetchall()]

                content = row["content"]
                preview = content[:200] + "..." if len(content) > 200 else content

                notes.append({
                    "id": row["id"],
                    "title": row["title"],
                    "preview": preview,
                    "tags": tags,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"]
                })

            return notes

    # =========================================================================
    # Tags
    # =========================================================================

    async def list_all_tags(self) -> list[dict]:
        """
        List all tags used by this user with note counts.

        Returns:
            List of dicts with tag name and count
        """
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT nt.tag, COUNT(*) as count
                FROM note_tags nt
                JOIN notes n ON n.id = nt.note_id
                WHERE n.user_id = %s
                GROUP BY nt.tag
                ORDER BY count DESC, nt.tag ASC
            """, (self.user_id,))

            return [
                {"tag": row["tag"], "count": row["count"]}
                for row in cursor.fetchall()
            ]

    async def add_tag(self, note_id: int, tag: str) -> bool:
        """
        Add a tag to a note.

        Args:
            note_id: Note ID
            tag: Tag to add (without # prefix)

        Returns:
            True if added, False if note not found or tag already exists
        """
        # Verify ownership first
        existing = await self.get_note(note_id)
        if not existing:
            return False

        tag = tag.lower().strip().lstrip('#')

        with get_db() as conn:
            cursor = conn.cursor()

            try:
                cursor.execute("""
                    INSERT INTO note_tags (note_id, tag) VALUES (%s, %s)
                """, (note_id, tag))
                return True
            except Exception as e:
                if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                    return False  # Tag already exists
                raise

    async def remove_tag(self, note_id: int, tag: str) -> bool:
        """
        Remove a tag from a note.

        Args:
            note_id: Note ID
            tag: Tag to remove (without # prefix)

        Returns:
            True if removed, False if note not found or tag didn't exist
        """
        # Verify ownership first
        existing = await self.get_note(note_id)
        if not existing:
            return False

        tag = tag.lower().strip().lstrip('#')

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                DELETE FROM note_tags WHERE note_id = %s AND tag = %s
            """, (note_id, tag))

            return cursor.rowcount > 0

    # =========================================================================
    # Links
    # =========================================================================

    async def get_linked_notes(self, note_id: int) -> list[dict]:
        """
        Get notes that this note links TO (outgoing links).

        Args:
            note_id: Note ID to get links from

        Returns:
            List of linked note dicts
        """
        # Verify ownership
        existing = await self.get_note(note_id)
        if not existing:
            return []

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT n.id, n.title, n.created_at, n.updated_at
                FROM note_links nl
                JOIN notes n ON n.id = nl.target_note_id
                WHERE nl.source_note_id = %s AND n.user_id = %s
            """, (note_id, self.user_id))

            return [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"]
                }
                for row in cursor.fetchall()
            ]

    async def get_backlinks(self, note_id: int) -> list[dict]:
        """
        Get notes that link TO this note (incoming links / backlinks).

        Args:
            note_id: Note ID to get backlinks for

        Returns:
            List of notes that link to this one
        """
        # Verify ownership
        existing = await self.get_note(note_id)
        if not existing:
            return []

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT n.id, n.title, n.created_at, n.updated_at
                FROM note_links nl
                JOIN notes n ON n.id = nl.source_note_id
                WHERE nl.target_note_id = %s AND n.user_id = %s
            """, (note_id, self.user_id))

            return [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"]
                }
                for row in cursor.fetchall()
            ]

    async def get_graph_data(self) -> dict:
        """
        Get all notes and links for graph visualization.

        Returns:
            Dict with nodes (notes with tags), edges (links), and tag_clusters for D3.js
        """
        with get_db() as conn:
            cursor = conn.cursor()

            # Get all notes as nodes with content length for size
            cursor.execute("""
                SELECT id, title, LENGTH(content) as content_len
                FROM notes WHERE user_id = %s
            """, (self.user_id,))

            notes_data = cursor.fetchall()

            # Get tags for each note
            note_tags = {}
            tag_notes = {}  # tag -> list of note IDs (for clusters)

            for note in notes_data:
                note_id = note["id"]
                cursor.execute("""
                    SELECT tag FROM note_tags WHERE note_id = %s
                """, (note_id,))
                tags = [row["tag"] for row in cursor.fetchall()]
                note_tags[note_id] = tags

                # Build tag clusters
                for tag in tags:
                    if tag not in tag_notes:
                        tag_notes[tag] = []
                    tag_notes[tag].append(note_id)

            nodes = [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "tags": note_tags.get(row["id"], []),
                    "size": max(10, min(50, (row["content_len"] or 0) // 20 + 10))
                }
                for row in notes_data
            ]

            # Get all links as edges
            cursor.execute("""
                SELECT nl.source_note_id, nl.target_note_id
                FROM note_links nl
                JOIN notes n1 ON n1.id = nl.source_note_id
                JOIN notes n2 ON n2.id = nl.target_note_id
                WHERE n1.user_id = %s AND n2.user_id = %s
            """, (self.user_id, self.user_id))

            edges = [
                {"source": row["source_note_id"], "target": row["target_note_id"], "type": "link"}
                for row in cursor.fetchall()
            ]

            # Build tag clusters
            tag_clusters = [
                {"tag": tag, "notes": note_ids}
                for tag, note_ids in tag_notes.items()
            ]

            return {"nodes": nodes, "edges": edges, "tag_clusters": tag_clusters}

    # =========================================================================
    # Internal Helpers
    # =========================================================================

    def _parse_tags(self, content: str) -> list[str]:
        """
        Extract #tags from content.

        Rules:
        - Tags start with #
        - Must begin with a letter
        - Can contain letters, numbers, underscores, hyphens
        - Case insensitive (normalized to lowercase)

        Args:
            content: Text to parse

        Returns:
            List of tag strings (without # prefix, lowercase)
        """
        if not content:
            return []

        matches = self.TAG_PATTERN.findall(content)
        return list(set(t.lower() for t in matches))

    def _parse_links(self, content: str) -> list[str]:
        """
        Extract [[wiki-links]] from content.

        Args:
            content: Text to parse

        Returns:
            List of link targets (note titles referenced)
        """
        if not content:
            return []

        matches = self.LINK_PATTERN.findall(content)
        return list(set(matches))

    def _sync_links(self, cursor, note_id: int, content: str) -> None:
        """
        Update note_links table based on [[links]] in content.

        Resolves link targets by matching note titles (case insensitive).
        Only creates links to notes owned by the same user.

        Args:
            cursor: Database cursor
            note_id: Source note ID
            content: Note content with [[links]]
        """
        # Delete existing outgoing links
        cursor.execute("""
            DELETE FROM note_links WHERE source_note_id = %s
        """, (note_id,))

        # Parse links from content
        link_targets = self._parse_links(content)

        if not link_targets:
            return

        # Resolve each link to a note ID
        for target_title in link_targets:
            # Find note by title (case insensitive, same user)
            cursor.execute("""
                SELECT id FROM notes
                WHERE user_id = %s AND title ILIKE %s
                LIMIT 1
            """, (self.user_id, target_title.strip()))

            row = cursor.fetchone()
            if row and row["id"] != note_id:  # Don't link to self
                cursor.execute("""
                    INSERT INTO note_links (source_note_id, target_note_id)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                """, (note_id, row["id"]))

    def _sync_reverse_links(self, cursor, note_id: int, title: str) -> None:
        """
        Find other notes that contain [[title]] and create links TO this note.

        This handles the case where Note A is created with [[Note B]], but
        Note B doesn't exist yet. When Note B is later created, this method
        finds Note A and creates the link from A to B.

        Args:
            cursor: Database cursor
            note_id: The newly created/renamed note's ID
            title: The note's title to search for in other notes
        """
        # Search for other notes containing [[title]] (case insensitive)
        # Using LIKE with the exact pattern [[title]]
        pattern = f"%[[{title}]]%"

        cursor.execute("""
            SELECT id, content FROM notes
            WHERE user_id = %s AND id != %s AND content ILIKE %s
        """, (self.user_id, note_id, pattern))

        for row in cursor.fetchall():
            source_id = row["id"]
            cursor.execute("""
                INSERT INTO note_links (source_note_id, target_note_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
            """, (source_id, note_id))
            logger.debug(f"Created reverse link: note {source_id} -> note {note_id}")
