"""
YouTube API endpoints for Seny.

Provides endpoints for YouTube subscriptions, playlists, and liked videos:
- POST /api/youtube/sync - Trigger YouTube sync
- GET /api/youtube/status - Get sync status
- GET /api/youtube/subscriptions - List/search subscriptions
- GET /api/youtube/playlists - List playlists
- GET /api/youtube/liked - List liked videos
- GET /api/youtube/stats - Get YouTube statistics
"""

from typing import Optional
import asyncio
from fastapi import APIRouter, HTTPException, status, Depends, Query
from pydantic import BaseModel

from web.auth.jwt_utils import require_auth
from web.core.database import list_gmail_tokens
from web.services.youtube_service import YouTubeService, get_sync_progress, set_sync_progress


# Create youtube router
router = APIRouter()


# Response models
class SyncResponse(BaseModel):
    """Response for sync operation."""
    success: bool
    subscriptions: int = 0
    playlists: int = 0
    liked_videos: int = 0
    errors: list[str] = []
    duration_seconds: float = 0
    message: str = ""


class SyncStatusResponse(BaseModel):
    """Response for sync status."""
    last_sync_at: Optional[str] = None
    subscriptions: int = 0
    playlists: int = 0
    liked_videos: int = 0
    sync_in_progress: bool = False
    has_synced: bool = False


class AccountSyncStatus(BaseModel):
    """Sync status for a single account."""
    email: str
    subscriptions: int = 0
    playlists: int = 0
    liked_videos: int = 0
    sync_in_progress: bool = False


class AllAccountsSyncStatusResponse(BaseModel):
    """Response for aggregate sync status."""
    total_subscriptions: int = 0
    total_playlists: int = 0
    total_liked_videos: int = 0
    any_sync_in_progress: bool = False
    all_syncs_complete: bool = True
    accounts: list[AccountSyncStatus] = []
    message: str = ""


class Subscription(BaseModel):
    """A YouTube subscription."""
    subscription_id: str
    channel_id: str
    channel_title: Optional[str] = None
    channel_description: Optional[str] = None
    thumbnail_url: Optional[str] = None
    subscribed_at: Optional[str] = None


class Playlist(BaseModel):
    """A YouTube playlist."""
    playlist_id: str
    title: Optional[str] = None
    description: Optional[str] = None
    thumbnail_url: Optional[str] = None
    item_count: Optional[int] = None
    privacy_status: Optional[str] = None
    created_at: Optional[str] = None


class LikedVideo(BaseModel):
    """A liked YouTube video."""
    video_id: str
    title: Optional[str] = None
    description: Optional[str] = None
    channel_title: Optional[str] = None
    thumbnail_url: Optional[str] = None
    duration: Optional[str] = None
    published_at: Optional[str] = None
    liked_at: Optional[str] = None
    url: Optional[str] = None


class SubscriptionsResponse(BaseModel):
    """Response for subscriptions endpoint."""
    subscriptions: list[Subscription]
    query: Optional[str] = None
    count: int


class PlaylistsResponse(BaseModel):
    """Response for playlists endpoint."""
    playlists: list[Playlist]
    count: int


class LikedVideosResponse(BaseModel):
    """Response for liked videos endpoint."""
    videos: list[LikedVideo]
    count: int


class StatsResponse(BaseModel):
    """Response for YouTube statistics."""
    subscriptions: int
    playlists: int
    liked_videos: int


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


def _sync_youtube_blocking(user_id: int, email: str, full_sync: bool) -> dict:
    """Synchronous blocking function to run YouTube sync."""
    import asyncio

    print(f"[YOUTUBE SYNC THREAD] Starting blocking sync for {email}", flush=True)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        youtube = YouTubeService(user_id, email)
        result = loop.run_until_complete(youtube.sync_all(full_sync=full_sync))
        return result
    finally:
        loop.close()


async def _run_sync_in_background(user_id: int, email: str, full_sync: bool):
    """Run YouTube sync in background thread pool."""
    print(f"[YOUTUBE BACKGROUND SYNC] Starting sync for {email}", flush=True)

    set_sync_progress(user_id, email, 0, 0, 0, True)

    try:
        result = await asyncio.to_thread(_sync_youtube_blocking, user_id, email, full_sync)

        if "error" in result:
            print(f"[YOUTUBE BACKGROUND SYNC] Error for {email}: {result['error']}", flush=True)
        else:
            print(f"[YOUTUBE BACKGROUND SYNC] Complete for {email}: "
                  f"{result.get('subscriptions', 0)} subs, {result.get('playlists', 0)} playlists, "
                  f"{result.get('liked_videos', 0)} liked", flush=True)
    except Exception as e:
        print(f"[YOUTUBE BACKGROUND SYNC] Exception for {email}: {e}", flush=True)
        set_sync_progress(user_id, email, 0, 0, 0, False)


async def _run_sync_all_accounts_in_background(user_id: int, full_sync: bool):
    """Run YouTube sync for ALL connected accounts."""
    accounts = list_gmail_tokens(user_id)
    print(f"[YOUTUBE SYNC ALL] Starting sync for {len(accounts)} accounts", flush=True)

    for account in accounts:
        set_sync_progress(user_id, account["email"], 0, 0, 0, True)

    totals = {"subscriptions": 0, "playlists": 0, "liked_videos": 0}

    for account in accounts:
        email = account["email"]
        try:
            result = await asyncio.to_thread(_sync_youtube_blocking, user_id, email, full_sync)

            if "error" not in result:
                totals["subscriptions"] += result.get("subscriptions", 0)
                totals["playlists"] += result.get("playlists", 0)
                totals["liked_videos"] += result.get("liked_videos", 0)
        except Exception as e:
            print(f"[YOUTUBE SYNC ALL] Exception for {email}: {e}", flush=True)
            set_sync_progress(user_id, email, 0, 0, 0, False)

    print(f"[YOUTUBE SYNC ALL] All accounts done: {totals}", flush=True)


@router.post("/sync", response_model=SyncResponse)
async def sync_youtube(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account to sync"),
    full_sync: bool = Query(False, description="Force full re-sync"),
    wait: bool = Query(False, description="Wait for sync to complete")
):
    """
    Trigger YouTube sync.

    Downloads subscriptions, playlists, and liked videos.
    """
    print(f"[YOUTUBE SYNC] Request: user={user_id}, email={email}, full_sync={full_sync}", flush=True)

    if email:
        youtube = YouTubeService(int(user_id), email)
        if not youtube.is_connected():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Google account {email} is not connected"
            )

        if wait:
            result = await youtube.sync_all(full_sync=full_sync)
            if "error" in result:
                return SyncResponse(success=False, message=result["error"])
            return SyncResponse(
                success=True,
                subscriptions=result.get("subscriptions", 0),
                playlists=result.get("playlists", 0),
                liked_videos=result.get("liked_videos", 0),
                errors=result.get("errors", []),
                duration_seconds=result.get("duration_seconds", 0),
                message=f"Synced {result.get('subscriptions', 0)} subs, {result.get('playlists', 0)} playlists, {result.get('liked_videos', 0)} liked"
            )
        else:
            asyncio.create_task(_run_sync_in_background(int(user_id), email, full_sync))
            return SyncResponse(
                success=True,
                message=f"Sync started for {email}. Check /api/youtube/status/all for progress."
            )

    accounts = list_gmail_tokens(int(user_id))
    if not accounts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No Google accounts connected."
        )

    asyncio.create_task(_run_sync_all_accounts_in_background(int(user_id), full_sync))

    return SyncResponse(
        success=True,
        message=f"Sync started for {len(accounts)} accounts. Check /api/youtube/status/all for progress."
    )


@router.get("/status", response_model=SyncStatusResponse)
async def get_sync_status_endpoint(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account")
):
    """Get YouTube sync status."""
    account_email = _get_email(int(user_id), email)

    youtube = YouTubeService(int(user_id), account_email)
    if not youtube.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google account {account_email} is not connected"
        )

    status_data = await youtube.get_sync_status()

    return SyncStatusResponse(
        last_sync_at=status_data.get("last_sync_at"),
        subscriptions=status_data.get("subscriptions", 0),
        playlists=status_data.get("playlists", 0),
        liked_videos=status_data.get("liked_videos", 0),
        sync_in_progress=status_data.get("sync_in_progress", False),
        has_synced=status_data.get("has_synced", False)
    )


@router.get("/status/all", response_model=AllAccountsSyncStatusResponse)
async def get_all_accounts_sync_status(
    user_id: str = Depends(require_auth)
):
    """Get YouTube sync status for ALL connected accounts."""
    accounts = list_gmail_tokens(int(user_id))
    if not accounts:
        return AllAccountsSyncStatusResponse(message="No Google accounts connected")

    account_statuses = []
    totals = {"subscriptions": 0, "playlists": 0, "liked_videos": 0}
    any_in_progress = False

    for account in accounts:
        email = account["email"]
        progress = get_sync_progress(int(user_id), email)

        subs = progress.get("subscriptions", 0)
        playlists = progress.get("playlists", 0)
        liked = progress.get("liked_videos", 0)
        in_progress = progress.get("in_progress", False)

        totals["subscriptions"] += subs
        totals["playlists"] += playlists
        totals["liked_videos"] += liked

        if in_progress:
            any_in_progress = True

        account_statuses.append(AccountSyncStatus(
            email=email,
            subscriptions=subs,
            playlists=playlists,
            liked_videos=liked,
            sync_in_progress=in_progress
        ))

    if any_in_progress:
        syncing = [a.email for a in account_statuses if a.sync_in_progress]
        message = f"Syncing: {', '.join(syncing)}..."
    else:
        message = f"Sync complete: {totals['subscriptions']} subs, {totals['playlists']} playlists, {totals['liked_videos']} liked"

    return AllAccountsSyncStatusResponse(
        total_subscriptions=totals["subscriptions"],
        total_playlists=totals["playlists"],
        total_liked_videos=totals["liked_videos"],
        any_sync_in_progress=any_in_progress,
        all_syncs_complete=not any_in_progress,
        accounts=account_statuses,
        message=message
    )


@router.get("/subscriptions", response_model=SubscriptionsResponse)
async def list_subscriptions(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account"),
    q: Optional[str] = Query(None, description="Search query for channel title"),
    limit: int = Query(50, ge=1, le=200, description="Max results")
):
    """
    List or search YouTube subscriptions.

    Args:
        q: Optional search filter for channel title
        limit: Maximum results
    """
    account_email = _get_email(int(user_id), email)

    youtube = YouTubeService(int(user_id), account_email)
    if not youtube.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google account {account_email} is not connected"
        )

    results = await youtube.list_subscriptions(query=q, limit=limit)

    return SubscriptionsResponse(
        subscriptions=[Subscription(**s) for s in results],
        query=q,
        count=len(results)
    )


@router.get("/playlists", response_model=PlaylistsResponse)
async def list_playlists(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account"),
    limit: int = Query(50, ge=1, le=100, description="Max results")
):
    """List YouTube playlists."""
    account_email = _get_email(int(user_id), email)

    youtube = YouTubeService(int(user_id), account_email)
    if not youtube.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google account {account_email} is not connected"
        )

    results = await youtube.list_playlists(limit=limit)

    return PlaylistsResponse(
        playlists=[Playlist(**p) for p in results],
        count=len(results)
    )


@router.get("/liked", response_model=LikedVideosResponse)
async def list_liked_videos(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account"),
    limit: int = Query(50, ge=1, le=200, description="Max results")
):
    """List liked YouTube videos (most recent first)."""
    account_email = _get_email(int(user_id), email)

    youtube = YouTubeService(int(user_id), account_email)
    if not youtube.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google account {account_email} is not connected"
        )

    results = await youtube.list_liked_videos(limit=limit)

    return LikedVideosResponse(
        videos=[LikedVideo(**v) for v in results],
        count=len(results)
    )


@router.get("/stats", response_model=StatsResponse)
async def get_youtube_stats(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account")
):
    """Get YouTube statistics."""
    account_email = _get_email(int(user_id), email)

    youtube = YouTubeService(int(user_id), account_email)
    if not youtube.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google account {account_email} is not connected"
        )

    stats = await youtube.get_stats()

    return StatsResponse(
        subscriptions=stats.get("subscriptions", 0),
        playlists=stats.get("playlists", 0),
        liked_videos=stats.get("liked_videos", 0)
    )
