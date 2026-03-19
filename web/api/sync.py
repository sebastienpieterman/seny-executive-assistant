"""
Sync endpoints for Seny - Phase 7.

Local agent sync API for receiving data from desktop agents:
- POST /api/sync/browser-history - Receive browser history batch
- GET /api/sync/status - Get sync status for all machines
- GET /api/sync/status/{machine_id} - Get sync status for specific machine
"""

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, status, Depends
from pydantic import BaseModel

from web.auth.jwt_utils import require_auth
from web.services.history_service import HistoryService
from web.services.files_service import FilesService

logger = logging.getLogger(__name__)


# Create sync router
router = APIRouter()


# Request/Response models
class HistoryEntry(BaseModel):
    """Single browser history entry from agent."""
    url: str
    title: Optional[str] = None
    visit_time: str  # ISO format datetime
    visit_count: Optional[int] = 1


class BrowserHistorySyncRequest(BaseModel):
    """Request model for browser history sync."""
    machine_id: str
    entries: list[HistoryEntry]


class SyncResult(BaseModel):
    """Response model for sync operation."""
    success: bool
    inserted_count: int
    skipped_count: int
    message: str


class SyncStatusEntry(BaseModel):
    """Sync status for a machine/type combination."""
    machine_id: str
    sync_type: str
    last_sync_time: Optional[str]
    last_sync_count: int
    status: str
    error_message: Optional[str]
    created_at: str
    updated_at: str


class SyncStatusResponse(BaseModel):
    """Response model for sync status."""
    statuses: list[SyncStatusEntry]


# ============================================================================
# Local Files Sync Models
# ============================================================================

class FileEntry(BaseModel):
    """Single file entry from desktop agent."""
    file_path: str
    file_name: str
    file_extension: Optional[str] = None
    file_size: Optional[int] = None
    file_created: Optional[str] = None  # ISO format datetime
    file_modified: Optional[str] = None  # ISO format datetime
    content_preview: Optional[str] = None  # First 10KB of text
    drive_letter: Optional[str] = None
    parent_folder: Optional[str] = None


class FilesSyncRequest(BaseModel):
    """Request model for files sync."""
    machine_id: str
    files: list[FileEntry]


class FilesSyncResult(BaseModel):
    """Response model for files sync operation."""
    success: bool
    inserted_count: int
    updated_count: int
    message: str


class FilesDeleteRequest(BaseModel):
    """Request model for marking files as deleted."""
    machine_id: str
    file_paths: list[str]


class FilesDeleteResult(BaseModel):
    """Response model for files delete operation."""
    success: bool
    deleted_count: int
    message: str


class SyncedPathsResponse(BaseModel):
    """Response model for getting synced file paths."""
    machine_id: str
    file_paths: list[str]
    count: int


# ============================================================================
# Sync Endpoints
# ============================================================================

@router.post("/browser-history", response_model=SyncResult)
async def sync_browser_history(
    request: BrowserHistorySyncRequest,
    user_id: int = Depends(require_auth)
):
    """
    Receive browser history batch from local agent.

    The agent sends batches of browser history entries which are
    stored and deduplicated based on URL + visit_time.

    Args:
        request: Browser history sync request with machine_id and entries
        user_id: Authenticated user ID from JWT

    Returns:
        SyncResult with counts of inserted and skipped entries
    """
    logger.info(
        f"Browser history sync from machine {request.machine_id}: "
        f"{len(request.entries)} entries"
    )

    try:
        history_service = HistoryService(user_id)

        # Convert Pydantic models to dicts
        entries = [
            {
                "url": e.url,
                "title": e.title,
                "visit_time": e.visit_time,
                "visit_count": e.visit_count
            }
            for e in request.entries
        ]

        result = await history_service.sync_history(
            request.machine_id,
            entries
        )

        return SyncResult(
            success=True,
            inserted_count=result["inserted_count"],
            skipped_count=result["skipped_count"],
            message=f"Synced {result['inserted_count']} new entries"
        )

    except Exception as e:
        logger.error(f"Browser history sync failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Sync failed: {str(e)}"
        )


@router.get("/status", response_model=SyncStatusResponse)
async def get_sync_status(
    user_id: int = Depends(require_auth)
):
    """
    Get sync status for all connected machines.

    Returns status information for each machine/sync_type combination
    including last sync time, count, and any errors.

    Args:
        user_id: Authenticated user ID from JWT

    Returns:
        SyncStatusResponse with list of status entries
    """
    try:
        history_service = HistoryService(user_id)
        statuses = await history_service.get_sync_status_for_machines()

        return SyncStatusResponse(
            statuses=[
                SyncStatusEntry(
                    machine_id=s["machine_id"],
                    sync_type=s["sync_type"],
                    last_sync_time=s["last_sync_time"],
                    last_sync_count=s["last_sync_count"],
                    status=s["status"],
                    error_message=s["error_message"],
                    created_at=s["created_at"],
                    updated_at=s["updated_at"]
                )
                for s in statuses
            ]
        )

    except Exception as e:
        logger.error(f"Failed to get sync status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get sync status: {str(e)}"
        )


@router.get("/status/{machine_id}", response_model=SyncStatusResponse)
async def get_machine_sync_status(
    machine_id: str,
    user_id: int = Depends(require_auth)
):
    """
    Get sync status for a specific machine.

    Args:
        machine_id: The machine identifier to get status for
        user_id: Authenticated user ID from JWT

    Returns:
        SyncStatusResponse with status entries for the machine
    """
    try:
        history_service = HistoryService(user_id)
        statuses = await history_service.get_sync_status_for_machines(machine_id)

        if not statuses:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No sync status found for machine: {machine_id}"
            )

        return SyncStatusResponse(
            statuses=[
                SyncStatusEntry(
                    machine_id=s["machine_id"],
                    sync_type=s["sync_type"],
                    last_sync_time=s["last_sync_time"],
                    last_sync_count=s["last_sync_count"],
                    status=s["status"],
                    error_message=s["error_message"],
                    created_at=s["created_at"],
                    updated_at=s["updated_at"]
                )
                for s in statuses
            ]
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get sync status for {machine_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get sync status: {str(e)}"
        )


# ============================================================================
# Local Files Sync Endpoints
# ============================================================================

@router.post("/files", response_model=FilesSyncResult)
async def sync_files(
    request: FilesSyncRequest,
    user_id: int = Depends(require_auth)
):
    """
    Receive file metadata batch from desktop agent.

    The agent sends batches of file metadata which are stored and
    deduplicated based on file_path + machine_id.

    Args:
        request: Files sync request with machine_id and file entries
        user_id: Authenticated user ID from JWT

    Returns:
        FilesSyncResult with counts of inserted and updated files
    """
    logger.info(
        f"Files sync from machine {request.machine_id}: "
        f"{len(request.files)} files"
    )

    try:
        files_service = FilesService(user_id)

        # Convert Pydantic models to dicts
        files = [f.model_dump() for f in request.files]

        result = await files_service.sync_files(
            request.machine_id,
            files
        )

        total = result["inserted_count"] + result["updated_count"]
        return FilesSyncResult(
            success=True,
            inserted_count=result["inserted_count"],
            updated_count=result["updated_count"],
            message=f"Synced {total} files ({result['inserted_count']} new, {result['updated_count']} updated)"
        )

    except Exception as e:
        logger.error(f"Files sync failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Sync failed: {str(e)}"
        )


@router.post("/files/deleted", response_model=FilesDeleteResult)
async def mark_files_deleted(
    request: FilesDeleteRequest,
    user_id: int = Depends(require_auth)
):
    """
    Mark files as deleted when agent detects removal.

    Called by the desktop agent when files that were previously
    indexed are no longer found on disk.

    Args:
        request: Delete request with machine_id and file_paths
        user_id: Authenticated user ID from JWT

    Returns:
        FilesDeleteResult with count of files marked deleted
    """
    logger.info(
        f"Marking {len(request.file_paths)} files as deleted "
        f"from machine {request.machine_id}"
    )

    try:
        files_service = FilesService(user_id)

        count = await files_service.mark_deleted(
            request.machine_id,
            request.file_paths
        )

        return FilesDeleteResult(
            success=True,
            deleted_count=count,
            message=f"Marked {count} files as deleted"
        )

    except Exception as e:
        logger.error(f"Mark files deleted failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to mark files as deleted: {str(e)}"
        )


@router.get("/files/paths/{machine_id}", response_model=SyncedPathsResponse)
async def get_synced_paths(
    machine_id: str,
    user_id: int = Depends(require_auth)
):
    """
    Get all synced file paths for a machine.

    Used by the desktop agent to detect deleted files by comparing
    current files with previously synced files.

    Args:
        machine_id: Machine identifier
        user_id: Authenticated user ID from JWT

    Returns:
        SyncedPathsResponse with list of file paths
    """
    try:
        files_service = FilesService(user_id)
        paths = await files_service.get_synced_paths(machine_id)

        return SyncedPathsResponse(
            machine_id=machine_id,
            file_paths=paths,
            count=len(paths)
        )

    except Exception as e:
        logger.error(f"Get synced paths failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get synced paths: {str(e)}"
        )
