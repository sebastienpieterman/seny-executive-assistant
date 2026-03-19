"""
Google Drive API endpoints for Seny.

Provides endpoints for Drive file search, sync, and content reading:
- POST /api/drive/sync - Trigger Drive file sync
- GET /api/drive/status - Get sync status
- GET /api/drive/search - Search files by name/content
- GET /api/drive/recent - Get recently modified files
- GET /api/drive/file/{file_id} - Get file metadata
- GET /api/drive/file/{file_id}/content - Get file content (Google Docs)
- GET /api/drive/stats - Get Drive statistics
"""

from typing import Optional
import asyncio
import logging
from fastapi import APIRouter, HTTPException, status, Depends, Query, BackgroundTasks
from pydantic import BaseModel

from web.auth.jwt_utils import require_auth
from web.core.database import list_gmail_tokens
from web.services.drive_service import DriveService, get_sync_progress, set_sync_progress


# Create drive router
router = APIRouter()


# Response models
class SyncResponse(BaseModel):
    """Response for sync operation."""
    success: bool
    files_synced: int = 0
    errors: list[str] = []
    duration_seconds: float = 0
    message: str = ""


class SyncStatusResponse(BaseModel):
    """Response for sync status."""
    last_sync_at: Optional[str] = None
    files_synced: int = 0
    sync_in_progress: bool = False
    has_synced: bool = False


class AccountSyncStatus(BaseModel):
    """Sync status for a single account."""
    email: str
    files_synced: int = 0
    sync_in_progress: bool = False
    last_sync_at: Optional[str] = None


class AllAccountsSyncStatusResponse(BaseModel):
    """Response for aggregate sync status across all accounts."""
    total_files: int = 0
    any_sync_in_progress: bool = False
    all_syncs_complete: bool = True
    accounts: list[AccountSyncStatus] = []
    message: str = ""


class DriveFile(BaseModel):
    """A Drive file."""
    file_id: str
    name: str
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    modified_time: Optional[str] = None
    web_view_link: Optional[str] = None
    path: Optional[str] = None
    type: str = "file"


class SearchResponse(BaseModel):
    """Response for search endpoint."""
    files: list[DriveFile]
    query: str
    count: int


class FileContentResponse(BaseModel):
    """Response for file content."""
    file_id: str
    name: str
    content: str
    mime_type: Optional[str] = None


class StatsResponse(BaseModel):
    """Response for Drive statistics."""
    total_files: int
    total_size_bytes: int
    total_size_mb: float
    by_type: dict


def _get_email(user_id: int, email: Optional[str]) -> str:
    """Get the email account to use - specified or first connected."""
    if email:
        return email
    accounts = list_gmail_tokens(user_id)
    if not accounts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No Google accounts connected. Connect Gmail first."
        )
    return accounts[0]["email"]


logger = logging.getLogger(__name__)


def _sync_drive_blocking(user_id: int, email: str, full_sync: bool) -> dict:
    """
    Synchronous blocking function to run Drive sync.
    This runs in a thread pool to avoid blocking the event loop.
    """
    import asyncio

    print(f"[SYNC THREAD] Starting blocking sync for {email}", flush=True)

    # Create a new event loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        drive = DriveService(user_id, email)
        # Run the async sync in this thread's event loop
        result = loop.run_until_complete(drive.sync_files(full_sync=full_sync))
        return result
    finally:
        loop.close()


async def _run_sync_in_background(user_id: int, email: str, full_sync: bool):
    """Run Drive sync in background thread pool (non-blocking)."""
    print(f"[BACKGROUND SYNC] Starting sync for {email} (running in thread pool)", flush=True)

    # Mark sync as in_progress BEFORE spawning thread to avoid race condition
    # where status poll arrives before thread has started
    set_sync_progress(user_id, email, 0, True)
    print(f"[BACKGROUND SYNC] Marked {email} as in_progress=True", flush=True)

    try:
        # Run blocking sync in thread pool to avoid blocking the event loop
        result = await asyncio.to_thread(_sync_drive_blocking, user_id, email, full_sync)

        if "error" in result:
            print(f"[BACKGROUND SYNC] Error for {email}: {result['error']}", flush=True)
        else:
            print(f"[BACKGROUND SYNC] Complete for {email}: {result.get('files_synced', 0)} files in {result.get('duration_seconds', 0):.1f}s", flush=True)
    except Exception as e:
        print(f"[BACKGROUND SYNC] Exception for {email}: {e}", flush=True)
        import traceback
        print(traceback.format_exc(), flush=True)
        # Mark as not in progress on error
        set_sync_progress(user_id, email, 0, False)


async def _run_sync_all_accounts_in_background(user_id: int, full_sync: bool):
    """Run Drive sync for ALL connected accounts in background thread pool (non-blocking)."""
    accounts = list_gmail_tokens(user_id)
    print(f"[BACKGROUND SYNC ALL] Starting sync for {len(accounts)} accounts (using thread pool)", flush=True)

    # Mark ALL accounts as in_progress BEFORE starting any sync
    # This prevents race condition where status poll arrives before threads have started
    for account in accounts:
        set_sync_progress(user_id, account["email"], 0, True)
    print(f"[BACKGROUND SYNC ALL] Marked all {len(accounts)} accounts as in_progress=True", flush=True)

    total_files = 0
    total_errors = []

    for account in accounts:
        email = account["email"]
        print(f"[BACKGROUND SYNC ALL] Syncing account: {email}", flush=True)
        try:
            # Run blocking sync in thread pool to avoid blocking the event loop
            result = await asyncio.to_thread(_sync_drive_blocking, user_id, email, full_sync)

            if "error" in result:
                print(f"[BACKGROUND SYNC ALL] Error for {email}: {result['error']}", flush=True)
                total_errors.append(f"{email}: {result['error']}")
            else:
                files = result.get('files_synced', 0)
                total_files += files
                print(f"[BACKGROUND SYNC ALL] Complete for {email}: {files} files", flush=True)
        except Exception as e:
            print(f"[BACKGROUND SYNC ALL] Exception for {email}: {e}", flush=True)
            import traceback
            print(traceback.format_exc(), flush=True)
            total_errors.append(f"{email}: {str(e)}")
            # Mark as not in progress on error
            set_sync_progress(user_id, email, 0, False)

    print(f"[BACKGROUND SYNC ALL] All accounts done: {total_files} total files, {len(total_errors)} errors", flush=True)


@router.post("/sync", response_model=SyncResponse)
async def sync_drive(
    background_tasks: BackgroundTasks,
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account to sync (omit to sync ALL accounts)"),
    full_sync: bool = Query(False, description="Force full re-sync"),
    wait: bool = Query(False, description="Wait for sync to complete (may timeout on large accounts)")
):
    """
    Trigger Drive file sync.

    Downloads file metadata from Google Drive and indexes locally for
    fast searching. Uses incremental sync when possible.

    By default, syncs ALL connected accounts in the background.
    Specify email to sync a single account only.

    Args:
        email: Google account to sync (optional, omit to sync ALL accounts)
        full_sync: Force full re-sync instead of incremental
        wait: Wait for sync to complete instead of running in background

    Returns:
        Sync results or acknowledgment that sync started
    """
    print(f"[SYNC ENDPOINT] Request received: user={user_id}, email={email}, full_sync={full_sync}, wait={wait}", flush=True)

    # If specific email provided, sync only that account
    if email:
        drive = DriveService(int(user_id), email)
        if not drive.is_connected():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Google account {email} is not connected"
            )

        if wait:
            result = await drive.sync_files(full_sync=full_sync)
            if "error" in result:
                return SyncResponse(success=False, message=result["error"])
            return SyncResponse(
                success=True,
                files_synced=result.get("files_synced", 0),
                errors=result.get("errors", []),
                duration_seconds=result.get("duration_seconds", 0),
                message=f"Synced {result.get('files_synced', 0)} files"
            )
        else:
            print(f"[SYNC ENDPOINT] Creating background task for {email}", flush=True)
            asyncio.create_task(_run_sync_in_background(int(user_id), email, full_sync))
            return SyncResponse(
                success=True,
                message=f"Sync started for {email}. Check /api/drive/status/all for progress."
            )

    # No email specified - sync ALL accounts
    accounts = list_gmail_tokens(int(user_id))
    if not accounts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No Google accounts connected. Connect Gmail first."
        )

    print(f"[SYNC ENDPOINT] Creating background task for ALL {len(accounts)} accounts", flush=True)
    asyncio.create_task(_run_sync_all_accounts_in_background(int(user_id), full_sync))

    account_list = ", ".join([a["email"] for a in accounts])
    return SyncResponse(
        success=True,
        message=f"Sync started for {len(accounts)} accounts: {account_list}. Check /api/drive/status/all for progress."
    )


@router.get("/status", response_model=SyncStatusResponse)
async def get_sync_status(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account")
):
    """
    Get Drive sync status.

    Returns when the last sync occurred and how many files are indexed.

    Args:
        email: Google account to check (optional, defaults to first connected)

    Returns:
        Sync status with last sync time and file count
    """
    account_email = _get_email(int(user_id), email)

    drive = DriveService(int(user_id), account_email)
    if not drive.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google account {account_email} is not connected"
        )

    status_data = await drive.get_sync_status()

    return SyncStatusResponse(
        last_sync_at=status_data.get("last_sync_at"),
        files_synced=status_data.get("files_synced", 0),
        sync_in_progress=status_data.get("sync_in_progress", False),
        has_synced=status_data.get("has_synced", False)
    )


@router.get("/status/all", response_model=AllAccountsSyncStatusResponse)
async def get_all_accounts_sync_status(
    user_id: str = Depends(require_auth)
):
    """
    Get Drive sync status for ALL connected accounts.

    Returns aggregate status showing total files, which accounts are syncing,
    and whether all syncs are complete.
    """
    print(f"[STATUS/ALL] Request received for user {user_id}", flush=True)

    accounts = list_gmail_tokens(int(user_id))
    if not accounts:
        print(f"[STATUS/ALL] No accounts connected", flush=True)
        return AllAccountsSyncStatusResponse(
            message="No Google accounts connected"
        )

    print(f"[STATUS/ALL] Found {len(accounts)} accounts", flush=True)
    account_statuses = []
    total_files = 0
    any_in_progress = False

    for account in accounts:
        email = account["email"]
        # Use in-memory progress (fast, no DB lock) instead of database query
        progress = get_sync_progress(int(user_id), email)
        files = progress.get("files_synced", 0)
        in_progress = progress.get("in_progress", False)

        print(f"[STATUS/ALL] {email}: {files} files, in_progress={in_progress}", flush=True)

        total_files += files
        if in_progress:
            any_in_progress = True

        account_statuses.append(AccountSyncStatus(
            email=email,
            files_synced=files,
            sync_in_progress=in_progress,
            last_sync_at=None  # Not available from in-memory, but not needed for progress
        ))

    # Build status message
    if any_in_progress:
        syncing = [a.email for a in account_statuses if a.sync_in_progress]
        message = f"Syncing: {', '.join(syncing)}... ({total_files} files so far)"
    else:
        message = f"Sync complete: {total_files} total files across {len(accounts)} accounts"

    print(f"[STATUS/ALL] Responding: {message}", flush=True)
    return AllAccountsSyncStatusResponse(
        total_files=total_files,
        any_sync_in_progress=any_in_progress,
        all_syncs_complete=not any_in_progress,
        accounts=account_statuses,
        message=message
    )


@router.get("/search", response_model=SearchResponse)
async def search_files(
    q: str = Query(..., description="Search query"),
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account"),
    file_type: Optional[str] = Query(None, description="Filter by type: document, spreadsheet, pdf, etc."),
    limit: int = Query(20, ge=1, le=100, description="Max results")
):
    """
    Search Drive files by name or content.

    Uses full-text search to find files matching the query.
    Requires Drive to be synced first.

    Args:
        q: Search query (file name or content)
        email: Google account (optional, defaults to first connected)
        file_type: Filter by type - document, spreadsheet, presentation, pdf, image, video
        limit: Maximum results (1-100, default 20)

    Returns:
        List of matching files
    """
    account_email = _get_email(int(user_id), email)

    drive = DriveService(int(user_id), account_email)
    if not drive.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google account {account_email} is not connected"
        )

    files = await drive.search_files(q, file_type=file_type, limit=limit)

    return SearchResponse(
        files=[DriveFile(**f) for f in files],
        query=q,
        count=len(files)
    )


@router.get("/recent", response_model=SearchResponse)
async def get_recent_files(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account"),
    days: int = Query(7, ge=1, le=90, description="Days to look back"),
    file_type: Optional[str] = Query(None, description="Filter by type"),
    limit: int = Query(20, ge=1, le=100, description="Max results")
):
    """
    Get recently modified files.

    Returns files modified within the specified number of days.

    Args:
        email: Google account (optional, defaults to first connected)
        days: Look back this many days (1-90, default 7)
        file_type: Filter by type (optional)
        limit: Maximum results (1-100, default 20)

    Returns:
        List of recently modified files
    """
    account_email = _get_email(int(user_id), email)

    drive = DriveService(int(user_id), account_email)
    if not drive.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google account {account_email} is not connected"
        )

    files = await drive.list_recent(days=days, limit=limit, file_type=file_type)

    return SearchResponse(
        files=[DriveFile(**f) for f in files],
        query=f"modified in last {days} days",
        count=len(files)
    )


@router.get("/file/{file_id}", response_model=DriveFile)
async def get_file(
    file_id: str,
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account")
):
    """
    Get file metadata by ID.

    Args:
        file_id: Google Drive file ID
        email: Google account (optional, defaults to first connected)

    Returns:
        File metadata
    """
    account_email = _get_email(int(user_id), email)

    drive = DriveService(int(user_id), account_email)
    if not drive.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google account {account_email} is not connected"
        )

    file_data = await drive.get_file(file_id)

    if not file_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File {file_id} not found"
        )

    return DriveFile(**file_data)


@router.get("/file/{file_id}/content", response_model=FileContentResponse)
async def get_file_content(
    file_id: str,
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account"),
    max_chars: int = Query(10000, ge=100, le=100000, description="Max content length")
):
    """
    Read content of a Google Doc, Sheet, or text file.

    For Google Workspace files, exports as plain text.
    For regular text files, downloads content directly.
    Binary files (images, videos) cannot be read this way.

    Args:
        file_id: Google Drive file ID
        email: Google account (optional, defaults to first connected)
        max_chars: Maximum characters to return (100-100000, default 10000)

    Returns:
        File content as text
    """
    account_email = _get_email(int(user_id), email)

    drive = DriveService(int(user_id), account_email)
    if not drive.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google account {account_email} is not connected"
        )

    # Get file metadata
    file_data = await drive.get_file(file_id)
    if not file_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File {file_id} not found"
        )

    # Get content
    content = await drive.get_file_content(file_id, max_chars=max_chars)

    if content is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to read file content"
        )

    return FileContentResponse(
        file_id=file_id,
        name=file_data.get("name", ""),
        content=content,
        mime_type=file_data.get("mime_type")
    )


@router.get("/stats", response_model=StatsResponse)
async def get_drive_stats(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account")
):
    """
    Get Drive file statistics.

    Returns total file count, size, and breakdown by type.

    Args:
        email: Google account (optional, defaults to first connected)

    Returns:
        Drive statistics
    """
    account_email = _get_email(int(user_id), email)

    drive = DriveService(int(user_id), account_email)
    if not drive.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google account {account_email} is not connected"
        )

    stats = await drive.get_stats()

    return StatsResponse(
        total_files=stats.get("total_files", 0),
        total_size_bytes=stats.get("total_size_bytes", 0),
        total_size_mb=stats.get("total_size_mb", 0),
        by_type=stats.get("by_type", {})
    )


@router.get("/debug")
async def debug_drive(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account")
):
    """
    Debug endpoint to test Drive API directly.
    Only fetches 5 files to avoid crashing on large accounts.
    """
    import logging
    logger = logging.getLogger(__name__)

    logger.info("[DEBUG ENDPOINT] Starting debug_drive")
    account_email = _get_email(int(user_id), email)
    logger.info(f"[DEBUG ENDPOINT] Account: {account_email}")

    drive = DriveService(int(user_id), account_email)

    # Get credentials info
    logger.info("[DEBUG ENDPOINT] Getting credentials...")
    creds = await drive.get_credentials()
    if not creds:
        logger.info("[DEBUG ENDPOINT] No credentials found")
        return {"error": "No credentials", "email": account_email}

    creds_info = {
        "email": account_email,
        "scopes": list(creds.scopes) if creds.scopes else [],
        "expired": creds.expired,
        "valid": creds.valid,
    }
    logger.info(f"[DEBUG ENDPOINT] Credentials: valid={creds.valid}, expired={creds.expired}")

    # Try to get service
    logger.info("[DEBUG ENDPOINT] Building Drive service...")
    service = await drive.get_service()
    if not service:
        logger.info("[DEBUG ENDPOINT] Could not build service")
        return {"error": "Could not build service", "credentials": creds_info}

    logger.info("[DEBUG ENDPOINT] Service built, fetching 5 files...")

    try:
        # SAFE: Only fetch 5 files to test the API
        results = service.files().list(
            pageSize=5,
            fields="files(id, name, mimeType, modifiedTime)",
            q="trashed = false"
        ).execute()

        files = results.get("files", [])
        logger.info(f"[DEBUG ENDPOINT] Got {len(files)} files")

        return {
            "credentials": creds_info,
            "api_works": True,
            "files_fetched": len(files),
            "sample_files": [{"name": f.get("name"), "type": f.get("mimeType")} for f in files],
            "note": "This only fetches 5 files to test. Use /api/drive/sync to do full sync."
        }
    except Exception as e:
        import traceback
        logger.error(f"[DEBUG ENDPOINT] Error: {e}")
        return {
            "credentials": creds_info,
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": traceback.format_exc()
        }


@router.get("/debug-sync")
async def debug_sync(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account"),
    max_files: int = Query(100, description="Max files to sync (default 100)")
):
    """
    Debug endpoint to test sync logic with limited files.
    Tests the actual save-to-database flow without syncing entire account.
    """
    import logging
    import time
    logger = logging.getLogger(__name__)

    logger.info(f"[DEBUG SYNC] Starting with max_files={max_files}")
    account_email = _get_email(int(user_id), email)

    drive = DriveService(int(user_id), account_email)

    service = await drive.get_service()
    if not service:
        return {"error": "Could not build service"}

    start_time = time.time()
    files_saved = 0
    errors = []

    try:
        logger.info("[DEBUG SYNC] Clearing existing files...")
        drive._clear_files()
        logger.info("[DEBUG SYNC] Files cleared")

        logger.info("[DEBUG SYNC] Fetching files from API...")
        results = service.files().list(
            pageSize=min(max_files, 100),
            fields="files(id, name, mimeType, modifiedTime, size, webViewLink, parents, owners)",
            q="trashed = false"
        ).execute()

        files = results.get("files", [])
        logger.info(f"[DEBUG SYNC] Got {len(files)} files from API")

        for i, file in enumerate(files):
            try:
                logger.info(f"[DEBUG SYNC] Saving file {i+1}/{len(files)}: {file.get('name', 'unnamed')[:50]}")
                drive._save_file(file)
                files_saved += 1
            except Exception as e:
                error_msg = f"Error saving {file.get('name')}: {e}"
                logger.error(f"[DEBUG SYNC] {error_msg}")
                errors.append(error_msg)

        duration = time.time() - start_time
        logger.info(f"[DEBUG SYNC] Complete: {files_saved} files in {duration:.2f}s")

        return {
            "success": True,
            "files_fetched": len(files),
            "files_saved": files_saved,
            "errors": errors,
            "duration_seconds": round(duration, 2)
        }

    except Exception as e:
        import traceback
        logger.error(f"[DEBUG SYNC] Error: {e}")
        return {
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": traceback.format_exc(),
            "files_saved_before_error": files_saved
        }


# Request/Response models for document creation
class CreateDocumentRequest(BaseModel):
    """Request to create a new document."""
    title: str
    content: str
    folder: str = "Seny"
    doc_type: str = "document"  # "document" for Google Doc, "text" for .txt


class CreateDocumentResponse(BaseModel):
    """Response after creating a document."""
    success: bool
    file_id: Optional[str] = None
    name: Optional[str] = None
    web_view_link: Optional[str] = None
    message: str = ""


@router.post("/create", response_model=CreateDocumentResponse)
async def create_document(
    request: CreateDocumentRequest,
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account")
):
    """
    Create a new document in Google Drive.

    Creates a Google Doc or plain text file in the specified folder.
    The folder is created if it doesn't exist.

    Args:
        request: Document details (title, content, folder, type)
        email: Google account (optional, defaults to first connected)

    Returns:
        Created file info with link to open in Drive
    """
    account_email = _get_email(int(user_id), email)

    drive = DriveService(int(user_id), account_email)
    if not drive.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google account {account_email} is not connected"
        )

    result = await drive.create_document(
        title=request.title,
        content=request.content,
        folder_name=request.folder,
        doc_type=request.doc_type
    )

    if result:
        return CreateDocumentResponse(
            success=True,
            file_id=result.get("file_id"),
            name=result.get("name"),
            web_view_link=result.get("web_view_link"),
            message=f"Created '{result.get('name')}' in {request.folder} folder"
        )
    else:
        return CreateDocumentResponse(
            success=False,
            message="Failed to create document. Make sure Drive permissions are granted."
        )
