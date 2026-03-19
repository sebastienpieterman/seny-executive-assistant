"""
Browser History Service for Seny - Phase 7

Provides access to synced browser history from local agents:
- Search history by URL/title
- Get recent browsing history
- Domain usage statistics
- Privacy controls (delete history)

Usage:
    history = HistoryService(user_id)
    results = await history.search_history("github")
    recent = await history.get_recent(limit=20)
"""

import logging
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse

from web.core.database import (
    get_db,
    save_browser_history_batch,
    search_browser_history,
    get_recent_browser_history,
    get_domain_stats,
    get_browser_history_by_date,
    delete_browser_history,
    update_sync_status,
    get_sync_status
)

logger = logging.getLogger(__name__)


class HistoryService:
    """
    Browser history service for accessing synced local history.

    One instance per user - do not share across users.

    Attributes:
        user_id: The user's database ID
    """

    def __init__(self, user_id: int):
        """
        Initialize History service for a specific user.

        Args:
            user_id: User's database ID
        """
        self.user_id = user_id

    # =========================================================================
    # Sync Operations (called by agent)
    # =========================================================================

    async def sync_history(
        self,
        machine_id: str,
        entries: list[dict]
    ) -> dict:
        """
        Receive batch of history entries from local agent.

        Processes entries to extract domains and stores them.
        Updates sync status for the machine.

        Args:
            machine_id: Identifier for the source machine
            entries: List of history entries with url, title, visit_time

        Returns:
            Dict with sync results (inserted_count, skipped_count)
        """
        logger.info(f"Syncing {len(entries)} history entries from {machine_id}")

        # Process entries to extract domains
        processed_entries = []
        for entry in entries:
            url = entry.get("url", "")
            domain = self._extract_domain(url)
            processed_entries.append({
                "url": url,
                "title": entry.get("title"),
                "visit_time": entry.get("visit_time"),
                "visit_count": entry.get("visit_count", 1),
                "domain": domain
            })

        # Save to database
        result = save_browser_history_batch(
            self.user_id,
            machine_id,
            processed_entries
        )

        # Update sync status
        update_sync_status(
            self.user_id,
            machine_id,
            "browser_history",
            sync_count=result["inserted_count"],
            status="active"
        )

        logger.info(
            f"Sync complete: {result['inserted_count']} inserted, "
            f"{result['skipped_count']} skipped"
        )

        return result

    async def get_sync_status_for_machines(
        self,
        machine_id: str = None
    ) -> list[dict]:
        """
        Get sync status for user's machines.

        Args:
            machine_id: Optional filter by specific machine

        Returns:
            List of sync status entries
        """
        return get_sync_status(self.user_id, machine_id)

    # =========================================================================
    # Query Operations
    # =========================================================================

    async def search_history(
        self,
        query: str,
        limit: int = 20,
        since: datetime = None,
        domain: str = None
    ) -> list[dict]:
        """
        Search browsing history by text.

        Matches against URL and title.

        Args:
            query: Search query string
            limit: Maximum results (default 20)
            since: Optional datetime to filter from
            domain: Optional domain filter

        Returns:
            List of matching history entries
        """
        if not query or not query.strip():
            return []

        since_str = since.isoformat() if since else None

        results = search_browser_history(
            self.user_id,
            query.strip(),
            limit,
            since_str,
            domain
        )

        logger.debug(f"History search '{query}' returned {len(results)} results")
        return results

    async def get_recent(
        self,
        limit: int = 50,
        machine_id: str = None
    ) -> list[dict]:
        """
        Get most recent browsing history.

        Args:
            limit: Maximum entries (default 50)
            machine_id: Optional filter by specific machine

        Returns:
            List of recent history entries
        """
        return get_recent_browser_history(self.user_id, limit, machine_id)

    async def get_domain_stats(
        self,
        since: datetime = None,
        limit: int = 20
    ) -> list[dict]:
        """
        Get most visited domains with counts.

        Args:
            since: Optional datetime to filter from
            limit: Maximum domains (default 20)

        Returns:
            List of domain stats with visit counts
        """
        since_str = since.isoformat() if since else None
        return get_domain_stats(self.user_id, since_str, limit)

    async def get_history_by_date(self, date: datetime) -> list[dict]:
        """
        Get all history for a specific date.

        Args:
            date: The date to get history for

        Returns:
            List of history entries for that date
        """
        date_str = date.strftime("%Y-%m-%d")
        return get_browser_history_by_date(self.user_id, date_str)

    # =========================================================================
    # Management Operations
    # =========================================================================

    async def delete_history(
        self,
        before: datetime = None,
        domain: str = None
    ) -> int:
        """
        Delete history entries (privacy control).

        Args:
            before: Optional datetime - delete entries before this time
            domain: Optional domain - delete only entries from this domain

        Returns:
            Number of entries deleted
        """
        before_str = before.isoformat() if before else None

        deleted = delete_browser_history(self.user_id, before_str, domain)

        logger.info(
            f"Deleted {deleted} history entries "
            f"(before={before}, domain={domain})"
        )

        return deleted

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _extract_domain(self, url: str) -> Optional[str]:
        """
        Extract domain from URL.

        Args:
            url: Full URL string

        Returns:
            Domain string or None if invalid
        """
        if not url:
            return None

        try:
            parsed = urlparse(url)
            domain = parsed.netloc

            # Remove port if present
            if ":" in domain:
                domain = domain.split(":")[0]

            # Remove www. prefix
            if domain.startswith("www."):
                domain = domain[4:]

            return domain if domain else None
        except Exception:
            return None
