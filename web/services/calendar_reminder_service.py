"""
Independent Calendar Reminder Service for Seny.

Bypasses the nudge pipeline entirely to provide deterministic, fast calendar
reminders with proper event filtering (organizer/attendee relationship check),
cross-account deduplication, configurable reminder offsets, and meeting-context
enrichment for longer-lead reminders.

This service does NOT import or use NudgeService, Claude, or Haiku.
"""

import json
import logging
import re
from datetime import datetime
from typing import List, Optional, Tuple

from web.core.database import (
    get_user_by_id,
    get_calendar_reminder_offsets,
    list_google_tokens,
    list_microsoft_tokens,
)

logger = logging.getLogger(__name__)

# Defaults
DEFAULT_TIMED_OFFSETS = [-60, -15]       # 1 hour before, 15 min before
DEFAULT_ALLDAY_OFFSETS = [-1440, 0]      # day before (fires 9am), day-of (fires 9am)


class CalendarReminderService:
    """
    Independent calendar reminder service.

    Determines which events belong to the user, builds human-friendly reminder
    messages, deduplicates cross-account events, and reads user offset
    preferences from the database.
    """

    def __init__(self, user_id: int):
        self.user_id = user_id

    # ------------------------------------------------------------------
    # 1. Event ownership check
    # ------------------------------------------------------------------

    def is_my_event(self, event: dict, user_emails: List[str]) -> bool:
        """
        Decide whether the user should be reminded about *event*.

        Returns True when:
        - The user is the organizer, OR
        - The user is a non-declined attendee, OR
        - The event has no attendees and no organizer email (solo event)

        Returns False when:
        - The event is cancelled
        - The user explicitly declined
        - Shared-calendar event with no attendee match

        Handles both Google Calendar and Outlook/Graph event formats.
        """
        # Cancelled events: never remind
        if event.get("status", "").lower() == "cancelled":
            return False

        emails_lower = [e.lower() for e in user_emails]

        # --- Organizer check ---
        organizer_email = (
            event.get("organizer", {}).get("email", "")
            or event.get("organizer", {}).get("emailAddress", {}).get("address", "")
        ).lower()

        is_organizer = organizer_email in emails_lower if organizer_email else False

        # --- Attendee check ---
        attendees = event.get("attendees", [])

        if attendees:
            for att in attendees:
                # Google format: attendee.email / attendee.responseStatus
                att_email = att.get("email", "").lower()
                # Outlook format: attendee.emailAddress.address
                if not att_email:
                    att_email = (
                        att.get("emailAddress", {}).get("address", "")
                    ).lower()

                if att_email not in emails_lower:
                    continue

                # Found a matching attendee row; check response status.
                # Google: responseStatus field
                response = att.get("responseStatus", "")
                # Outlook: status.response field
                if not response:
                    response = att.get("status", {}).get("response", "")

                if response.lower() == "declined":
                    return False  # explicitly declined

                return True  # accepted, tentative, needsAction, etc.

            # Had attendees but none matched the user's emails
            if is_organizer:
                return True
            return False

        # No attendees list at all (solo / simple event)
        if not organizer_email:
            # No organizer email and no attendees: user created it on own cal
            return True

        if is_organizer:
            return True

        # Organizer is someone else and no attendee list: shared calendar leak
        return False

    # ------------------------------------------------------------------
    # 2. Reminder offset preferences
    # ------------------------------------------------------------------

    def get_reminder_offsets(self, is_all_day: bool) -> List[int]:
        """
        Return the user's configured reminder offsets (minutes before event).

        Falls back to defaults if no preference is stored.
        Returns sorted descending (largest-magnitude negative first, so the
        earliest reminder fires first: e.g. [-60, -15]).
        """
        prefs = get_calendar_reminder_offsets(self.user_id)
        key = "allday" if is_all_day else "timed"
        offsets = prefs.get(key, [])

        if not offsets:
            offsets = DEFAULT_ALLDAY_OFFSETS if is_all_day else DEFAULT_TIMED_OFFSETS

        return sorted(offsets)  # e.g. [-1440, 0] or [-60, -15]

    # ------------------------------------------------------------------
    # 3. Reminder message builder
    # ------------------------------------------------------------------

    def build_reminder_message(
        self,
        event: dict,
        offset_minutes: int,
        include_prep: bool = False,
    ) -> Tuple[str, str]:
        """
        Build a (title, body) tuple for a calendar reminder.

        The title is a concise human-readable timing label.
        The body includes event time, location, and optionally attendee/prep
        information for longer-lead reminders.
        """
        summary = event.get("summary", event.get("subject", "Untitled event"))

        # --- Title ---
        title = self._format_title(summary, offset_minutes)

        # --- Body ---
        parts: List[str] = []

        # Time range
        time_range = self._format_time_range(event)
        if time_range:
            parts.append(time_range)

        # Location
        location = event.get("location", "")
        # Outlook stores location differently
        if not location and isinstance(event.get("location"), dict):
            location = event.get("location", {}).get("displayName", "")
        if isinstance(location, dict):
            location = location.get("displayName", "")
        if location:
            parts.append(f"Location: {location}")

        # Prep info for longer-lead reminders
        if include_prep:
            attendees = event.get("attendees", [])
            if attendees:
                names = []
                for att in attendees:
                    name = (
                        att.get("displayName")
                        or att.get("emailAddress", {}).get("name")
                        or att.get("email")
                        or att.get("emailAddress", {}).get("address", "")
                    )
                    if name:
                        names.append(name)
                if names:
                    shown = names[:8]
                    extra = len(names) - 8
                    line = "With: " + ", ".join(shown)
                    if extra > 0:
                        line += f" +{extra} more"
                    parts.append(line)

            # Description snippet
            desc = event.get("description", "") or event.get("bodyPreview", "") or ""
            if desc:
                snippet = desc[:200].strip()
                if len(desc) > 200:
                    snippet += "..."
                parts.append(snippet)

            # Video/meeting link
            link = self._extract_meeting_link(event)
            if link:
                parts.append(f"Join: {link}")

        body = "\n".join(parts)
        return title, body

    # ------------------------------------------------------------------
    # 4. Cross-account deduplication
    # ------------------------------------------------------------------

    @staticmethod
    def deduplicate_events(events: List[dict]) -> List[dict]:
        """
        Remove duplicates where the same event appears across multiple
        connected accounts (e.g. invited to user's work and personal email).

        Groups by (normalised title, start ISO, end ISO) and keeps the first
        occurrence from each group.
        """
        seen = {}
        result = []
        for ev in events:
            summary = (ev.get("summary") or ev.get("subject") or "").lower().strip()
            start_iso = _extract_iso(ev, "start")
            end_iso = _extract_iso(ev, "end")
            key = (summary, start_iso, end_iso)
            if key not in seen:
                seen[key] = True
                result.append(ev)
        return result

    # ------------------------------------------------------------------
    # 5. Collect all user emails
    # ------------------------------------------------------------------

    @staticmethod
    def get_all_user_emails(user_id: int) -> List[str]:
        """
        Gather every email address associated with a user across all
        connected calendar accounts plus their primary login email.

        Returns a deduplicated lowercase list.
        """
        emails = set()

        # Primary login email
        try:
            user = get_user_by_id(user_id)
            if user and user.get("email"):
                emails.add(user["email"].lower())
        except Exception:
            logger.warning("get_all_user_emails: failed to read primary email for user %d", user_id)

        # Google Calendar accounts
        try:
            from web.services.calendar_service import CalendarService
            google_accounts = CalendarService.list_connected_accounts(user_id)
            for acct in google_accounts:
                email = acct.get("email", "")
                if email:
                    emails.add(email.lower())
        except Exception:
            logger.warning("get_all_user_emails: failed to read Google accounts for user %d", user_id)

        # Outlook Calendar accounts
        try:
            from web.services.outlook_calendar_service import OutlookCalendarService
            outlook_accounts = OutlookCalendarService.list_connected_accounts(user_id)
            for acct in outlook_accounts:
                email = acct.get("email", "")
                if email:
                    emails.add(email.lower())
        except Exception:
            logger.warning("get_all_user_emails: failed to read Outlook accounts for user %d", user_id)

        return list(emails)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_title(summary: str, offset_minutes: int) -> str:
        """Build a concise timing-aware title string."""
        if offset_minutes <= -1440:
            return f"Tomorrow: {summary}"
        if offset_minutes == 0:
            return f"Today: {summary}"
        if offset_minutes == -60:
            return f"1 hour: {summary}"
        if offset_minutes == -15:
            return f"15 min: {summary}"
        if -240 <= offset_minutes <= -61:
            hours = abs(offset_minutes) // 60
            remainder = abs(offset_minutes) % 60
            if remainder:
                return f"In {hours}h{remainder}m: {summary}"
            return f"In {hours}h: {summary}"
        if -30 <= offset_minutes <= -16:
            return f"In {abs(offset_minutes)} min: {summary}"
        # Generic fallback for other negative offsets
        if offset_minutes < 0:
            return f"In {abs(offset_minutes)} min: {summary}"
        return f"{summary}"

    @staticmethod
    def _format_time_range(event: dict) -> str:
        """Return a human-readable time range like '10:00 AM - 11:00 AM'."""
        start = event.get("start", {})
        end = event.get("end", {})

        # Google all-day uses 'date', timed uses 'dateTime'
        start_str = start.get("dateTime") or start.get("date") or ""
        end_str = end.get("dateTime") or end.get("date") or ""

        # Outlook uses 'dateTime' directly at event level sometimes
        if not start_str:
            start_str = event.get("start", "")
        if not end_str:
            end_str = event.get("end", "")

        if isinstance(start_str, dict):
            start_str = start_str.get("dateTime", "")
        if isinstance(end_str, dict):
            end_str = end_str.get("dateTime", "")

        if not start_str:
            return ""

        # Try to make it readable
        try:
            # Handle ISO format with timezone
            start_str_clean = str(start_str).replace("Z", "+00:00")
            end_str_clean = str(end_str).replace("Z", "+00:00") if end_str else ""

            # Date-only (all-day)
            if len(start_str_clean) <= 10:
                return start_str_clean

            start_dt = datetime.fromisoformat(start_str_clean)
            fmt = "%I:%M %p"
            if end_str_clean and len(end_str_clean) > 10:
                end_dt = datetime.fromisoformat(end_str_clean)
                return f"{start_dt.strftime(fmt).lstrip('0')} - {end_dt.strftime(fmt).lstrip('0')}"
            return start_dt.strftime(fmt).lstrip("0")
        except Exception:
            return str(start_str)

    @staticmethod
    def _extract_meeting_link(event: dict) -> Optional[str]:
        """Pull out a video conference / meeting link if one exists."""
        # Google: hangoutLink or conferenceData
        link = event.get("hangoutLink", "")
        if link:
            return link

        conf = event.get("conferenceData", {})
        entry_points = conf.get("entryPoints", [])
        for ep in entry_points:
            if ep.get("entryPointType") == "video":
                return ep.get("uri", "")

        # Outlook: onlineMeeting.joinUrl
        online = event.get("onlineMeeting") or {}
        join_url = online.get("joinUrl", "")
        if join_url:
            return join_url

        # Check description for common meeting URLs
        desc = event.get("description", "") or event.get("bodyPreview", "") or ""
        match = re.search(
            r'https?://(?:meet\.google\.com|zoom\.us|teams\.microsoft\.com)/\S+',
            desc,
        )
        if match:
            return match.group(0)

        return None


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _extract_iso(event: dict, field: str) -> str:
    """Extract an ISO timestamp string from a Google or Outlook event."""
    val = event.get(field, {})
    if isinstance(val, dict):
        return val.get("dateTime") or val.get("date") or ""
    return str(val) if val else ""
