"""
Digest Service for Seny - Phase 8 (08-07, 08-08) Daily Briefings & Weekly Reviews

Generates daily digests that surface:
- Top 3 priorities (overdue items, active projects, tasks due today)
- Today's calendar events
- Relationship follow-ups (people not contacted in 30+ days)
- Stuck items (blocked projects, overdue errands)
- Recent win (something completed in last 24-48 hours)

Generates weekly reviews that surface:
- Week activity summary (tasks completed, projects started/finished)
- Open loops (stalled projects, orphaned ideas)
- AI-generated pattern analysis
- Suggested focus areas for next week
- Relationship health check
- Wins to celebrate

Usage:
    digest_service = DigestService(user_id)
    digest = await digest_service.generate_daily_digest()
    await digest_service.deliver_digest()

    weekly = await digest_service.generate_weekly_review()
    await digest_service.deliver_weekly_review()
"""

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from anthropic import AsyncAnthropic

from web.core.database import (
    get_db,
    list_google_tokens,
    get_digest_preferences,
    update_digest_preferences,
    get_weekly_review_preferences,
    update_weekly_review_preferences,
    get_needs_reply_items,
    get_pending_actions,
    resolve_entity,
    get_cross_references_for_entity,
    create_email_feedback_token,
    is_sender_ignored,
    get_user_identifiers,
    get_recent_nudges,
)
from web.api.settings import get_user_settings

logger = logging.getLogger(__name__)


class DigestService:
    """
    Daily digest generation and delivery service.

    Generates morning briefings with top priorities, calendar events,
    relationship follow-ups, and stuck items.

    Attributes:
        user_id: The user's database ID
    """

    def __init__(self, user_id: int):
        """
        Initialize Digest service for a specific user.

        Args:
            user_id: User's database ID
        """
        self.user_id = user_id

    # =========================================================================
    # Digest Generation
    # =========================================================================

    async def generate_daily_digest(self) -> dict:
        """
        Generate the daily morning briefing.

        Returns:
            Dict with date, summary, top_priorities, calendar_today,
            relationship_followups, stuck_items, and recent_win
        """
        try:
            from zoneinfo import ZoneInfo as _ZI
            _s = get_user_settings(self.user_id)
            _tz = _ZI(_s.get('digest_timezone', 'America/Chicago') if _s else 'America/Chicago')
        except Exception:
            from zoneinfo import ZoneInfo as _ZI
            _tz = _ZI('America/Chicago')
        from datetime import timezone as _utc
        today = datetime.now(_utc.utc).astimezone(_tz).strftime("%A, %B %d, %Y")

        # Gather all sections
        top_priorities = await self._get_top_priorities(limit=3)
        calendar_today = await self._get_calendar_today()
        relationship_followups = await self._get_relationship_followups(limit=3)
        stuck_items = await self._get_stuck_items()
        recent_win = await self._get_recent_win()
        needs_reply = await self._get_needs_reply(limit=5)
        detected_actions = await self._get_detected_actions(limit=5)
        unfulfilled_commitments = await self._get_unfulfilled_commitments(limit=5)

        # Generate summary
        summary = self._generate_summary(
            len(top_priorities),
            len(calendar_today),
            len(relationship_followups),
            len(stuck_items),
            len(needs_reply),
            len(detected_actions),
            len(unfulfilled_commitments)
        )

        return {
            'date': today,
            'summary': summary,
            'top_priorities': top_priorities,
            'calendar_today': calendar_today,
            'relationship_followups': relationship_followups,
            'stuck_items': stuck_items,
            'recent_win': recent_win,
            'needs_reply': needs_reply,
            'detected_actions': detected_actions,
            'unfulfilled_commitments': unfulfilled_commitments
        }

    def _generate_summary(
        self,
        priority_count: int,
        event_count: int,
        followup_count: int,
        stuck_count: int,
        needs_reply_count: int = 0,
        detected_actions_count: int = 0,
        unfulfilled_commitments_count: int = 0
    ) -> str:
        """Generate a 2-3 sentence overview of the day."""
        parts = []

        if priority_count > 0:
            parts.append(f"{priority_count} priority item{'s' if priority_count > 1 else ''}")

        if event_count > 0:
            parts.append(f"{event_count} event{'s' if event_count > 1 else ''}")

        if followup_count > 0:
            parts.append(f"{followup_count} relationship check-in{'s' if followup_count > 1 else ''}")

        if stuck_count > 0:
            parts.append(f"{stuck_count} stuck item{'s' if stuck_count > 1 else ''}")

        if not parts:
            summary = "You have a clear day ahead!"
        else:
            summary = "Today you have " + ", ".join(parts) + "."

        if stuck_count > 0:
            summary += " Some items need attention."

        # Add inbound intelligence counts
        inbound_parts = []
        if needs_reply_count > 0:
            inbound_parts.append(f"{needs_reply_count} item{'s' if needs_reply_count > 1 else ''} need{'s' if needs_reply_count == 1 else ''} your reply")
        if detected_actions_count > 0:
            inbound_parts.append(f"{detected_actions_count} action item{'s' if detected_actions_count > 1 else ''} detected")
        if inbound_parts:
            summary += " " + ", ".join(inbound_parts).capitalize() + "."

        # Add unfulfilled commitments accountability
        if unfulfilled_commitments_count > 0:
            summary += f" {unfulfilled_commitments_count} commitment{'s' if unfulfilled_commitments_count > 1 else ''} you haven't acted on yet."

        return summary

    def _get_user_email_addresses(self) -> set[str]:
        """Get all email addresses belonging to the user (for filtering own messages)."""
        emails = set()
        try:
            google_accounts = list_google_tokens(self.user_id)
            for account in google_accounts:
                email = account.get('email', '').lower()
                if email:
                    emails.add(email)
        except Exception:
            pass
        return emails

    def _get_user_display_names(self) -> set[str]:
        """Get the user's own display names (e.g. 'Your Name') for filtering action items
        that mistakenly instruct the user to respond to themselves."""
        try:
            identifiers = get_user_identifiers(self.user_id)
            return {n.lower() for n in identifiers.get('display_names', []) if n}
        except Exception:
            return set()

    def _get_user_slack_ids(self) -> set[str]:
        """Get all Slack user IDs belonging to the user (for filtering own messages)."""
        slack_ids = set()
        try:
            from web.core.database import list_slack_tokens, get_slack_token
            workspaces = list_slack_tokens(self.user_id)
            for ws in workspaces:
                team_id = ws.get('team_id')
                if team_id:
                    token_data = get_slack_token(self.user_id, team_id)
                    if token_data and token_data.get('authed_user_id'):
                        slack_ids.add(token_data['authed_user_id'])
        except Exception:
            pass
        return slack_ids

    def _is_from_user(self, metadata: dict, source_type: str, user_emails: set, user_slack_ids: set, user_telegram_ids: set = None) -> bool:
        """Check if an item is from the user themselves (should be filtered from action items)."""
        if source_type == 'gmail':
            from_field = metadata.get('from', '').lower()
            return any(email in from_field for email in user_emails)
        elif source_type == 'slack':
            sender_id = metadata.get('user_id', '')
            return sender_id in user_slack_ids
        elif source_type == 'telegram':
            # is_outgoing is the most reliable flag
            if metadata.get('is_outgoing'):
                return True
            # Fallback: check sender_id against user's known Telegram user IDs
            sender_id = str(metadata.get('sender_id', ''))
            if sender_id and user_telegram_ids and sender_id in user_telegram_ids:
                return True
        return False

    async def _get_needs_reply(self, limit: int = 5) -> list[dict]:
        """
        Get items from the last 24h that need user reply.

        Queries item_classifications for actionable items where the
        extracted_actions contain a "reply" type action.

        Returns list of dicts with source_type, sender, subject/preview,
        timestamp, and optional linked person.
        """
        results = []
        filtered_count = 0
        user_emails = self._get_user_email_addresses()
        user_slack_ids = self._get_user_slack_ids()

        try:
            since = (datetime.now() - timedelta(hours=24)).isoformat()
            # Fetch more items than needed to account for filtering
            items = get_needs_reply_items(self.user_id, since=since, limit=limit * 2)

            for item in items:
                if len(results) >= limit:
                    break

                metadata = {}
                try:
                    metadata = json.loads(item.get('source_metadata') or '{}')
                except (ValueError, TypeError):
                    pass

                # Extract sender from source metadata
                sender = metadata.get('from') or metadata.get('sender') or metadata.get('sender_name') or 'Unknown'
                source_type = item.get('source', 'unknown')

                # Filter out items FROM the user themselves (don't tell them to respond to themselves)
                if self._is_from_user(metadata, source_type, user_emails, user_slack_ids):
                    filtered_count += 1
                    continue

                # Filter out ignored senders
                sender_identifier = self._extract_sender_identifier(
                    {'sender': sender, 'source_context': sender},
                    source_type
                )
                if sender_identifier and is_sender_ignored(self.user_id, source_type, sender_identifier):
                    filtered_count += 1
                    continue

                subject = item.get('summary') or metadata.get('subject') or metadata.get('preview') or ''

                # Check for linked person via cross_references
                person_name = None
                person_id = None
                try:
                    from web.core.database import get_db as _get_db
                    with _get_db() as conn:
                        cursor = conn.cursor()
                        cursor.execute("""
                            SELECT cr.entity_id, p.name
                            FROM cross_references cr
                            JOIN people p ON cr.entity_id = p.id AND cr.entity_type = 'person'
                            WHERE cr.scanned_item_id = %s AND cr.user_id = %s
                            LIMIT 1
                        """, (item['scanned_item_id'], self.user_id))
                        row = cursor.fetchone()
                        if row:
                            person_id = row['entity_id']
                            person_name = row['name']
                except Exception:
                    pass

                results.append({
                    'source_type': source_type,
                    'sender': sender,
                    'subject': subject,
                    'received_at': item.get('detected_at'),
                    'classified_at': item.get('classified_at'),
                    'person_name': person_name,
                    'person_id': person_id,
                    'scanned_item_id': item.get('scanned_item_id'),
                })

            if filtered_count > 0:
                logger.info(f"Filtered {filtered_count} needs-reply items from ignored senders")

        except Exception as e:
            logger.error(f"Error fetching needs-reply items: {e}")

        return results

    async def _get_detected_actions(self, limit: int = 5) -> list[dict]:
        """
        Get undismissed, unpromoted detected actions from the last 24h.

        Returns list of dicts with action_text, source_type, source context,
        confidence, and optional linked person/project.
        """
        results = []
        filtered_count = 0
        user_emails = self._get_user_email_addresses()
        user_slack_ids = self._get_user_slack_ids()
        user_display_names = self._get_user_display_names()
        try:
            identifiers = get_user_identifiers(self.user_id)
            user_telegram_ids = {str(t) for t in identifiers.get('telegram_ids', []) if t}
        except Exception:
            user_telegram_ids = set()

        try:
            since = (datetime.now() - timedelta(hours=24)).isoformat()
            # Fetch more items to account for filtering
            actions = get_pending_actions(self.user_id, limit=limit * 3)

            for action in actions:
                if len(results) >= limit:
                    break

                # Filter to last 24h
                if action.get('detected_at') and action['detected_at'] < since:
                    continue

                metadata = {}
                try:
                    metadata = json.loads(action.get('source_metadata') or '{}')
                except (ValueError, TypeError):
                    pass

                # Source context: who/where it came from
                source_context = metadata.get('from') or metadata.get('sender_name') or metadata.get('channel_name') or metadata.get('subject') or ''
                source_type = action.get('source', 'unknown')

                # Filter out items FROM the user themselves (don't tell them to act on their own messages)
                if self._is_from_user(metadata, source_type, user_emails, user_slack_ids, user_telegram_ids):
                    filtered_count += 1
                    continue

                # Filter out action items that mention the user's own name as the person to respond to
                # (e.g. "Respond to Your Name" — that's always the user)
                if user_display_names:
                    action_text_lower = (action.get('action_text') or '').lower()
                    if any(name in action_text_lower for name in user_display_names):
                        filtered_count += 1
                        continue

                # Filter out ignored senders
                sender_identifier = self._extract_sender_identifier(
                    {'sender': source_context, 'source_context': source_context},
                    source_type
                )
                if sender_identifier and is_sender_ignored(self.user_id, source_type, sender_identifier):
                    filtered_count += 1
                    continue

                results.append({
                    'action_text': action.get('action_text', ''),
                    'action_type': action.get('action_type', ''),
                    'source_type': source_type,
                    'source_context': source_context,
                    'person_name': action.get('person_name'),
                    'person_id': action.get('person_id'),
                    'deadline': action.get('deadline'),
                    'detected_at': action.get('detected_at'),
                    'action_id': action.get('id'),
                })

            if filtered_count > 0:
                logger.info(f"Filtered {filtered_count} detected actions from ignored senders")

        except Exception as e:
            logger.error(f"Error fetching detected actions: {e}")

        return results

    async def _get_unfulfilled_commitments(self, limit: int = 5) -> list[dict]:
        """
        Get pending detected actions that look like personal commitments.

        Surfaces promises the user made (e.g., "I'll send you...", "Let me follow up...")
        that are still pending and older than 24 hours, giving the user time to act
        before flagging.

        Returns list of dicts with action_text, source, person_name, committed_at, days_ago.
        """
        results = []
        try:
            # Commitment patterns: things the user said they'd do
            commitment_patterns = (
                "I'll", "I will", "let me", "I need to", "I should",
                "follow up", "get back to", "I'm going to", "I'll send",
                "I'll check", "I'll look", "I'll get", "I'll do",
                "I'll make", "I'll call", "I'll email", "I'll reach"
            )

            cutoff_24h = (datetime.now() - timedelta(hours=24)).isoformat()

            # Get all pending actions (not just last 24h — we want older ones)
            all_pending = get_pending_actions(self.user_id, limit=100)

            now = datetime.now()
            for action in all_pending:
                detected_at = action.get('detected_at', '')

                # Only flag commitments older than 24 hours
                if detected_at and detected_at > cutoff_24h:
                    continue

                action_text = action.get('action_text', '').lower()

                # Check if this looks like a personal commitment
                is_commitment = any(
                    pattern.lower() in action_text
                    for pattern in commitment_patterns
                )

                if not is_commitment:
                    continue

                # Calculate age
                days_ago = None
                if detected_at:
                    try:
                        dt = datetime.fromisoformat(detected_at.replace('Z', '+00:00'))
                        days_ago = (now - dt).days
                    except (ValueError, AttributeError):
                        pass

                # Determine source context
                metadata = {}
                try:
                    metadata = json.loads(action.get('source_metadata') or '{}')
                except (ValueError, TypeError):
                    pass

                source_context = (
                    metadata.get('from') or metadata.get('sender_name') or
                    metadata.get('channel_name') or metadata.get('subject') or ''
                )

                results.append({
                    'action_text': action.get('action_text', ''),
                    'source_type': action.get('source', 'unknown'),
                    'source_context': source_context,
                    'person_name': action.get('person_name'),
                    'person_id': action.get('person_id'),
                    'committed_at': detected_at,
                    'days_ago': days_ago,
                    'action_id': action.get('id'),
                })

                if len(results) >= limit:
                    break

            # Sort by age descending (oldest unfulfilled first)
            results.sort(key=lambda x: x.get('days_ago') or 0, reverse=True)

        except Exception as e:
            logger.error(f"Error fetching unfulfilled commitments: {e}")

        return results[:limit]

    async def _get_top_priorities(self, limit: int = 3) -> list[dict]:
        """
        Get top 3 priorities across all sources.

        Priority logic:
        1. Overdue errands/tasks
        2. Active projects with next actions
        3. Tasks due today
        """
        priorities = []

        # 1. Get overdue tasks/errands
        from web.services.tasks_service import TasksService
        tasks_service = TasksService(self.user_id)
        overdue = await tasks_service.get_overdue()

        for task in overdue[:limit]:
            priorities.append({
                'source': 'errand' if task.get('type') == 'errand' else 'task',
                'title': task['title'],
                'next_action': task['title'],  # The task itself is the action
                'overdue': True,
                'due_date': task.get('due_date'),
                'id': task['id']
            })

        if len(priorities) >= limit:
            return priorities[:limit]

        # 2. Get active projects with next actions
        from web.services.projects_service import ProjectsService
        projects_service = ProjectsService(self.user_id)
        actionable = await projects_service.get_actionable_projects()

        for project in actionable:
            if len(priorities) >= limit:
                break
            if project.get('next_action'):
                priorities.append({
                    'source': 'project',
                    'title': project['name'],
                    'next_action': project['next_action'],
                    'overdue': False,
                    'id': project['id']
                })

        if len(priorities) >= limit:
            return priorities[:limit]

        # 3. Get tasks due today
        due_today = await tasks_service.get_due_today()

        for task in due_today:
            if len(priorities) >= limit:
                break
            # Skip if already in list
            if any(p.get('id') == task['id'] and p['source'] in ('task', 'errand') for p in priorities):
                continue
            priorities.append({
                'source': 'errand' if task.get('type') == 'errand' else 'task',
                'title': task['title'],
                'next_action': task['title'],
                'overdue': False,
                'due_date': task.get('due_date'),
                'id': task['id']
            })

        return priorities[:limit]

    async def _get_calendar_today(self) -> list[dict]:
        """Get today's calendar events from all connected calendars."""
        events = []

        try:
            # Get all connected Google accounts
            google_accounts = list_google_tokens(self.user_id)

            if not google_accounts:
                return events

            from web.services.calendar_service import CalendarService

            for account in google_accounts:
                email = account.get('email')
                if not email:
                    continue

                calendar_service = CalendarService(self.user_id, email)

                if not calendar_service.is_connected():
                    continue

                # Get today's events (1 day ahead)
                _tz_settings = get_user_settings(self.user_id)
                _user_tz = _tz_settings.get('digest_timezone', 'America/Chicago') if _tz_settings else 'America/Chicago'
                today_events = await calendar_service.get_all_events(
                    days_ahead=1,
                    max_results_per_calendar=10,
                    timezone=_user_tz
                )

                for event in today_events[:5]:  # Limit per account
                    event_dict = {
                        'summary': event.get('summary', 'Untitled'),
                        'start': event.get('start'),
                        'end': event.get('end'),
                        'location': event.get('location'),
                        'has_video': event.get('has_video', False),
                        'calendar_name': event.get('calendar_name'),
                        'id': event.get('id'),
                        'attendee_context': []
                    }

                    # Enrich attendees with relationship context
                    for attendee in event.get('attendees', []):
                        attendee_email = attendee.get('email', '')
                        attendee_name = attendee.get('displayName', attendee_email)
                        context_entry = {
                            'name': attendee_name,
                            'email': attendee_email,
                            'last_contact': None,
                            'recent_topic': None
                        }
                        try:
                            context_entry = self._enrich_attendee(attendee_email, attendee_name)
                        except Exception:
                            pass
                        event_dict['attendee_context'].append(context_entry)

                    events.append(event_dict)

        except Exception as e:
            logger.error(f"Error fetching calendar events: {e}")

        # Sort by start time and limit total
        events.sort(key=lambda e: e.get('start', ''))
        return events[:5]

    def _enrich_attendee(self, email: str, display_name: str) -> dict:
        """
        Look up relationship context for a calendar attendee.

        Uses entity_mappings to find linked People entry, then fetches
        last_contact_date and recent cross-references for context.

        Returns dict with name, email, last_contact, recent_topic.
        Falls back gracefully if no People match found.
        """
        result = {
            'name': display_name,
            'email': email,
            'last_contact': None,
            'recent_topic': None
        }

        try:
            # 1. Try entity_mappings lookup (email -> People)
            mapping = resolve_entity(self.user_id, 'contacts', email)
            person_id = mapping.get('person_id') if mapping else None

            # 2. If no mapping, try direct People search by email
            if not person_id:
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT id, name, last_contact_date, context
                        FROM people
                        WHERE user_id = %s AND (
                            email = %s OR email LIKE %s
                        )
                        LIMIT 1
                    """, (self.user_id, email, f'%{email}%'))
                    row = cursor.fetchone()
                    if row:
                        person_id = row['id']
                        result['name'] = row['name'] or display_name
                        result['last_contact'] = row['last_contact_date']
                        if row['context']:
                            result['recent_topic'] = row['context'][:100]

            # 3. If we found a person_id, get full context
            if person_id:
                with get_db() as conn:
                    cursor = conn.cursor()
                    # Get People entry details if not already fetched
                    if not result['last_contact']:
                        cursor.execute("""
                            SELECT name, last_contact_date, context
                            FROM people WHERE id = %s AND user_id = %s
                        """, (person_id, self.user_id))
                        person_row = cursor.fetchone()
                        if person_row:
                            result['name'] = person_row['name'] or display_name
                            result['last_contact'] = person_row['last_contact_date']
                            if person_row['context']:
                                result['recent_topic'] = person_row['context'][:100]

                    # Get recent cross-references (last 7 days) for richer context
                    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
                    refs = get_cross_references_for_entity(
                        self.user_id, 'person', person_id, limit=3
                    )
                    for ref in refs:
                        if ref.get('detected_at') and ref['detected_at'] >= week_ago:
                            # Use the source metadata for a recent topic
                            try:
                                meta = json.loads(ref.get('source_metadata') or '{}')
                                topic = meta.get('subject') or meta.get('preview') or ref.get('relationship')
                                if topic:
                                    result['recent_topic'] = str(topic)[:100]
                                    break
                            except (ValueError, TypeError):
                                pass

        except Exception as e:
            logger.debug(f"Attendee enrichment failed for {email}: {e}")

        return result

    async def _get_relationship_followups(self, limit: int = 3) -> list[dict]:
        """Get pending follow-ups, prioritized by staleness."""
        followups = []

        try:
            from web.services.people_service import PeopleService
            people_service = PeopleService(self.user_id)

            # Get stale relationships (haven't contacted in 30+ days)
            stale = await people_service.get_stale_relationships(days=30)

            for person in stale[:limit]:
                # Get any pending follow-ups for this person
                pending = await people_service.get_followups(person['id'], include_completed=False)
                followup_content = pending[0]['content'] if pending else None

                followups.append({
                    'person': person['name'],
                    'person_id': person['id'],
                    'days_since_contact': person.get('days_since_contact'),
                    'followup': followup_content,
                    'context': person.get('context')
                })

        except Exception as e:
            logger.error(f"Error fetching relationship followups: {e}")

        return followups[:limit]

    async def _get_stuck_items(self) -> list[dict]:
        """Get blocked projects and overdue errands."""
        stuck_items = []

        try:
            # Get blocked projects
            from web.services.projects_service import ProjectsService
            projects_service = ProjectsService(self.user_id)
            stuck_projects = await projects_service.get_stuck_projects()

            for project in stuck_projects[:3]:
                reason = "No next action defined"
                if project.get('status') == 'blocked':
                    reason = "Marked as blocked"
                elif project.get('status') == 'waiting':
                    reason = "Waiting on external"

                stuck_items.append({
                    'type': 'project',
                    'title': project['name'],
                    'reason': reason,
                    'id': project['id']
                })

            # Get significantly overdue errands (more than 3 days)
            from web.services.tasks_service import TasksService
            tasks_service = TasksService(self.user_id)
            overdue = await tasks_service.get_overdue(task_type='errand')

            cutoff = (datetime.now() - timedelta(days=3)).isoformat()

            for task in overdue:
                if task.get('due_date') and task['due_date'] < cutoff:
                    if len(stuck_items) >= 5:
                        break
                    stuck_items.append({
                        'type': 'errand',
                        'title': task['title'],
                        'reason': 'Overdue by 3+ days',
                        'id': task['id']
                    })

        except Exception as e:
            logger.error(f"Error fetching stuck items: {e}")

        return stuck_items[:5]

    async def _get_recent_win(self) -> Optional[str]:
        """Find something completed in last 24-48 hours to celebrate."""
        try:
            # Check for recently completed projects
            from web.services.projects_service import ProjectsService
            projects_service = ProjectsService(self.user_id)
            recent_projects = await projects_service.get_recently_completed(days=2)

            if recent_projects:
                return f"Completed project: {recent_projects[0]['name']}"

            # Check for recently completed tasks
            from web.services.tasks_service import TasksService
            tasks_service = TasksService(self.user_id)

            cutoff = datetime.now() - timedelta(days=2)

            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT title, completed_at, type FROM tasks
                    WHERE user_id = %s AND status = 'completed'
                    AND completed_at >= %s
                    ORDER BY completed_at DESC
                    LIMIT 1
                """, (self.user_id, cutoff.isoformat()))

                row = cursor.fetchone()
                if row:
                    item_type = "errand" if row['type'] == 'errand' else "task"
                    return f"Completed {item_type}: {row['title']}"

        except Exception as e:
            logger.error(f"Error fetching recent win: {e}")

        return None

    # =========================================================================
    # Email Feedback Links
    # =========================================================================

    def _get_base_url(self) -> str:
        """Get the base URL for feedback links."""
        # Check for Railway production domain
        railway_domain = os.environ.get('RAILWAY_PUBLIC_DOMAIN')
        if railway_domain:
            return f"https://{railway_domain}"

        # Fallback to APP_URL env var or localhost
        app_url = os.environ.get('APP_URL', 'http://localhost:8000')
        return app_url.rstrip('/')

    def _generate_feedback_link(
        self,
        item_type: str,
        feedback_action: str,
        item_id: Optional[int] = None,
        scanned_item_id: Optional[int] = None,
        sender_identifier: Optional[str] = None,
        source_type: Optional[str] = None
    ) -> Optional[str]:
        """
        Generate a secure feedback link for an item in the digest email.

        Args:
            item_type: Type of item ('needs_reply', 'detected_action', etc.)
            feedback_action: Feedback action ('not_helpful', 'ignore_sender')
            item_id: Optional item ID
            scanned_item_id: Optional scanned_item ID
            sender_identifier: Optional sender email/ID for ignore_sender
            source_type: Optional source type (gmail, slack, telegram)

        Returns:
            Full URL for the feedback link, or None on error
        """
        token = create_email_feedback_token(
            user_id=self.user_id,
            item_type=item_type,
            feedback_action=feedback_action,
            item_id=item_id,
            scanned_item_id=scanned_item_id,
            sender_identifier=sender_identifier,
            source_type=source_type
        )

        if not token:
            logger.warning("Failed to create feedback token for %s", item_type)
            return None

        base_url = self._get_base_url()
        return f"{base_url}/api/feedback/email/{token}"

    def _extract_sender_identifier(self, item: dict, source_type: str) -> Optional[str]:
        """
        Extract sender identifier from an item based on source type.

        Args:
            item: Item dict with sender, source_context, or similar fields
            source_type: Source type (gmail, slack, telegram)

        Returns:
            Sender identifier string or None
        """
        if source_type == 'gmail':
            # Email address from 'sender', 'from', or 'source_context' field
            return item.get('sender') or item.get('from') or item.get('source_context')
        elif source_type == 'slack':
            # channel_id + sender_id or just channel for channel messages
            sender_name = item.get('sender') or item.get('source_context', '')
            channel = item.get('channel_id', '')
            if channel:
                return f"{channel}:{sender_name}"
            return sender_name
        elif source_type == 'telegram':
            # chat_id + sender
            sender_name = item.get('sender') or item.get('source_context', '')
            chat_id = item.get('chat_id', '')
            if chat_id:
                return f"{chat_id}:{sender_name}"
            return sender_name
        else:
            return item.get('sender') or item.get('source_context')

    def _generate_feedback_links_html(
        self,
        item_type: str,
        item_id: Optional[int] = None,
        scanned_item_id: Optional[int] = None,
        sender_identifier: Optional[str] = None,
        source_type: Optional[str] = None,
        include_ignore_sender: bool = True
    ) -> str:
        """
        Generate HTML for feedback links (Not useful | Ignore sender).

        Args:
            item_type: Type of item
            item_id: Optional item ID
            scanned_item_id: Optional scanned_item ID
            sender_identifier: Optional sender for ignore_sender link
            source_type: Optional source type
            include_ignore_sender: Whether to include "Ignore sender" link

        Returns:
            HTML string with feedback links
        """
        links = []

        # "Not useful" link
        not_useful_url = self._generate_feedback_link(
            item_type=item_type,
            feedback_action='not_helpful',
            item_id=item_id,
            scanned_item_id=scanned_item_id,
            sender_identifier=sender_identifier,
            source_type=source_type
        )
        if not_useful_url:
            links.append(f'<a href="{not_useful_url}" style="color: #999; text-decoration: none; font-size: 11px;">Not useful</a>')

        # "Ignore sender" link (only if we have sender info)
        if include_ignore_sender and sender_identifier and source_type:
            ignore_url = self._generate_feedback_link(
                item_type=item_type,
                feedback_action='ignore_sender',
                item_id=item_id,
                scanned_item_id=scanned_item_id,
                sender_identifier=sender_identifier,
                source_type=source_type
            )
            if ignore_url:
                links.append(f'<a href="{ignore_url}" style="color: #999; text-decoration: none; font-size: 11px;">Ignore sender</a>')

        if links:
            return f'<span style="margin-left: 8px; color: #ccc;">[ {" | ".join(links)} ]</span>'
        return ''

    # =========================================================================
    # Digest Formatting
    # =========================================================================

    def format_digest_text(self, digest: dict) -> str:
        """Format digest as plain text for email/display."""
        lines = []

        # Header
        lines.append(f"Good morning! Here's your briefing for {digest['date']}:")
        lines.append("")
        lines.append(digest['summary'])
        lines.append("")

        # Top Priorities
        if digest['top_priorities']:
            lines.append("TOP PRIORITIES")
            lines.append("-" * 20)
            for i, p in enumerate(digest['top_priorities'], 1):
                overdue_marker = " (overdue!)" if p.get('overdue') else ""
                lines.append(f"{i}. {p['next_action']}{overdue_marker}")
                if p['source'] == 'project':
                    lines.append(f"   Project: {p['title']}")
            lines.append("")

        # Calendar
        if digest['calendar_today']:
            lines.append("TODAY'S CALENDAR")
            lines.append("-" * 20)
            for event in digest['calendar_today']:
                start = event.get('start', '')
                if 'T' in start:
                    # Parse time from ISO format
                    try:
                        dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
                        time_str = dt.strftime("%-I:%M %p")
                    except:
                        time_str = start
                else:
                    time_str = "All day"
                lines.append(f"* {time_str} - {event['summary']}")
                # Attendee context sub-lines
                for att in event.get('attendee_context', []):
                    if att.get('last_contact') or att.get('recent_topic'):
                        contact_str = f"last contact {att['last_contact']}" if att.get('last_contact') else "no prior contact"
                        topic_str = f", recent: {att['recent_topic']}" if att.get('recent_topic') else ""
                        lines.append(f"  \U0001f464 {att['name']}: {contact_str}{topic_str}")
            lines.append("")

        # Needs Your Reply
        needs_reply = digest.get('needs_reply', [])
        if needs_reply:
            lines.append("\U0001f4ec NEEDS YOUR REPLY")
            lines.append("-" * 20)
            for item in needs_reply:
                person_link = f" (linked: {item['person_name']})" if item.get('person_name') else ""
                lines.append(f"* From: {item['sender']} | {item['source_type']} | {item['subject']}{person_link}")
        else:
            lines.append("\U0001f4ec NEEDS YOUR REPLY")
            lines.append("-" * 20)
            lines.append("Nothing detected")
        lines.append("")

        # Detected Actions
        detected_actions = digest.get('detected_actions', [])
        if detected_actions:
            lines.append("\u26a1 DETECTED ACTIONS")
            lines.append("-" * 20)
            for item in detected_actions:
                source = item.get('source_context') or item.get('source_type', 'unknown')
                lines.append(f"* Action: {item['action_text']} | From: {source} | Type: {item.get('action_type', '')}")
        else:
            lines.append("\u26a1 DETECTED ACTIONS")
            lines.append("-" * 20)
            lines.append("Nothing detected")
        lines.append("")

        # Unfulfilled Commitments
        commitments = digest.get('unfulfilled_commitments', [])
        if commitments:
            lines.append("\U0001f514 UNFULFILLED COMMITMENTS")
            lines.append("-" * 20)
            for item in commitments:
                days_str = f"{item['days_ago']} days ago" if item.get('days_ago') else "recently"
                source = item.get('source_type', 'unknown')
                lines.append(f'* You said: "{item["action_text"]}" ({days_str}, via {source})')
                if item.get('person_name'):
                    lines.append(f"  \u2192 To: {item['person_name']}")
        else:
            lines.append("\U0001f514 UNFULFILLED COMMITMENTS")
            lines.append("-" * 20)
            lines.append("Nothing detected")
        lines.append("")

        # Relationship Follow-ups
        if digest['relationship_followups']:
            lines.append("RELATIONSHIP CHECK-INS")
            lines.append("-" * 20)
            for f in digest['relationship_followups']:
                days = f.get('days_since_contact')
                days_str = f" ({days} days)" if days else ""
                lines.append(f"* {f['person']}{days_str}")
                if f.get('followup'):
                    lines.append(f"  Follow up: {f['followup']}")
            lines.append("")

        # Stuck Items
        if digest['stuck_items']:
            lines.append("STUCK ITEMS")
            lines.append("-" * 20)
            for item in digest['stuck_items']:
                lines.append(f"* {item['title']} - {item['reason']}")
            lines.append("")

        # Recent Win
        if digest['recent_win']:
            lines.append("RECENT WIN")
            lines.append("-" * 20)
            lines.append(f"* {digest['recent_win']}")
            lines.append("")

        return "\n".join(lines)

    def format_digest_html(self, digest: dict) -> str:
        """Format digest as HTML for email."""
        html = []

        # Header
        html.append(f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <h1 style="color: #333; font-size: 24px; margin-bottom: 10px;">Good morning!</h1>
            <p style="color: #666; font-size: 16px; margin-bottom: 20px;">Here's your briefing for {digest['date']}</p>
            <p style="color: #444; font-size: 14px; background: #f5f5f5; padding: 12px; border-radius: 6px;">{digest['summary']}</p>
        """)

        # Top Priorities
        if digest['top_priorities']:
            html.append("""
            <h2 style="color: #333; font-size: 18px; margin-top: 24px; border-bottom: 2px solid #4a90d9; padding-bottom: 8px;">
                Top Priorities
            </h2>
            <ol style="padding-left: 20px;">
            """)
            for p in digest['top_priorities']:
                overdue_style = "color: #e74c3c; font-weight: bold;" if p.get('overdue') else ""
                html.append(f"""
                <li style="margin-bottom: 12px; {overdue_style}">
                    {p['next_action']}
                    {' (overdue!)' if p.get('overdue') else ''}
                    {f"<br><span style='color: #888; font-size: 12px;'>Project: {p['title']}</span>" if p['source'] == 'project' else ''}
                </li>
                """)
            html.append("</ol>")

        # Calendar
        if digest['calendar_today']:
            html.append("""
            <h2 style="color: #333; font-size: 18px; margin-top: 24px; border-bottom: 2px solid #4a90d9; padding-bottom: 8px;">
                Today's Calendar
            </h2>
            <ul style="list-style: none; padding: 0;">
            """)
            for event in digest['calendar_today']:
                start = event.get('start', '')
                if 'T' in start:
                    try:
                        dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
                        time_str = dt.strftime("%-I:%M %p")
                    except:
                        time_str = start
                else:
                    time_str = "All day"
                att_html = ""
                for att in event.get('attendee_context', []):
                    if att.get('last_contact') or att.get('recent_topic'):
                        contact_str = f"last contact {att['last_contact']}" if att.get('last_contact') else "no prior contact"
                        topic_str = f", recent: {att['recent_topic']}" if att.get('recent_topic') else ""
                        att_html += f"<br><span style='color: #888; font-size: 12px;'>\U0001f464 {att['name']}: {contact_str}{topic_str}</span>"
                html.append(f"""
                <li style="margin-bottom: 8px; padding: 8px; background: #f9f9f9; border-radius: 4px;">
                    <span style="color: #4a90d9; font-weight: bold;">{time_str}</span> - {event['summary']}{att_html}
                </li>
                """)
            html.append("</ul>")

        # Needs Your Reply
        needs_reply = digest.get('needs_reply', [])
        html.append("""
            <h2 style="color: #333; font-size: 18px; margin-top: 24px; border-bottom: 2px solid #3498db; padding-bottom: 8px;">
                \U0001f4ec Needs Your Reply
            </h2>
        """)
        if needs_reply:
            html.append("<ul style='list-style: none; padding: 0;'>")
            for item in needs_reply:
                person_link = f" <span style='color: #888;'>(linked: {item['person_name']})</span>" if item.get('person_name') else ""
                # Generate feedback links
                source_type = item.get('source_type', 'unknown')
                sender_id = self._extract_sender_identifier(item, source_type)
                feedback_links = self._generate_feedback_links_html(
                    item_type='needs_reply',
                    scanned_item_id=item.get('scanned_item_id'),
                    sender_identifier=sender_id,
                    source_type=source_type,
                    include_ignore_sender=bool(sender_id)
                )
                html.append(f"""
                <li style="margin-bottom: 8px; padding: 8px; background: #eef6ff; border-radius: 4px; border-left: 3px solid #3498db;">
                    <strong>From: {item['sender']}</strong> | {item['source_type']}{person_link}
                    <br><span style="color: #666; font-size: 13px;">{item['subject']}</span>
                    <br>{feedback_links}
                </li>
                """)
            html.append("</ul>")
        else:
            html.append("<p style='color: #888; font-size: 13px; padding: 8px;'>Nothing detected</p>")

        # Detected Actions
        detected_actions = digest.get('detected_actions', [])
        html.append("""
            <h2 style="color: #333; font-size: 18px; margin-top: 24px; border-bottom: 2px solid #f39c12; padding-bottom: 8px;">
                \u26a1 Detected Actions
            </h2>
        """)
        if detected_actions:
            html.append("<ul style='list-style: none; padding: 0;'>")
            for item in detected_actions:
                source = item.get('source_context') or item.get('source_type', 'unknown')
                # Generate feedback links
                source_type = item.get('source_type', 'unknown')
                sender_id = self._extract_sender_identifier(item, source_type)
                feedback_links = self._generate_feedback_links_html(
                    item_type='detected_action',
                    item_id=item.get('action_id'),
                    sender_identifier=sender_id,
                    source_type=source_type,
                    include_ignore_sender=bool(sender_id)
                )
                html.append(f"""
                <li style="margin-bottom: 8px; padding: 8px; background: #fef9e7; border-radius: 4px; border-left: 3px solid #f39c12;">
                    <strong>{item['action_text']}</strong>
                    <br><span style="color: #888; font-size: 12px;">From: {source} | Type: {item.get('action_type', '')}</span>
                    <br>{feedback_links}
                </li>
                """)
            html.append("</ul>")
        else:
            html.append("<p style='color: #888; font-size: 13px; padding: 8px;'>Nothing detected</p>")

        # Unfulfilled Commitments
        commitments = digest.get('unfulfilled_commitments', [])
        html.append("""
            <h2 style="color: #e67e22; font-size: 18px; margin-top: 24px; border-bottom: 2px solid #e67e22; padding-bottom: 8px;">
                \U0001f514 Unfulfilled Commitments
            </h2>
        """)
        if commitments:
            html.append("<ul style='list-style: none; padding: 0;'>")
            for item in commitments:
                days_str = f"{item['days_ago']} days ago" if item.get('days_ago') else "recently"
                source = item.get('source_type', 'unknown')
                person_line = f"<br><span style='color: #e67e22;'>\u2192 To: {item['person_name']}</span>" if item.get('person_name') else ""
                # Generate feedback links (no ignore sender for commitments - user made them)
                feedback_links = self._generate_feedback_links_html(
                    item_type='unfulfilled_commitment',
                    item_id=item.get('action_id'),
                    source_type=source,
                    include_ignore_sender=False  # No sender to ignore for commitments
                )
                html.append(f"""
                <li style="margin-bottom: 8px; padding: 8px; background: #fdf2e9; border-radius: 4px; border-left: 3px solid #e67e22;">
                    You said: "<em>{item['action_text']}</em>" ({days_str}, via {source}){person_line}
                    <br>{feedback_links}
                </li>
                """)
            html.append("</ul>")
        else:
            html.append("<p style='color: #888; font-size: 13px; padding: 8px;'>Nothing detected</p>")

        # Relationship Follow-ups
        if digest['relationship_followups']:
            html.append("""
            <h2 style="color: #333; font-size: 18px; margin-top: 24px; border-bottom: 2px solid #4a90d9; padding-bottom: 8px;">
                Relationship Check-ins
            </h2>
            <ul style="list-style: none; padding: 0;">
            """)
            for f in digest['relationship_followups']:
                days = f.get('days_since_contact')
                days_str = f" <span style='color: #888;'>({days} days)</span>" if days else ""
                html.append(f"""
                <li style="margin-bottom: 8px; padding: 8px; background: #f9f9f9; border-radius: 4px;">
                    <strong>{f['person']}</strong>{days_str}
                    {f"<br><span style='color: #666; font-size: 13px;'>{f['followup']}</span>" if f.get('followup') else ''}
                </li>
                """)
            html.append("</ul>")

        # Stuck Items
        if digest['stuck_items']:
            html.append("""
            <h2 style="color: #e74c3c; font-size: 18px; margin-top: 24px; border-bottom: 2px solid #e74c3c; padding-bottom: 8px;">
                Stuck Items
            </h2>
            <ul style="list-style: none; padding: 0;">
            """)
            for item in digest['stuck_items']:
                html.append(f"""
                <li style="margin-bottom: 8px; padding: 8px; background: #fef5f5; border-radius: 4px; border-left: 3px solid #e74c3c;">
                    <strong>{item['title']}</strong> - <span style="color: #888;">{item['reason']}</span>
                </li>
                """)
            html.append("</ul>")

        # Recent Win
        if digest['recent_win']:
            html.append(f"""
            <h2 style="color: #27ae60; font-size: 18px; margin-top: 24px; border-bottom: 2px solid #27ae60; padding-bottom: 8px;">
                Recent Win
            </h2>
            <p style="padding: 12px; background: #f0fff4; border-radius: 4px; border-left: 3px solid #27ae60;">
                {digest['recent_win']}
            </p>
            """)

        # Footer
        html.append("""
            <p style="color: #888; font-size: 12px; margin-top: 30px; text-align: center; border-top: 1px solid #eee; padding-top: 15px;">
                Sent by Seny - Your Personal AI Assistant
            </p>
        </div>
        """)

        return "".join(html)

    def format_digest_summary(self, digest: dict) -> str:
        """Format short summary for push notification (max 100 chars)."""
        parts = []

        priority_count = len(digest['top_priorities'])
        event_count = len(digest['calendar_today'])
        followup_count = len(digest['relationship_followups'])
        reply_count = len(digest.get('needs_reply', []))
        action_count = len(digest.get('detected_actions', []))
        commitment_count = len(digest.get('unfulfilled_commitments', []))

        if priority_count > 0:
            parts.append(f"{priority_count} priorities")

        if event_count > 0:
            parts.append(f"{event_count} events")

        if reply_count > 0:
            parts.append(f"{reply_count} replies needed")

        if action_count > 0:
            parts.append(f"{action_count} actions")

        if commitment_count > 0:
            parts.append(f"{commitment_count} commitments")

        if followup_count > 0:
            parts.append(f"{followup_count} check-ins")

        if not parts:
            return "You have a clear day ahead!"

        summary = ", ".join(parts)

        # Truncate if too long
        if len(summary) > 95:
            summary = summary[:92] + "..."

        return summary

    # =========================================================================
    # Digest Delivery
    # =========================================================================

    async def send_digest_email(self, digest: dict) -> bool:
        """
        Send digest via Gmail to self.

        Args:
            digest: Generated digest dict

        Returns:
            True if sent successfully
        """
        try:
            # Get user's connected Gmail account
            google_accounts = list_google_tokens(self.user_id)

            if not google_accounts:
                logger.warning(f"No Gmail accounts for user {self.user_id} - cannot send digest email")
                return False

            # Use the first connected account
            email = google_accounts[0].get('email')
            if not email:
                return False

            from web.services.gmail_service import GmailService
            gmail_service = GmailService(self.user_id, email)

            if not gmail_service.is_connected():
                logger.warning(f"Gmail not connected for user {self.user_id}")
                return False

            html_content = self.format_digest_html(digest)
            text_content = self.format_digest_text(digest)

            result = await gmail_service.send_email(
                to=email,  # Send to self
                subject=f"Your Daily Briefing - {digest['date']}",
                body=text_content,
                html_body=html_content
            )

            if result and not result.get('error'):
                logger.info(f"Sent digest email to {email}")
                return True

            return False

        except Exception as e:
            logger.error(f"Error sending digest email: {e}")
            return False

    async def send_digest_push(self, digest: dict) -> bool:
        """
        Send digest summary via push notification.

        Args:
            digest: Generated digest dict

        Returns:
            True if sent successfully
        """
        try:
            from web.services.notification_service import NotificationService
            notification_service = NotificationService(self.user_id)

            summary = self.format_digest_summary(digest)

            result = await notification_service.send_notification(
                title="Good Morning!",
                body=summary,
                url="/",  # Deep link to app
                notification_type="digest"
            )

            if result.get('sent', 0) > 0:
                logger.info(f"Sent digest push notification for user {self.user_id}")
                return True

            return False

        except Exception as e:
            logger.error(f"Error sending digest push: {e}")
            return False

    async def deliver_digest(self) -> dict:
        """
        Generate and deliver digest based on user preferences.

        Returns:
            Dict with generated, email_sent, push_sent flags
        """
        prefs = get_digest_preferences(self.user_id)

        if not prefs.get('digest_enabled', True):
            return {'generated': False, 'reason': 'disabled'}

        digest = await self.generate_daily_digest()

        result = {
            'generated': True,
            'email_sent': False,
            'push_sent': False,
            'digest': digest
        }

        if prefs.get('digest_email', True):
            result['email_sent'] = await self.send_digest_email(digest)

        if prefs.get('digest_push', True):
            result['push_sent'] = await self.send_digest_push(digest)

        logger.info(f"Delivered digest for user {self.user_id}: email={result['email_sent']}, push={result['push_sent']}")

        return result

    # =========================================================================
    # Weekly Review Generation
    # =========================================================================

    async def generate_weekly_review(self) -> dict:
        """
        Generate the weekly review (typically for Sunday).

        Returns comprehensive week summary with AI-generated insights:
        - week_of: Date range string
        - summary: 2-3 sentence overview
        - what_happened: Metrics and lists of completed work
        - open_loops: Items needing attention
        - patterns_noticed: AI-generated observations
        - suggested_focus: 3 focus areas for next week
        - relationships: Contact status
        - wins_to_celebrate: Recent accomplishments
        """
        # Calculate week date range
        now = datetime.now()
        week_end = now
        week_start = now - timedelta(days=7)
        week_of = f"{week_start.strftime('%b %d')} - {week_end.strftime('%b %d, %Y')}"

        # Gather all data
        activity = await self._analyze_week_activity()
        open_loops = await self._find_open_loops()
        relationships = await self._analyze_relationships()
        wins = await self._get_wins_to_celebrate()
        cross_source_connections = await self._get_cross_source_connections(limit=5)

        # Generate AI insights
        patterns = await self._generate_patterns(activity)
        focus_areas = await self._suggest_focus_areas(activity, open_loops, relationships)

        # Generate summary
        summary = self._generate_weekly_summary(activity)

        return {
            'week_of': week_of,
            'summary': summary,
            'what_happened': activity,
            'open_loops': open_loops,
            'cross_source_connections': cross_source_connections,
            'patterns_noticed': patterns,
            'suggested_focus': focus_areas,
            'relationships': relationships,
            'wins_to_celebrate': wins
        }

    def _generate_weekly_summary(self, activity: dict) -> str:
        """Generate 2-3 sentence week overview."""
        parts = []

        tasks_done = activity.get('tasks_completed', 0)
        errands_done = activity.get('errands_completed', 0)
        projects_completed = len(activity.get('projects_completed', []))
        projects_started = len(activity.get('projects_started', []))
        people_contacted = len(activity.get('people_contacted', []))
        ideas_captured = activity.get('ideas_captured', 0)

        if tasks_done + errands_done > 0:
            total_done = tasks_done + errands_done
            parts.append(f"You completed {total_done} item{'s' if total_done != 1 else ''}")

        if projects_completed > 0:
            parts.append(f"finished {projects_completed} project{'s' if projects_completed != 1 else ''}")

        if projects_started > 0:
            parts.append(f"started {projects_started} new project{'s' if projects_started != 1 else ''}")

        if people_contacted > 0:
            parts.append(f"connected with {people_contacted} {'people' if people_contacted != 1 else 'person'}")

        if ideas_captured > 0:
            parts.append(f"captured {ideas_captured} idea{'s' if ideas_captured != 1 else ''}")

        if not parts:
            return "A quiet week with space for reflection."

        summary = parts[0].capitalize()
        if len(parts) > 1:
            summary += ", " + ", ".join(parts[1:])
        summary += "."

        return summary

    async def _analyze_week_activity(self) -> dict:
        """Get activity metrics for the past 7 days."""
        cutoff = (datetime.now() - timedelta(days=7)).isoformat()

        result = {
            'projects_completed': [],
            'projects_started': [],
            'tasks_completed': 0,
            'errands_completed': 0,
            'people_contacted': [],
            'ideas_captured': 0,
            'completion_by_day': {}  # day -> count for pattern analysis
        }

        with get_db() as conn:
            cursor = conn.cursor()

            # Projects completed this week (projects table has no completed_at, use updated_at)
            cursor.execute("""
                SELECT name, updated_at FROM projects
                WHERE user_id = %s AND status = 'completed'
                AND updated_at >= %s
                ORDER BY updated_at DESC
            """, (self.user_id, cutoff))
            result['projects_completed'] = [row['name'] for row in cursor.fetchall()]

            # Projects started this week
            cursor.execute("""
                SELECT name, created_at FROM projects
                WHERE user_id = %s AND created_at >= %s
                ORDER BY created_at DESC
            """, (self.user_id, cutoff))
            result['projects_started'] = [row['name'] for row in cursor.fetchall()]

            # Tasks completed (type = task)
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM tasks
                WHERE user_id = %s AND status = 'completed'
                AND type = 'task' AND completed_at >= %s
            """, (self.user_id, cutoff))
            row = cursor.fetchone()
            result['tasks_completed'] = row['cnt'] if row else 0

            # Errands completed (type = errand)
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM tasks
                WHERE user_id = %s AND status = 'completed'
                AND type = 'errand' AND completed_at >= %s
            """, (self.user_id, cutoff))
            row = cursor.fetchone()
            result['errands_completed'] = row['cnt'] if row else 0

            # Task completions by day of week
            cursor.execute("""
                SELECT completed_at FROM tasks
                WHERE user_id = %s AND status = 'completed'
                AND completed_at >= %s
            """, (self.user_id, cutoff))
            for row in cursor.fetchall():
                if row['completed_at']:
                    try:
                        dt = datetime.fromisoformat(row['completed_at'].replace('Z', '+00:00'))
                        day_name = dt.strftime('%A')
                        result['completion_by_day'][day_name] = result['completion_by_day'].get(day_name, 0) + 1
                    except (ValueError, AttributeError):
                        pass

            # People contacted this week
            cursor.execute("""
                SELECT name FROM people
                WHERE user_id = %s AND last_contact_date >= %s
                ORDER BY last_contact_date DESC
            """, (self.user_id, cutoff))
            result['people_contacted'] = [row['name'] for row in cursor.fetchall()]

            # Ideas captured this week
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM ideas
                WHERE user_id = %s AND created_at >= %s
            """, (self.user_id, cutoff))
            row = cursor.fetchone()
            result['ideas_captured'] = row['cnt'] if row else 0

        return result

    async def _find_open_loops(self) -> list:
        """
        Find items that have been open "too long":
        - Projects with no activity in 14+ days
        - Errands with no due date and 7+ days old
        - Follow-ups pending for 30+ days
        - Ideas with no tags (orphaned)

        Each open loop is enriched with cross-reference data:
        - related_activity: recent cross-references mentioning the same entity
        - linked_person: name of linked Person if applicable
        """
        open_loops = []
        now = datetime.now()

        with get_db() as conn:
            cursor = conn.cursor()

            # Stalled projects (active but no update in 14+ days)
            stalled_cutoff = (now - timedelta(days=14)).isoformat()
            cursor.execute("""
                SELECT id, name, updated_at FROM projects
                WHERE user_id = %s AND status = 'active'
                AND updated_at < %s
                ORDER BY updated_at ASC
                LIMIT 5
            """, (self.user_id, stalled_cutoff))

            for row in cursor.fetchall():
                try:
                    updated = datetime.fromisoformat(row['updated_at'].replace('Z', '+00:00'))
                    age_days = (now - updated).days
                except (ValueError, AttributeError):
                    age_days = 14

                open_loops.append({
                    'type': 'project',
                    'title': row['name'],
                    'age_days': age_days,
                    'suggested_action': 'Schedule time to move forward or mark as paused',
                    'entity_type': 'project',
                    'entity_id': row['id'],
                    'person_id': None,
                    'related_activity': [],
                    'linked_person': None
                })

            # Old errands without due dates
            errand_cutoff = (now - timedelta(days=7)).isoformat()
            cursor.execute("""
                SELECT id, title, created_at FROM tasks
                WHERE user_id = %s AND type = 'errand'
                AND status = 'pending' AND due_date IS NULL
                AND created_at < %s
                ORDER BY created_at ASC
                LIMIT 5
            """, (self.user_id, errand_cutoff))

            for row in cursor.fetchall():
                try:
                    created = datetime.fromisoformat(row['created_at'].replace('Z', '+00:00'))
                    age_days = (now - created).days
                except (ValueError, AttributeError):
                    age_days = 7

                open_loops.append({
                    'type': 'errand',
                    'title': row['title'],
                    'age_days': age_days,
                    'suggested_action': 'Set a due date or complete today',
                    'entity_type': 'task',
                    'entity_id': row['id'],
                    'person_id': None,
                    'related_activity': [],
                    'linked_person': None
                })

            # Old pending follow-ups
            followup_cutoff = (now - timedelta(days=30)).isoformat()
            cursor.execute("""
                SELECT pf.id, pf.content, pf.person_id, p.name as person_name, pf.created_at
                FROM people_followups pf
                JOIN people p ON pf.person_id = p.id
                WHERE p.user_id = %s AND pf.status = 'active'
                AND pf.created_at < %s
                ORDER BY pf.created_at ASC
                LIMIT 5
            """, (self.user_id, followup_cutoff))

            for row in cursor.fetchall():
                try:
                    created = datetime.fromisoformat(row['created_at'].replace('Z', '+00:00'))
                    age_days = (now - created).days
                except (ValueError, AttributeError):
                    age_days = 30

                open_loops.append({
                    'type': 'followup',
                    'title': f"Follow up with {row['person_name']}: {row['content'][:50]}",
                    'age_days': age_days,
                    'suggested_action': 'Reach out or mark as no longer needed',
                    'entity_type': 'person',
                    'entity_id': row['person_id'],
                    'person_id': row['person_id'],
                    'related_activity': [],
                    'linked_person': row['person_name']
                })

            # Orphaned ideas (no tags)
            cursor.execute("""
                SELECT id, title FROM ideas
                WHERE user_id = %s
                AND (tags IS NULL OR tags = '')
                ORDER BY created_at DESC
                LIMIT 3
            """, (self.user_id,))

            for row in cursor.fetchall():
                open_loops.append({
                    'type': 'idea',
                    'title': row['title'],
                    'age_days': None,
                    'suggested_action': 'Add tags to connect to other ideas',
                    'entity_type': 'idea',
                    'entity_id': row['id'],
                    'person_id': None,
                    'related_activity': [],
                    'linked_person': None
                })

        # Enrich open loops with cross-reference activity
        self._enrich_open_loops_with_cross_refs(open_loops)

        return open_loops

    def _enrich_open_loops_with_cross_refs(self, open_loops: list) -> None:
        """
        Enrich each open loop with recent cross-reference activity.

        For items linked to a Person or Project, finds recent scanned_items
        that reference the same entity in the last 7 days.
        """
        week_ago = (datetime.now() - timedelta(days=7)).isoformat()

        for loop in open_loops:
            entity_type = loop.get('entity_type')
            entity_id = loop.get('entity_id')

            if not entity_type or not entity_id:
                continue

            try:
                refs = get_cross_references_for_entity(
                    self.user_id, entity_type, entity_id, limit=3
                )

                for ref in refs:
                    detected_at = ref.get('detected_at', '')
                    if detected_at and detected_at >= week_ago:
                        preview = ''
                        try:
                            meta = json.loads(ref.get('source_metadata') or '{}')
                            preview = (
                                meta.get('subject') or
                                meta.get('preview') or
                                ref.get('relationship') or
                                ''
                            )
                        except (ValueError, TypeError):
                            preview = ref.get('relationship', '')

                        loop['related_activity'].append({
                            'source_type': ref.get('source', 'unknown'),
                            'preview': str(preview)[:120],
                            'date': detected_at
                        })

                # If entity is a person, set linked_person name
                if entity_type == 'person' and not loop.get('linked_person'):
                    try:
                        with get_db() as conn:
                            cursor = conn.cursor()
                            cursor.execute(
                                "SELECT name FROM people WHERE id = %s AND user_id = %s",
                                (entity_id, self.user_id)
                            )
                            row = cursor.fetchone()
                            if row:
                                loop['linked_person'] = row['name']
                    except Exception:
                        pass

            except Exception as e:
                logger.debug(f"Cross-ref enrichment failed for {entity_type}/{entity_id}: {e}")

    async def _get_cross_source_connections(self, limit: int = 5) -> list[dict]:
        """
        Find entities (People/Projects/Ideas) that appeared across multiple
        data sources this week. Surfaces connections like "Sarah was mentioned
        in Gmail, Slack, AND Calendar this week."

        Returns list of dicts with entity_type, entity_name, sources, source_count,
        and a sample preview from the most recent scanned_item.
        """
        results = []
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                seven_days_ago = (datetime.now() - timedelta(days=7)).isoformat()
                cursor.execute("""
                    SELECT cr.entity_type, cr.entity_id,
                           COUNT(DISTINCT si.source) as source_count,
                           STRING_AGG(DISTINCT si.source, ',') as sources
                    FROM cross_references cr
                    JOIN scanned_items si ON cr.scanned_item_id = si.id
                    WHERE cr.user_id = %s AND cr.created_at > %s
                    GROUP BY cr.entity_type, cr.entity_id
                    HAVING COUNT(DISTINCT si.source) >= 2
                    ORDER BY source_count DESC
                    LIMIT %s
                """, (self.user_id, seven_days_ago, limit))

                rows = cursor.fetchall()

                for row in rows:
                    entity_type = row['entity_type']
                    entity_id = row['entity_id']
                    sources = row['sources'].split(',') if row['sources'] else []

                    # Look up entity name
                    entity_name = self._resolve_entity_name(
                        cursor, entity_type, entity_id
                    )
                    if not entity_name:
                        continue

                    # Get a sample preview from the most recent scanned_item
                    preview = ''
                    cursor.execute("""
                        SELECT si.source_metadata, si.source
                        FROM cross_references cr
                        JOIN scanned_items si ON cr.scanned_item_id = si.id
                        WHERE cr.user_id = %s AND cr.entity_type = %s AND cr.entity_id = %s
                        ORDER BY si.detected_at DESC
                        LIMIT 1
                    """, (self.user_id, entity_type, entity_id))
                    sample = cursor.fetchone()
                    if sample:
                        try:
                            meta = json.loads(sample['source_metadata'] or '{}')
                            preview = (
                                meta.get('subject') or
                                meta.get('preview') or
                                meta.get('channel_name') or
                                ''
                            )
                        except (ValueError, TypeError):
                            pass

                    results.append({
                        'entity_type': entity_type,
                        'entity_name': entity_name,
                        'sources': sources,
                        'source_count': row['source_count'],
                        'sample_preview': str(preview)[:120]
                    })

        except Exception as e:
            logger.error(f"Error fetching cross-source connections: {e}")

        return results

    def _resolve_entity_name(self, cursor, entity_type: str, entity_id: int) -> Optional[str]:
        """Look up entity name from People, Projects, or Ideas table."""
        table_map = {
            'person': ('people', 'name'),
            'project': ('projects', 'name'),
            'idea': ('ideas', 'title'),
            'task': ('tasks', 'title'),
        }
        mapping = table_map.get(entity_type)
        if not mapping:
            return None

        table, col = mapping
        try:
            cursor.execute(
                f"SELECT {col} FROM {table} WHERE id = %s AND user_id = %s",
                (entity_id, self.user_id)
            )
            row = cursor.fetchone()
            return row[col] if row else None
        except Exception:
            return None

    async def _analyze_relationships(self) -> dict:
        """Analyze relationship health for the week."""
        now = datetime.now()
        week_cutoff = (now - timedelta(days=7)).isoformat()
        stale_cutoff = (now - timedelta(days=21)).isoformat()

        result = {
            'contacted_this_week': [],
            'getting_stale': []
        }

        with get_db() as conn:
            cursor = conn.cursor()

            # People contacted this week
            cursor.execute("""
                SELECT name, last_contact_date FROM people
                WHERE user_id = %s AND last_contact_date >= %s
                ORDER BY last_contact_date DESC
            """, (self.user_id, week_cutoff))
            result['contacted_this_week'] = [row['name'] for row in cursor.fetchall()]

            # People getting stale (21+ days since last contact)
            cursor.execute("""
                SELECT name, last_contact_date FROM people
                WHERE user_id = %s
                AND (last_contact_date IS NULL OR last_contact_date < %s)
                ORDER BY last_contact_date ASC NULLS FIRST
                LIMIT 5
            """, (self.user_id, stale_cutoff))

            for row in cursor.fetchall():
                days_since = None
                if row['last_contact_date']:
                    try:
                        last = datetime.fromisoformat(row['last_contact_date'].replace('Z', '+00:00'))
                        days_since = (now - last).days
                    except (ValueError, AttributeError):
                        pass
                result['getting_stale'].append({
                    'name': row['name'],
                    'days_since_contact': days_since
                })

        return result

    async def _get_wins_to_celebrate(self) -> list:
        """Get accomplishments from this week worth celebrating."""
        wins = []
        cutoff = (datetime.now() - timedelta(days=7)).isoformat()

        with get_db() as conn:
            cursor = conn.cursor()

            # Completed projects (projects table uses updated_at, no completed_at)
            cursor.execute("""
                SELECT name FROM projects
                WHERE user_id = %s AND status = 'completed'
                AND updated_at >= %s
                ORDER BY updated_at DESC
                LIMIT 3
            """, (self.user_id, cutoff))
            for row in cursor.fetchall():
                wins.append(f"Completed project: {row['name']}")

            # High-priority tasks completed
            cursor.execute("""
                SELECT title FROM tasks
                WHERE user_id = %s AND status = 'completed'
                AND priority IN ('high', 'urgent')
                AND completed_at >= %s
                ORDER BY completed_at DESC
                LIMIT 3
            """, (self.user_id, cutoff))
            for row in cursor.fetchall():
                wins.append(f"Finished important task: {row['title']}")

        return wins[:5]  # Max 5 wins

    def _get_classification_intelligence(self) -> dict:
        """
        Gather classification pipeline stats for the past 7 days.

        Returns counts of classified items by source, detected action statuses,
        cross-reference counts, and top-referenced People/Projects.
        """
        intel = {
            'classifications_by_source': {},
            'detected_actions_by_status': {},
            'cross_references_created': 0,
            'top_referenced_people': [],
            'top_referenced_projects': [],
        }

        try:
            with get_db() as conn:
                cursor = conn.cursor()
                week_ago = (datetime.now() - timedelta(days=7)).isoformat()

                # Classifications by source type
                cursor.execute("""
                    SELECT si.source, COUNT(*) as cnt
                    FROM item_classifications ic
                    JOIN scanned_items si ON ic.scanned_item_id = si.id
                    WHERE ic.user_id = %s AND ic.classified_at >= %s
                    GROUP BY si.source
                """, (self.user_id, week_ago))
                for row in cursor.fetchall():
                    intel['classifications_by_source'][row['source']] = row['cnt']

                # Detected actions by status
                cursor.execute("""
                    SELECT status, COUNT(*) as cnt
                    FROM detected_actions
                    WHERE user_id = %s AND detected_at >= %s
                    GROUP BY status
                """, (self.user_id, week_ago))
                for row in cursor.fetchall():
                    intel['detected_actions_by_status'][row['status']] = row['cnt']

                # Cross-references created this week
                cursor.execute("""
                    SELECT COUNT(*) as cnt
                    FROM cross_references
                    WHERE user_id = %s AND created_at >= %s
                """, (self.user_id, week_ago))
                row = cursor.fetchone()
                intel['cross_references_created'] = row['cnt'] if row else 0

                # Top 3 most-referenced People
                cursor.execute("""
                    SELECT cr.entity_id, p.name, COUNT(*) as ref_count
                    FROM cross_references cr
                    JOIN people p ON cr.entity_id = p.id
                    WHERE cr.user_id = %s AND cr.entity_type = 'person'
                    AND cr.created_at >= %s
                    GROUP BY cr.entity_id
                    ORDER BY ref_count DESC
                    LIMIT 3
                """, (self.user_id, week_ago))
                for row in cursor.fetchall():
                    intel['top_referenced_people'].append({
                        'name': row['name'],
                        'ref_count': row['ref_count']
                    })

                # Top 3 most-referenced Projects
                cursor.execute("""
                    SELECT cr.entity_id, pr.name, COUNT(*) as ref_count
                    FROM cross_references cr
                    JOIN projects pr ON cr.entity_id = pr.id
                    WHERE cr.user_id = %s AND cr.entity_type = 'project'
                    AND cr.created_at >= %s
                    GROUP BY cr.entity_id
                    ORDER BY ref_count DESC
                    LIMIT 3
                """, (self.user_id, week_ago))
                for row in cursor.fetchall():
                    intel['top_referenced_projects'].append({
                        'name': row['name'],
                        'ref_count': row['ref_count']
                    })

        except Exception as e:
            logger.debug(f"Classification intelligence unavailable: {e}")

        return intel

    async def _generate_patterns(self, activity: dict) -> list:
        """
        Use Claude Haiku to analyze the week's activity and surface patterns.

        Includes classification intelligence (items classified, detected actions,
        cross-references, top-referenced people/projects) for richer analysis.

        Returns 2-3 observation strings.
        """
        try:
            api_key = os.environ.get('ANTHROPIC_API_KEY')
            if not api_key:
                logger.warning("No ANTHROPIC_API_KEY found for pattern generation")
                return []

            client = AsyncAnthropic(api_key=api_key)

            # Gather classification intelligence for richer context
            classification_intel = self._get_classification_intelligence()

            # Build classification context string
            classification_context = ""
            if any(v for v in classification_intel.values() if v):
                parts = []
                by_source = classification_intel.get('classifications_by_source', {})
                if by_source:
                    source_str = ", ".join(f"{cnt} {src}" for src, cnt in by_source.items())
                    parts.append(f"Items classified this week: {source_str}")

                by_status = classification_intel.get('detected_actions_by_status', {})
                if by_status:
                    status_str = ", ".join(f"{cnt} {st}" for st, cnt in by_status.items())
                    parts.append(f"Detected actions: {status_str}")

                xref_count = classification_intel.get('cross_references_created', 0)
                if xref_count:
                    parts.append(f"Cross-references created: {xref_count}")

                top_people = classification_intel.get('top_referenced_people', [])
                if top_people:
                    people_str = ", ".join(f"{p['name']} ({p['ref_count']} refs)" for p in top_people)
                    parts.append(f"Most-referenced people: {people_str}")

                top_projects = classification_intel.get('top_referenced_projects', [])
                if top_projects:
                    proj_str = ", ".join(f"{p['name']} ({p['ref_count']} refs)" for p in top_projects)
                    parts.append(f"Most-referenced projects: {proj_str}")

                if parts:
                    classification_context = "\n\nClassification intelligence (from inbound scanning):\n" + "\n".join(f"- {p}" for p in parts)

            prompt = f"""Analyze this week's activity data and identify 2-3 patterns or observations.
Be specific and actionable. Examples:
- "You completed most tasks on Tuesday and Friday - consider protecting those days for deep work"
- "3 projects are stuck waiting on others - batch your follow-ups"
- "You captured 5 ideas about AI this week - emerging interest area?"
- "3 people tried to reach you about budget topics - might be worth a group discussion"
- "You received a lot of Slack messages about Project X but haven't updated it"

Keep each observation to 1-2 sentences. Be direct and helpful.

Activity data:
{json.dumps(activity, indent=2, default=str)}{classification_context}

Return ONLY a JSON array of 2-3 observation strings. No other text."""

            response = await client.messages.create(
                model="claude-haiku-4-5-20250929",
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}]
            )

            # Parse JSON response
            text = response.content[0].text.strip()
            # Handle case where response might have markdown code blocks
            if text.startswith('```'):
                text = text.split('\n', 1)[1]
                if text.endswith('```'):
                    text = text[:-3].strip()
                elif '```' in text:
                    text = text.split('```')[0].strip()

            patterns = json.loads(text)
            if isinstance(patterns, list):
                return patterns[:3]
            return []

        except Exception as e:
            logger.error(f"Error generating patterns: {e}")
            return self._generate_fallback_patterns(activity)

    def _generate_fallback_patterns(self, activity: dict) -> list:
        """Generate simple patterns without AI if API fails."""
        patterns = []

        # Completion by day pattern
        completion_by_day = activity.get('completion_by_day', {})
        if completion_by_day:
            best_day = max(completion_by_day, key=completion_by_day.get)
            count = completion_by_day[best_day]
            patterns.append(f"Your most productive day was {best_day} with {count} item{'s' if count != 1 else ''} completed")

        # Ideas pattern
        ideas_count = activity.get('ideas_captured', 0)
        if ideas_count >= 3:
            patterns.append(f"You captured {ideas_count} ideas this week - consider reviewing them for themes")

        # People pattern
        people_count = len(activity.get('people_contacted', []))
        if people_count >= 3:
            patterns.append(f"Good relationship maintenance - you connected with {people_count} people")
        elif people_count == 0:
            patterns.append("No relationship check-ins recorded this week - consider reaching out to someone")

        return patterns[:3]

    async def _suggest_focus_areas(self, activity: dict, open_loops: list, relationships: dict) -> list:
        """
        Suggest 3 focus areas for next week based on data.

        Uses AI for nuanced suggestions, falls back to rule-based if needed.
        """
        try:
            api_key = os.environ.get('ANTHROPIC_API_KEY')
            if not api_key:
                return self._generate_fallback_focus(activity, open_loops, relationships)

            client = AsyncAnthropic(api_key=api_key)

            # Build context for AI
            context = {
                'activity': activity,
                'open_loops_count': len(open_loops),
                'open_loop_types': [ol['type'] for ol in open_loops[:5]],
                'stale_relationships_count': len(relationships.get('getting_stale', [])),
            }

            prompt = f"""Based on this week's data, suggest 3 focus areas for next week.
Each should have an 'area' (short title) and 'reason' (1 sentence why).

Context:
{json.dumps(context, indent=2, default=str)}

Return ONLY a JSON array of 3 objects with 'area' and 'reason' keys. Example:
[
  {{"area": "Unblock Website project", "reason": "It's been stalled for 2 weeks and is your biggest active project"}},
  {{"area": "Relationship catch-ups", "reason": "2 important contacts are going stale"}}
]"""

            response = await client.messages.create(
                model="claude-haiku-4-5-20250929",
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}]
            )

            text = response.content[0].text.strip()
            if text.startswith('```'):
                text = text.split('\n', 1)[1]
                if text.endswith('```'):
                    text = text[:-3].strip()
                elif '```' in text:
                    text = text.split('```')[0].strip()

            focus_areas = json.loads(text)
            if isinstance(focus_areas, list):
                return focus_areas[:3]
            return []

        except Exception as e:
            logger.error(f"Error generating focus areas: {e}")
            return self._generate_fallback_focus(activity, open_loops, relationships)

    def _generate_fallback_focus(self, activity: dict, open_loops: list, relationships: dict) -> list:
        """Generate focus areas without AI if needed."""
        focus = []

        # Check for stalled projects
        project_loops = [ol for ol in open_loops if ol['type'] == 'project']
        if project_loops:
            focus.append({
                'area': f"Unblock {project_loops[0]['title']}",
                'reason': f"This project has been inactive for {project_loops[0]['age_days']} days"
            })

        # Check for stale relationships
        stale = relationships.get('getting_stale', [])
        if stale:
            names = [s['name'] for s in stale[:2]]
            focus.append({
                'area': 'Relationship check-ins',
                'reason': f"Reconnect with {' and '.join(names)}"
            })

        # Check for old errands
        errand_loops = [ol for ol in open_loops if ol['type'] == 'errand']
        if errand_loops:
            focus.append({
                'area': 'Clear errand backlog',
                'reason': f"{len(errand_loops)} errands have been lingering - batch process them"
            })

        # Default focus if nothing else
        if not focus:
            focus.append({
                'area': 'Weekly planning',
                'reason': 'Set intentions for the week ahead'
            })

        return focus[:3]

    # =========================================================================
    # Weekly Review Formatting
    # =========================================================================

    def format_weekly_review_text(self, review: dict) -> str:
        """Format weekly review as plain text for email/display."""
        lines = []

        # Header
        lines.append(f"WEEKLY REVIEW: {review['week_of']}")
        lines.append("=" * 40)
        lines.append("")
        lines.append(review['summary'])
        lines.append("")

        # What Happened
        what = review.get('what_happened', {})
        lines.append("WHAT HAPPENED")
        lines.append("-" * 20)

        projects_completed = what.get('projects_completed', [])
        if projects_completed:
            lines.append(f"* {len(projects_completed)} project{'s' if len(projects_completed) != 1 else ''} completed: {', '.join(projects_completed)}")

        projects_started = what.get('projects_started', [])
        if projects_started:
            lines.append(f"* {len(projects_started)} project{'s' if len(projects_started) != 1 else ''} started: {', '.join(projects_started)}")

        tasks_done = what.get('tasks_completed', 0)
        errands_done = what.get('errands_completed', 0)
        if tasks_done + errands_done > 0:
            lines.append(f"* {tasks_done + errands_done} tasks/errands completed")

        people = what.get('people_contacted', [])
        if people:
            lines.append(f"* {len(people)} people contacted: {', '.join(people[:5])}")

        ideas = what.get('ideas_captured', 0)
        if ideas:
            lines.append(f"* {ideas} ideas captured")
        lines.append("")

        # Open Loops
        open_loops = review.get('open_loops', [])
        if open_loops:
            lines.append("OPEN LOOPS (needs attention)")
            lines.append("-" * 20)
            for loop in open_loops[:5]:
                age = f" ({loop['age_days']} days)" if loop.get('age_days') else ""
                lines.append(f"* {loop['title']}{age}")
                lines.append(f"  -> {loop['suggested_action']}")
                if loop.get('linked_person'):
                    lines.append(f"  \U0001f464 Linked to: {loop['linked_person']}")
                for activity in loop.get('related_activity', [])[:2]:
                    lines.append(f"  Related: {activity['source_type']} activity {activity.get('date', '')}")
            lines.append("")

        # Cross-Source Connections
        connections = review.get('cross_source_connections', [])
        if connections:
            lines.append("\U0001f517 CROSS-SOURCE CONNECTIONS")
            lines.append("-" * 20)
            for conn in connections:
                sources_str = ', '.join(conn['sources'])
                lines.append(f"* {conn['entity_name']} appeared in {sources_str} ({conn['source_count']} sources)")
                if conn.get('sample_preview'):
                    lines.append(f"  Latest: {conn['sample_preview']}")
            lines.append("")

        # Patterns Noticed
        patterns = review.get('patterns_noticed', [])
        if patterns:
            lines.append("PATTERNS NOTICED")
            lines.append("-" * 20)
            for pattern in patterns:
                lines.append(f"* {pattern}")
            lines.append("")

        # Suggested Focus
        focus_areas = review.get('suggested_focus', [])
        if focus_areas:
            lines.append("SUGGESTED FOCUS FOR NEXT WEEK")
            lines.append("-" * 20)
            for i, focus in enumerate(focus_areas, 1):
                lines.append(f"{i}. {focus['area']}")
                lines.append(f"   {focus['reason']}")
            lines.append("")

        # Relationships
        relationships = review.get('relationships', {})
        contacted = relationships.get('contacted_this_week', [])
        stale = relationships.get('getting_stale', [])

        if contacted or stale:
            lines.append("RELATIONSHIPS")
            lines.append("-" * 20)
            if contacted:
                lines.append(f"Connected with: {', '.join(contacted[:5])}")
            if stale:
                stale_names = [s['name'] for s in stale[:3]]
                lines.append(f"Getting stale: {', '.join(stale_names)}")
            lines.append("")

        # Wins
        wins = review.get('wins_to_celebrate', [])
        if wins:
            lines.append("WINS TO CELEBRATE")
            lines.append("-" * 20)
            for win in wins:
                lines.append(f"* {win}")
            lines.append("")

        return "\n".join(lines)

    def format_weekly_review_html(self, review: dict) -> str:
        """Format weekly review as HTML for email."""
        html = []

        # Header
        html.append(f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <h1 style="color: #333; font-size: 24px; margin-bottom: 10px; border-bottom: 3px solid #4a90d9; padding-bottom: 10px;">
                Weekly Review: {review['week_of']}
            </h1>
            <p style="color: #444; font-size: 14px; background: #f5f5f5; padding: 12px; border-radius: 6px;">
                {review['summary']}
            </p>
        """)

        # What Happened
        what = review.get('what_happened', {})
        html.append("""
            <h2 style="color: #333; font-size: 18px; margin-top: 24px; border-bottom: 2px solid #4a90d9; padding-bottom: 8px;">
                What Happened
            </h2>
            <ul style="list-style: none; padding: 0;">
        """)

        projects_completed = what.get('projects_completed', [])
        if projects_completed:
            html.append(f"""
            <li style="margin-bottom: 8px; padding: 8px; background: #f0fff4; border-radius: 4px; border-left: 3px solid #27ae60;">
                <strong>{len(projects_completed)} project{'s' if len(projects_completed) != 1 else ''} completed:</strong> {', '.join(projects_completed)}
            </li>
            """)

        projects_started = what.get('projects_started', [])
        if projects_started:
            html.append(f"""
            <li style="margin-bottom: 8px; padding: 8px; background: #f9f9f9; border-radius: 4px;">
                <strong>{len(projects_started)} project{'s' if len(projects_started) != 1 else ''} started:</strong> {', '.join(projects_started)}
            </li>
            """)

        tasks_done = what.get('tasks_completed', 0)
        errands_done = what.get('errands_completed', 0)
        if tasks_done + errands_done > 0:
            html.append(f"""
            <li style="margin-bottom: 8px; padding: 8px; background: #f9f9f9; border-radius: 4px;">
                <strong>{tasks_done + errands_done}</strong> tasks/errands completed
            </li>
            """)

        people = what.get('people_contacted', [])
        if people:
            html.append(f"""
            <li style="margin-bottom: 8px; padding: 8px; background: #f9f9f9; border-radius: 4px;">
                Connected with <strong>{len(people)}</strong> people: {', '.join(people[:5])}
            </li>
            """)

        ideas = what.get('ideas_captured', 0)
        if ideas:
            html.append(f"""
            <li style="margin-bottom: 8px; padding: 8px; background: #f9f9f9; border-radius: 4px;">
                <strong>{ideas}</strong> ideas captured
            </li>
            """)

        html.append("</ul>")

        # Open Loops
        open_loops = review.get('open_loops', [])
        if open_loops:
            html.append("""
            <h2 style="color: #e74c3c; font-size: 18px; margin-top: 24px; border-bottom: 2px solid #e74c3c; padding-bottom: 8px;">
                Open Loops (needs attention)
            </h2>
            <ul style="list-style: none; padding: 0;">
            """)
            for loop in open_loops[:5]:
                age = f" ({loop['age_days']} days)" if loop.get('age_days') else ""
                person_html = f"<br><span style='color: #888; font-size: 12px;'>\U0001f464 Linked to: {loop['linked_person']}</span>" if loop.get('linked_person') else ""
                activity_html = ""
                for activity in loop.get('related_activity', [])[:2]:
                    activity_html += f"<br><span style='color: #888; font-size: 12px;'>Related: {activity['source_type']} activity {activity.get('date', '')}</span>"
                html.append(f"""
                <li style="margin-bottom: 8px; padding: 8px; background: #fef5f5; border-radius: 4px; border-left: 3px solid #e74c3c;">
                    <strong>{loop['title']}</strong>{age}
                    <br><span style="color: #666; font-size: 13px;">{loop['suggested_action']}</span>{person_html}{activity_html}
                </li>
                """)
            html.append("</ul>")

        # Cross-Source Connections
        connections = review.get('cross_source_connections', [])
        if connections:
            html.append("""
            <h2 style="color: #2980b9; font-size: 18px; margin-top: 24px; border-bottom: 2px solid #2980b9; padding-bottom: 8px;">
                \U0001f517 Cross-Source Connections
            </h2>
            <ul style="list-style: none; padding: 0;">
            """)
            for conn in connections:
                sources_str = ', '.join(conn['sources'])
                preview_html = f"<br><span style='color: #888; font-size: 12px;'>Latest: {conn['sample_preview']}</span>" if conn.get('sample_preview') else ""
                html.append(f"""
                <li style="margin-bottom: 8px; padding: 8px; background: #eaf2f8; border-radius: 4px; border-left: 3px solid #2980b9;">
                    <strong>{conn['entity_name']}</strong> appeared in {sources_str} ({conn['source_count']} sources){preview_html}
                </li>
                """)
            html.append("</ul>")

        # Patterns Noticed
        patterns = review.get('patterns_noticed', [])
        if patterns:
            html.append("""
            <h2 style="color: #9b59b6; font-size: 18px; margin-top: 24px; border-bottom: 2px solid #9b59b6; padding-bottom: 8px;">
                Patterns Noticed
            </h2>
            <ul style="list-style: none; padding: 0;">
            """)
            for pattern in patterns:
                html.append(f"""
                <li style="margin-bottom: 8px; padding: 12px; background: #f5f0f9; border-radius: 4px; border-left: 3px solid #9b59b6;">
                    {pattern}
                </li>
                """)
            html.append("</ul>")

        # Suggested Focus
        focus_areas = review.get('suggested_focus', [])
        if focus_areas:
            html.append("""
            <h2 style="color: #4a90d9; font-size: 18px; margin-top: 24px; border-bottom: 2px solid #4a90d9; padding-bottom: 8px;">
                Suggested Focus for Next Week
            </h2>
            <ol style="padding-left: 20px;">
            """)
            for focus in focus_areas:
                html.append(f"""
                <li style="margin-bottom: 12px;">
                    <strong>{focus['area']}</strong>
                    <br><span style="color: #666; font-size: 13px;">{focus['reason']}</span>
                </li>
                """)
            html.append("</ol>")

        # Relationships
        relationships = review.get('relationships', {})
        contacted = relationships.get('contacted_this_week', [])
        stale = relationships.get('getting_stale', [])

        if contacted or stale:
            html.append("""
            <h2 style="color: #333; font-size: 18px; margin-top: 24px; border-bottom: 2px solid #4a90d9; padding-bottom: 8px;">
                Relationships
            </h2>
            """)
            if contacted:
                html.append(f"""
                <p style="color: #27ae60;"><strong>Connected with:</strong> {', '.join(contacted[:5])}</p>
                """)
            if stale:
                stale_names = [s['name'] for s in stale[:3]]
                html.append(f"""
                <p style="color: #e74c3c;"><strong>Getting stale:</strong> {', '.join(stale_names)}</p>
                """)

        # Wins
        wins = review.get('wins_to_celebrate', [])
        if wins:
            html.append("""
            <h2 style="color: #27ae60; font-size: 18px; margin-top: 24px; border-bottom: 2px solid #27ae60; padding-bottom: 8px;">
                Wins to Celebrate
            </h2>
            <ul style="list-style: none; padding: 0;">
            """)
            for win in wins:
                html.append(f"""
                <li style="margin-bottom: 8px; padding: 12px; background: #f0fff4; border-radius: 4px; border-left: 3px solid #27ae60;">
                    {win}
                </li>
                """)
            html.append("</ul>")

        # Footer
        html.append("""
            <p style="color: #888; font-size: 12px; margin-top: 30px; text-align: center; border-top: 1px solid #eee; padding-top: 15px;">
                Sent by Seny - Your Personal AI Assistant
            </p>
        </div>
        """)

        return "".join(html)

    def format_weekly_review_summary(self, review: dict) -> str:
        """Format short summary for push notification (max 100 chars)."""
        what = review.get('what_happened', {})
        tasks_done = what.get('tasks_completed', 0) + what.get('errands_completed', 0)
        projects_done = len(what.get('projects_completed', []))

        parts = []
        if projects_done > 0:
            parts.append(f"{projects_done} project{'s' if projects_done != 1 else ''}")
        if tasks_done > 0:
            parts.append(f"{tasks_done} task{'s' if tasks_done != 1 else ''}")

        if parts:
            summary = f"Week in review: {', '.join(parts)} completed"
        else:
            summary = "Your weekly review is ready"

        if len(summary) > 95:
            summary = summary[:92] + "..."

        return summary

    # =========================================================================
    # Weekly Review Delivery
    # =========================================================================

    async def send_weekly_review_email(self, review: dict) -> bool:
        """
        Send weekly review via Gmail to self.

        Args:
            review: Generated weekly review dict

        Returns:
            True if sent successfully
        """
        try:
            google_accounts = list_google_tokens(self.user_id)

            if not google_accounts:
                logger.warning(f"No Gmail accounts for user {self.user_id} - cannot send weekly review email")
                return False

            email = google_accounts[0].get('email')
            if not email:
                return False

            from web.services.gmail_service import GmailService
            gmail_service = GmailService(self.user_id, email)

            if not gmail_service.is_connected():
                logger.warning(f"Gmail not connected for user {self.user_id}")
                return False

            html_content = self.format_weekly_review_html(review)
            text_content = self.format_weekly_review_text(review)

            result = await gmail_service.send_email(
                to=email,
                subject=f"Weekly Review - {review['week_of']}",
                body=text_content,
                html_body=html_content
            )

            if result and not result.get('error'):
                logger.info(f"Sent weekly review email to {email}")
                return True

            return False

        except Exception as e:
            logger.error(f"Error sending weekly review email: {e}")
            return False

    async def send_weekly_review_push(self, review: dict) -> bool:
        """
        Send weekly review summary via push notification.

        Args:
            review: Generated weekly review dict

        Returns:
            True if sent successfully
        """
        try:
            from web.services.notification_service import NotificationService
            notification_service = NotificationService(self.user_id)

            summary = self.format_weekly_review_summary(review)

            result = await notification_service.send_notification(
                title="Weekly Review Ready",
                body=summary,
                url="/",
                notification_type="weekly_review"
            )

            if result.get('sent', 0) > 0:
                logger.info(f"Sent weekly review push notification for user {self.user_id}")
                return True

            return False

        except Exception as e:
            logger.error(f"Error sending weekly review push: {e}")
            return False

    async def deliver_weekly_review(self) -> dict:
        """
        Generate and deliver weekly review based on user preferences.

        Returns:
            Dict with generated, email_sent, push_sent flags
        """
        prefs = get_weekly_review_preferences(self.user_id)

        if not prefs.get('weekly_review_enabled', True):
            return {'generated': False, 'reason': 'disabled'}

        review = await self.generate_weekly_review()

        result = {
            'generated': True,
            'email_sent': False,
            'push_sent': False,
            'review': review
        }

        # Use digest email/push preferences for weekly review too
        digest_prefs = get_digest_preferences(self.user_id)

        if digest_prefs.get('digest_email', True):
            result['email_sent'] = await self.send_weekly_review_email(review)

        if digest_prefs.get('digest_push', True):
            result['push_sent'] = await self.send_weekly_review_push(review)

        logger.info(f"Delivered weekly review for user {self.user_id}: email={result['email_sent']}, push={result['push_sent']}")

        return result

    async def _assemble_claude_weekly_data(self) -> dict:
        """
        Assemble the full ~5,000-token data package for the Claude weekly reasoning call.

        Queries 6 data sections over a 7-day window:
        1. Tasks — completed count, created this week, still open from before
        2. Projects — updated this week, gone quiet (no activity > 7 days)
        3. People contacted — names from _analyze_week_activity
        4. Nudges — total sent, responded to, grouped by type
        5. Ideas — titles captured this week
        6. Unfulfilled commitments — pending detected_actions with age

        Returns a dict ready to be JSON-serialized and passed to Sonnet.
        """
        try:
            from zoneinfo import ZoneInfo as _ZI2
            _s2 = get_user_settings(self.user_id)
            _tz2 = _ZI2(_s2.get('digest_timezone', 'America/Chicago') if _s2 else 'America/Chicago')
        except Exception:
            from zoneinfo import ZoneInfo as _ZI2
            _tz2 = _ZI2('America/Chicago')
        from datetime import timezone as _utc2
        _now_local = datetime.now(_utc2.utc).astimezone(_tz2)
        cutoff = _now_local - timedelta(days=7)
        cutoff_str = cutoff.isoformat()

        # Build week-of string
        week_start = cutoff.strftime("%b %-d")
        week_end = _now_local.strftime("%b %-d, %Y")
        week_of = f"{week_start} - {week_end}"

        # --- 1. Tasks ---
        tasks_completed_count = 0
        errands_completed_count = 0
        tasks_created_this_week = []
        tasks_still_open_from_before = []

        # --- 2. Projects ---
        projects_updated_this_week = []
        projects_gone_quiet = []

        # --- 5. Ideas ---
        ideas_captured = []

        with get_db() as conn:
            cursor = conn.cursor()

            # Tasks completed this week
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM tasks
                WHERE user_id = %s AND status = 'completed'
                AND type = 'task' AND completed_at >= %s
            """, (self.user_id, cutoff_str))
            row = cursor.fetchone()
            tasks_completed_count = row['cnt'] if row else 0

            # Errands completed this week
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM tasks
                WHERE user_id = %s AND status = 'completed'
                AND type = 'errand' AND completed_at >= %s
            """, (self.user_id, cutoff_str))
            row = cursor.fetchone()
            errands_completed_count = row['cnt'] if row else 0

            # Task titles created this week (not completed)
            cursor.execute("""
                SELECT title FROM tasks
                WHERE user_id = %s AND created_at >= %s AND status != 'completed'
                ORDER BY created_at DESC
            """, (self.user_id, cutoff_str))
            tasks_created_this_week = [r['title'] for r in cursor.fetchall()]

            # Tasks still open from before this week
            cursor.execute("""
                SELECT title, due_date FROM tasks
                WHERE user_id = %s AND status = 'active' AND created_at < %s
                ORDER BY created_at ASC
                LIMIT 10
            """, (self.user_id, cutoff_str))
            for r in cursor.fetchall():
                tasks_still_open_from_before.append({
                    "title": r['title'],
                    "due_date": r['due_date'] or ""
                })

            # Projects updated this week
            cursor.execute("""
                SELECT name, status, updated_at FROM projects
                WHERE user_id = %s AND updated_at >= %s
                ORDER BY updated_at DESC
            """, (self.user_id, cutoff_str))
            for r in cursor.fetchall():
                projects_updated_this_week.append({
                    "name": r['name'],
                    "status": r['status']
                })

            # Projects gone quiet (active but no update in 7+ days)
            cursor.execute("""
                SELECT name, updated_at FROM projects
                WHERE user_id = %s AND status = 'active' AND updated_at < %s
                ORDER BY updated_at ASC
                LIMIT 5
            """, (self.user_id, cutoff_str))
            now = datetime.now()
            for r in cursor.fetchall():
                days_since = 0
                if r['updated_at']:
                    try:
                        dt = datetime.fromisoformat(r['updated_at'].replace('Z', '+00:00'))
                        days_since = (now - dt).days
                    except (ValueError, AttributeError):
                        pass
                projects_gone_quiet.append({
                    "name": r['name'],
                    "days_since_update": days_since
                })

            # Ideas captured this week
            cursor.execute("""
                SELECT title, created_at FROM ideas
                WHERE user_id = %s AND created_at >= %s
                ORDER BY created_at DESC
            """, (self.user_id, cutoff_str))
            ideas_captured = [r['title'] for r in cursor.fetchall()]

        # --- 3. People contacted (reuse _analyze_week_activity data) ---
        activity = await self._analyze_week_activity()
        people_contacted = activity.get('people_contacted', [])

        # --- 4. Nudges ---
        nudges_raw = get_recent_nudges(self.user_id, hours=168, limit=30)
        total_sent = len(nudges_raw)
        responded_to = sum(1 for n in nudges_raw if n.get('acted_at'))

        by_type: dict = {}
        for n in nudges_raw:
            ntype = n.get('nudge_type', 'unknown')
            if ntype not in by_type:
                by_type[ntype] = []
            body_raw = n.get('body') or ''
            by_type[ntype].append({
                "title": n.get('title', ''),
                "body": body_raw[:120],
                "responded": n.get('acted_at') is not None,
                "user_response": n.get('user_response') or ''
            })

        nudges_section = {
            "total_sent": total_sent,
            "responded_to": responded_to,
            "by_type": by_type
        }

        # --- 6. Unfulfilled commitments ---
        commitments_raw = await self._get_unfulfilled_commitments(limit=8)
        unfulfilled_commitments = []
        for c in commitments_raw:
            unfulfilled_commitments.append({
                "text": (c.get('action_text') or '')[:100],
                "days_old": c.get('days_ago', 0) or 0
            })

        return {
            "week_of": week_of,
            "tasks": {
                "completed_count": tasks_completed_count + errands_completed_count,
                "created_this_week": tasks_created_this_week,
                "still_open_from_before": tasks_still_open_from_before
            },
            "projects": {
                "updated_this_week": projects_updated_this_week,
                "gone_quiet": projects_gone_quiet
            },
            "people_contacted": people_contacted,
            "nudges": nudges_section,
            "ideas_captured": ideas_captured,
            "unfulfilled_commitments": unfulfilled_commitments
        }

    async def generate_claude_weekly_review(self) -> dict:
        """
        Generate a Claude Sonnet-powered weekly review with deep reasoning.

        Assembles a ~5,000-token data package and passes it to claude-sonnet-4-6
        for pattern analysis. Returns insights that the template-based review
        can't surface: behavioural patterns, avoidance signals, repeated themes
        without action, and one hard question for the user to sit with.

        Returns a dict with 'week_of', 'mode', 'data_package', and 'claude_insights'.
        Callers can return this dict directly from API endpoints.
        """
        data = {}
        try:
            data = await self._assemble_claude_weekly_data()
            data_str = json.dumps(data, indent=2, default=str)

            prompt = f"""You are Seny — a warm but direct personal assistant. Think of yourself as the user's cool uncle: financially fluent, genuinely opinionated, pushes back when needed, and never just a cheerleader. You've known them long enough to notice patterns they don't see in themselves.

You're reviewing a week of data for someone you genuinely care about. Your job is not to celebrate every completed task — it's to notice what actually matters: what they're avoiding, what keeps coming up without action, and the question they probably don't want to answer.

Here is the full week's data:
{data_str}

Reason across all 6 sections (tasks, projects, people, nudges+responses, ideas, unfulfilled commitments). Look for:
- What behaviour patterns emerge from how they spent their time?
- What topic or task keeps getting deferred or ignored?
- What themes came up multiple times (in nudges, ideas, commitments) but were never acted on?
- What is the single most important question they should sit with this week?

Be specific. Reference actual data points (e.g., "You had 3 nudges about X but didn't respond to any"). Don't be vague. Don't soften it unnecessarily.

Respond ONLY with valid JSON in exactly this format (no markdown, no extra text):
{{
  "patterns_noticed": ["string", "string"],
  "avoiding_what": "string",
  "repeated_themes": ["string"],
  "hard_question": "string"
}}

Rules:
- patterns_noticed: 2-3 specific observations backed by data points from the package
- avoiding_what: one clear sentence naming the topic or task that keeps getting deferred
- repeated_themes: 1-2 things that appeared multiple times across sections but weren't acted on
- hard_question: one direct question for the user to sit with — make it specific, not generic"""

            api_key = os.environ.get('ANTHROPIC_API_KEY')
            client = AsyncAnthropic(api_key=api_key)
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}]
            )

            text = response.content[0].text.strip()
            # Handle markdown code fences (same pattern as _generate_patterns)
            if text.startswith('```'):
                text = text.split('\n', 1)[1]
                if text.endswith('```'):
                    text = text[:-3].strip()
                elif '```' in text:
                    text = text.split('```')[0].strip()

            try:
                insights = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("Claude weekly review returned non-JSON; using fallback")
                insights = {
                    "patterns_noticed": ["Could not parse insights from Claude response."],
                    "avoiding_what": "Unable to determine — review the data package directly.",
                    "repeated_themes": [],
                    "hard_question": "What one thing have you been putting off the longest?"
                }

            return {
                "week_of": data.get("week_of", ""),
                "mode": "claude",
                "data_package": data,
                "claude_insights": insights
            }

        except Exception as e:
            logger.error(f"generate_claude_weekly_review error: {repr(e)}")
            return {
                "week_of": data.get("week_of", "") if data else "",
                "mode": "claude",
                "error": str(e),
                "data_package": data,
                "claude_insights": None
            }
