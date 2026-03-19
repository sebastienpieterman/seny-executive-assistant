"""
Calendar endpoints for Seny.

Google Calendar integration for viewing and managing events:
- GET /api/calendar/calendars - List user's calendars
- GET /api/calendar/events - Get upcoming events
- GET /api/calendar/event/{id} - Get single event details
- GET /api/calendar/agenda - Get events grouped by day for sidebar

Note: OAuth is handled through /api/email/* endpoints (combined Gmail+Calendar OAuth)
"""

import logging
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from fastapi import APIRouter, HTTPException, status, Depends, Query
from pydantic import BaseModel

from web.auth.jwt_utils import require_auth
from web.core.cache import response_cache

logger = logging.getLogger(__name__)
from web.core.database import list_google_tokens
from web.services.calendar_service import CalendarService, CALENDAR_SCOPE


# Create calendar router
router = APIRouter()


# Response models
class CalendarInfo(BaseModel):
    """Calendar metadata."""
    id: str
    summary: str
    primary: bool
    accessRole: Optional[str] = None
    backgroundColor: Optional[str] = None


class CalendarsResponse(BaseModel):
    """Response for list calendars endpoint."""
    calendars: list[CalendarInfo]
    account: str


class EventSummary(BaseModel):
    """Event summary for list view."""
    id: str
    summary: str
    start: str
    end: str
    is_all_day: bool
    location: Optional[str] = None
    has_video: bool = False
    video_link: Optional[str] = None
    html_link: Optional[str] = None


class EventsResponse(BaseModel):
    """Response for list events endpoint."""
    events: list[EventSummary]
    account: str
    timezone: str


class Attendee(BaseModel):
    """Event attendee."""
    email: str
    displayName: Optional[str] = None
    responseStatus: Optional[str] = None
    self_: Optional[bool] = None

    class Config:
        populate_by_name = True


class Organizer(BaseModel):
    """Event organizer."""
    email: str
    displayName: Optional[str] = None
    self_: Optional[bool] = None


class EventDetail(BaseModel):
    """Full event details for preview."""
    id: str
    summary: str
    start: str
    end: str
    is_all_day: bool
    location: Optional[str] = None
    description: Optional[str] = None
    has_video: bool = False
    video_link: Optional[str] = None
    html_link: Optional[str] = None
    organizer: Optional[dict] = None
    attendees: list[dict] = []
    recurrence: Optional[list[str]] = None
    status: Optional[str] = None


class CalendarStatusResponse(BaseModel):
    """Response for calendar connection status."""
    connected: bool
    email: Optional[str] = None


class CalendarAccount(BaseModel):
    """Connected calendar account info."""
    email: str
    created_at: Optional[str] = None


class CalendarAccountsResponse(BaseModel):
    """Response for list calendar accounts endpoint."""
    accounts: list[CalendarAccount]


# Agenda endpoint models
class AgendaEvent(BaseModel):
    """Event in agenda view."""
    id: str
    summary: str
    start: str
    end: str
    start_time: str  # Formatted time like "9:00 AM"
    end_time: str
    is_all_day: bool
    location: Optional[str] = None
    has_video: bool = False
    video_link: Optional[str] = None
    account: Optional[str] = None  # Email account this event belongs to (for unified view)
    calendar_id: Optional[str] = None  # Calendar ID this event belongs to
    calendar_name: Optional[str] = None  # Display name of the calendar
    calendar_color: Optional[str] = None  # Background color of the calendar


class AgendaDay(BaseModel):
    """A day's events in the agenda."""
    date: str  # ISO date like "2024-01-15"
    label: str  # "Today", "Tomorrow", "Monday", etc.
    events: list[AgendaEvent]


class AgendaResponse(BaseModel):
    """Response for agenda endpoint."""
    timezone: str
    days: list[AgendaDay]
    accounts: list[str] = []  # All accounts included


def _get_relative_day_label(date: datetime, today: datetime) -> str:
    """Get relative label for a date (Today, Tomorrow, day name, etc.)."""
    delta = (date.date() - today.date()).days

    if delta == 0:
        return "Today"
    elif delta == 1:
        return "Tomorrow"
    elif delta < 7:
        return date.strftime("%A")  # Monday, Tuesday, etc.
    else:
        return date.strftime("%A, %b %d")  # Monday, Jan 20


def _format_time(dt_str: str, timezone: str, is_all_day: bool) -> str:
    """Format datetime string to display time."""
    if is_all_day:
        return "All day"
    try:
        # Parse ISO datetime
        if "T" in dt_str:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            tz = ZoneInfo(timezone)
            local_dt = dt.astimezone(tz)
            return local_dt.strftime("%-I:%M %p").lstrip("0")  # "9:00 AM"
        return dt_str
    except Exception:
        return dt_str


def _get_calendar_account(user_id: int, email: Optional[str]) -> str:
    """Get the email account to use - specified or first with calendar scope."""
    all_tokens = list_google_tokens(user_id)

    if email:
        # Verify specified email has calendar scope
        for token in all_tokens:
            if token["email"] == email:
                if CALENDAR_SCOPE in token.get("scopes", ""):
                    return email
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Account {email} does not have calendar access. Please reconnect to grant calendar permissions."
                )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Account {email} is not connected"
        )

    # Find first account with calendar scope
    for token in all_tokens:
        if CALENDAR_SCOPE in token.get("scopes", ""):
            return token["email"]

    # No accounts with calendar scope
    if all_tokens:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No accounts have calendar access. Please reconnect your Google account to grant calendar permissions."
        )

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="No Google accounts connected"
    )


@router.get("/status", response_model=CalendarStatusResponse)
async def get_calendar_status(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account email to check")
):
    """
    Check if calendar is connected for the user.

    Protected endpoint - requires valid JWT token.

    Args:
        email: Optional specific account to check

    Returns:
        Connection status and email if connected
    """
    all_tokens = list_google_tokens(int(user_id))

    if email:
        # Check specific account
        for token in all_tokens:
            if token["email"] == email and CALENDAR_SCOPE in token.get("scopes", ""):
                return CalendarStatusResponse(connected=True, email=email)
        return CalendarStatusResponse(connected=False)

    # Check if any account has calendar scope
    for token in all_tokens:
        if CALENDAR_SCOPE in token.get("scopes", ""):
            return CalendarStatusResponse(connected=True, email=token["email"])

    return CalendarStatusResponse(connected=False)


@router.get("/accounts", response_model=CalendarAccountsResponse)
async def list_calendar_accounts(user_id: str = Depends(require_auth)):
    """
    List all Google accounts with calendar access.

    Protected endpoint - requires valid JWT token.

    Returns:
        List of connected accounts with calendar scope
    """
    accounts = CalendarService.list_connected_accounts(int(user_id))
    return CalendarAccountsResponse(
        accounts=[CalendarAccount(email=a["email"], created_at=a.get("created_at")) for a in accounts]
    )


@router.get("/debug/scopes")
async def debug_scopes(user_id: str = Depends(require_auth)):
    """
    Debug endpoint to see what scopes are stored for each account.
    """
    from web.core.database import list_google_tokens
    all_tokens = list_google_tokens(int(user_id))
    return {
        "expected_calendar_scope": CALENDAR_SCOPE,
        "accounts": [
            {
                "email": t["email"],
                "scopes": t.get("scopes", ""),
                "has_calendar": CALENDAR_SCOPE in t.get("scopes", "")
            }
            for t in all_tokens
        ]
    }


@router.get("/debug/event/{event_id}")
async def debug_event(
    event_id: str,
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account to use"),
    calendar_id: str = Query("primary", description="Calendar ID")
):
    """
    Debug endpoint to test getting and deleting a specific event.
    Shows exactly what event_id and calendar_id would be used.
    """
    account_email = _get_calendar_account(int(user_id), email)

    calendar = CalendarService(int(user_id), account_email)
    if not calendar.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Calendar not connected for {account_email}"
        )

    # Try to get the event
    event = await calendar.get_event(event_id=event_id, calendar_id=calendar_id)

    return {
        "event_id_used": event_id,
        "event_id_length": len(event_id),
        "event_id_repr": repr(event_id),
        "calendar_id_used": calendar_id,
        "account_used": account_email,
        "event_found": event is not None,
        "event_data": event
    }


@router.delete("/debug/event/{event_id}")
async def debug_delete_event(
    event_id: str,
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account to use"),
    calendar_id: str = Query("primary", description="Calendar ID")
):
    """
    Debug endpoint to test deleting a specific event directly.
    Returns detailed diagnostic info.
    """
    account_email = _get_calendar_account(int(user_id), email)

    calendar = CalendarService(int(user_id), account_email)
    if not calendar.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Calendar not connected for {account_email}"
        )

    result = await calendar.delete_event(event_id=event_id, calendar_id=calendar_id)

    return {
        "event_id_used": event_id,
        "event_id_length": len(event_id),
        "event_id_repr": repr(event_id),
        "calendar_id_used": calendar_id,
        "account_used": account_email,
        "delete_result": result
    }


@router.get("/debug/find-event/{event_id}")
async def debug_find_event_in_all_calendars(
    event_id: str,
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account to use")
):
    """
    Debug endpoint to find an event across ALL calendars.
    Useful to determine which calendar actually contains an event.
    """
    account_email = _get_calendar_account(int(user_id), email)

    calendar = CalendarService(int(user_id), account_email)
    if not calendar.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Calendar not connected for {account_email}"
        )

    # Get all calendars
    calendars = await calendar.list_calendars()

    results = []
    for cal in calendars:
        cal_id = cal["id"]
        event = await calendar.get_event(event_id=event_id, calendar_id=cal_id)
        results.append({
            "calendar_id": cal_id,
            "calendar_name": cal["summary"],
            "is_primary": cal.get("primary", False),
            "event_found": event is not None,
            "event_summary": event.get("summary") if event else None
        })

    return {
        "event_id_searched": event_id,
        "account_used": account_email,
        "calendars_searched": len(calendars),
        "results": results,
        "found_in_calendars": [r["calendar_id"] for r in results if r["event_found"]]
    }


@router.get("/calendars", response_model=CalendarsResponse)
async def list_calendars(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account to use")
):
    """
    List all calendars the user has access to.

    Protected endpoint - requires valid JWT token.

    Args:
        email: Google account to use (optional, defaults to first with calendar scope)

    Returns:
        List of calendars with metadata
    """
    account_email = _get_calendar_account(int(user_id), email)

    calendar = CalendarService(int(user_id), account_email)
    if not calendar.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Calendar not connected for {account_email}"
        )

    calendars = await calendar.list_calendars()

    return CalendarsResponse(
        calendars=[CalendarInfo(**cal) for cal in calendars],
        account=account_email
    )


@router.get("/events", response_model=EventsResponse)
async def list_events(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account to use"),
    calendar_id: str = Query("primary", description="Calendar ID (default: primary)"),
    days: int = Query(7, ge=1, le=30, description="Days to look ahead (1-30)"),
    timezone: str = Query("UTC", description="IANA timezone (e.g., America/New_York)")
):
    """
    Get upcoming events from a calendar.

    Protected endpoint - requires valid JWT token.

    Args:
        email: Google account to use (optional)
        calendar_id: Calendar to fetch from (default: "primary")
        days: Number of days to look ahead (1-30, default: 7)
        timezone: IANA timezone string (default: UTC)

    Returns:
        List of events for the specified time range
    """
    account_email = _get_calendar_account(int(user_id), email)

    calendar = CalendarService(int(user_id), account_email)
    if not calendar.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Calendar not connected for {account_email}"
        )

    events = await calendar.get_events(
        calendar_id=calendar_id,
        days_ahead=days,
        timezone=timezone
    )

    return EventsResponse(
        events=[EventSummary(**event) for event in events],
        account=account_email,
        timezone=timezone
    )


@router.get("/event/{event_id}", response_model=EventDetail)
async def get_event(
    event_id: str,
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account to use"),
    calendar_id: str = Query("primary", description="Calendar ID")
):
    """
    Get full details of a specific event.

    Protected endpoint - requires valid JWT token.

    Args:
        event_id: The event ID
        email: Google account to use (optional)
        calendar_id: Calendar containing the event (default: "primary")

    Returns:
        Full event details including attendees and description
    """
    account_email = _get_calendar_account(int(user_id), email)

    calendar = CalendarService(int(user_id), account_email)
    if not calendar.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Calendar not connected for {account_email}"
        )

    event = await calendar.get_event(event_id=event_id, calendar_id=calendar_id)

    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event not found"
        )

    return EventDetail(**event)


@router.get("/agenda", response_model=AgendaResponse)
async def get_agenda(
    user_id: str = Depends(require_auth),
    days: int = Query(7, ge=1, le=14, description="Days to look ahead (1-14)"),
    timezone: str = Query("UTC", description="IANA timezone (e.g., America/New_York)")
):
    """
    Get upcoming events grouped by day for sidebar agenda view.
    Aggregates events from ALL connected accounts and visible calendars.

    Protected endpoint - requires valid JWT token.

    Args:
        days: Number of days to look ahead (1-14, default: 7)
        timezone: IANA timezone string (default: UTC)

    Returns:
        Events grouped by day with relative date labels
    """
    # Get all connected calendar accounts
    accounts = CalendarService.list_connected_accounts(int(user_id))
    logger.info(f"[AGENDA DEBUG] Found {len(accounts)} connected accounts: {[a.get('email') for a in accounts]}")

    if not accounts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No calendar accounts connected"
        )

    # Aggregate events from ALL accounts
    all_events = []
    for account in accounts:
        account_email = account["email"]
        logger.info(f"[AGENDA DEBUG] Processing account: {account_email}")
        calendar = CalendarService(int(user_id), account_email)

        # Get events from all visible calendars for this account
        events = await calendar.get_all_events(
            days_ahead=days,
            timezone=timezone
        )
        logger.info(f"[AGENDA DEBUG] Account {account_email}: got {len(events)} events")

        # Add account info to each event
        for event in events:
            event["account"] = account_email
            all_events.append(event)

    logger.info(f"[AGENDA DEBUG] Total events after aggregation: {len(all_events)}")

    # Sort all events by start time
    all_events.sort(key=lambda e: e.get("start", ""))

    # Get current time in user's timezone
    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Group events by date
    events_by_date: dict[str, list[dict]] = {}

    for event in all_events:
        # Parse event start date
        start_str = event.get("start", "")
        try:
            if "T" in start_str:
                event_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                event_date = event_dt.astimezone(tz).strftime("%Y-%m-%d")
            else:
                # All-day event - date only
                event_date = start_str
        except Exception:
            continue  # Skip events we can't parse

        if event_date not in events_by_date:
            events_by_date[event_date] = []

        # Format event for agenda
        is_all_day = event.get("is_all_day", False)
        events_by_date[event_date].append({
            "id": event.get("id"),
            "summary": event.get("summary", "(No title)"),
            "start": start_str,
            "end": event.get("end", ""),
            "start_time": _format_time(start_str, timezone, is_all_day),
            "end_time": _format_time(event.get("end", ""), timezone, is_all_day),
            "is_all_day": is_all_day,
            "location": event.get("location"),
            "has_video": event.get("has_video", False),
            "video_link": event.get("video_link"),
            "account": event.get("account"),
            "calendar_id": event.get("calendar_id"),
            "calendar_name": event.get("calendar_name"),
            "calendar_color": event.get("calendar_color")
        })

    # Build day list with labels
    agenda_days = []
    for i in range(days):
        date = today + timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")

        if date_str in events_by_date:
            agenda_days.append(AgendaDay(
                date=date_str,
                label=_get_relative_day_label(date, today),
                events=[AgendaEvent(**e) for e in events_by_date[date_str]]
            ))

    return AgendaResponse(
        timezone=timezone,
        days=agenda_days,
        accounts=[a["email"] for a in accounts]
    )


@router.get("/agenda/all", response_model=AgendaResponse)
async def get_unified_agenda(
    user_id: str = Depends(require_auth),
    days: int = Query(7, ge=1, le=14, description="Days to look ahead (1-14)"),
    timezone: str = Query("UTC", description="IANA timezone (e.g., America/New_York)")
):
    """
    Get upcoming events from ALL connected calendar accounts, merged and sorted.

    Protected endpoint - requires valid JWT token.

    Args:
        days: Number of days to look ahead (1-14, default: 7)
        timezone: IANA timezone string (default: UTC)

    Returns:
        Events grouped by day with relative date labels, merged from all accounts
    """
    import asyncio

    cache_key = f"calendar_agenda_{user_id}"
    cached = response_cache.get(cache_key)
    if cached is not None:
        return cached

    # Get all connected calendar accounts (same logic as list_connected_accounts)
    accounts = CalendarService.list_connected_accounts(int(user_id))
    logger.info(f"[AGENDA/ALL DEBUG] Found {len(accounts)} connected accounts: {[a.get('email') for a in accounts]}")

    if not accounts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No Google accounts with calendar access connected"
        )

    # Fetch events from all accounts in parallel
    async def fetch_account_events(account_email: str) -> list[dict]:
        """Fetch events from ALL visible calendars for a single account."""
        try:
            logger.info(f"[AGENDA/ALL DEBUG] Fetching events for account: {account_email}")
            calendar = CalendarService(int(user_id), account_email)

            # Use get_all_events to get events from ALL visible calendars
            events = await calendar.get_all_events(
                days_ahead=days,
                timezone=timezone
            )
            logger.info(f"[AGENDA/ALL DEBUG] Account {account_email}: got {len(events)} events")

            # Add account email to each event
            for event in events:
                event["account"] = account_email

            return events
        except Exception as e:
            logger.error(f"[AGENDA/ALL DEBUG] Error fetching events from {account_email}: {e}")
            return []

    # Fetch all accounts in parallel
    all_events_lists = await asyncio.gather(*[
        fetch_account_events(a["email"]) for a in accounts
    ])

    # Flatten into single list
    all_events = []
    for events_list in all_events_lists:
        all_events.extend(events_list)

    logger.info(f"[AGENDA/ALL DEBUG] Total events after aggregation: {len(all_events)}")

    # Get current time in user's timezone
    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Group events by date
    events_by_date: dict[str, list[dict]] = {}

    for event in all_events:
        # Parse event start date
        start_str = event.get("start", "")
        try:
            if "T" in start_str:
                event_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                event_date = event_dt.astimezone(tz).strftime("%Y-%m-%d")
            else:
                # All-day event - date only
                event_date = start_str
        except Exception:
            continue  # Skip events we can't parse

        if event_date not in events_by_date:
            events_by_date[event_date] = []

        # Format event for agenda (include calendar metadata from get_all_events)
        is_all_day = event.get("is_all_day", False)
        events_by_date[event_date].append({
            "id": event.get("id"),
            "summary": event.get("summary", "(No title)"),
            "start": start_str,
            "end": event.get("end", ""),
            "start_time": _format_time(start_str, timezone, is_all_day),
            "end_time": _format_time(event.get("end", ""), timezone, is_all_day),
            "is_all_day": is_all_day,
            "location": event.get("location"),
            "has_video": event.get("has_video", False),
            "video_link": event.get("video_link"),
            "account": event.get("account"),
            "calendar_id": event.get("calendar_id"),
            "calendar_name": event.get("calendar_name"),
            "calendar_color": event.get("calendar_color")
        })

    # Sort events within each day by start time
    for date_str in events_by_date:
        events_by_date[date_str].sort(key=lambda e: e["start"])

    # Build day list with labels
    agenda_days = []
    for i in range(days):
        date = today + timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")

        if date_str in events_by_date:
            agenda_days.append(AgendaDay(
                date=date_str,
                label=_get_relative_day_label(date, today),
                events=[AgendaEvent(**e) for e in events_by_date[date_str]]
            ))

    result = AgendaResponse(
        timezone=timezone,
        days=agenda_days,
        accounts=[a["email"] for a in accounts]  # List of all accounts included
    )
    response_cache.set(cache_key, result, ttl_seconds=300)  # 5 min TTL
    return result


# ============================================================================
# Multi-Calendar Support Endpoints (07-08)
# ============================================================================


class CalendarPreference(BaseModel):
    """Calendar with user preference data."""
    id: str
    name: str
    is_primary: bool
    is_visible: bool
    access_role: Optional[str] = None
    color: Optional[str] = None
    account: Optional[str] = None  # Google account this calendar belongs to


class CalendarPreferencesResponse(BaseModel):
    """Response for calendar preferences endpoint."""
    calendars: list[CalendarPreference]
    accounts: list[str] = []  # All accounts included


class VisibilityUpdate(BaseModel):
    """Request body for visibility toggle."""
    visible: bool


class MultiCalendarEvent(BaseModel):
    """Event with calendar metadata."""
    id: str
    summary: str
    start: str
    end: str
    is_all_day: bool
    location: Optional[str] = None
    has_video: bool = False
    video_link: Optional[str] = None
    html_link: Optional[str] = None
    calendar_id: str
    calendar_name: str
    calendar_color: Optional[str] = None


class MultiCalendarEventsResponse(BaseModel):
    """Response for multi-calendar events endpoint."""
    events: list[MultiCalendarEvent]
    calendars_queried: list[str]
    account: str
    timezone: str


@router.get("/preferences", response_model=CalendarPreferencesResponse)
async def get_calendar_preferences(
    user_id: str = Depends(require_auth)
):
    """
    Get all calendars with their visibility preferences from ALL connected accounts.

    Protected endpoint - requires valid JWT token.

    If no preferences exist yet, syncs calendars from Google first.

    Returns:
        List of calendars with visibility and color preferences
    """
    # Get all connected calendar accounts
    accounts = CalendarService.list_connected_accounts(int(user_id))
    if not accounts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No calendar accounts connected"
        )

    all_calendars = []
    account_emails = []

    for account in accounts:
        account_email = account["email"]
        account_emails.append(account_email)

        calendar = CalendarService(int(user_id), account_email)
        calendars = await calendar.get_all_calendars()

        # Add account info to each calendar
        for cal in calendars:
            cal["account"] = account_email
            all_calendars.append(cal)

    return CalendarPreferencesResponse(
        calendars=[CalendarPreference(**cal) for cal in all_calendars],
        accounts=account_emails
    )


@router.post("/preferences/sync")
async def sync_calendar_preferences(
    user_id: str = Depends(require_auth)
):
    """
    Sync calendar list from Google to local preferences for ALL connected accounts.

    Adds new calendars (default visible), updates names/colors.

    Returns:
        Number of calendars synced across all accounts
    """
    # Get all connected calendar accounts
    accounts = CalendarService.list_connected_accounts(int(user_id))
    if not accounts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No calendar accounts connected"
        )

    all_calendars = []
    account_emails = []

    for account in accounts:
        account_email = account["email"]
        account_emails.append(account_email)

        calendar = CalendarService(int(user_id), account_email)
        calendars = await calendar.sync_calendar_list()

        for cal in calendars:
            cal["account"] = account_email
            all_calendars.append(cal)

    return {
        "synced": len(all_calendars),
        "calendars": [{"id": c["id"], "name": c["name"], "account": c["account"]} for c in all_calendars],
        "accounts": account_emails
    }


@router.put("/preferences/{calendar_id}/visibility")
async def update_calendar_visibility_endpoint(
    calendar_id: str,
    body: VisibilityUpdate,
    user_id: str = Depends(require_auth)
):
    """
    Toggle visibility for a specific calendar.

    Args:
        calendar_id: Google calendar ID (URL encoded)
        body: VisibilityUpdate with visible boolean

    Returns:
        Updated visibility status
    """
    from urllib.parse import unquote
    from web.core.database import update_calendar_visibility_by_id

    calendar_id = unquote(calendar_id)  # Handle URL encoded calendar IDs

    # Update visibility (finds the account automatically)
    updated = update_calendar_visibility_by_id(
        user_id=int(user_id),
        calendar_id=calendar_id,
        is_visible=body.visible
    )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Calendar not found: {calendar_id}"
        )

    # Invalidate cached agenda since visible calendars changed
    response_cache.invalidate(f"calendar_agenda_{user_id}")

    return {
        "calendar_id": calendar_id,
        "visible": body.visible
    }


@router.get("/events/all", response_model=MultiCalendarEventsResponse)
async def get_all_calendar_events(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account to use"),
    days: int = Query(7, ge=1, le=365, description="Days to look ahead"),
    timezone: str = Query("UTC", description="IANA timezone")
):
    """
    Get events from ALL visible calendars, merged and sorted.

    Events include calendar metadata (name, color) for display.

    Args:
        email: Google account to use
        days: Days to look ahead (1-365)
        timezone: IANA timezone string

    Returns:
        List of events from all visible calendars, sorted by start time
    """
    account_email = _get_calendar_account(int(user_id), email)

    calendar = CalendarService(int(user_id), account_email)
    if not calendar.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Calendar not connected for {account_email}"
        )

    events = await calendar.get_all_events(
        days_ahead=days,
        timezone=timezone
    )

    # Get unique calendar IDs that were queried
    calendar_ids = list(set(e.get("calendar_id") for e in events if e.get("calendar_id")))

    return MultiCalendarEventsResponse(
        events=[MultiCalendarEvent(**e) for e in events],
        calendars_queried=calendar_ids,
        account=account_email,
        timezone=timezone
    )
