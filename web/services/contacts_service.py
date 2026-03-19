"""
Google Contacts (People API) service wrapper for Seny.

Provides Contacts access with automatic token refresh:
- Load credentials from database (shared with Gmail)
- Auto-refresh expired tokens
- Build People API service object
- Sync contacts to local database for fast searching

Usage:
    contacts = ContactsService(user_id, email)
    if contacts.is_connected():
        results = await contacts.search_contacts("John")
"""

import os
import json
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

# In-memory sync progress tracking (to avoid database lock contention)
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
            schedule_token_alert(user_id, "contacts", email)
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
    key = f"contacts:{user_id}:{email}"
    return _sync_progress.get(key, {"contacts_synced": 0, "in_progress": False})

def set_sync_progress(user_id: int, email: str, contacts_synced: int, in_progress: bool):
    """Set in-memory sync progress for an account."""
    key = f"contacts:{user_id}:{email}"
    _sync_progress[key] = {"contacts_synced": contacts_synced, "in_progress": in_progress}

# OAuth Configuration (shared with Gmail)
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# Fields to request from People API
PERSON_FIELDS = "names,emailAddresses,phoneNumbers,addresses,organizations,biographies,birthdays,photos"


class ContactsService:
    """
    Google Contacts (People API) wrapper with automatic credential management.

    Handles OAuth token refresh and People API service creation.
    Uses same OAuth credentials as Gmail (contacts scope added to combined flow).

    Attributes:
        user_id: The user's database ID
        email: The Google account email
    """

    def __init__(self, user_id: int, email: str):
        """
        Initialize Contacts service for a specific user and email account.

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
        Get or create People API service.

        Returns:
            People API service object, or None if not connected/authorized
        """
        if self._service is not None:
            return self._service

        credentials = await self.get_credentials()
        if credentials is None:
            return None

        self._service = build("people", "v1", credentials=credentials)
        return self._service

    def _execute_with_backoff(self, request, max_retries: int = 3):
        """
        Execute People API request with exponential backoff for rate limits.

        Args:
            request: API request object
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
                    logger.warning(f"People API error {e.resp.status}, retrying in {wait_time:.1f}s")
                    time.sleep(wait_time)
                else:
                    logger.error(f"People API error: {e}")
                    raise
        return None

    # =========================================================================
    # Sync Methods
    # =========================================================================

    async def sync_contacts(self, full_sync: bool = False) -> dict:
        """
        Sync Google Contacts to local database for fast searching.

        Args:
            full_sync: Force full re-sync even if sync token exists

        Returns:
            Dict with sync results: contacts_synced, errors, duration_seconds
        """
        service = await self.get_service()
        if not service:
            return {"error": "Not connected to Google Contacts"}

        start_time = time.time()
        contacts_synced = 0
        errors = []

        # Check for existing sync status
        sync_status = self._get_sync_status()
        sync_token = sync_status.get("sync_token") if not full_sync else None

        # Mark sync as in progress
        self._update_sync_status(sync_in_progress=True)
        set_sync_progress(self.user_id, self.email, 0, True)

        try:
            if sync_token and not full_sync:
                # Incremental sync using sync token
                contacts_synced, errors, new_token = await self._sync_with_token(service, sync_token)
            else:
                # Full sync - fetch all contacts
                contacts_synced, errors, new_token = await self._sync_all_contacts(service)

            # Update sync status
            self._update_sync_status(
                last_sync_at=datetime.utcnow(),
                contacts_synced=contacts_synced,
                sync_token=new_token,
                sync_in_progress=False
            )

            # Update in-memory progress
            set_sync_progress(self.user_id, self.email, contacts_synced, False)

            duration = time.time() - start_time
            logger.info(f"Contacts sync completed: {contacts_synced} contacts in {duration:.1f}s")

            return {
                "contacts_synced": contacts_synced,
                "errors": errors,
                "duration_seconds": round(duration, 1)
            }

        except Exception as e:
            self._update_sync_status(sync_in_progress=False)
            set_sync_progress(self.user_id, self.email, 0, False)
            logger.error(f"Contacts sync failed: {e}")
            return {"error": str(e)}

    async def _sync_all_contacts(self, service: Resource) -> tuple[int, list, Optional[str]]:
        """
        Perform full sync of all contacts.

        Returns:
            Tuple of (contacts_synced, errors_list, new_sync_token)
        """
        contacts_synced = 0
        errors = []
        page_token = None
        new_sync_token = None

        # Clear existing contacts for this account
        print(f"[CONTACTS SYNC] Starting sync for {self.email}", flush=True)
        self._clear_contacts()
        print(f"[CONTACTS SYNC] Cleared existing contacts", flush=True)

        while True:
            try:
                # List contacts with all needed fields
                request = service.people().connections().list(
                    resourceName="people/me",
                    pageSize=100,
                    pageToken=page_token,
                    personFields=PERSON_FIELDS,
                    requestSyncToken=True
                )
                results = self._execute_with_backoff(request)

                if not results:
                    print(f"[CONTACTS SYNC] API returned None/empty results", flush=True)
                    errors.append("API returned None/empty results")
                    break

                connections = results.get("connections", [])
                print(f"[CONTACTS SYNC] Got {len(connections)} contacts, total so far: {contacts_synced}", flush=True)

                for person in connections:
                    try:
                        self._save_contact(person)
                        contacts_synced += 1
                    except Exception as e:
                        errors.append(f"Error saving contact: {e}")

                # Update in-memory progress
                set_sync_progress(self.user_id, self.email, contacts_synced, True)

                # Get sync token from last page
                if "nextSyncToken" in results:
                    new_sync_token = results["nextSyncToken"]

                page_token = results.get("nextPageToken")
                if not page_token:
                    break

            except HttpError as e:
                print(f"[CONTACTS SYNC] HttpError: {e}", flush=True)
                errors.append(f"API error during sync: {e}")
                break
            except Exception as e:
                print(f"[CONTACTS SYNC] Unexpected error: {e}", flush=True)
                errors.append(f"Unexpected error: {e}")
                break

        print(f"[CONTACTS SYNC] Sync complete: {contacts_synced} contacts, {len(errors)} errors", flush=True)
        return contacts_synced, errors, new_sync_token

    async def _sync_with_token(self, service: Resource, sync_token: str) -> tuple[int, list, Optional[str]]:
        """
        Sync only changed contacts since last sync using sync token.

        Args:
            service: People API service
            sync_token: Sync token from previous sync

        Returns:
            Tuple of (contacts_synced, errors_list, new_sync_token)
        """
        contacts_synced = 0
        errors = []
        page_token = None
        new_sync_token = None

        while True:
            try:
                request = service.people().connections().list(
                    resourceName="people/me",
                    pageSize=100,
                    pageToken=page_token,
                    personFields=PERSON_FIELDS,
                    syncToken=sync_token,
                    requestSyncToken=True
                )
                results = self._execute_with_backoff(request)

                if not results:
                    break

                connections = results.get("connections", [])
                for person in connections:
                    try:
                        # Check if contact was deleted
                        if person.get("metadata", {}).get("deleted"):
                            self._delete_contact(person.get("resourceName"))
                        else:
                            self._save_contact(person)
                            contacts_synced += 1
                    except Exception as e:
                        errors.append(f"Error processing contact: {e}")

                # Update in-memory progress
                set_sync_progress(self.user_id, self.email, contacts_synced, True)

                if "nextSyncToken" in results:
                    new_sync_token = results["nextSyncToken"]

                page_token = results.get("nextPageToken")
                if not page_token:
                    break

            except HttpError as e:
                if e.resp.status == 410:
                    # Sync token expired, need full sync
                    print(f"[CONTACTS SYNC] Sync token expired, falling back to full sync", flush=True)
                    return await self._sync_all_contacts(service)
                errors.append(f"API error during sync: {e}")
                break

        return contacts_synced, errors, new_sync_token

    # =========================================================================
    # Query Methods
    # =========================================================================

    async def search_contacts(self, query: str, limit: int = 20) -> list[dict]:
        """
        Search contacts by name, email, or phone using FTS5.

        Args:
            query: Search query (name, email, or phone)
            limit: Maximum results (default 20)

        Returns:
            List of contact dicts
        """
        with get_db() as conn:
            cursor = conn.cursor()

            like_query = f"%{query}%"
            sql = """
                SELECT gc.id, gc.resource_name, gc.display_name, gc.given_name,
                       gc.family_name, gc.emails, gc.phones, gc.company, gc.job_title,
                       gc.photo_url
                FROM google_contacts gc
                WHERE gc.display_name ILIKE %s
                AND gc.user_id = %s
                AND gc.google_email = %s
                ORDER BY gc.display_name
                LIMIT %s
            """
            cursor.execute(sql, [like_query, self.user_id, self.email, limit])
            rows = cursor.fetchall()

            return [self._row_to_dict(row) for row in rows]

    async def list_contacts(self, limit: int = 100) -> list[dict]:
        """
        List all contacts sorted by name.

        Args:
            limit: Maximum results (default 100)

        Returns:
            List of contact dicts
        """
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT id, resource_name, display_name, given_name,
                       family_name, emails, phones, company, job_title, photo_url
                FROM google_contacts
                WHERE user_id = %s
                AND google_email = %s
                ORDER BY display_name
                LIMIT %s
            """, [self.user_id, self.email, limit])

            rows = cursor.fetchall()
            return [self._row_to_dict(row) for row in rows]

    async def get_contact(self, resource_name: str) -> Optional[dict]:
        """
        Get full contact details by resource name.

        Args:
            resource_name: Google resource name (e.g., "people/c123456")

        Returns:
            Full contact dict or None if not found
        """
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT id, resource_name, display_name, given_name, family_name,
                       emails, phones, addresses, company, job_title, notes,
                       birthday, photo_url
                FROM google_contacts
                WHERE user_id = %s AND google_email = %s AND resource_name = %s
            """, [self.user_id, self.email, resource_name])

            row = cursor.fetchone()
            if not row:
                return None

            return {
                "resource_name": row["resource_name"],
                "display_name": row["display_name"],
                "given_name": row["given_name"],
                "family_name": row["family_name"],
                "emails": json.loads(row["emails"]) if row["emails"] else [],
                "phones": json.loads(row["phones"]) if row["phones"] else [],
                "addresses": json.loads(row["addresses"]) if row["addresses"] else [],
                "company": row["company"],
                "job_title": row["job_title"],
                "notes": row["notes"],
                "birthday": row["birthday"],
                "photo_url": row["photo_url"]
            }

    async def get_sync_status(self) -> dict:
        """
        Get current sync status for this account.

        Returns:
            Dict with: last_sync_at, contacts_synced, sync_in_progress
        """
        status = self._get_sync_status()

        # Get total contact count
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) as count FROM google_contacts
                WHERE user_id = %s AND google_email = %s
            """, [self.user_id, self.email])
            row = cursor.fetchone()
            total_contacts = row["count"] if row else 0

        return {
            "last_sync_at": status.get("last_sync_at"),
            "contacts_synced": total_contacts,
            "sync_in_progress": bool(status.get("sync_in_progress")),
            "has_synced": status.get("last_sync_at") is not None
        }

    async def get_stats(self) -> dict:
        """
        Get contact statistics.

        Returns:
            Dict with total count and counts with email/phone
        """
        with get_db() as conn:
            cursor = conn.cursor()

            # Total contacts
            cursor.execute("""
                SELECT COUNT(*) as count FROM google_contacts
                WHERE user_id = %s AND google_email = %s
            """, [self.user_id, self.email])
            total = cursor.fetchone()["count"]

            # Contacts with email
            cursor.execute("""
                SELECT COUNT(*) as count FROM google_contacts
                WHERE user_id = %s AND google_email = %s
                AND emails IS NOT NULL AND emails != '[]'
            """, [self.user_id, self.email])
            with_email = cursor.fetchone()["count"]

            # Contacts with phone
            cursor.execute("""
                SELECT COUNT(*) as count FROM google_contacts
                WHERE user_id = %s AND google_email = %s
                AND phones IS NOT NULL AND phones != '[]'
            """, [self.user_id, self.email])
            with_phone = cursor.fetchone()["count"]

            return {
                "total_contacts": total,
                "with_email": with_email,
                "with_phone": with_phone
            }

    # =========================================================================
    # Database Helper Methods
    # =========================================================================

    def _save_contact(self, person: dict) -> None:
        """Save or update a contact in the database."""
        with get_db() as conn:
            cursor = conn.cursor()

            # Extract name info
            names = person.get("names", [{}])
            primary_name = names[0] if names else {}
            display_name = primary_name.get("displayName", "")
            given_name = primary_name.get("givenName", "")
            family_name = primary_name.get("familyName", "")

            # Extract emails
            emails = []
            for email in person.get("emailAddresses", []):
                emails.append({
                    "value": email.get("value", ""),
                    "type": email.get("type", "")
                })

            # Extract phones
            phones = []
            for phone in person.get("phoneNumbers", []):
                phones.append({
                    "value": phone.get("value", ""),
                    "type": phone.get("type", "")
                })

            # Extract addresses
            addresses = []
            for addr in person.get("addresses", []):
                addresses.append({
                    "formatted": addr.get("formattedValue", ""),
                    "type": addr.get("type", "")
                })

            # Extract organization
            orgs = person.get("organizations", [{}])
            primary_org = orgs[0] if orgs else {}
            company = primary_org.get("name", "")
            job_title = primary_org.get("title", "")

            # Extract notes
            bios = person.get("biographies", [{}])
            notes = bios[0].get("value", "") if bios else ""

            # Extract birthday
            birthdays = person.get("birthdays", [{}])
            birthday = ""
            if birthdays:
                bd = birthdays[0].get("date", {})
                if bd.get("year") and bd.get("month") and bd.get("day"):
                    birthday = f"{bd['year']:04d}-{bd['month']:02d}-{bd['day']:02d}"

            # Extract photo
            photos = person.get("photos", [{}])
            photo_url = photos[0].get("url", "") if photos else ""

            cursor.execute("""
                INSERT INTO google_contacts (
                    user_id, google_email, resource_name, etag,
                    display_name, given_name, family_name,
                    emails, phones, addresses,
                    company, job_title, notes, birthday, photo_url,
                    last_synced_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, google_email, resource_name)
                DO UPDATE SET
                    etag = excluded.etag,
                    display_name = excluded.display_name,
                    given_name = excluded.given_name,
                    family_name = excluded.family_name,
                    emails = excluded.emails,
                    phones = excluded.phones,
                    addresses = excluded.addresses,
                    company = excluded.company,
                    job_title = excluded.job_title,
                    notes = excluded.notes,
                    birthday = excluded.birthday,
                    photo_url = excluded.photo_url,
                    last_synced_at = CURRENT_TIMESTAMP
            """, [
                self.user_id,
                self.email,
                person.get("resourceName"),
                person.get("etag"),
                display_name,
                given_name,
                family_name,
                json.dumps(emails) if emails else None,
                json.dumps(phones) if phones else None,
                json.dumps(addresses) if addresses else None,
                company,
                job_title,
                notes,
                birthday,
                photo_url
            ])

    def _delete_contact(self, resource_name: str) -> None:
        """Delete a contact from the database."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM google_contacts
                WHERE user_id = %s AND google_email = %s AND resource_name = %s
            """, [self.user_id, self.email, resource_name])

    def _clear_contacts(self) -> None:
        """Clear all contacts for this account (for full re-sync)."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM google_contacts
                WHERE user_id = %s AND google_email = %s
            """, [self.user_id, self.email])

    def _get_sync_status(self) -> dict:
        """Get sync status from database."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT last_sync_at, contacts_synced, sync_token, sync_in_progress
                FROM contacts_sync_status
                WHERE user_id = %s AND google_email = %s
            """, [self.user_id, self.email])

            row = cursor.fetchone()
            if row:
                return {
                    "last_sync_at": row["last_sync_at"],
                    "contacts_synced": row["contacts_synced"],
                    "sync_token": row["sync_token"],
                    "sync_in_progress": row["sync_in_progress"]
                }
            return {}

    def _update_sync_status(
        self,
        last_sync_at: datetime = None,
        contacts_synced: int = None,
        sync_token: str = None,
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
            if contacts_synced is not None:
                updates.append("contacts_synced = %s")
                params.append(contacts_synced)
            if sync_token is not None:
                updates.append("sync_token = %s")
                params.append(sync_token)
            if sync_in_progress is not None:
                updates.append("sync_in_progress = %s")
                params.append(1 if sync_in_progress else 0)

            if not updates:
                return

            # Upsert
            cursor.execute(f"""
                INSERT INTO contacts_sync_status (user_id, google_email, {', '.join(u.split(' = ')[0] for u in updates)})
                VALUES (%s, %s, {', '.join(['%s'] * len(params))})
                ON CONFLICT(user_id, google_email)
                DO UPDATE SET {', '.join(updates)}
            """, [self.user_id, self.email] + params + params)

    def _row_to_dict(self, row) -> dict:
        """Convert a database row to a contact dict."""
        # Parse first email and phone for display
        emails = json.loads(row["emails"]) if row["emails"] else []
        phones = json.loads(row["phones"]) if row["phones"] else []

        primary_email = emails[0]["value"] if emails else None
        primary_phone = phones[0]["value"] if phones else None

        return {
            "resource_name": row["resource_name"],
            "display_name": row["display_name"],
            "given_name": row["given_name"],
            "family_name": row["family_name"],
            "email": primary_email,
            "phone": primary_phone,
            "company": row["company"],
            "job_title": row["job_title"],
            "photo_url": row["photo_url"]
        }
