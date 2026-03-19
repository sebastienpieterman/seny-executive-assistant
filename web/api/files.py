"""
Files endpoints for Seny - Phase 7.

Local files search API for querying indexed files:
- GET /api/files/search - Search files by name or content
- GET /api/files/recent - Get recently modified files
- GET /api/files/by-type - Get files by extension
- GET /api/files/stats - Get file statistics
"""

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, status, Depends, Query
from pydantic import BaseModel

from web.auth.jwt_utils import require_auth
from web.services.files_service import FilesService

logger = logging.getLogger(__name__)


# Create files router
router = APIRouter()


# Response models
class FileResult(BaseModel):
    """Single file result."""
    id: int
    file_path: str
    file_name: str
    file_extension: Optional[str]
    file_size: Optional[int]
    file_size_formatted: Optional[str]
    file_created: Optional[str]
    file_modified: Optional[str]
    content_preview: Optional[str]
    drive_letter: Optional[str]
    parent_folder: Optional[str]
    machine_id: str
    indexed_at: str
    snippet: Optional[str] = None


class FilesSearchResponse(BaseModel):
    """Response for file search."""
    files: list[FileResult]
    total: int
    query: str


class FilesListResponse(BaseModel):
    """Response for file listing."""
    files: list[FileResult]
    total: int


class ExtensionStat(BaseModel):
    """Extension statistics."""
    extension: str
    count: int


class DriveStat(BaseModel):
    """Drive statistics."""
    drive: str
    count: int


class MachineStat(BaseModel):
    """Machine statistics."""
    machine_id: str
    count: int


class FileStatsResponse(BaseModel):
    """Response for file statistics."""
    total_files: int
    by_extension: list[ExtensionStat]
    by_drive: list[DriveStat]
    by_machine: list[MachineStat]


# Helper function
def format_file_size(size_bytes: Optional[int]) -> Optional[str]:
    """Format file size in human-readable format."""
    if size_bytes is None:
        return None
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


# ============================================================================
# Files Endpoints
# ============================================================================

@router.get("/search", response_model=FilesSearchResponse)
async def search_files(
    q: str = Query(..., description="Search query"),
    file_type: Optional[str] = Query(None, description="Filter by extension (e.g., '.mp4')"),
    folder: Optional[str] = Query(None, description="Filter by folder path prefix"),
    modified_since: Optional[str] = Query(None, description="Filter by modified date (ISO format)"),
    limit: int = Query(20, ge=1, le=100, description="Maximum results"),
    user_id: int = Depends(require_auth)
):
    """
    Search files by name or content.

    Uses FTS5 full-text search to match file names, paths, and
    text content (for files where content was indexed).

    Args:
        q: Search query
        file_type: Optional extension filter (e.g., '.mp4', '.docx')
        folder: Optional folder path prefix filter
        modified_since: Optional date filter (ISO format)
        limit: Maximum results to return
        user_id: Authenticated user ID

    Returns:
        FilesSearchResponse with matching files and snippets
    """
    try:
        files_service = FilesService(user_id)

        results = await files_service.search_files(
            query=q,
            file_type=file_type,
            folder=folder,
            modified_since=modified_since,
            limit=limit
        )

        files = [
            FileResult(
                id=f["id"],
                file_path=f["file_path"],
                file_name=f["file_name"],
                file_extension=f["file_extension"],
                file_size=f["file_size"],
                file_size_formatted=format_file_size(f.get("file_size")),
                file_created=f["file_created"],
                file_modified=f["file_modified"],
                content_preview=f.get("content_preview"),
                drive_letter=f["drive_letter"],
                parent_folder=f["parent_folder"],
                machine_id=f["machine_id"],
                indexed_at=f["indexed_at"],
                snippet=f.get("snippet")
            )
            for f in results
        ]

        return FilesSearchResponse(
            files=files,
            total=len(files),
            query=q
        )

    except Exception as e:
        logger.error(f"File search failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Search failed: {str(e)}"
        )


@router.get("/recent", response_model=FilesListResponse)
async def get_recent_files(
    days: int = Query(7, ge=1, le=365, description="Number of days back"),
    file_type: Optional[str] = Query(None, description="Filter by extension"),
    machine_id: Optional[str] = Query(None, description="Filter by machine"),
    limit: int = Query(20, ge=1, le=100, description="Maximum results"),
    user_id: int = Depends(require_auth)
):
    """
    Get recently modified files.

    Args:
        days: How many days back to look (default 7)
        file_type: Optional extension filter
        machine_id: Optional machine filter
        limit: Maximum results
        user_id: Authenticated user ID

    Returns:
        FilesListResponse with recent files
    """
    try:
        files_service = FilesService(user_id)

        results = await files_service.get_recent_files(
            days=days,
            file_type=file_type,
            machine_id=machine_id,
            limit=limit
        )

        files = [
            FileResult(
                id=f["id"],
                file_path=f["file_path"],
                file_name=f["file_name"],
                file_extension=f["file_extension"],
                file_size=f["file_size"],
                file_size_formatted=format_file_size(f.get("file_size")),
                file_created=f["file_created"],
                file_modified=f["file_modified"],
                content_preview=f.get("content_preview"),
                drive_letter=f["drive_letter"],
                parent_folder=f["parent_folder"],
                machine_id=f["machine_id"],
                indexed_at=f["indexed_at"]
            )
            for f in results
        ]

        return FilesListResponse(
            files=files,
            total=len(files)
        )

    except Exception as e:
        logger.error(f"Get recent files failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get recent files: {str(e)}"
        )


@router.get("/by-type", response_model=FilesListResponse)
async def get_files_by_type(
    extension: str = Query(..., description="File extension (e.g., '.mp4')"),
    folder: Optional[str] = Query(None, description="Filter by folder path prefix"),
    limit: int = Query(50, ge=1, le=200, description="Maximum results"),
    user_id: int = Depends(require_auth)
):
    """
    Get files by extension.

    Args:
        extension: File extension to filter by (e.g., '.mp4', '.prproj')
        folder: Optional folder path prefix filter
        limit: Maximum results
        user_id: Authenticated user ID

    Returns:
        FilesListResponse with matching files
    """
    try:
        files_service = FilesService(user_id)

        results = await files_service.get_files_by_type(
            extension=extension,
            folder=folder,
            limit=limit
        )

        files = [
            FileResult(
                id=f["id"],
                file_path=f["file_path"],
                file_name=f["file_name"],
                file_extension=f["file_extension"],
                file_size=f["file_size"],
                file_size_formatted=format_file_size(f.get("file_size")),
                file_created=f["file_created"],
                file_modified=f["file_modified"],
                content_preview=None,  # Not included in by-type queries
                drive_letter=f["drive_letter"],
                parent_folder=f["parent_folder"],
                machine_id=f["machine_id"],
                indexed_at=f["indexed_at"]
            )
            for f in results
        ]

        return FilesListResponse(
            files=files,
            total=len(files)
        )

    except Exception as e:
        logger.error(f"Get files by type failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get files: {str(e)}"
        )


@router.get("/stats", response_model=FileStatsResponse)
async def get_file_stats(
    user_id: int = Depends(require_auth)
):
    """
    Get file statistics.

    Returns counts by extension, drive, and machine.

    Args:
        user_id: Authenticated user ID

    Returns:
        FileStatsResponse with statistics
    """
    try:
        files_service = FilesService(user_id)
        stats = await files_service.get_stats()

        return FileStatsResponse(
            total_files=stats["total_files"],
            by_extension=[
                ExtensionStat(extension=e["extension"], count=e["count"])
                for e in stats["by_extension"]
            ],
            by_drive=[
                DriveStat(drive=d["drive"], count=d["count"])
                for d in stats["by_drive"]
            ],
            by_machine=[
                MachineStat(machine_id=m["machine_id"], count=m["count"])
                for m in stats["by_machine"]
            ]
        )

    except Exception as e:
        logger.error(f"Get file stats failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get statistics: {str(e)}"
        )
