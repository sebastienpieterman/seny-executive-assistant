"""
PredictiveService — Phase 27-01: Meeting Prep Briefing Engine

Proactively sends a context brief 30–90 minutes before calendar events with attendees.
For every qualifying meeting, assembles:
  - Attendee list (with last-contact dates from People tracker)
  - Semantic context from notes, items, and conversations
  - Open follow-ups linked to any tracked attendee

Delivered as an 'urgent' nudge via NudgeService so it arrives in time to be useful.

Usage (called from scheduler):
    service = PredictiveService(user_id)
    result = await service.send_meeting_prep_nudges()
"""

import logging
import os
from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from web.core.database import (
    get_db,
    list_google_tokens,
    search_people,
    get_person_followups,
    get_nudge_for_source,
    get_stale_contacts,
    get_contact_frequency,
    get_overdue_followups,
    get_recent_nudge_for_source,
    count_nudges_today,
    get_family_contacts,
    get_unanswered_task_nudges,
    get_nudge_preferences,
)

logger = logging.getLogger(__name__)


class PredictiveService:
    """
    Predictive intelligence service — assembles and sends proactive briefings.

    Phase 27-01: Meeting prep briefings sent 30–90 min before events with attendees.
    Future plans: relationship follow-up predictions, task deadline forecasting.

    One instance per user. Instantiated by the scheduler for each user.
    """

    def __init__(self, user_id: int):
        self.user_id = user_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send_meeting_prep_nudges(self) -> dict:
        """
        Find upcoming meetings needing prep briefs and send them.

        Returns:
            Dict with {'sent': int} count of nudges delivered.
        """
        from web.services.pattern_learning_service import PatternLearningService
        pattern_service = PatternLearningService(self.user_id)
        if await pattern_service.should_suppress_item_type('meeting_prep'):
            logger.info(
                "[predictive] meeting_prep suppressed for user %d — user preference score < -0.5",
                self.user_id,
            )
            return {'sent': 0}

        from web.services.nudge_service import NudgeService

        events = await self.get_upcoming_events_needing_prep()
        sent = 0

        for event in events:
            # Daily cap: max 5 meeting_prep nudges per day across all scheduler runs
            if count_nudges_today(self.user_id, 'meeting_prep') >= 5:
                logger.info(
                    "Meeting prep daily cap reached for user %d — skipping remaining events",
                    self.user_id,
                )
                break

            try:
                title, body = await self._assemble_brief(event)
                source_id = hash(f"{event['id']}:{event['start'][:10]}") % (2 ** 31)
                nudge_svc = NudgeService(self.user_id)
                await nudge_svc.send_nudge(
                    nudge_type='meeting_prep',
                    title=title,
                    body=body,
                    urgency='urgent',
                    source_type='calendar_event',
                    source_id=source_id,
                )
                sent += 1
                logger.info(
                    "Meeting prep nudge sent for user %d: event='%s' source_id=%d",
                    self.user_id, event.get('summary', ''), source_id
                )
            except Exception as e:
                logger.error(
                    "Meeting prep nudge failed for user %d event '%s': %r",
                    self.user_id, event.get('summary', ''), e
                )

        return {'sent': sent}

    async def send_relationship_nudges(self) -> dict:
        """
        Find stale contacts and nudge the user to check in.

        A contact qualifies when:
        - last_contact_date is older than 14 days (or older than 1.5× their personal contact
          frequency, whichever is greater).
        - No 'relationship_check' nudge was sent for that person in the last 7 days.

        Caps at 3 nudges per run to avoid flooding.

        Returns:
            Dict with {'sent': int} count of nudges created.
        """
        from web.services.pattern_learning_service import PatternLearningService
        pattern_service = PatternLearningService(self.user_id)
        if await pattern_service.should_suppress_item_type('relationship_check'):
            logger.info(
                "[predictive] relationship_check suppressed for user %d — user preference score < -0.5",
                self.user_id,
            )
            return {'sent': 0}

        from web.services.nudge_service import NudgeService

        stale = get_stale_contacts(self.user_id, min_days_stale=14, limit=10)
        sent = 0

        for person in stale:
            if sent >= 3:
                break

            # Daily cap: max 3 relationship_check nudges per day across all scheduler runs
            if count_nudges_today(self.user_id, 'relationship_check') >= 3:
                logger.info(
                    "Relationship check daily cap reached for user %d — skipping",
                    self.user_id,
                )
                break

            last_contact_str = person.get('last_contact_date', '')
            try:
                days_since = (date.today() - date.fromisoformat(str(last_contact_str)[:10])).days
            except (ValueError, TypeError):
                continue

            # Personalise threshold using contact history
            frequency = get_contact_frequency(self.user_id, person['id'])
            threshold = max(14, frequency * 1.5) if frequency else 14

            if days_since < threshold:
                continue

            # 7-day dedup — skip if we already nudged about this person this week
            if get_recent_nudge_for_source(
                self.user_id, 'person', person['id'], nudge_type='relationship_check', days=7
            ):
                continue

            context_snippet = (person.get('context') or '')[:100]
            body_parts = [f"Last contact: {str(last_contact_str)[:10]} ({days_since} days ago)."]
            if context_snippet:
                body_parts.append(context_snippet)
            body = ' '.join(body_parts)

            try:
                nudge_svc = NudgeService(self.user_id)
                await nudge_svc.send_nudge(
                    nudge_type='relationship_check',
                    title=f"👋 Check in with {person['name']}?",
                    body=body,
                    urgency='normal',
                    source_type='person',
                    source_id=person['id'],
                )
                sent += 1
                logger.info(
                    "Relationship nudge sent for user %d: person='%s' days_since=%d",
                    self.user_id, person['name'], days_since,
                )
            except Exception as e:
                logger.error(
                    "Relationship nudge failed for user %d person '%s': %r",
                    self.user_id, person['name'], e,
                )

        return {'sent': sent}

    async def send_followup_nudges(self) -> dict:
        """
        Find open follow-up items older than 7 days and remind the user.

        Deduplicates: no 'open_followup' nudge is sent for the same follow-up
        within the last 7 days. Caps at 3 nudges per run.

        Returns:
            Dict with {'sent': int} count of nudges created.
        """
        from web.services.pattern_learning_service import PatternLearningService
        pattern_service = PatternLearningService(self.user_id)
        if await pattern_service.should_suppress_item_type('open_followup'):
            logger.info(
                "[predictive] open_followup suppressed for user %d — user preference score < -0.5",
                self.user_id,
            )
            return {'sent': 0}

        from web.services.nudge_service import NudgeService

        overdue = get_overdue_followups(self.user_id, min_age_days=7, limit=10)
        sent = 0

        for fu in overdue:
            if sent >= 3:
                break

            # Daily cap: max 3 open_followup nudges per day across all scheduler runs
            if count_nudges_today(self.user_id, 'open_followup') >= 3:
                logger.info(
                    "Open followup daily cap reached for user %d — skipping",
                    self.user_id,
                )
                break

            created_at_str = fu.get('created_at', '')
            try:
                days_old = (date.today() - date.fromisoformat(str(created_at_str)[:10])).days
            except (ValueError, TypeError):
                days_old = 0

            # 7-day dedup
            if get_recent_nudge_for_source(
                self.user_id, 'followup', fu['followup_id'], nudge_type='open_followup', days=7
            ):
                continue

            try:
                nudge_svc = NudgeService(self.user_id)
                await nudge_svc.send_nudge(
                    nudge_type='open_followup',
                    title=f"📌 Open follow-up with {fu['person_name']}",
                    body=f"{fu['content']}\n(added {days_old} days ago)",
                    urgency='normal',
                    source_type='followup',
                    source_id=fu['followup_id'],
                )
                sent += 1
                logger.info(
                    "Follow-up nudge sent for user %d: person='%s' followup_id=%d days_old=%d",
                    self.user_id, fu['person_name'], fu['followup_id'], days_old,
                )
            except Exception as e:
                logger.error(
                    "Follow-up nudge failed for user %d followup %d: %r",
                    self.user_id, fu.get('followup_id'), e,
                )

        return {'sent': sent}

    async def send_family_checkin_nudges(self) -> dict:
        """
        Prompt the user to update Seny on family members who've gone quiet.

        Unlike relationship_check (which assumes silence = no contact),
        this nudge says "want to update me on X?" — it prompts the user
        to share what's happening, not assume they haven't been in touch.

        Fires when:
        - Person has relationship_type = 'family'
        - last_contact_date older than 14 days (or NULL)
        - No cross_references for this person from the last 14 days
          (if there are recent scanned items, we already have context)
        - No relationship_checkin_prompt nudge in last 14 days

        Daily cap: 2 family checkin nudges per day.

        Returns:
            Dict with {'sent': int} count.
        """
        from web.services.pattern_learning_service import PatternLearningService
        pattern_service = PatternLearningService(self.user_id)
        if await pattern_service.should_suppress_item_type('relationship_checkin_prompt'):
            logger.info(
                "[predictive] relationship_checkin_prompt suppressed for user %d — user preference score < -0.5",
                self.user_id,
            )
            return {'sent': 0}

        from web.services.nudge_service import NudgeService

        family = get_family_contacts(self.user_id)
        sent = 0

        for person in family:
            if sent >= 2:
                break

            if count_nudges_today(self.user_id, 'relationship_checkin_prompt') >= 2:
                logger.info(
                    "Family checkin daily cap reached for user %d — skipping",
                    self.user_id,
                )
                break

            # Check last_contact_date — skip if recently updated
            last_contact_str = person.get('last_contact_date', '')
            try:
                days_since = (date.today() - date.fromisoformat(str(last_contact_str)[:10])).days
            except (ValueError, TypeError):
                days_since = 999  # NULL / unknown → treat as stale

            if days_since < 14:
                continue

            # Context check: skip if we have recent scanned items mentioning this person
            if self._has_recent_cross_references(person['id'], days=14):
                logger.debug(
                    "Family checkin: skipping %s — recent scanned activity found",
                    person['name'],
                )
                continue

            # 14-day dedup — don't re-nudge about the same person this fortnight
            if get_recent_nudge_for_source(
                self.user_id, 'person', person['id'],
                nudge_type='relationship_checkin_prompt', days=14
            ):
                continue

            try:
                nudge_svc = NudgeService(self.user_id)
                await nudge_svc.send_nudge(
                    nudge_type='relationship_checkin_prompt',
                    title=f"💭 Want to update me on {person['name']}?",
                    body=(
                        f"It's been a while since I last saw an update on {person['name']}. "
                        f"No worries if you've been in touch — just let me know how they're doing "
                        f"when you get a chance."
                    ),
                    urgency='normal',
                    source_type='person',
                    source_id=person['id'],
                )
                sent += 1
                logger.info(
                    "Family checkin nudge sent for user %d: person='%s' days_since=%d",
                    self.user_id, person['name'], days_since,
                )
            except Exception as e:
                logger.error(
                    "Family checkin nudge failed for user %d person '%s': %r",
                    self.user_id, person['name'], e,
                )

        return {'sent': sent}

    async def send_task_followup_nudges(self) -> dict:
        """
        Follow up on overdue_task nudges that received no response within 4–24 hours.

        Sends a single gentle follow-up per original nudge. Once a follow-up
        is sent, no further follow-ups are sent for that nudge (deduped by
        source_type='nudge', source_id=original_nudge_id).

        Daily cap: 3 follow-up nudges per day.

        Returns:
            Dict with {'sent': int} count.
        """
        from web.services.pattern_learning_service import PatternLearningService
        pattern_service = PatternLearningService(self.user_id)
        if await pattern_service.should_suppress_item_type('nudge_followup'):
            logger.info(
                "[predictive] nudge_followup suppressed for user %d — user preference score < -0.5",
                self.user_id,
            )
            return {'sent': 0}

        from web.services.nudge_service import NudgeService

        unanswered = get_unanswered_task_nudges(self.user_id, min_hours=4, max_hours=24)
        sent = 0

        for nudge in unanswered:
            if sent >= 3:
                break

            # Daily cap
            if count_nudges_today(self.user_id, 'nudge_followup') >= 3:
                logger.info(
                    "Nudge followup daily cap reached for user %d — skipping",
                    self.user_id,
                )
                break

            # Dedup: one follow-up per original nudge only
            if get_recent_nudge_for_source(
                self.user_id, 'nudge', nudge['id'],
                nudge_type='nudge_followup', days=2
            ):
                continue

            original_title = nudge.get('title', 'your task')
            # Strip any leading emoji from original title for cleaner follow-up
            display_title = original_title.lstrip('📌⚠️⏰🔔 ')

            try:
                nudge_svc = NudgeService(self.user_id)
                await nudge_svc.send_nudge(
                    nudge_type='nudge_followup',
                    title=f"⏰ Still on your list: {display_title}",
                    body=(
                        f"Earlier I reminded you about this. "
                        f"Did you make progress? Even a small step counts."
                    ),
                    urgency='normal',
                    source_type='nudge',
                    source_id=nudge['id'],
                )
                sent += 1
                logger.info(
                    "Nudge followup sent for user %d: original_nudge_id=%d",
                    self.user_id, nudge['id'],
                )
            except Exception as e:
                logger.error(
                    "Nudge followup failed for user %d nudge_id=%d: %r",
                    self.user_id, nudge['id'], e,
                )

        return {'sent': sent}

    def _has_recent_cross_references(self, person_id: int, days: int = 14) -> bool:
        """Check if there are recent scanned items cross-referenced to this person."""
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT 1 FROM cross_references cr
                    JOIN scanned_items si ON cr.scanned_item_id = si.id
                    WHERE cr.user_id = %s AND cr.entity_type = 'person' AND cr.entity_id = %s
                      AND si.detected_at > NOW() + (%s || ' days')::interval
                    LIMIT 1
                """, (self.user_id, person_id, f'-{days}'))
                return cursor.fetchone() is not None
        except Exception as e:
            logger.debug("Family checkin: cross-ref check failed for person %d: %r", person_id, e)
            return False

    async def get_upcoming_events_needing_prep(self) -> list[dict]:
        """
        Return calendar events that start in 30–90 minutes and have attendees,
        filtered to only those that haven't already received a briefing today.

        Returns:
            List of event dicts qualifying for a meeting prep nudge.
        """
        from web.services.calendar_service import CalendarService

        now = datetime.now(timezone.utc)
        window_start = now + timedelta(minutes=30)
        window_end = now + timedelta(minutes=90)

        qualifying = []

        # Iterate over all Google accounts connected for this user
        accounts = list_google_tokens(self.user_id)
        calendar_accounts = [
            a for a in accounts
            if 'calendar' in a.get('scopes', '').lower()
        ]

        if not calendar_accounts:
            logger.debug("Meeting prep: user %d has no calendar accounts", self.user_id)
            return []

        for account in calendar_accounts:
            email = account['email']
            try:
                cal = CalendarService(self.user_id, email)
                if not cal.is_connected():
                    continue

                # Fetch next 24 hours of events (days_ahead=1)
                events = await cal.get_events(days_ahead=1, max_results=20)

                for event in events:
                    # Skip all-day events (start is a plain date string 'YYYY-MM-DD')
                    start_str = event.get('start', '')
                    if not start_str or len(start_str) <= 10:
                        # Pure date string — all-day event
                        continue
                    if event.get('is_all_day', False):
                        continue

                    # Skip events with no attendees (personal reminders)
                    attendees = event.get('attendees', [])
                    if not attendees:
                        continue

                    # Parse start time and check 30–90 min window
                    try:
                        # ISO 8601 format from CalendarService includes timezone offset
                        event_start = datetime.fromisoformat(start_str)
                        # Normalize to UTC for comparison
                        if event_start.tzinfo is None:
                            event_start = event_start.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        logger.debug(
                            "Meeting prep: could not parse start time '%s' for event '%s'",
                            start_str, event.get('summary', '')
                        )
                        continue

                    if not (window_start <= event_start <= window_end):
                        continue

                    # Dedup: skip if we already sent a briefing for this event today
                    source_id = hash(f"{event['id']}:{event['start'][:10]}") % (2 ** 31)
                    existing = get_nudge_for_source(
                        self.user_id, 'calendar_event', source_id
                    )
                    if existing:
                        logger.debug(
                            "Meeting prep: briefing already sent for event '%s' (source_id=%d)",
                            event.get('summary', ''), source_id
                        )
                        continue

                    qualifying.append(event)

            except Exception as e:
                logger.error(
                    "Meeting prep: error fetching events for user %d email %s: %r",
                    self.user_id, email, e
                )

        return qualifying

    # ------------------------------------------------------------------
    # Brief assembly
    # ------------------------------------------------------------------

    async def _assemble_brief(self, event: dict) -> tuple[str, str]:
        """
        Assemble the title and body for a meeting prep nudge.

        Args:
            event: Formatted event dict from CalendarService

        Returns:
            (title, body) tuple — body capped at 1000 chars
        """
        now = datetime.now(timezone.utc)
        start_str = event.get('start', '')
        try:
            event_start = datetime.fromisoformat(start_str)
            if event_start.tzinfo is None:
                event_start = event_start.replace(tzinfo=timezone.utc)
            minutes_until = int((event_start - now).total_seconds() / 60)
        except (ValueError, TypeError):
            minutes_until = 60  # Fallback

        summary = event.get('summary', 'Meeting')
        title = f"📅 Meeting in {minutes_until}min: {summary}"

        # --- Section 1: Attendees ---
        attendee_lines, tracked_person_ids = await self._resolve_attendees(event)

        # --- Section 2: Semantic context ---
        context_lines = await self._get_semantic_context(summary)

        # --- Section 3: Open follow-ups ---
        followup_lines = self._get_followups(tracked_person_ids)

        # --- Assemble body ---
        if attendee_lines:
            attendees_text = "📋 Attendees: " + ", ".join(attendee_lines)
        else:
            attendees_text = "📋 Attendees: None tracked in Seny"

        if context_lines:
            context_text = "🔗 Related context:\n" + "\n".join(context_lines)
        else:
            context_text = "🔗 Related context: None found"

        if followup_lines:
            followup_text = "✅ Open follow-ups with attendees:\n" + "\n".join(followup_lines)
        else:
            followup_text = "✅ Open follow-ups with attendees: None"

        footer = '💬 Reply "skip" to dismiss future briefings for this meeting type'

        body = "\n\n".join([attendees_text, context_text, followup_text, footer])

        # Cap at 1000 chars
        if len(body) > 1000:
            body = body[:997] + "…"

        return title, body

    async def _resolve_attendees(self, event: dict) -> tuple[list[str], list[int]]:
        """
        Match event attendees to tracked People in Seny.

        Returns:
            (attendee_lines, person_ids) where attendee_lines are display strings
            and person_ids are the DB IDs of matched people (for follow-up lookup).
        """
        raw_attendees = event.get('attendees', [])
        attendee_lines = []
        tracked_ids = []

        for att in raw_attendees:
            name = att.get('name') or att.get('email', 'Unknown')
            if not name or name == 'Unknown':
                continue

            # Try to look up in People tracker
            try:
                matches = search_people(self.user_id, name, limit=1)
                if matches:
                    person = matches[0]
                    last_contact = person.get('last_contact_date')
                    if last_contact:
                        # Trim to date only for readability
                        last_contact_display = str(last_contact)[:10]
                        attendee_lines.append(f"{name} (last contact: {last_contact_display})")
                    else:
                        attendee_lines.append(name)
                    tracked_ids.append(person['id'])
                else:
                    attendee_lines.append(name)
            except Exception as e:
                logger.debug("Meeting prep: people lookup failed for '%s': %r", name, e)
                attendee_lines.append(name)

        return attendee_lines, tracked_ids

    async def _get_semantic_context(self, query: str) -> list[str]:
        """
        Use SemanticSearchService to find relevant notes, items, and conversations.

        Returns:
            List of up to 3 bullet lines, or [] if embeddings are disabled.
        """
        try:
            from web.services.semantic_search_service import SemanticSearchService
            svc = SemanticSearchService()

            # Graceful degradation if embeddings are disabled
            if not svc.embedding_service.enabled:
                return []

            results = svc.search(
                user_id=self.user_id,
                query=query,
                entity_types=['items', 'notes', 'conversations'],
                n_results=5,
                threshold=1.3,
            )

            lines = []
            for r in results[:3]:
                entity_type = r.get('entity_type', 'item')
                text = r.get('text', '')[:120]
                lines.append(f"• [{entity_type}]: {text}")

            return lines

        except Exception as e:
            logger.debug("Meeting prep: semantic search failed: %r", e)
            return []

    def _get_followups(self, person_ids: list[int]) -> list[str]:
        """
        Retrieve open follow-ups for tracked attendees.

        Args:
            person_ids: List of person DB IDs to check

        Returns:
            List of up to 3 follow-up bullet strings.
        """
        lines = []
        seen = set()

        for pid in person_ids:
            try:
                followups = get_person_followups(pid, status='active')
                for fu in followups:
                    content = fu.get('content', '').strip()
                    if content and content not in seen:
                        lines.append(f"• {content}")
                        seen.add(content)
                    if len(lines) >= 3:
                        break
            except Exception as e:
                logger.debug("Meeting prep: followup lookup failed for person %d: %r", pid, e)

            if len(lines) >= 3:
                break

        return lines

    # ------------------------------------------------------------------
    # Proactive task nudging — Smart Forward-Looking Nudges
    # ------------------------------------------------------------------

    async def send_upcoming_task_nudges(self) -> dict:
        """
        Nudge about tasks due within the next 24–48 hours, before they become overdue.

        Lead time by priority:
        - urgent:  nudge up to 48 hours before due
        - high:    nudge up to 24 hours before due
        - medium:  nudge up to 12 hours before due
        - low:     nudge up to 4 hours before due

        Dedup: one upcoming nudge per task per day.
        Daily cap: 6 upcoming-task nudges per day.

        Returns:
            Dict with {'sent': int} count.
        """
        from web.services.pattern_learning_service import PatternLearningService
        pattern_service = PatternLearningService(self.user_id)
        if await pattern_service.should_suppress_item_type('upcoming_task'):
            logger.info(
                "[predictive] upcoming_task suppressed for user %d — user preference score < -0.5",
                self.user_id,
            )
            return {'sent': 0}

        from web.services.nudge_service import NudgeService

        now = datetime.now()
        tasks = self._get_upcoming_tasks(now)
        sent = 0

        for task in tasks:
            if sent >= 6:
                break

            if count_nudges_today(self.user_id, 'upcoming_task') >= 6:
                logger.info("Upcoming task daily cap reached for user %d", self.user_id)
                break

            task_id = task['id']

            # Dedup: skip if we already nudged about this task today
            if get_recent_nudge_for_source(
                self.user_id, 'task', task_id,
                nudge_type='upcoming_task', days=1
            ):
                continue

            try:
                due_str = task.get('due_date', '')
                due_dt = datetime.fromisoformat(due_str.replace('Z', '+00:00')) if due_str else None
                if not due_dt:
                    continue

                hours_until = (due_dt.replace(tzinfo=None) - now).total_seconds() / 3600
                if hours_until <= 0:
                    continue  # already overdue — let the overdue nudger handle it

                if hours_until < 1:
                    time_label = "less than an hour"
                elif hours_until < 2:
                    time_label = "about an hour"
                else:
                    time_label = f"{int(hours_until)} hours"

                priority = task.get('priority', 'medium')
                title = task.get('title', 'Task')

                nudge_svc = NudgeService(self.user_id)
                await nudge_svc.send_nudge(
                    nudge_type='upcoming_task',
                    title=f"⏳ Due in {time_label}: {title}",
                    body=(
                        f"This is due in {time_label}. "
                        f"Priority: {priority}. Get ahead of it now."
                    ),
                    urgency='urgent' if priority in ('urgent', 'high') else 'normal',
                    source_type='task',
                    source_id=task_id,
                )
                sent += 1
                logger.info(
                    "Upcoming task nudge sent for user %d: task_id=%d due_in=%.1fh",
                    self.user_id, task_id, hours_until,
                )
            except Exception as e:
                logger.error(
                    "Upcoming task nudge failed for user %d task_id=%d: %r",
                    self.user_id, task.get('id'), e,
                )

        return {'sent': sent}

    def _get_upcoming_tasks(self, now: datetime) -> list[dict]:
        """
        Return tasks due within their priority-based lead window but not yet overdue.

        Lead windows: urgent=48h, high=24h, medium=12h, low=4h.
        """
        lead_hours = {'urgent': 48, 'high': 24, 'medium': 12, 'low': 4}
        lookahead = now + timedelta(hours=48)

        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, title, priority, due_date, status
                    FROM tasks
                    WHERE user_id = %s
                      AND status NOT IN ('completed', 'cancelled')
                      AND due_date > %s
                      AND due_date <= %s
                    ORDER BY due_date ASC
                    LIMIT 20
                """, (self.user_id, now.isoformat(), lookahead.isoformat()))
                rows = [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error("Failed to fetch upcoming tasks for user %d: %r", self.user_id, e)
            return []

        result = []
        for task in rows:
            due_str = task.get('due_date', '')
            try:
                due_dt = datetime.fromisoformat(due_str.replace('Z', '+00:00'))
                hours_until = (due_dt.replace(tzinfo=None) - now).total_seconds() / 3600
            except (ValueError, TypeError):
                continue

            priority = task.get('priority', 'medium')
            window = lead_hours.get(priority, 12)
            if 0 < hours_until <= window:
                result.append(task)

        return result

    async def send_ai_coach_nudge(self) -> dict:
        """
        Use Claude Haiku to decide what the user should focus on right now,
        regardless of whether anything is technically due soon.

        Looks at all pending tasks and the next 8 hours of calendar events,
        then sends a short, direct recommendation in Seny's voice.

        Dedup: skips if an ai_coach nudge was sent within the last 2 hours.
        Daily cap: 4 coach nudges per day.

        Returns:
            Dict with {'sent': bool}.
        """
        from web.services.pattern_learning_service import PatternLearningService
        pattern_service = PatternLearningService(self.user_id)
        if await pattern_service.should_suppress_item_type('ai_coach'):
            logger.info(
                "[predictive] ai_coach suppressed for user %d — user preference score < -0.5",
                self.user_id,
            )
            return {'sent': 0}

        from anthropic import AsyncAnthropic
        from web.services.nudge_service import NudgeService

        # Daily cap
        if count_nudges_today(self.user_id, 'ai_coach') >= 4:
            logger.info("AI coach daily cap reached for user %d", self.user_id)
            return {'sent': False}

        # Dedup: skip if we sent a coach nudge within last 2 hours
        if self._recent_ai_coach_nudge():
            logger.debug("AI coach: sent within last 2h for user %d, skipping", self.user_id)
            return {'sent': False}

        # Gather pending tasks
        tasks = self._get_all_pending_tasks()
        if not tasks:
            return {'sent': False}

        # Gather upcoming calendar snapshot
        calendar_summary = await self._get_calendar_snapshot()

        prefs = get_nudge_preferences(self.user_id)
        tz_str = prefs.get('digest_timezone', 'America/Chicago')
        try:
            user_tz = ZoneInfo(tz_str)
        except Exception:
            user_tz = ZoneInfo('America/Chicago')
        now = datetime.now(user_tz)
        task_lines = []
        for t in tasks[:20]:
            due = t.get('due_date', '')
            if due:
                try:
                    due_dt = datetime.fromisoformat(due.replace('Z', '+00:00'))
                    hours_until = (due_dt.replace(tzinfo=None) - now).total_seconds() / 3600
                    if hours_until < 0:
                        due_label = f"OVERDUE by {abs(int(hours_until))}h"
                    elif hours_until < 24:
                        due_label = f"due in {int(hours_until)}h"
                    else:
                        days = int(hours_until / 24)
                        due_label = f"due in {days}d"
                except (ValueError, TypeError):
                    due_label = f"due {due[:10]}"
            else:
                due_label = "no due date"
            task_lines.append(
                f"- [{t.get('priority', 'medium').upper()}] {t['title']} ({due_label})"
            )

        task_block = "\n".join(task_lines)
        cal_block = calendar_summary or "(no events in next 8 hours)"

        prompt = f"""You are Seny — a sharp, direct personal assistant who helps the user stay ahead of their work.

It is currently {now.strftime('%A, %B %-d at %-I:%M %p')}.

Pending tasks:
{task_block}

Upcoming calendar (next 8 hours):
{cal_block}

Based on this, identify the 1–2 most important things the user should be working on RIGHT NOW. Think critically:
- What's due soon and likely hasn't been started yet?
- What high-priority item takes the most effort and should be tackled while energy is high?
- What will create the biggest problems if left any longer?

Respond with a short, direct message in Seny's voice: warm but no-nonsense, like a trusted friend with zero tolerance for avoidance. 2–4 sentences max. Be specific about WHAT to do and WHY now. No bullet points — speak to them directly."""

        try:
            client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
            response = await client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            coach_message = response.content[0].text.strip()
        except Exception as e:
            logger.error("AI coach Claude call failed for user %d: %r", self.user_id, e)
            return {'sent': False}

        if not coach_message:
            return {'sent': False}

        try:
            nudge_svc = NudgeService(self.user_id)
            await nudge_svc.send_nudge(
                nudge_type='ai_coach',
                title="🧠 Focus check",
                body=coach_message,
                urgency='normal',
                source_type='ai_coach',
                source_id=0,
            )
            logger.info("AI coach nudge sent for user %d", self.user_id)
            return {'sent': True}
        except Exception as e:
            logger.error("AI coach nudge delivery failed for user %d: %r", self.user_id, e)
            return {'sent': False}

    def _recent_ai_coach_nudge(self) -> bool:
        """Return True if an ai_coach nudge was sent within the last 2 hours."""
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT 1 FROM nudges
                    WHERE user_id = %s
                      AND nudge_type = 'ai_coach'
                      AND created_at > NOW() - INTERVAL '2 hours'
                    LIMIT 1
                """, (self.user_id,))
                return cursor.fetchone() is not None
        except Exception as e:
            logger.debug("AI coach dedup check failed: %r", e)
            return False

    def _get_all_pending_tasks(self) -> list[dict]:
        """Return all non-completed tasks ordered by priority then due date."""
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, title, priority, due_date, status
                    FROM tasks
                    WHERE user_id = %s
                      AND status NOT IN ('completed', 'cancelled')
                    ORDER BY
                        CASE priority
                            WHEN 'urgent' THEN 0
                            WHEN 'high' THEN 1
                            WHEN 'medium' THEN 2
                            WHEN 'low' THEN 3
                            ELSE 4
                        END,
                        due_date ASC
                    LIMIT 30
                """, (self.user_id,))
                return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error("Failed to fetch pending tasks for user %d: %r", self.user_id, e)
            return []

    async def _get_calendar_snapshot(self) -> str:
        """Return a plain-text summary of the next 8 hours of calendar events."""
        try:
            from web.services.calendar_service import CalendarService

            tokens = list_google_tokens(self.user_id)
            if not tokens:
                return ""

            cal_svc = CalendarService(self.user_id)
            # Fetch next day's events and filter to the 8-hour window
            events = await cal_svc.get_events(days_ahead=1, max_results=10)
            if not events:
                return ""

            now = datetime.now()
            cutoff = now + timedelta(hours=8)
            lines = []
            for ev in events:
                start = ev.get('start', '')
                summary = ev.get('summary', 'Untitled event')
                try:
                    # CalendarService returns start as a string
                    start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
                    start_naive = start_dt.replace(tzinfo=None)
                    if start_naive < now or start_naive > cutoff:
                        continue
                    label = start_naive.strftime('%-I:%M %p')
                except (ValueError, TypeError, AttributeError):
                    label = str(start)[:10]
                lines.append(f"- {label}: {summary}")
                if len(lines) >= 5:
                    break

            return "\n".join(lines)
        except Exception as e:
            logger.debug("AI coach: calendar snapshot failed: %r", e)
            return ""
