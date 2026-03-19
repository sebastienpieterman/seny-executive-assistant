"""
Microsoft Outlook Calendar service wrapper for Seny.

Provides Outlook calendar access via Microsoft Graph API with automatic token refresh:
- Load credentials from database
- Auto-refresh expired tokens
- Execute Microsoft Graph API calls for calendar operations

Usage:
    calendar = OutlookCalendarService(user_id, email)
    if calendar.is_connected():
        events = await calendar.get_events(days_ahead=7)
"""

import logging
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from web.core.database import (
    get_microsoft_token,
    list_microsoft_tokens
)
from web.services.outlook_service import OutlookService

logger = logging.getLogger(__name__)


# Calendar scope for checking if calendar access is granted
CALENDAR_SCOPE = "Calendars.ReadWrite"


class OutlookCalendarService(OutlookService):
    """
    Microsoft Outlook Calendar service using Graph API.

    Inherits from OutlookService for credential management and API methods.
    Adds calendar-specific operations.

    Attributes:
        user_id: The user's database ID
        email: The Microsoft account email to use
    """

    def is_connected(self) -> bool:
        """
        Check if this user has Microsoft credentials with calendar scope.

        Returns:
            True if Microsoft tokens exist with calendar scope
        """
        token_data = get_microsoft_token(self.user_id, self.email)
        if token_data is None:
            return False
        # Check if calendar scope is included
        scopes = token_data.get("scopes", "")
        return CALENDAR_SCOPE.lower() in scopes.lower() or "calendar" in scopes.lower()

    @staticmethod
    def list_connected_accounts(user_id: int) -> list[dict]:
        """
        List all Microsoft accounts with calendar access for a user.

        Args:
            user_id: User's database ID

        Returns:
            List of connected account info with calendar scope
        """
        all_tokens = list_microsoft_tokens(user_id)
        # Filter to only accounts with calendar scope
        return [
            t for t in all_tokens
            if CALENDAR_SCOPE.lower() in t.get("scopes", "").lower()
            or "calendar" in t.get("scopes", "").lower()
        ]

    async def list_calendars(self) -> list[dict]:
        """
        List all calendars the user has access to.

        Returns:
            List of calendar dicts with: id, name, color, isDefaultCalendar
        """
        result = await self._api_get('/me/calendars')
        if not result:
            return []

        calendars = result.get('value', [])
        return [
            {
                'id': cal.get('id'),
                'name': cal.get('name', 'Untitled'),
                'color': cal.get('color'),
                'isDefaultCalendar': cal.get('isDefaultCalendar', False),
                'canEdit': cal.get('canEdit', True),
                'owner': cal.get('owner', {}).get('address', '')
            }
            for cal in calendars
        ]

    async def get_events(
        self,
        calendar_id: str = None,
        days_ahead: int = 7,
        start_date: str = None,
        end_date: str = None,
        max_results: int = 50,
        timezone: str = "UTC"
    ) -> list[dict]:
        """
        Get events from a calendar within time range.

        Args:
            calendar_id: Calendar ID (default: primary calendar)
            days_ahead: Number of days to look ahead (default: 7, ignored if dates provided)
            start_date: Start date (ISO 8601, optional)
            end_date: End date (ISO 8601, optional)
            max_results: Maximum events to return (default: 50)
            timezone: IANA timezone string for time bounds (default: "UTC")

        Returns:
            List of event dicts with: id, subject, start, end, location, etc.
        """
        # Calculate time bounds
        tz = ZoneInfo(timezone)
        if start_date:
            try:
                time_min = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
            except ValueError:
                time_min = datetime.now(tz)
        else:
            time_min = datetime.now(tz)

        if end_date:
            try:
                time_max = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
            except ValueError:
                time_max = time_min + timedelta(days=days_ahead)
        else:
            time_max = time_min + timedelta(days=days_ahead)

        # Format for Microsoft Graph API
        time_min_str = time_min.strftime('%Y-%m-%dT%H:%M:%S')
        time_max_str = time_max.strftime('%Y-%m-%dT%H:%M:%S')

        # Build endpoint
        if calendar_id:
            endpoint = f'/me/calendars/{calendar_id}/events'
        else:
            endpoint = '/me/calendar/events'

        params = {
            '$top': min(max(1, max_results), 100),
            '$select': 'id,subject,start,end,location,organizer,attendees,isAllDay,isCancelled,bodyPreview,webLink',
            '$orderby': 'start/dateTime',
            '$filter': f"start/dateTime ge '{time_min_str}' and start/dateTime le '{time_max_str}'"
        }

        result = await self._api_get(endpoint, params)
        if not result:
            return []

        events = result.get('value', [])
        return [self._format_event(event) for event in events]

    async def get_event(
        self,
        event_id: str,
        calendar_id: str = None
    ) -> Optional[dict]:
        """
        Get a single event by ID.

        Args:
            event_id: The event ID
            calendar_id: Calendar containing the event (optional)

        Returns:
            Event dict or None if not found
        """
        if calendar_id:
            endpoint = f'/me/calendars/{calendar_id}/events/{event_id}'
        else:
            endpoint = f'/me/events/{event_id}'

        result = await self._api_get(endpoint)
        if not result:
            return None

        return self._format_event(result, include_full_details=True)

    async def create_event(
        self,
        summary: str,
        start_time: str,
        end_time: str,
        description: str = None,
        location: str = None,
        attendees: list[str] = None,
        calendar_id: str = None,
        timezone: str = "UTC",
        is_all_day: bool = False
    ) -> Optional[dict]:
        """
        Create a new calendar event.

        Args:
            summary: Event title (called 'subject' in Microsoft)
            start_time: Start time (ISO 8601 format)
            end_time: End time (ISO 8601 format)
            description: Event description/body (optional)
            location: Event location (optional)
            attendees: List of email addresses to invite (optional)
            calendar_id: Calendar to create in (optional, default calendar if None)
            timezone: Timezone for the event (default: "UTC")
            is_all_day: Whether this is an all-day event

        Returns:
            Created event dict or None if failed
        """
        # Build event body
        if is_all_day:
            # All-day events use date format, not dateTime
            start_date = start_time.split('T')[0] if 'T' in start_time else start_time
            end_date = end_time.split('T')[0] if 'T' in end_time else end_time
            event_body = {
                'subject': summary,
                'start': {
                    'date': start_date,
                    'timeZone': timezone
                },
                'end': {
                    'date': end_date,
                    'timeZone': timezone
                },
                'isAllDay': True
            }
        else:
            event_body = {
                'subject': summary,
                'start': {
                    'dateTime': start_time,
                    'timeZone': timezone
                },
                'end': {
                    'dateTime': end_time,
                    'timeZone': timezone
                }
            }

        if description:
            event_body['body'] = {
                'contentType': 'text',
                'content': description
            }

        if location:
            event_body['location'] = {
                'displayName': location
            }

        if attendees:
            event_body['attendees'] = [
                {
                    'emailAddress': {'address': email},
                    'type': 'required'
                }
                for email in attendees
            ]

        # Build endpoint
        if calendar_id:
            endpoint = f'/me/calendars/{calendar_id}/events'
        else:
            endpoint = '/me/calendar/events'

        result = await self._api_post(endpoint, event_body)
        if not result:
            return None

        logger.info(f"Created Outlook event: {result.get('id')} - {summary}")
        return self._format_event(result)

    async def update_event(
        self,
        event_id: str,
        summary: str = None,
        start_time: str = None,
        end_time: str = None,
        description: str = None,
        location: str = None,
        attendees: list[str] = None,
        calendar_id: str = None,
        timezone: str = "UTC"
    ) -> Optional[dict]:
        """
        Update an existing calendar event.

        Args:
            event_id: The event ID to update
            summary: New event title (optional)
            start_time: New start time (optional)
            end_time: New end time (optional)
            description: New description (optional)
            location: New location (optional)
            attendees: New attendee list (optional, replaces existing)
            calendar_id: Calendar containing the event (optional)
            timezone: Timezone for time values

        Returns:
            Updated event dict or None if failed
        """
        # Build update body with only provided fields
        update_body = {}

        if summary is not None:
            update_body['subject'] = summary

        if start_time is not None:
            update_body['start'] = {
                'dateTime': start_time,
                'timeZone': timezone
            }

        if end_time is not None:
            update_body['end'] = {
                'dateTime': end_time,
                'timeZone': timezone
            }

        if description is not None:
            update_body['body'] = {
                'contentType': 'text',
                'content': description
            }

        if location is not None:
            update_body['location'] = {
                'displayName': location
            }

        if attendees is not None:
            update_body['attendees'] = [
                {
                    'emailAddress': {'address': email},
                    'type': 'required'
                }
                for email in attendees
            ]

        if not update_body:
            # Nothing to update
            return await self.get_event(event_id, calendar_id)

        # Build endpoint
        if calendar_id:
            endpoint = f'/me/calendars/{calendar_id}/events/{event_id}'
        else:
            endpoint = f'/me/events/{event_id}'

        result = await self._api_patch(endpoint, update_body)
        if not result:
            return None

        logger.info(f"Updated Outlook event: {event_id}")
        return self._format_event(result)

    async def delete_event(
        self,
        event_id: str,
        calendar_id: str = None
    ) -> bool:
        """
        Delete a calendar event.

        Args:
            event_id: The event ID to delete
            calendar_id: Calendar containing the event (optional)

        Returns:
            True if deleted successfully
        """
        if calendar_id:
            endpoint = f'/me/calendars/{calendar_id}/events/{event_id}'
        else:
            endpoint = f'/me/events/{event_id}'

        success = await self._api_delete(endpoint)
        if success:
            logger.info(f"Deleted Outlook event: {event_id}")
        return success

    def _format_event(self, event: dict, include_full_details: bool = False) -> dict:
        """
        Format Microsoft Graph event to consistent structure.

        Args:
            event: Raw event from Graph API
            include_full_details: Include body and extended info

        Returns:
            Formatted event dict
        """
        # Parse start/end times
        start = event.get('start', {})
        end = event.get('end', {})

        # Handle both dateTime and date formats
        start_time = start.get('dateTime') or start.get('date', '')
        end_time = end.get('dateTime') or end.get('date', '')
        start_tz = start.get('timeZone', 'UTC')
        end_tz = end.get('timeZone', 'UTC')

        formatted = {
            'id': event.get('id'),
            'subject': event.get('subject', '(No title)'),
            'start': start_time,
            'end': end_time,
            'startTimezone': start_tz,
            'endTimezone': end_tz,
            'isAllDay': event.get('isAllDay', False),
            'isCancelled': event.get('isCancelled', False),
            'location': event.get('location', {}).get('displayName', ''),
            'organizer': event.get('organizer', {}).get('emailAddress', {}).get('address', ''),
            'webLink': event.get('webLink', '')
        }

        # Add attendees
        attendees = event.get('attendees', [])
        if attendees:
            formatted['attendees'] = [
                {
                    'email': att.get('emailAddress', {}).get('address', ''),
                    'name': att.get('emailAddress', {}).get('name', ''),
                    'status': att.get('status', {}).get('response', 'none')
                }
                for att in attendees
            ]
        else:
            formatted['attendees'] = []

        if include_full_details:
            # Add body content
            body = event.get('body', {})
            if body.get('contentType') == 'text':
                formatted['description'] = body.get('content', '')
            else:
                # HTML - convert to text
                formatted['description'] = self._strip_html(body.get('content', ''))
                formatted['descriptionHtml'] = body.get('content', '')

            # Add recurrence info if present
            recurrence = event.get('recurrence')
            if recurrence:
                formatted['recurrence'] = {
                    'pattern': recurrence.get('pattern', {}),
                    'range': recurrence.get('range', {})
                }

            # Add sensitivity
            formatted['sensitivity'] = event.get('sensitivity', 'normal')

            # Add response status
            formatted['responseStatus'] = event.get('responseStatus', {})
        else:
            # Just include preview
            formatted['bodyPreview'] = event.get('bodyPreview', '')[:200]

        return formatted

    async def get_events_for_date(
        self,
        date: str,
        timezone: str = "UTC"
    ) -> list[dict]:
        """
        Get all events for a specific date.

        Args:
            date: Date in YYYY-MM-DD format
            timezone: Timezone for the date

        Returns:
            List of events on that date
        """
        try:
            dt = datetime.strptime(date, '%Y-%m-%d')
            tz = ZoneInfo(timezone)
            start = dt.replace(hour=0, minute=0, second=0, tzinfo=tz)
            end = dt.replace(hour=23, minute=59, second=59, tzinfo=tz)

            return await self.get_events(
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                timezone=timezone
            )
        except ValueError as e:
            logger.error(f"Invalid date format: {date} - {e}")
            return []

    async def find_free_time(
        self,
        date: str,
        duration_minutes: int = 60,
        start_hour: int = 9,
        end_hour: int = 17,
        timezone: str = "UTC"
    ) -> list[dict]:
        """
        Find available time slots on a given date.

        Args:
            date: Date in YYYY-MM-DD format
            duration_minutes: Length of desired free slot
            start_hour: Earliest hour to consider (24h format)
            end_hour: Latest hour to consider (24h format)
            timezone: Timezone

        Returns:
            List of free time slots with start and end times
        """
        events = await self.get_events_for_date(date, timezone)
        if events is None:
            return []

        try:
            dt = datetime.strptime(date, '%Y-%m-%d')
            tz = ZoneInfo(timezone)

            # Create list of busy periods
            busy = []
            for event in events:
                if event.get('isCancelled'):
                    continue
                try:
                    start = datetime.fromisoformat(event['start'].replace('Z', '+00:00'))
                    end = datetime.fromisoformat(event['end'].replace('Z', '+00:00'))
                    busy.append((start, end))
                except (ValueError, KeyError):
                    continue

            # Sort by start time
            busy.sort(key=lambda x: x[0])

            # Find free slots
            free_slots = []
            current = dt.replace(hour=start_hour, minute=0, second=0, tzinfo=tz)
            day_end = dt.replace(hour=end_hour, minute=0, second=0, tzinfo=tz)
            duration = timedelta(minutes=duration_minutes)

            for busy_start, busy_end in busy:
                if current + duration <= busy_start:
                    # There's a free slot before this event
                    slot_end = min(busy_start, day_end)
                    if slot_end - current >= duration:
                        free_slots.append({
                            'start': current.isoformat(),
                            'end': slot_end.isoformat(),
                            'duration_minutes': int((slot_end - current).total_seconds() / 60)
                        })
                current = max(current, busy_end)

            # Check for free time after last event
            if current + duration <= day_end:
                free_slots.append({
                    'start': current.isoformat(),
                    'end': day_end.isoformat(),
                    'duration_minutes': int((day_end - current).total_seconds() / 60)
                })

            return free_slots

        except Exception as e:
            logger.error(f"Error finding free time: {e}")
            return []
