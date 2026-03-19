"""
Google Drive API service wrapper for Seny.

Provides Drive access with automatic token refresh:
- Load credentials from database (shared with Gmail)
- Auto-refresh expired tokens
- Build Drive API service object
- Sync files to local database for fast searching

Usage:
    drive = DriveService(user_id, email)
    if drive.is_connected():
        files = await drive.search_files("tax documents")
"""

import os
import io
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build, Resource
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from web.core.database import get_gmail_token, save_gmail_token, get_db

logger = logging.getLogger(__name__)

# In-memory sync progress tracking (to avoid database lock contention)
# Key: "user_id:email", Value: {"files_synced": int, "in_progress": bool}
_sync_progress = {}


# ---------------------------------------------------------------------------
# Token refresh circuit breaker
# Prevents infinite retry storms when a Google token is revoked.
# Key: "user_id:email"
# ---------------------------------------------------------------------------
_token_circuit: dict[str, dict] = {}
_TOKEN_CIRCUIT_THRESHOLD = 3          # failures before opening
_TOKEN_CIRCUIT_RECOVERY_SECONDS = 3600  # 1-hour cooldown


def _check_token_circuit(user_id: int, email: str) -> bool:
    """Return True if circuit is open (refresh should be skipped)."""
    key = f"{user_id}:{email}"
    state = _token_circuit.get(key)
    if not state:
        return False
    if state["failures"] < _TOKEN_CIRCUIT_THRESHOLD:
        return False
    elapsed = time.time() - state["opened_at"]
    if elapsed >= _TOKEN_CIRCUIT_RECOVERY_SECONDS:
        _token_circuit.pop(key, None)
        return False
    return True  # Circuit open


def _record_token_failure(user_id: int, email: str, error: Exception) -> None:
    """Increment failure count; open circuit after threshold."""
    key = f"{user_id}:{email}"
    state = _token_circuit.setdefault(key, {"failures": 0, "opened_at": None})
    state["failures"] += 1
    failure_count = state["failures"]
    if failure_count >= _TOKEN_CIRCUIT_THRESHOLD:
        state["opened_at"] = time.time()
        logger.warning(
            "Token circuit open for %s (user %d) — skipping refresh for %d min",
            email, user_id, _TOKEN_CIRCUIT_RECOVERY_SECONDS // 60
        )
        if failure_count == _TOKEN_CIRCUIT_THRESHOLD:
            from web.services.integration_alerts import schedule_token_alert
            schedule_token_alert(user_id, "drive", email)
    else:
        logger.error(
            "Token refresh failed for %s (user %d): %s — circuit failure %d/%d",
            email, user_id, repr(error), failure_count, _TOKEN_CIRCUIT_THRESHOLD
        )


def _reset_token_circuit(user_id: int, email: str) -> None:
    """Reset circuit after a successful token refresh."""
    _token_circuit.pop(f"{user_id}:{email}", None)

def get_sync_progress(user_id: int, email: str) -> dict:
    """Get in-memory sync progress for an account."""
    key = f"{user_id}:{email}"
    return _sync_progress.get(key, {"files_synced": 0, "in_progress": False})

def set_sync_progress(user_id: int, email: str, files_synced: int, in_progress: bool):
    """Set in-memory sync progress for an account."""
    key = f"{user_id}:{email}"
    _sync_progress[key] = {"files_synced": files_synced, "in_progress": in_progress}

# OAuth Configuration (shared with Gmail)
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# MIME type mappings for user-friendly file types
MIME_TYPE_MAP = {
    "document": [
        "application/vnd.google-apps.document",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ],
    "spreadsheet": [
        "application/vnd.google-apps.spreadsheet",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ],
    "presentation": [
        "application/vnd.google-apps.presentation",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ],
    "pdf": ["application/pdf"],
    "image": ["image/jpeg", "image/png", "image/gif", "image/webp", "image/svg+xml"],
    "video": ["video/mp4", "video/quicktime", "video/x-msvideo", "video/webm"],
    "folder": ["application/vnd.google-apps.folder"],
}

# Google Workspace export MIME types (for reading Google Docs as text)
EXPORT_MIME_TYPES = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


class DriveService:
    """
    Google Drive API wrapper with automatic credential management.

    Handles OAuth token refresh and Drive API service creation.
    Uses same OAuth credentials as Gmail (Drive scope added to combined flow).

    Attributes:
        user_id: The user's database ID
        email: The Google account email
    """

    def __init__(self, user_id: int, email: str):
        """
        Initialize Drive service for a specific user and email account.

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

        Note: This only checks if tokens exist, not if they're valid
        or if Drive scope is included.

        Returns:
            True if this email has Google tokens stored
        """
        token_data = get_gmail_token(self.user_id, self.email)
        return token_data is not None

    async def get_credentials(self) -> Optional[Credentials]:
        """
        Load credentials from database and refresh if expired.

        Returns:
            Valid Credentials object, or None if:
            - No tokens stored for this email
            - Refresh token is invalid/revoked (needs re-authorization)

        Side effect:
            Updates database if tokens were refreshed
        """
        if self._credentials is not None:
            if not self._credentials.expired:
                return self._credentials

        token_data = get_gmail_token(self.user_id, self.email)
        if not token_data:
            return None

        # Parse expiry string to datetime if present
        expiry = None
        if token_data["expiry"]:
            try:
                expiry = datetime.fromisoformat(token_data["expiry"])
            except ValueError:
                pass

        # Build Credentials object from stored data
        self._credentials = Credentials(
            token=token_data["access_token"],
            refresh_token=token_data["refresh_token"],
            token_uri=token_data["token_uri"],
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=token_data["scopes"].split(",") if token_data["scopes"] else None,
            expiry=expiry
        )

        # Refresh if expired
        if self._credentials.expired and self._credentials.refresh_token:
            # Check circuit breaker before hitting Google's OAuth server
            if _check_token_circuit(self.user_id, self.email):
                self._credentials = None
                return None
            try:
                self._credentials.refresh(Request())
                save_gmail_token(self.user_id, self.email, self._credentials)
                # Successful refresh — reset any previous failure count
                _reset_token_circuit(self.user_id, self.email)
            except Exception as e:
                _record_token_failure(self.user_id, self.email, e)
                self._credentials = None
                return None

        return self._credentials

    async def get_service(self) -> Optional[Resource]:
        """
        Get or create Drive API service.

        Returns:
            Drive API service object, or None if not connected/authorized
        """
        if self._service is not None:
            return self._service

        credentials = await self.get_credentials()
        if credentials is None:
            return None

        self._service = build("drive", "v3", credentials=credentials)
        return self._service

    def _execute_with_backoff(self, request, max_retries: int = 3):
        """
        Execute Drive API request with exponential backoff for rate limits.

        Args:
            request: Drive API request object
            max_retries: Maximum number of retry attempts

        Returns:
            API response or None if all retries failed
        """
        for attempt in range(max_retries):
            try:
                return request.execute()
            except HttpError as e:
                if e.resp.status in (429, 500, 503):
                    wait_time = (2 ** attempt) + (time.time() % 1)
                    logger.warning(f"Drive API error {e.resp.status}, retrying in {wait_time:.1f}s")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Drive API error: {e}")
                    raise
        return None

    # =========================================================================
    # Sync Methods
    # =========================================================================

    async def sync_files(self, full_sync: bool = False) -> dict:
        """
        Sync Drive files to local database for fast searching.

        Performs incremental sync using change tokens when possible,
        or full sync if requested or no previous sync exists.

        Args:
            full_sync: Force full re-sync even if change token exists

        Returns:
            Dict with sync results: files_synced, errors, duration_seconds
        """
        service = await self.get_service()
        if not service:
            return {"error": "Not connected to Google Drive"}

        start_time = time.time()
        files_synced = 0
        errors = []

        # Check for existing sync status
        sync_status = self._get_sync_status()
        change_token = sync_status.get("change_token") if not full_sync else None

        # Mark sync as in progress
        self._update_sync_status(sync_in_progress=True)

        try:
            if change_token and not full_sync:
                # Incremental sync using changes API
                files_synced, errors = await self._sync_changes(service, change_token)
            else:
                # Full sync - fetch all files
                files_synced, errors = await self._sync_all_files(service)

            # Get new change token for next sync
            new_token = self._get_start_page_token(service)

            # Update sync status
            self._update_sync_status(
                last_sync_at=datetime.utcnow(),
                files_synced=files_synced,
                change_token=new_token,
                sync_in_progress=False
            )

            duration = time.time() - start_time
            logger.info(f"Drive sync completed: {files_synced} files in {duration:.1f}s")

            return {
                "files_synced": files_synced,
                "errors": errors,
                "duration_seconds": round(duration, 1)
            }

        except Exception as e:
            self._update_sync_status(sync_in_progress=False)
            logger.error(f"Drive sync failed: {e}")
            return {"error": str(e)}

    async def _sync_all_files(self, service: Resource) -> tuple[int, list]:
        """
        Perform full sync of all Drive files.

        Returns:
            Tuple of (files_synced, errors_list)
        """
        files_synced = 0
        errors = []
        page_token = None

        # Mark sync as in progress (in memory for fast status checks)
        set_sync_progress(self.user_id, self.email, 0, True)

        # Clear existing files for this account
        print(f"[SYNC DEBUG] Starting sync for {self.email}", flush=True)
        self._clear_files()
        print(f"[SYNC DEBUG] Cleared existing files", flush=True)

        while True:
            try:
                print(f"[SYNC DEBUG] Calling files().list(), page_token={page_token is not None}", flush=True)
                # List files with all needed fields
                results = self._execute_with_backoff(
                    service.files().list(
                        pageSize=100,
                        pageToken=page_token,
                        fields="nextPageToken, files(id, name, mimeType, size, parents, createdTime, modifiedTime, webViewLink, fileExtension)",
                        q="trashed = false"
                    )
                )

                print(f"[SYNC DEBUG] API returned, results is not None: {results is not None}", flush=True)

                if not results:
                    print(f"[SYNC DEBUG] results is falsy, breaking", flush=True)
                    errors.append("API returned None/empty results")
                    break

                files = results.get("files", [])
                print(f"[SYNC DEBUG] Got {len(files)} files, total so far: {files_synced}", flush=True)

                for file in files:
                    try:
                        self._save_file(file)
                        files_synced += 1
                    except Exception as e:
                        errors.append(f"Error saving {file.get('name')}: {e}")

                # Update in-memory progress after each page (fast, no DB lock)
                set_sync_progress(self.user_id, self.email, files_synced, True)

                # Update database progress every 1000 files (to reduce lock contention)
                if files_synced % 1000 < 100:
                    self._update_sync_status(files_synced=files_synced)

                page_token = results.get("nextPageToken")
                print(f"[SYNC DEBUG] Page done. Has more pages: {page_token is not None}", flush=True)
                if not page_token:
                    break

            except HttpError as e:
                print(f"[SYNC DEBUG] HttpError: {e}", flush=True)
                errors.append(f"API error during sync: {e}")
                break
            except Exception as e:
                print(f"[SYNC DEBUG] Unexpected error: {e}", flush=True)
                import traceback
                print(traceback.format_exc(), flush=True)
                errors.append(f"Unexpected error: {e}")
                break

        # Mark sync as complete (in memory)
        set_sync_progress(self.user_id, self.email, files_synced, False)

        print(f"[SYNC DEBUG] Sync complete: {files_synced} files, {len(errors)} errors", flush=True)
        return files_synced, errors

    async def _sync_changes(self, service: Resource, change_token: str) -> tuple[int, list]:
        """
        Sync only changed files since last sync.

        Args:
            service: Drive API service
            change_token: Start page token from previous sync

        Returns:
            Tuple of (files_synced, errors_list)
        """
        files_synced = 0
        errors = []
        page_token = change_token

        while True:
            try:
                results = self._execute_with_backoff(
                    service.changes().list(
                        pageToken=page_token,
                        pageSize=100,
                        fields="nextPageToken, newStartPageToken, changes(fileId, removed, file(id, name, mimeType, size, parents, createdTime, modifiedTime, webViewLink, fileExtension))"
                    )
                )

                if not results:
                    break

                changes = results.get("changes", [])
                for change in changes:
                    try:
                        if change.get("removed"):
                            self._delete_file(change.get("fileId"))
                        else:
                            file = change.get("file")
                            if file:
                                self._save_file(file)
                                files_synced += 1
                    except Exception as e:
                        errors.append(f"Error processing change: {e}")

                page_token = results.get("nextPageToken")
                if not page_token:
                    break

            except HttpError as e:
                errors.append(f"API error during change sync: {e}")
                break

        return files_synced, errors

    def _get_start_page_token(self, service: Resource) -> Optional[str]:
        """Get the start page token for incremental sync."""
        try:
            response = self._execute_with_backoff(
                service.changes().getStartPageToken()
            )
            return response.get("startPageToken")
        except Exception as e:
            logger.error(f"Failed to get start page token: {e}")
            return None

    # =========================================================================
    # Query Methods
    # =========================================================================

    async def search_files(
        self,
        query: str,
        file_type: str = None,
        limit: int = 20
    ) -> list[dict]:
        """
        Search Drive files by name or content using FTS5.

        Args:
            query: Search query (file name or content)
            file_type: Filter by type (document, spreadsheet, pdf, etc.)
            limit: Maximum results (default 20)

        Returns:
            List of file dicts with: id, name, mimeType, modifiedTime, webViewLink
        """
        with get_db() as conn:
            cursor = conn.cursor()

            # Build query based on file_type filter
            like_query = f"%{query}%"
            if file_type and file_type in MIME_TYPE_MAP:
                mime_types = MIME_TYPE_MAP[file_type]
                placeholders = ",".join("%s" * len(mime_types))
                sql = f"""
                    SELECT df.file_id, df.name, df.mime_type, df.size_bytes,
                           df.modified_time, df.web_view_link, df.path
                    FROM drive_files df
                    WHERE df.name ILIKE %s
                    AND df.user_id = %s
                    AND df.google_email = %s
                    AND df.mime_type IN ({placeholders})
                    ORDER BY df.modified_time DESC
                    LIMIT %s
                """
                params = [like_query, self.user_id, self.email] + mime_types + [limit]
            else:
                sql = """
                    SELECT df.file_id, df.name, df.mime_type, df.size_bytes,
                           df.modified_time, df.web_view_link, df.path
                    FROM drive_files df
                    WHERE df.name ILIKE %s
                    AND df.user_id = %s
                    AND df.google_email = %s
                    ORDER BY df.modified_time DESC
                    LIMIT %s
                """
                params = [like_query, self.user_id, self.email, limit]

            cursor.execute(sql, params)
            rows = cursor.fetchall()

            return [
                {
                    "file_id": row["file_id"],
                    "name": row["name"],
                    "mime_type": row["mime_type"],
                    "size_bytes": row["size_bytes"],
                    "modified_time": row["modified_time"],
                    "web_view_link": row["web_view_link"],
                    "path": row["path"],
                    "type": self._get_friendly_type(row["mime_type"])
                }
                for row in rows
            ]

    async def list_recent(self, days: int = 7, limit: int = 20, file_type: str = None) -> list[dict]:
        """
        Get recently modified files.

        Args:
            days: Look back this many days (default 7)
            limit: Maximum results (default 20)
            file_type: Filter by type (optional)

        Returns:
            List of file dicts sorted by modified time
        """
        cutoff = datetime.utcnow() - timedelta(days=days)

        with get_db() as conn:
            cursor = conn.cursor()

            if file_type and file_type in MIME_TYPE_MAP:
                mime_types = MIME_TYPE_MAP[file_type]
                placeholders = ",".join("%s" * len(mime_types))
                sql = f"""
                    SELECT file_id, name, mime_type, size_bytes,
                           modified_time, web_view_link, path
                    FROM drive_files
                    WHERE user_id = %s
                    AND google_email = %s
                    AND modified_time >= %s
                    AND mime_type IN ({placeholders})
                    ORDER BY modified_time DESC
                    LIMIT %s
                """
                params = [self.user_id, self.email, cutoff.isoformat()] + mime_types + [limit]
            else:
                sql = """
                    SELECT file_id, name, mime_type, size_bytes,
                           modified_time, web_view_link, path
                    FROM drive_files
                    WHERE user_id = %s
                    AND google_email = %s
                    AND modified_time >= %s
                    ORDER BY modified_time DESC
                    LIMIT %s
                """
                params = [self.user_id, self.email, cutoff.isoformat(), limit]

            cursor.execute(sql, params)
            rows = cursor.fetchall()

            return [
                {
                    "file_id": row["file_id"],
                    "name": row["name"],
                    "mime_type": row["mime_type"],
                    "size_bytes": row["size_bytes"],
                    "modified_time": row["modified_time"],
                    "web_view_link": row["web_view_link"],
                    "path": row["path"],
                    "type": self._get_friendly_type(row["mime_type"])
                }
                for row in rows
            ]

    async def get_file(self, file_id: str) -> Optional[dict]:
        """
        Get file metadata by Drive file ID.

        Args:
            file_id: Google Drive file ID

        Returns:
            File dict or None if not found
        """
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT file_id, name, mime_type, size_bytes, path,
                       created_time, modified_time, web_view_link
                FROM drive_files
                WHERE user_id = %s AND google_email = %s AND file_id = %s
            """, [self.user_id, self.email, file_id])

            row = cursor.fetchone()
            if not row:
                return None

            return {
                "file_id": row["file_id"],
                "name": row["name"],
                "mime_type": row["mime_type"],
                "size_bytes": row["size_bytes"],
                "path": row["path"],
                "created_time": row["created_time"],
                "modified_time": row["modified_time"],
                "web_view_link": row["web_view_link"],
                "type": self._get_friendly_type(row["mime_type"])
            }

    async def get_file_content(self, file_id: str, max_chars: int = 10000) -> Optional[str]:
        """
        Read content of a Google Doc, Sheet, or text file.

        For Google Workspace files (Docs, Sheets, Slides), exports as plain text.
        For regular text files, downloads content directly.

        Args:
            file_id: Google Drive file ID
            max_chars: Maximum characters to return (default 10000)

        Returns:
            File content as text, or None if not readable
        """
        service = await self.get_service()
        if not service:
            return None

        try:
            # Get file metadata to check MIME type
            file_meta = self._execute_with_backoff(
                service.files().get(fileId=file_id, fields="mimeType, name")
            )

            if not file_meta:
                return None

            mime_type = file_meta.get("mimeType", "")

            # Google Workspace files need to be exported
            if mime_type in EXPORT_MIME_TYPES:
                export_mime = EXPORT_MIME_TYPES[mime_type]
                request = service.files().export_media(fileId=file_id, mimeType=export_mime)
            elif mime_type.startswith("text/") or mime_type == "application/json":
                # Regular text files can be downloaded directly
                request = service.files().get_media(fileId=file_id)
            else:
                # Binary files - not readable as text
                return f"[Cannot read content of {file_meta.get('name')} - binary file type: {mime_type}]"

            # Download content
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()

            content = fh.getvalue().decode("utf-8", errors="replace")

            # Truncate if too long
            if len(content) > max_chars:
                content = content[:max_chars] + f"\n\n[Content truncated at {max_chars} characters]"

            return content

        except HttpError as e:
            logger.error(f"Failed to read file {file_id}: {e}")
            return None

    # =========================================================================
    # Write Methods
    # =========================================================================

    async def create_document(
        self,
        title: str,
        content: str,
        folder_name: str = "Seny",
        doc_type: str = "document"
    ) -> Optional[dict]:
        """
        Create a new Google Doc or text file in Drive.

        Args:
            title: Document title/filename
            content: Text content to write
            folder_name: Folder to create document in (default "Seny", created if needed)
            doc_type: "document" for Google Doc, "text" for plain .txt file

        Returns:
            Dict with file info (id, name, webViewLink) or None if failed
        """
        from googleapiclient.http import MediaInMemoryUpload

        service = await self.get_service()
        if not service:
            return None

        try:
            # Find or create the target folder
            folder_id = await self._get_or_create_folder(service, folder_name)

            if doc_type == "document":
                # Create as Google Doc (can be edited in Google Docs)
                file_metadata = {
                    "name": title,
                    "mimeType": "application/vnd.google-apps.document",
                    "parents": [folder_id] if folder_id else []
                }

                # Google Docs need content uploaded separately, but we can
                # create with plain text and it converts automatically
                media = MediaInMemoryUpload(
                    content.encode("utf-8"),
                    mimetype="text/plain",
                    resumable=True
                )

                file = self._execute_with_backoff(
                    service.files().create(
                        body=file_metadata,
                        media_body=media,
                        fields="id, name, webViewLink, mimeType, createdTime"
                    )
                )
            else:
                # Create as plain text file
                file_metadata = {
                    "name": f"{title}.txt" if not title.endswith(".txt") else title,
                    "mimeType": "text/plain",
                    "parents": [folder_id] if folder_id else []
                }

                media = MediaInMemoryUpload(
                    content.encode("utf-8"),
                    mimetype="text/plain",
                    resumable=True
                )

                file = self._execute_with_backoff(
                    service.files().create(
                        body=file_metadata,
                        media_body=media,
                        fields="id, name, webViewLink, mimeType, createdTime"
                    )
                )

            if file:
                logger.info(f"Created Drive document: {file.get('name')} ({file.get('id')})")

                # Save to local database for searching
                self._save_file({
                    "id": file.get("id"),
                    "name": file.get("name"),
                    "mimeType": file.get("mimeType"),
                    "webViewLink": file.get("webViewLink"),
                    "createdTime": file.get("createdTime"),
                    "modifiedTime": file.get("createdTime"),
                    "parents": [folder_id] if folder_id else []
                })

                return {
                    "file_id": file.get("id"),
                    "name": file.get("name"),
                    "web_view_link": file.get("webViewLink"),
                    "mime_type": file.get("mimeType")
                }

            return None

        except HttpError as e:
            logger.error(f"Failed to create document: {e}")
            return None

    async def _get_or_create_folder(self, service: Resource, folder_name: str) -> Optional[str]:
        """
        Find or create a folder by name in Drive root.

        Args:
            service: Drive API service
            folder_name: Name of folder to find/create

        Returns:
            Folder ID or None
        """
        try:
            # Search for existing folder
            results = self._execute_with_backoff(
                service.files().list(
                    q=f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
                    spaces="drive",
                    fields="files(id, name)"
                )
            )

            files = results.get("files", []) if results else []

            if files:
                # Folder exists
                return files[0]["id"]

            # Create folder
            folder_metadata = {
                "name": folder_name,
                "mimeType": "application/vnd.google-apps.folder"
            }

            folder = self._execute_with_backoff(
                service.files().create(
                    body=folder_metadata,
                    fields="id"
                )
            )

            if folder:
                logger.info(f"Created Drive folder: {folder_name} ({folder.get('id')})")
                return folder.get("id")

            return None

        except HttpError as e:
            logger.error(f"Failed to get/create folder {folder_name}: {e}")
            return None

    async def get_sync_status(self) -> dict:
        """
        Get current sync status for this account.

        Returns:
            Dict with: last_sync_at, files_synced, sync_in_progress
        """
        status = self._get_sync_status()

        # Get total file count
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) as count FROM drive_files
                WHERE user_id = %s AND google_email = %s
            """, [self.user_id, self.email])
            row = cursor.fetchone()
            total_files = row["count"] if row else 0

        return {
            "last_sync_at": status.get("last_sync_at"),
            "files_synced": total_files,
            "sync_in_progress": bool(status.get("sync_in_progress")),
            "has_synced": status.get("last_sync_at") is not None
        }

    async def get_stats(self) -> dict:
        """
        Get Drive file statistics.

        Returns:
            Dict with file counts by type and total size
        """
        with get_db() as conn:
            cursor = conn.cursor()

            # Total files
            cursor.execute("""
                SELECT COUNT(*) as count FROM drive_files
                WHERE user_id = %s AND google_email = %s
            """, [self.user_id, self.email])
            total = cursor.fetchone()["count"]

            # Total size
            cursor.execute("""
                SELECT SUM(size_bytes) as total FROM drive_files
                WHERE user_id = %s AND google_email = %s
            """, [self.user_id, self.email])
            total_size = cursor.fetchone()["total"] or 0

            # Count by type
            type_counts = {}
            for type_name, mime_types in MIME_TYPE_MAP.items():
                if type_name == "folder":
                    continue
                placeholders = ",".join("%s" * len(mime_types))
                cursor.execute(f"""
                    SELECT COUNT(*) as count FROM drive_files
                    WHERE user_id = %s AND google_email = %s
                    AND mime_type IN ({placeholders})
                """, [self.user_id, self.email] + mime_types)
                type_counts[type_name] = cursor.fetchone()["count"]

            return {
                "total_files": total,
                "total_size_bytes": total_size,
                "total_size_mb": round(total_size / (1024 * 1024), 1) if total_size else 0,
                "by_type": type_counts
            }

    # =========================================================================
    # Database Helper Methods
    # =========================================================================

    def _save_file(self, file: dict) -> None:
        """Save or update a file in the database."""
        with get_db() as conn:
            cursor = conn.cursor()

            # Extract file extension from name if not provided
            name = file.get("name", "")
            extension = file.get("fileExtension") or ""
            if not extension and "." in name:
                extension = name.rsplit(".", 1)[-1].lower()

            cursor.execute("""
                INSERT INTO drive_files (
                    user_id, google_email, file_id, name, mime_type,
                    file_extension, size_bytes, parent_id, created_time,
                    modified_time, web_view_link, last_synced_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, google_email, file_id)
                DO UPDATE SET
                    name = excluded.name,
                    mime_type = excluded.mime_type,
                    file_extension = excluded.file_extension,
                    size_bytes = excluded.size_bytes,
                    parent_id = excluded.parent_id,
                    modified_time = excluded.modified_time,
                    web_view_link = excluded.web_view_link,
                    last_synced_at = CURRENT_TIMESTAMP
            """, [
                self.user_id,
                self.email,
                file.get("id"),
                name,
                file.get("mimeType"),
                extension,
                file.get("size"),
                file.get("parents", [None])[0] if file.get("parents") else None,
                file.get("createdTime"),
                file.get("modifiedTime"),
                file.get("webViewLink")
            ])

    def _delete_file(self, file_id: str) -> None:
        """Delete a file from the database."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM drive_files
                WHERE user_id = %s AND google_email = %s AND file_id = %s
            """, [self.user_id, self.email, file_id])

    def _clear_files(self) -> None:
        """Clear all files for this account (for full re-sync)."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM drive_files
                WHERE user_id = %s AND google_email = %s
            """, [self.user_id, self.email])

    def _get_sync_status(self) -> dict:
        """Get sync status from database."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT last_sync_at, files_synced, change_token, sync_in_progress
                FROM drive_sync_status
                WHERE user_id = %s AND google_email = %s
            """, [self.user_id, self.email])

            row = cursor.fetchone()
            if row:
                return {
                    "last_sync_at": row["last_sync_at"],
                    "files_synced": row["files_synced"],
                    "change_token": row["change_token"],
                    "sync_in_progress": row["sync_in_progress"]
                }
            return {}

    def _update_sync_status(
        self,
        last_sync_at: datetime = None,
        files_synced: int = None,
        change_token: str = None,
        sync_in_progress: bool = None
    ) -> None:
        """Update sync status in database."""
        with get_db() as conn:
            cursor = conn.cursor()

            # Build update fields
            updates = []
            params = []

            if last_sync_at is not None:
                updates.append("last_sync_at = %s")
                params.append(last_sync_at.isoformat())
            if files_synced is not None:
                updates.append("files_synced = %s")
                params.append(files_synced)
            if change_token is not None:
                updates.append("change_token = %s")
                params.append(change_token)
            if sync_in_progress is not None:
                updates.append("sync_in_progress = %s")
                params.append(1 if sync_in_progress else 0)

            if not updates:
                return

            # Upsert
            cursor.execute(f"""
                INSERT INTO drive_sync_status (user_id, google_email, {', '.join(u.split(' = ')[0] for u in updates)})
                VALUES (%s, %s, {', '.join(['%s'] * len(params))})
                ON CONFLICT(user_id, google_email)
                DO UPDATE SET {', '.join(updates)}
            """, [self.user_id, self.email] + params + params)

    def _get_friendly_type(self, mime_type: str) -> str:
        """Convert MIME type to friendly name."""
        if not mime_type:
            return "file"

        for type_name, mime_types in MIME_TYPE_MAP.items():
            if mime_type in mime_types:
                return type_name

        # Check common patterns
        if mime_type.startswith("image/"):
            return "image"
        if mime_type.startswith("video/"):
            return "video"
        if mime_type.startswith("audio/"):
            return "audio"
        if mime_type.startswith("text/"):
            return "text"

        return "file"
