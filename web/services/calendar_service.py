"""
Google Calendar API service wrapper for Seny.

Provides Calendar access with automatic token refresh:
- Load credentials from database
- Auto-refresh expired tokens
- Build Calendar API service object

Usage:
    calendar = CalendarService(user_id, email)
    if calendar.is_connected():
        events = await calendar.get_events(days_ahead=7)
"""

import os
import logging
import time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build, Resource
from googleapiclient.errors import HttpError

from web.core.database import (
    get_google_token, save_google_token, list_google_tokens,
    get_calendar_preferences, save_calendar_preference, update_calendar_visibility,
    get_visible_calendar_ids
)

logger = logging.getLogger(__name__)


# OAuth Configuration
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# Calendar scope for checking if calendar access is granted
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"


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
            schedule_token_alert(user_id, "calendar", email)
    else:
        logger.error(
            "Token refresh failed for %s (user %d): %s — circuit failure %d/%d",
            email, user_id, repr(error), failure_count, _TOKEN_CIRCUIT_THRESHOLD
        )


def _reset_token_circuit(user_id: int, email: str) -> None:
    """Reset circuit after a successful token refresh."""
    _token_circuit.pop(f"{user_id}:{email}", None)


class CalendarService:
    """
    Google Calendar API wrapper with automatic credential management.

    Handles OAuth token refresh and Calendar API service creation.
    One instance per user/email combination - do not share across requests.

    Attributes:
        user_id: The user's database ID
        email: The Google account email to use
    """

    def __init__(self, user_id: int, email: str):
        """
        Initialize Calendar service for a specific user and email account.

        Args:
            user_id: User's database ID
            email: Google account email to use for API calls
        """
        self.user_id = user_id
        self.email = email
        self._service: Optional[Resource] = None
        self._credentials: Optional[Credentials] = None

    def is_connected(self) -> bool:
        """
        Check if this email has Calendar credentials stored with calendar scope.

        Note: This only checks if tokens exist with calendar scope, not if they're valid.
        Use get_credentials() to verify tokens are valid/refreshable.

        Returns:
            True if this email has Google tokens with calendar scope
        """
        token_data = get_google_token(self.user_id, self.email)
        if token_data is None:
            return False
        # Check if calendar scope is included
        scopes = token_data.get("scopes", "")
        return CALENDAR_SCOPE in scopes or "calendar" in scopes.lower()

    @staticmethod
    def list_connected_accounts(user_id: int) -> list[dict]:
        """
        List all Google accounts with calendar access for a user.

        Args:
            user_id: User's database ID

        Returns:
            List of connected account info (email, created_at) with calendar scope
        """
        all_tokens = list_google_tokens(user_id)
        # Filter to only accounts with calendar scope
        return [
            t for t in all_tokens
            if CALENDAR_SCOPE in t.get("scopes", "") or "calendar" in t.get("scopes", "").lower()
        ]

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
            # Already loaded this request
            if not self._credentials.expired:
                return self._credentials
            # Fall through to refresh

        token_data = get_google_token(self.user_id, self.email)
        if not token_data:
            return None

        # Parse expiry string to datetime if present
        expiry = None
        if token_data["expiry"]:
            try:
                expiry = datetime.fromisoformat(token_data["expiry"])
            except ValueError:
                pass  # Invalid expiry format, will trigger refresh

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
                # Save refreshed tokens back to database
                save_google_token(self.user_id, self.email, self._credentials)
                # Successful refresh — reset any previous failure count
                _reset_token_circuit(self.user_id, self.email)
            except Exception as e:
                # Refresh failed - token may be revoked
                _record_token_failure(self.user_id, self.email, e)
                self._credentials = None
                return None

        return self._credentials

    async def get_service(self) -> Optional[Resource]:
        """
        Get or create Calendar API service.

        Returns:
            Calendar API service object, or None if not connected/authorized

        Usage:
            service = await calendar.get_service()
            if service:
                events = service.events().list(calendarId='primary').execute()
        """
        if self._service is not None:
            return self._service

        credentials = await self.get_credentials()
        if credentials is None:
            return None

        # Build Calendar API service
        self._service = build("calendar", "v3", credentials=credentials)
        return self._service

    def _execute_with_backoff(self, request, max_retries: int = 3):
        """
        Execute Calendar API request with exponential backoff for rate limits.

        Args:
            request: Calendar API request object
            max_retries: Maximum number of retry attempts

        Returns:
            API response or None if all retries failed
        """
        for attempt in range(max_retries):
            try:
                return request.execute()
            except HttpError as e:
                if e.resp.status in (429, 500, 503):
                    # Rate limit or server error - retry with backoff
                    wait_time = (2 ** attempt) + (time.time() % 1)
                    logger.warning(f"Calendar API error {e.resp.status}, retrying in {wait_time:.1f}s")
                    time.sleep(wait_time)
                else:
                    # Other HTTP error - don't retry
                    logger.error(f"Calendar API error: {e}")
                    raise
        return None

    async def list_calendars(self) -> list[dict]:
        """
        List all calendars the user has access to.

        Returns:
            List of calendar dicts with: id, summary, primary, accessRole
        """
        service = await self.get_service()
        if not service:
            return []

        try:
            results = self._execute_with_backoff(
                service.calendarList().list()
            )

            if not results:
                return []

            calendars = results.get("items", [])
            return [
                {
                    "id": cal.get("id"),
                    "summary": cal.get("summary", "Untitled"),
                    "primary": cal.get("primary", False),
                    "accessRole": cal.get("accessRole"),
                    "backgroundColor": cal.get("backgroundColor")
                }
                for cal in calendars
            ]

        except HttpError as e:
            logger.error(f"Failed to list calendars: {e}")
            return []

    async def get_events(
        self,
        calendar_id: str = "primary",
        days_ahead: int = 7,
        max_results: int = 50,
        timezone: str = "UTC",
        days_back: int = 0
    ) -> list[dict]:
        """
        Get events from a calendar within time range.

        Args:
            calendar_id: Calendar to fetch from (default: "primary")
            days_ahead: Number of days to look ahead from start (default: 7)
            max_results: Maximum events to return (default: 50)
            timezone: IANA timezone string for time bounds (default: "UTC")
            days_back: Number of days to look back from now (default: 0 = start from now)

        Returns:
            List of event dicts with: id, summary, start, end, location, etc.
        """
        service = await self.get_service()
        if not service:
            return []

        try:
            # Calculate time bounds in user's timezone
            tz = ZoneInfo(timezone)
            now = datetime.now(tz)
            start = now - timedelta(days=days_back)
            time_min = start.isoformat()
            time_max = (start + timedelta(days=days_ahead)).isoformat()

            results = self._execute_with_backoff(
                service.events().list(
                    calendarId=calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    maxResults=max_results,
                    singleEvents=True,  # Expand recurring events
                    orderBy="startTime",
                    timeZone=timezone
                )
            )

            if not results:
                return []

            events = results.get("items", [])
            return [self._format_event(event) for event in events]

        except HttpError as e:
            if e.resp.status == 404:
                # Re-raise 404 so callers can detect and retire dead calendar IDs.
                raise
            logger.error(f"Failed to get events: {e}")
            return []

    async def get_event(
        self,
        event_id: str,
        calendar_id: str = "primary"
    ) -> Optional[dict]:
        """
        Get a single event by ID.

        Args:
            event_id: The event ID
            calendar_id: Calendar containing the event (default: "primary")

        Returns:
            Event dict or None if not found
        """
        service = await self.get_service()
        if not service:
            return None

        try:
            event = self._execute_with_backoff(
                service.events().get(
                    calendarId=calendar_id,
                    eventId=event_id
                )
            )

            if not event:
                return None

            return self._format_event(event, include_full_details=True)

        except HttpError as e:
            logger.error(f"Failed to get event {event_id}: {e}")
            return None

    async def create_event(
        self,
        summary: str,
        start_time: str,
        end_time: str,
        description: str = None,
        location: str = None,
        attendees: list[str] = None,
        calendar_id: str = "primary",
        timezone: str = "UTC"
    ) -> Optional[dict]:
        """
        Create a new calendar event.

        Args:
            summary: Event title
            start_time: Start time (ISO 8601 format)
            end_time: End time (ISO 8601 format)
            description: Event description (optional)
            location: Event location (optional)
            attendees: List of email addresses to invite (optional)
            calendar_id: Calendar to create in (default: "primary")
            timezone: Timezone for the event (default: "UTC")

        Returns:
            Created event dict or None if failed
        """
        service = await self.get_service()
        if not service:
            return None

        try:
            event_body = {
                "summary": summary,
                "start": {
                    "dateTime": start_time,
                    "timeZone": timezone
                },
                "end": {
                    "dateTime": end_time,
                    "timeZone": timezone
                }
            }

            if description:
                event_body["description"] = description
            if location:
                event_body["location"] = location
            if attendees:
                event_body["attendees"] = [{"email": email} for email in attendees]

            result = self._execute_with_backoff(
                service.events().insert(
                    calendarId=calendar_id,
                    body=event_body,
                    sendUpdates="all" if attendees else "none"
                )
            )

            if result:
                logger.info(f"Created event: {result.get('id')} - {summary}")
                return self._format_event(result)

            return None

        except HttpError as e:
            logger.error(f"Failed to create event: {e}")
            # Return error info instead of None for better debugging
            return {"error": True, "message": str(e), "status": e.resp.status if hasattr(e, 'resp') else None}
        except Exception as e:
            logger.error(f"Unexpected error creating event: {e}")
            return {"error": True, "message": str(e)}

    async def update_event(
        self,
        event_id: str,
        calendar_id: str = "primary",
        timezone: str = "UTC",
        **updates
    ) -> Optional[dict]:
        """
        Update an existing event.

        Args:
            event_id: The event ID to update
            calendar_id: Calendar containing the event (default: "primary")
            timezone: Timezone for datetime fields (default: "UTC")
            **updates: Fields to update (summary, start_time, end_time, description, location, attendees)

        Returns:
            Updated event dict or None if failed
        """
        service = await self.get_service()
        if not service:
            return None

        try:
            # First get the existing event
            existing = self._execute_with_backoff(
                service.events().get(
                    calendarId=calendar_id,
                    eventId=event_id
                )
            )

            if not existing:
                return None

            # Apply updates
            if "summary" in updates:
                existing["summary"] = updates["summary"]
            if "description" in updates:
                existing["description"] = updates["description"]
            if "location" in updates:
                existing["location"] = updates["location"]
            if "start_time" in updates:
                existing["start"] = {
                    "dateTime": updates["start_time"],
                    "timeZone": timezone
                }
            if "end_time" in updates:
                existing["end"] = {
                    "dateTime": updates["end_time"],
                    "timeZone": timezone
                }
            if "attendees" in updates:
                existing["attendees"] = [{"email": email} for email in updates["attendees"]]

            result = self._execute_with_backoff(
                service.events().update(
                    calendarId=calendar_id,
                    eventId=event_id,
                    body=existing,
                    sendUpdates="all" if "attendees" in updates else "none"
                )
            )

            if result:
                logger.info(f"Updated event: {event_id}")
                return self._format_event(result)

            return None

        except HttpError as e:
            logger.error(f"Failed to update event {event_id}: {e}")
            return {"error": True, "message": str(e), "status": e.resp.status if hasattr(e, 'resp') else None}
        except Exception as e:
            logger.error(f"Unexpected error updating event {event_id}: {e}")
            return {"error": True, "message": str(e)}

    async def delete_event(
        self,
        event_id: str,
        calendar_id: str = "primary"
    ) -> dict:
        """
        Delete an event.

        Args:
            event_id: The event ID to delete
            calendar_id: Calendar containing the event (default: "primary")

        Returns:
            Dict with success status and error info if failed
        """
        service = await self.get_service()
        if not service:
            return {"success": False, "error": "Could not connect to calendar service"}

        # Log exact parameters for debugging
        logger.info(f"DELETE attempt - event_id: '{event_id}' (len={len(event_id)}), calendar_id: '{calendar_id}'")

        # First verify the event exists
        try:
            existing = self._execute_with_backoff(
                service.events().get(
                    calendarId=calendar_id,
                    eventId=event_id
                )
            )
            if existing:
                logger.info(f"Found event to delete: {existing.get('summary', 'Unknown')} (id={existing.get('id')})")
            else:
                logger.warning(f"Event lookup returned None for id: {event_id}")
                return {"success": False, "error": f"Event not found with ID: {event_id}", "status": 404}
        except HttpError as e:
            logger.error(f"Could not find event before delete: {e}")
            return {
                "success": False,
                "error": f"Event not found or not accessible: {str(e)}",
                "status": e.resp.status if hasattr(e, 'resp') else None,
                "event_id_used": event_id,
                "calendar_id_used": calendar_id
            }

        # Now try to delete
        try:
            self._execute_with_backoff(
                service.events().delete(
                    calendarId=calendar_id,
                    eventId=event_id,
                    sendUpdates="all"
                )
            )
            logger.info(f"Deleted event: {event_id}")
            return {"success": True, "event_id_used": event_id, "calendar_id_used": calendar_id}

        except HttpError as e:
            logger.error(f"Failed to delete event {event_id}: {e}")
            return {
                "success": False,
                "error": str(e),
                "status": e.resp.status if hasattr(e, 'resp') else None,
                "event_id_used": event_id,
                "calendar_id_used": calendar_id
            }
        except Exception as e:
            logger.error(f"Unexpected error deleting event {event_id}: {e}")
            return {"success": False, "error": str(e), "event_id_used": event_id, "calendar_id_used": calendar_id}

    def _format_event(self, event: dict, include_full_details: bool = False) -> dict:
        """
        Format a Calendar API event into a consistent structure.

        Args:
            event: Raw event from Calendar API
            include_full_details: Include attendees, description, etc.

        Returns:
            Formatted event dict
        """
        # Handle all-day events (date) vs timed events (dateTime)
        start = event.get("start", {})
        end = event.get("end", {})

        is_all_day = "date" in start

        formatted = {
            "id": event.get("id"),
            "summary": event.get("summary", "(No title)"),
            "start": start.get("dateTime") or start.get("date"),
            "end": end.get("dateTime") or end.get("date"),
            "is_all_day": is_all_day,
            "location": event.get("location"),
            "status": event.get("status"),
            "html_link": event.get("htmlLink")
        }

        # Extract video meeting link
        video_link = self._extract_video_link(event)
        if video_link:
            formatted["video_link"] = video_link
            formatted["has_video"] = True
        else:
            formatted["has_video"] = False

        # Always include attendees (needed for digest enrichment)
        formatted["attendees"] = event.get("attendees", [])

        if include_full_details:
            formatted["description"] = event.get("description")
            formatted["organizer"] = event.get("organizer")
            formatted["recurrence"] = event.get("recurrence")
            formatted["recurring_event_id"] = event.get("recurringEventId")
            formatted["created"] = event.get("created")
            formatted["updated"] = event.get("updated")

        return formatted

    def _extract_video_link(self, event: dict) -> Optional[str]:
        """
        Extract video meeting link from event.

        Checks conferenceData, location, and description for meeting links.

        Args:
            event: Raw event from Calendar API

        Returns:
            Video meeting URL or None
        """
        # Check conferenceData first (Google Meet, Zoom integration)
        conference_data = event.get("conferenceData", {})
        entry_points = conference_data.get("entryPoints", [])
        for entry in entry_points:
            if entry.get("entryPointType") == "video":
                return entry.get("uri")

        # Check location for meeting URLs
        location = event.get("location", "")
        if location:
            if "meet.google.com" in location or "zoom.us" in location or "teams.microsoft.com" in location:
                return location

        # Check description for meeting URLs (simplified check)
        description = event.get("description", "")
        if description:
            for pattern in ["meet.google.com/", "zoom.us/j/", "teams.microsoft.com/l/"]:
                if pattern in description:
                    # Extract URL - this is a simplified extraction
                    start_idx = description.find("https://")
                    if start_idx != -1:
                        end_idx = description.find(" ", start_idx)
                        if end_idx == -1:
                            end_idx = description.find("\n", start_idx)
                        if end_idx == -1:
                            end_idx = len(description)
                        return description[start_idx:end_idx]

        return None

    # =========================================================================
    # Multi-Calendar Support (07-08)
    # =========================================================================

    async def sync_calendar_list(self) -> list[dict]:
        """
        Sync available calendars from Google to local preferences.

        - Adds new calendars (default visible)
        - Updates names/colors if changed
        - Returns list of all calendars with merged preferences

        Returns:
            List of calendar dicts with preferences merged
        """
        service = await self.get_service()
        if not service:
            return []

        try:
            # Get all calendars from Google
            results = self._execute_with_backoff(
                service.calendarList().list()
            )

            if not results:
                return []

            calendars = results.get("items", [])
            synced_calendars = []

            for cal in calendars:
                calendar_id = cal.get("id")
                calendar_name = cal.get("summary", "Untitled")
                is_primary = cal.get("primary", False)
                access_role = cal.get("accessRole")
                background_color = cal.get("backgroundColor")

                # Save or update preference in database
                save_calendar_preference(
                    user_id=self.user_id,
                    google_email=self.email,
                    calendar_id=calendar_id,
                    calendar_name=calendar_name,
                    is_visible=True,  # New calendars default to visible
                    is_primary=is_primary,
                    access_role=access_role,
                    background_color=background_color
                )

                synced_calendars.append({
                    "id": calendar_id,
                    "name": calendar_name,
                    "is_primary": is_primary,
                    "access_role": access_role,
                    "background_color": background_color
                })

            logger.info(f"Synced {len(synced_calendars)} calendars for {self.email}")
            return synced_calendars

        except HttpError as e:
            logger.error(f"Failed to sync calendar list: {e}")
            return []

    async def get_visible_calendars(self) -> list[dict]:
        """
        Get calendars that are visible (enabled by user).

        Returns list of calendar dicts with preferences merged.
        If no preferences exist, syncs from Google first.

        Returns:
            List of visible calendar dicts
        """
        # Get preferences from database
        prefs = get_calendar_preferences(self.user_id, self.email)

        # If no preferences stored, sync from Google first
        if not prefs:
            await self.sync_calendar_list()
            prefs = get_calendar_preferences(self.user_id, self.email)

        # Filter to visible calendars
        visible_calendars = [
            {
                "id": p["calendar_id"],
                "name": p["calendar_name"],
                "is_primary": p["is_primary"],
                "is_visible": p["is_visible"],
                "access_role": p["access_role"],
                "color": p["color_override"] or p["background_color"]
            }
            for p in prefs if p["is_visible"]
        ]

        return visible_calendars

    async def get_all_calendars(self) -> list[dict]:
        """
        Get all calendars with their visibility status.

        Returns:
            List of all calendar dicts with preferences
        """
        # Get preferences from database
        prefs = get_calendar_preferences(self.user_id, self.email)

        # If no preferences stored, sync from Google first
        if not prefs:
            await self.sync_calendar_list()
            prefs = get_calendar_preferences(self.user_id, self.email)

        return [
            {
                "id": p["calendar_id"],
                "name": p["calendar_name"],
                "is_primary": p["is_primary"],
                "is_visible": p["is_visible"],
                "access_role": p["access_role"],
                "color": p["color_override"] or p["background_color"]
            }
            for p in prefs
        ]

    def set_calendar_visibility(self, calendar_id: str, visible: bool) -> bool:
        """
        Toggle calendar visibility.

        Args:
            calendar_id: Google calendar ID
            visible: Whether calendar should be visible

        Returns:
            True if updated, False if calendar not found
        """
        return update_calendar_visibility(
            user_id=self.user_id,
            google_email=self.email,
            calendar_id=calendar_id,
            is_visible=visible
        )

    async def get_all_events(
        self,
        days_ahead: int = 7,
        max_results_per_calendar: int = 50,
        timezone: str = "UTC",
        calendar_ids: list[str] = None,
        days_back: int = 0
    ) -> list[dict]:
        """
        Get events from all visible calendars, merged and sorted.

        Args:
            days_ahead: Number of days to look ahead from start
            max_results_per_calendar: Maximum events per calendar
            timezone: IANA timezone string
            calendar_ids: Specific calendars to query (None = all visible)
            days_back: Number of days to look back from now (default: 0)

        Returns:
            List of events with calendar metadata, sorted by start time
        """
        # Determine which calendars to query
        if calendar_ids is None:
            visible_calendars = await self.get_visible_calendars()
            logger.info(f"[GET_ALL_EVENTS DEBUG] Account {self.email}: visible_calendars = {[c.get('name') for c in visible_calendars]}")
            calendar_ids = [c["id"] for c in visible_calendars]
            # Build a lookup for calendar metadata
            calendar_lookup = {c["id"]: c for c in visible_calendars}
        else:
            # Get metadata for specified calendars
            all_calendars = await self.get_all_calendars()
            calendar_lookup = {c["id"]: c for c in all_calendars if c["id"] in calendar_ids}

        logger.info(f"[GET_ALL_EVENTS DEBUG] Account {self.email}: querying {len(calendar_ids)} calendars")

        if not calendar_ids:
            logger.info(f"[GET_ALL_EVENTS DEBUG] Account {self.email}: NO visible calendars, returning empty")
            return []

        # Query each calendar and collect events
        import asyncio
        all_events = []

        # Query calendars in parallel for better performance
        async def query_calendar(cal_id: str) -> list[dict]:
            try:
                events = await self.get_events(
                    calendar_id=cal_id,
                    days_ahead=days_ahead,
                    max_results=max_results_per_calendar,
                    timezone=timezone,
                    days_back=days_back
                )

                # Tag each event with calendar metadata
                cal_info = calendar_lookup.get(cal_id, {})
                for event in events:
                    event["calendar_id"] = cal_id
                    event["calendar_name"] = cal_info.get("name", "Unknown")
                    event["calendar_color"] = cal_info.get("color")

                return events
            except HttpError as e:
                if e.resp.status == 404:
                    logger.warning(
                        f"Calendar {cal_id} returned 404 — marking as inactive, "
                        f"will skip in future scans"
                    )
                    update_calendar_visibility(
                        self.user_id, self.email, cal_id, is_visible=False
                    )
                else:
                    logger.warning(f"Failed to get events from calendar {cal_id}: {e}")
                return []
            except Exception as e:
                logger.warning(f"Failed to get events from calendar {cal_id}: {e}")
                return []

        # Execute queries in parallel
        results = await asyncio.gather(*[query_calendar(cal_id) for cal_id in calendar_ids])

        # Flatten results
        for events in results:
            all_events.extend(events)

        # Sort by start time
        def get_start_time(event):
            start = event.get("start", "")
            if isinstance(start, dict):
                return start.get("dateTime", start.get("date", ""))
            return start

        all_events.sort(key=get_start_time)

        return all_events
