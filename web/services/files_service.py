"""
Files Service for Seny - Phase 7

Provides local file index management:
- Sync file metadata from desktop agents
- FTS5 full-text search on filenames and content
- Filter by extension, folder, date
- Track deleted files

Usage:
    files = FilesService(user_id)
    results = await files.search_files("Johnson wedding")
    recent = await files.get_recent_files(days=7)
"""

import logging
from typing import Optional

from web.core.database import (
    save_local_files_batch,
    search_local_files,
    get_recent_local_files,
    get_local_files_by_extension,
    mark_local_files_deleted,
    get_local_file_stats,
    get_local_file_paths,
    update_sync_status
)

logger = logging.getLogger(__name__)


class FilesService:
    """
    Local file index management service.

    One instance per user - do not share across users.

    Attributes:
        user_id: The user's database ID
    """

    def __init__(self, user_id: int):
        """
        Initialize Files service for a specific user.

        Args:
            user_id: User's database ID
        """
        self.user_id = user_id

    # =========================================================================
    # Sync Operations (called by desktop agent)
    # =========================================================================

    async def sync_files(
        self,
        machine_id: str,
        files: list[dict]
    ) -> dict:
        """
        Sync a batch of files from the desktop agent.

        Files are inserted or updated based on the unique constraint
        (user_id, machine_id, file_path).

        Args:
            machine_id: Identifier for the source machine
            files: List of file metadata dicts with:
                - file_path: Full path (required)
                - file_name: Just the filename (required)
                - file_extension: Extension with dot (e.g., '.mp4')
                - file_size: Size in bytes
                - file_created: ISO datetime string
                - file_modified: ISO datetime string
                - content_preview: First 10KB of text content
                - drive_letter: Drive letter (e.g., 'D:')
                - parent_folder: Parent directory path

        Returns:
            Dict with inserted_count, updated_count
        """
        logger.info(
            f"Syncing {len(files)} files from machine {machine_id} "
            f"for user {self.user_id}"
        )

        result = save_local_files_batch(self.user_id, machine_id, files)

        # Update sync status
        total_synced = result["inserted_count"] + result["updated_count"]
        update_sync_status(
            self.user_id,
            machine_id,
            "files",
            sync_count=total_synced,
            status="active"
        )

        logger.info(
            f"Sync complete: {result['inserted_count']} inserted, "
            f"{result['updated_count']} updated"
        )

        return result

    async def mark_deleted(
        self,
        machine_id: str,
        file_paths: list[str]
    ) -> int:
        """
        Mark files as deleted when agent detects they've been removed.

        Args:
            machine_id: Machine the files are from
            file_paths: List of file paths to mark as deleted

        Returns:
            Number of files marked as deleted
        """
        count = mark_local_files_deleted(self.user_id, machine_id, file_paths)
        logger.info(f"Marked {count} files as deleted from machine {machine_id}")
        return count

    async def get_synced_paths(self, machine_id: str) -> list[str]:
        """
        Get all synced file paths for a machine.

        Used by desktop agent to detect deleted files.

        Args:
            machine_id: Machine identifier

        Returns:
            List of file paths currently synced (not deleted)
        """
        return get_local_file_paths(self.user_id, machine_id)

    # =========================================================================
    # Search Operations (called by Claude tools and API)
    # =========================================================================

    async def search_files(
        self,
        query: str,
        file_type: str = None,
        folder: str = None,
        modified_since: str = None,
        limit: int = 20
    ) -> list[dict]:
        """
        Search files by name or content.

        Uses FTS5 full-text search with porter stemming for smart matching.

        Args:
            query: Search query (matches filename and content)
            file_type: Optional extension filter (e.g., '.mp4')
            folder: Optional folder path prefix filter
            modified_since: Optional ISO datetime filter
            limit: Maximum results (default 20)

        Returns:
            List of matching file dicts with snippet highlighting
        """
        results = search_local_files(
            self.user_id,
            query,
            file_type=file_type,
            folder=folder,
            modified_since=modified_since,
            include_deleted=False,
            limit=limit
        )

        logger.debug(f"Search '{query}' returned {len(results)} results")
        return results

    async def get_recent_files(
        self,
        days: int = 7,
        file_type: str = None,
        machine_id: str = None,
        limit: int = 20
    ) -> list[dict]:
        """
        Get recently modified files.

        Args:
            days: Number of days back to look (default 7)
            file_type: Optional extension filter
            machine_id: Optional machine filter
            limit: Maximum results (default 20)

        Returns:
            List of recent file dicts
        """
        return get_recent_local_files(
            self.user_id,
            days=days,
            file_type=file_type,
            machine_id=machine_id,
            limit=limit
        )

    async def get_files_by_type(
        self,
        extension: str,
        folder: str = None,
        limit: int = 50
    ) -> list[dict]:
        """
        Get files by extension.

        Args:
            extension: File extension (e.g., '.mp4', '.prproj')
            folder: Optional folder path prefix filter
            limit: Maximum results (default 50)

        Returns:
            List of matching file dicts
        """
        return get_local_files_by_extension(
            self.user_id,
            extension=extension,
            folder=folder,
            limit=limit
        )

    async def get_stats(self) -> dict:
        """
        Get file statistics.

        Returns:
            Dict with total_files, by_extension, by_drive, by_machine
        """
        return get_local_file_stats(self.user_id)

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def format_file_size(self, size_bytes: int) -> str:
        """
        Format file size in human-readable format.

        Args:
            size_bytes: Size in bytes

        Returns:
            Formatted string (e.g., '1.5 GB', '256 KB')
        """
        if size_bytes is None:
            return "Unknown"

        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if abs(size_bytes) < 1024.0:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f} PB"

    def format_file_result(self, file: dict) -> str:
        """
        Format a file result for display.

        Args:
            file: File dict from search/query

        Returns:
            Formatted string for display
        """
        size = self.format_file_size(file.get("file_size"))
        modified = file.get("file_modified", "Unknown date")
        if modified and "T" in modified:
            modified = modified.split("T")[0]  # Just the date

        result = f"📄 {file['file_name']}\n"
        result += f"   Path: {file['file_path']}\n"
        result += f"   Size: {size} | Modified: {modified}"

        if file.get("snippet"):
            result += f"\n   Match: ...{file['snippet']}..."

        return result
