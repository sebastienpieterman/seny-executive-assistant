"""
Browser History endpoints for Seny - Phase 7.

History query API for accessing synced browser history:
- GET /api/history/search?q=... - Search history
- GET /api/history/recent - Get recent history
- GET /api/history/domains - Get domain statistics
- GET /api/history/date/{date} - Get history for specific date
- DELETE /api/history - Delete history (privacy)
"""

import logging
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, status, Depends, Query
from pydantic import BaseModel

from web.auth.jwt_utils import require_auth
from web.services.history_service import HistoryService

logger = logging.getLogger(__name__)


# Create history router
router = APIRouter()


# Response models
class HistoryEntry(BaseModel):
    """Browser history entry."""
    url: str
    title: Optional[str]
    visit_time: str
    visit_count: int
    domain: Optional[str]
    machine_id: str


class HistorySearchResponse(BaseModel):
    """Response for history search."""
    entries: list[HistoryEntry]
    count: int


class DomainStat(BaseModel):
    """Domain visit statistics."""
    domain: str
    visit_count: int
    last_visit: str


class DomainStatsResponse(BaseModel):
    """Response for domain statistics."""
    domains: list[DomainStat]
    count: int


class DeleteHistoryRequest(BaseModel):
    """Request for deleting history."""
    before: Optional[str] = None  # ISO datetime
    domain: Optional[str] = None


class DeleteHistoryResponse(BaseModel):
    """Response for delete operation."""
    deleted_count: int
    message: str


# ============================================================================
# History Query Endpoints
# ============================================================================

@router.get("/search", response_model=HistorySearchResponse)
async def search_history(
    q: str = Query(..., description="Search query"),
    limit: int = Query(20, ge=1, le=100, description="Maximum results"),
    since: Optional[str] = Query(None, description="ISO datetime to filter from"),
    domain: Optional[str] = Query(None, description="Filter by domain"),
    user_id: int = Depends(require_auth)
):
    """
    Search browsing history by URL or title.

    Args:
        q: Search query (matches URL and title)
        limit: Maximum number of results (1-100)
        since: Optional ISO datetime to filter from
        domain: Optional domain to filter by
        user_id: Authenticated user ID from JWT

    Returns:
        HistorySearchResponse with matching entries
    """
    try:
        history_service = HistoryService(user_id)

        # Parse since datetime if provided
        since_dt = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid 'since' datetime format. Use ISO format."
                )

        entries = await history_service.search_history(
            q,
            limit=limit,
            since=since_dt,
            domain=domain
        )

        return HistorySearchResponse(
            entries=[
                HistoryEntry(
                    url=e["url"],
                    title=e["title"],
                    visit_time=e["visit_time"],
                    visit_count=e["visit_count"],
                    domain=e["domain"],
                    machine_id=e["machine_id"]
                )
                for e in entries
            ],
            count=len(entries)
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"History search failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Search failed: {str(e)}"
        )


@router.get("/recent", response_model=HistorySearchResponse)
async def get_recent_history(
    limit: int = Query(50, ge=1, le=200, description="Maximum entries"),
    machine_id: Optional[str] = Query(None, description="Filter by machine"),
    user_id: int = Depends(require_auth)
):
    """
    Get most recent browsing history.

    Args:
        limit: Maximum number of entries (1-200)
        machine_id: Optional machine ID to filter by
        user_id: Authenticated user ID from JWT

    Returns:
        HistorySearchResponse with recent entries
    """
    try:
        history_service = HistoryService(user_id)

        entries = await history_service.get_recent(
            limit=limit,
            machine_id=machine_id
        )

        return HistorySearchResponse(
            entries=[
                HistoryEntry(
                    url=e["url"],
                    title=e["title"],
                    visit_time=e["visit_time"],
                    visit_count=e["visit_count"],
                    domain=e["domain"],
                    machine_id=e["machine_id"]
                )
                for e in entries
            ],
            count=len(entries)
        )

    except Exception as e:
        logger.error(f"Failed to get recent history: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get recent history: {str(e)}"
        )


@router.get("/domains", response_model=DomainStatsResponse)
async def get_domain_stats(
    limit: int = Query(20, ge=1, le=100, description="Maximum domains"),
    since: Optional[str] = Query(None, description="ISO datetime to filter from"),
    user_id: int = Depends(require_auth)
):
    """
    Get most visited domains with statistics.

    Args:
        limit: Maximum number of domains (1-100)
        since: Optional ISO datetime to filter from
        user_id: Authenticated user ID from JWT

    Returns:
        DomainStatsResponse with domain visit counts
    """
    try:
        history_service = HistoryService(user_id)

        # Parse since datetime if provided
        since_dt = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid 'since' datetime format. Use ISO format."
                )

        domains = await history_service.get_domain_stats(
            since=since_dt,
            limit=limit
        )

        return DomainStatsResponse(
            domains=[
                DomainStat(
                    domain=d["domain"],
                    visit_count=d["visit_count"],
                    last_visit=d["last_visit"]
                )
                for d in domains
            ],
            count=len(domains)
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get domain stats: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get domain stats: {str(e)}"
        )


@router.get("/date/{date}", response_model=HistorySearchResponse)
async def get_history_by_date(
    date: str,
    user_id: int = Depends(require_auth)
):
    """
    Get all history for a specific date.

    Args:
        date: Date in YYYY-MM-DD format
        user_id: Authenticated user ID from JWT

    Returns:
        HistorySearchResponse with all entries for that date
    """
    try:
        # Validate date format
        try:
            date_obj = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid date format. Use YYYY-MM-DD."
            )

        history_service = HistoryService(user_id)
        entries = await history_service.get_history_by_date(date_obj)

        return HistorySearchResponse(
            entries=[
                HistoryEntry(
                    url=e["url"],
                    title=e["title"],
                    visit_time=e["visit_time"],
                    visit_count=e["visit_count"],
                    domain=e["domain"],
                    machine_id=e["machine_id"]
                )
                for e in entries
            ],
            count=len(entries)
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get history for date {date}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get history: {str(e)}"
        )


# ============================================================================
# History Management Endpoints
# ============================================================================

@router.delete("", response_model=DeleteHistoryResponse)
async def delete_history(
    before: Optional[str] = Query(None, description="Delete entries before this ISO datetime"),
    domain: Optional[str] = Query(None, description="Delete entries from this domain only"),
    user_id: int = Depends(require_auth)
):
    """
    Delete browser history entries (privacy control).

    At least one filter (before or domain) should be provided.
    If neither is provided, returns error to prevent accidental full deletion.

    Args:
        before: Optional ISO datetime - delete entries before this time
        domain: Optional domain - delete only entries from this domain
        user_id: Authenticated user ID from JWT

    Returns:
        DeleteHistoryResponse with count of deleted entries
    """
    # Require at least one filter to prevent accidental full deletion
    if not before and not domain:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one filter (before or domain) is required"
        )

    try:
        # Parse before datetime if provided
        before_dt = None
        if before:
            try:
                before_dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid 'before' datetime format. Use ISO format."
                )

        history_service = HistoryService(user_id)
        deleted_count = await history_service.delete_history(
            before=before_dt,
            domain=domain
        )

        filters = []
        if before:
            filters.append(f"before {before}")
        if domain:
            filters.append(f"domain {domain}")

        return DeleteHistoryResponse(
            deleted_count=deleted_count,
            message=f"Deleted {deleted_count} entries ({', '.join(filters)})"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete history: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete history: {str(e)}"
        )
