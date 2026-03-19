"""
YouTube Data API service wrapper for Seny.

Provides YouTube access with automatic token refresh:
- Load credentials from database (shared with Gmail)
- Auto-refresh expired tokens
- Build YouTube API service object
- Sync subscriptions, playlists, and liked videos

Note: Watch history is NOT available via API (Google blocked it in 2016)

Usage:
    youtube = YouTubeService(user_id, email)
    if youtube.is_connected():
        subs = await youtube.list_subscriptions()
"""

import os
import logging
import time
from datetime import datetime
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build, Resource
from googleapiclient.errors import HttpError

from web.core.database import get_gmail_token, save_gmail_token, get_db

logger = logging.getLogger(__name__)

# In-memory sync progress tracking
_sync_progress = {}

def get_sync_progress(user_id: int, email: str) -> dict:
    """Get in-memory sync progress for an account."""
    key = f"youtube:{user_id}:{email}"
    return _sync_progress.get(key, {
        "subscriptions": 0,
        "playlists": 0,
        "liked_videos": 0,
        "in_progress": False
    })

def set_sync_progress(user_id: int, email: str, subscriptions: int = 0,
                      playlists: int = 0, liked_videos: int = 0, in_progress: bool = False):
    """Set in-memory sync progress for an account."""
    key = f"youtube:{user_id}:{email}"
    _sync_progress[key] = {
        "subscriptions": subscriptions,
        "playlists": playlists,
        "liked_videos": liked_videos,
        "in_progress": in_progress
    }

# OAuth Configuration (shared with Gmail)
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")


class YouTubeService:
    """
    YouTube Data API wrapper with automatic credential management.

    Handles OAuth token refresh and YouTube API service creation.
    Uses same OAuth credentials as Gmail (youtube scope added to combined flow).

    Attributes:
        user_id: The user's database ID
        email: The Google account email
    """

    def __init__(self, user_id: int, email: str):
        """
        Initialize YouTube service for a specific user and email account.

        Args:
            user_id: User's database ID
            email: Google account email address
        """
        self.user_id = user_id
        self.email = email
        self._service: Optional[Resource] = None
        self._credentials: Optional[Credentials] = None

    def is_connected(self) -> bool:
        """
        Check if this email has Google credentials stored.

        Returns:
            True if this email has Google tokens stored
        """
        token_data = get_gmail_token(self.user_id, self.email)
        return token_data is not None

    async def get_credentials(self) -> Optional[Credentials]:
        """
        Load credentials from database and refresh if expired.

        Returns:
            Valid Credentials object, or None if not available
        """
        if self._credentials is not None:
            if not self._credentials.expired:
                return self._credentials

        token_data = get_gmail_token(self.user_id, self.email)
        if not token_data:
            return None

        expiry = None
        if token_data["expiry"]:
            try:
                expiry = datetime.fromisoformat(token_data["expiry"])
            except ValueError:
                pass

        self._credentials = Credentials(
            token=token_data["access_token"],
            refresh_token=token_data["refresh_token"],
            token_uri=token_data["token_uri"],
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=token_data["scopes"].split(",") if token_data["scopes"] else None,
            expiry=expiry
        )

        if self._credentials.expired and self._credentials.refresh_token:
            try:
                self._credentials.refresh(Request())
                save_gmail_token(self.user_id, self.email, self._credentials)
            except Exception as e:
                logger.error(f"Failed to refresh Google credentials: {e}")
                self._credentials = None
                return None

        return self._credentials

    async def get_service(self) -> Optional[Resource]:
        """
        Get or create YouTube API service.

        Returns:
            YouTube API service object, or None if not connected/authorized
        """
        if self._service is not None:
            return self._service

        credentials = await self.get_credentials()
        if credentials is None:
            return None

        self._service = build("youtube", "v3", credentials=credentials)
        return self._service

    def _execute_with_backoff(self, request, max_retries: int = 3):
        """Execute YouTube API request with exponential backoff for rate limits."""
        for attempt in range(max_retries):
            try:
                return request.execute()
            except HttpError as e:
                if e.resp.status in (429, 500, 503):
                    wait_time = (2 ** attempt) + (time.time() % 1)
                    logger.warning(f"YouTube API error {e.resp.status}, retrying in {wait_time:.1f}s")
                    time.sleep(wait_time)
                else:
                    logger.error(f"YouTube API error: {e}")
                    raise
        return None

    # =========================================================================
    # Sync Methods
    # =========================================================================

    async def sync_all(self, full_sync: bool = False) -> dict:
        """
        Sync all YouTube data (subscriptions, playlists, liked videos).

        Args:
            full_sync: Force full re-sync

        Returns:
            Dict with sync results
        """
        service = await self.get_service()
        if not service:
            return {"error": "Not connected to YouTube"}

        start_time = time.time()
        self._update_sync_status(sync_in_progress=True)
        set_sync_progress(self.user_id, self.email, 0, 0, 0, True)

        results = {
            "subscriptions": 0,
            "playlists": 0,
            "liked_videos": 0,
            "errors": []
        }

        try:
            # Sync subscriptions
            print(f"[YOUTUBE SYNC] Syncing subscriptions for {self.email}", flush=True)
            sub_result = await self._sync_subscriptions(service, full_sync)
            results["subscriptions"] = sub_result.get("count", 0)
            results["errors"].extend(sub_result.get("errors", []))
            set_sync_progress(self.user_id, self.email, results["subscriptions"], 0, 0, True)

            # Sync playlists
            print(f"[YOUTUBE SYNC] Syncing playlists for {self.email}", flush=True)
            pl_result = await self._sync_playlists(service, full_sync)
            results["playlists"] = pl_result.get("count", 0)
            results["errors"].extend(pl_result.get("errors", []))
            set_sync_progress(self.user_id, self.email, results["subscriptions"],
                            results["playlists"], 0, True)

            # Sync liked videos
            print(f"[YOUTUBE SYNC] Syncing liked videos for {self.email}", flush=True)
            liked_result = await self._sync_liked_videos(service, full_sync)
            results["liked_videos"] = liked_result.get("count", 0)
            results["errors"].extend(liked_result.get("errors", []))

            # Update sync status
            self._update_sync_status(
                last_sync_at=datetime.utcnow(),
                subscriptions_synced=results["subscriptions"],
                playlists_synced=results["playlists"],
                liked_videos_synced=results["liked_videos"],
                sync_in_progress=False
            )
            set_sync_progress(self.user_id, self.email, results["subscriptions"],
                            results["playlists"], results["liked_videos"], False)

            duration = time.time() - start_time
            results["duration_seconds"] = round(duration, 1)

            print(f"[YOUTUBE SYNC] Complete: {results['subscriptions']} subs, "
                  f"{results['playlists']} playlists, {results['liked_videos']} liked in {duration:.1f}s", flush=True)

            return results

        except Exception as e:
            self._update_sync_status(sync_in_progress=False)
            set_sync_progress(self.user_id, self.email, 0, 0, 0, False)
            logger.error(f"YouTube sync failed: {e}")
            return {"error": str(e)}

    async def _sync_subscriptions(self, service: Resource, full_sync: bool) -> dict:
        """Sync user's channel subscriptions."""
        count = 0
        errors = []
        page_token = None

        if full_sync:
            self._clear_subscriptions()

        while True:
            try:
                request = service.subscriptions().list(
                    part="snippet",
                    mine=True,
                    maxResults=50,
                    pageToken=page_token
                )
                results = self._execute_with_backoff(request)

                if not results:
                    break

                for item in results.get("items", []):
                    try:
                        self._save_subscription(item)
                        count += 1
                    except Exception as e:
                        errors.append(f"Error saving subscription: {e}")

                page_token = results.get("nextPageToken")
                if not page_token:
                    break

            except HttpError as e:
                errors.append(f"API error: {e}")
                break

        return {"count": count, "errors": errors}

    async def _sync_playlists(self, service: Resource, full_sync: bool) -> dict:
        """Sync user's playlists."""
        count = 0
        errors = []
        page_token = None

        if full_sync:
            self._clear_playlists()

        while True:
            try:
                request = service.playlists().list(
                    part="snippet,contentDetails,status",
                    mine=True,
                    maxResults=50,
                    pageToken=page_token
                )
                results = self._execute_with_backoff(request)

                if not results:
                    break

                for item in results.get("items", []):
                    try:
                        self._save_playlist(item)
                        count += 1
                    except Exception as e:
                        errors.append(f"Error saving playlist: {e}")

                page_token = results.get("nextPageToken")
                if not page_token:
                    break

            except HttpError as e:
                errors.append(f"API error: {e}")
                break

        return {"count": count, "errors": errors}

    async def _sync_liked_videos(self, service: Resource, full_sync: bool, limit: int = 200) -> dict:
        """Sync user's liked videos (from the 'LL' playlist)."""
        count = 0
        errors = []
        page_token = None

        if full_sync:
            self._clear_liked_videos()

        while count < limit:
            try:
                # 'LL' is the special playlist ID for liked videos
                request = service.playlistItems().list(
                    part="snippet,contentDetails",
                    playlistId="LL",
                    maxResults=50,
                    pageToken=page_token
                )
                results = self._execute_with_backoff(request)

                if not results:
                    break

                for item in results.get("items", []):
                    try:
                        self._save_liked_video(item)
                        count += 1
                        if count >= limit:
                            break
                    except Exception as e:
                        errors.append(f"Error saving liked video: {e}")

                page_token = results.get("nextPageToken")
                if not page_token:
                    break

            except HttpError as e:
                # User may not have liked videos accessible
                if e.resp.status == 404:
                    print(f"[YOUTUBE SYNC] Liked videos playlist not accessible", flush=True)
                    break
                errors.append(f"API error: {e}")
                break

        return {"count": count, "errors": errors}

    # =========================================================================
    # Query Methods
    # =========================================================================

    async def list_subscriptions(self, query: str = None, limit: int = 50) -> list[dict]:
        """
        List or search subscriptions.

        Args:
            query: Optional search filter for channel title
            limit: Maximum results

        Returns:
            List of subscription dicts
        """
        with get_db() as conn:
            cursor = conn.cursor()

            if query:
                cursor.execute("""
                    SELECT subscription_id, channel_id, channel_title,
                           channel_description, thumbnail_url, subscribed_at
                    FROM youtube_subscriptions
                    WHERE user_id = %s AND google_email = %s
                    AND channel_title LIKE %s
                    ORDER BY channel_title
                    LIMIT %s
                """, [self.user_id, self.email, f"%{query}%", limit])
            else:
                cursor.execute("""
                    SELECT subscription_id, channel_id, channel_title,
                           channel_description, thumbnail_url, subscribed_at
                    FROM youtube_subscriptions
                    WHERE user_id = %s AND google_email = %s
                    ORDER BY channel_title
                    LIMIT %s
                """, [self.user_id, self.email, limit])

            rows = cursor.fetchall()
            return [
                {
                    "subscription_id": row["subscription_id"],
                    "channel_id": row["channel_id"],
                    "channel_title": row["channel_title"],
                    "channel_description": row["channel_description"],
                    "thumbnail_url": row["thumbnail_url"],
                    "subscribed_at": row["subscribed_at"]
                }
                for row in rows
            ]

    async def list_playlists(self, limit: int = 50) -> list[dict]:
        """List user's playlists."""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT playlist_id, title, description, thumbnail_url,
                       item_count, privacy_status, created_at
                FROM youtube_playlists
                WHERE user_id = %s AND google_email = %s
                ORDER BY title
                LIMIT %s
            """, [self.user_id, self.email, limit])

            rows = cursor.fetchall()
            return [
                {
                    "playlist_id": row["playlist_id"],
                    "title": row["title"],
                    "description": row["description"],
                    "thumbnail_url": row["thumbnail_url"],
                    "item_count": row["item_count"],
                    "privacy_status": row["privacy_status"],
                    "created_at": row["created_at"]
                }
                for row in rows
            ]

    async def list_liked_videos(self, limit: int = 50) -> list[dict]:
        """List user's liked videos (most recent first)."""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT video_id, title, description, channel_title,
                       thumbnail_url, duration, published_at, liked_at
                FROM youtube_liked_videos
                WHERE user_id = %s AND google_email = %s
                ORDER BY liked_at DESC
                LIMIT %s
            """, [self.user_id, self.email, limit])

            rows = cursor.fetchall()
            return [
                {
                    "video_id": row["video_id"],
                    "title": row["title"],
                    "description": row["description"],
                    "channel_title": row["channel_title"],
                    "thumbnail_url": row["thumbnail_url"],
                    "duration": row["duration"],
                    "published_at": row["published_at"],
                    "liked_at": row["liked_at"],
                    "url": f"https://youtube.com/watch?v={row['video_id']}"
                }
                for row in rows
            ]

    async def get_sync_status(self) -> dict:
        """Get current sync status."""
        status = self._get_sync_status()

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) as count FROM youtube_subscriptions WHERE user_id = %s AND google_email = %s",
                          [self.user_id, self.email])
            subs = cursor.fetchone()["count"]

            cursor.execute("SELECT COUNT(*) as count FROM youtube_playlists WHERE user_id = %s AND google_email = %s",
                          [self.user_id, self.email])
            playlists = cursor.fetchone()["count"]

            cursor.execute("SELECT COUNT(*) as count FROM youtube_liked_videos WHERE user_id = %s AND google_email = %s",
                          [self.user_id, self.email])
            liked = cursor.fetchone()["count"]

        return {
            "last_sync_at": status.get("last_sync_at"),
            "subscriptions": subs,
            "playlists": playlists,
            "liked_videos": liked,
            "sync_in_progress": bool(status.get("sync_in_progress")),
            "has_synced": status.get("last_sync_at") is not None
        }

    async def get_stats(self) -> dict:
        """Get YouTube statistics."""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) as count FROM youtube_subscriptions WHERE user_id = %s AND google_email = %s",
                          [self.user_id, self.email])
            subs = cursor.fetchone()["count"]

            cursor.execute("SELECT COUNT(*) as count FROM youtube_playlists WHERE user_id = %s AND google_email = %s",
                          [self.user_id, self.email])
            playlists = cursor.fetchone()["count"]

            cursor.execute("SELECT COUNT(*) as count FROM youtube_liked_videos WHERE user_id = %s AND google_email = %s",
                          [self.user_id, self.email])
            liked = cursor.fetchone()["count"]

            return {
                "subscriptions": subs,
                "playlists": playlists,
                "liked_videos": liked
            }

    # =========================================================================
    # Database Helper Methods
    # =========================================================================

    def _save_subscription(self, item: dict) -> None:
        """Save a subscription to the database."""
        with get_db() as conn:
            cursor = conn.cursor()

            snippet = item.get("snippet", {})
            resource = snippet.get("resourceId", {})

            cursor.execute("""
                INSERT INTO youtube_subscriptions (
                    user_id, google_email, subscription_id, channel_id,
                    channel_title, channel_description, thumbnail_url,
                    subscribed_at, last_synced_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, google_email, subscription_id)
                DO UPDATE SET
                    channel_title = excluded.channel_title,
                    channel_description = excluded.channel_description,
                    thumbnail_url = excluded.thumbnail_url,
                    last_synced_at = CURRENT_TIMESTAMP
            """, [
                self.user_id,
                self.email,
                item.get("id"),
                resource.get("channelId"),
                snippet.get("title"),
                snippet.get("description", "")[:500],  # Truncate long descriptions
                snippet.get("thumbnails", {}).get("default", {}).get("url"),
                snippet.get("publishedAt")
            ])

    def _save_playlist(self, item: dict) -> None:
        """Save a playlist to the database."""
        with get_db() as conn:
            cursor = conn.cursor()

            snippet = item.get("snippet", {})
            content = item.get("contentDetails", {})
            status = item.get("status", {})

            cursor.execute("""
                INSERT INTO youtube_playlists (
                    user_id, google_email, playlist_id, title, description,
                    thumbnail_url, item_count, privacy_status, created_at, last_synced_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, google_email, playlist_id)
                DO UPDATE SET
                    title = excluded.title,
                    description = excluded.description,
                    thumbnail_url = excluded.thumbnail_url,
                    item_count = excluded.item_count,
                    privacy_status = excluded.privacy_status,
                    last_synced_at = CURRENT_TIMESTAMP
            """, [
                self.user_id,
                self.email,
                item.get("id"),
                snippet.get("title"),
                snippet.get("description", "")[:500],
                snippet.get("thumbnails", {}).get("default", {}).get("url"),
                content.get("itemCount", 0),
                status.get("privacyStatus"),
                snippet.get("publishedAt")
            ])

    def _save_liked_video(self, item: dict) -> None:
        """Save a liked video to the database."""
        with get_db() as conn:
            cursor = conn.cursor()

            snippet = item.get("snippet", {})
            content = item.get("contentDetails", {})

            cursor.execute("""
                INSERT INTO youtube_liked_videos (
                    user_id, google_email, video_id, title, description,
                    channel_title, thumbnail_url, duration, published_at,
                    liked_at, last_synced_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, google_email, video_id)
                DO UPDATE SET
                    title = excluded.title,
                    description = excluded.description,
                    channel_title = excluded.channel_title,
                    thumbnail_url = excluded.thumbnail_url,
                    last_synced_at = CURRENT_TIMESTAMP
            """, [
                self.user_id,
                self.email,
                content.get("videoId") or snippet.get("resourceId", {}).get("videoId"),
                snippet.get("title"),
                snippet.get("description", "")[:500],
                snippet.get("videoOwnerChannelTitle"),
                snippet.get("thumbnails", {}).get("default", {}).get("url"),
                None,  # Duration requires separate video details API call
                snippet.get("publishedAt"),
                content.get("videoPublishedAt") or snippet.get("publishedAt")
            ])

    def _clear_subscriptions(self) -> None:
        """Clear all subscriptions for this account."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM youtube_subscriptions WHERE user_id = %s AND google_email = %s",
                          [self.user_id, self.email])

    def _clear_playlists(self) -> None:
        """Clear all playlists for this account."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM youtube_playlists WHERE user_id = %s AND google_email = %s",
                          [self.user_id, self.email])

    def _clear_liked_videos(self) -> None:
        """Clear all liked videos for this account."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM youtube_liked_videos WHERE user_id = %s AND google_email = %s",
                          [self.user_id, self.email])

    def _get_sync_status(self) -> dict:
        """Get sync status from database."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT last_sync_at, subscriptions_synced, playlists_synced,
                       liked_videos_synced, sync_in_progress
                FROM youtube_sync_status
                WHERE user_id = %s AND google_email = %s
            """, [self.user_id, self.email])

            row = cursor.fetchone()
            if row:
                return {
                    "last_sync_at": row["last_sync_at"],
                    "subscriptions_synced": row["subscriptions_synced"],
                    "playlists_synced": row["playlists_synced"],
                    "liked_videos_synced": row["liked_videos_synced"],
                    "sync_in_progress": row["sync_in_progress"]
                }
            return {}

    def _update_sync_status(
        self,
        last_sync_at: datetime = None,
        subscriptions_synced: int = None,
        playlists_synced: int = None,
        liked_videos_synced: int = None,
        sync_in_progress: bool = None
    ) -> None:
        """Update sync status in database."""
        with get_db() as conn:
            cursor = conn.cursor()

            updates = []
            params = []

            if last_sync_at is not None:
                updates.append("last_sync_at = %s")
                params.append(last_sync_at.isoformat())
            if subscriptions_synced is not None:
                updates.append("subscriptions_synced = %s")
                params.append(subscriptions_synced)
            if playlists_synced is not None:
                updates.append("playlists_synced = %s")
                params.append(playlists_synced)
            if liked_videos_synced is not None:
                updates.append("liked_videos_synced = %s")
                params.append(liked_videos_synced)
            if sync_in_progress is not None:
                updates.append("sync_in_progress = %s")
                params.append(1 if sync_in_progress else 0)

            if not updates:
                return

            cursor.execute(f"""
                INSERT INTO youtube_sync_status (user_id, google_email, {', '.join(u.split(' = ')[0] for u in updates)})
                VALUES (%s, %s, {', '.join('%s' * len(params))})
                ON CONFLICT(user_id, google_email)
                DO UPDATE SET {', '.join(updates)}
            """, [self.user_id, self.email] + params + params)
