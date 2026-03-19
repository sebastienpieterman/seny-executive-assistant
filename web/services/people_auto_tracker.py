"""
People Auto Tracker - Automatically updates People tracker from communications.

Analyzes scanned communications (Gmail, Slack, Telegram) to:
1. Auto-update last_contact_date for recognized people
2. Extract meaningful context using Haiku AI
3. Respect manual updates (don't overwrite newer user edits)

Only counts INBOUND communications (messages from others to you).

Phase 19-01 - Automatic People Tracker
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from anthropic import AsyncAnthropic

from web.core.database import (
    get_db,
    get_people_by_user,
    update_person as db_update_person,
)
from web.services.activity_log_service import ActivityLogService
from src.core.config import Config

logger = logging.getLogger(__name__)

# Haiku model for context evaluation (same as inbound_classifier)
CONTEXT_MODEL = "claude-haiku-4-5-20251001"

# Maximum content length sent to Haiku
MAX_CONTENT_CHARS = 1000

# Context evaluation prompt
CONTEXT_EVALUATION_PROMPT = """Evaluate this message from {person_name}:

<message>
{content}
</message>

Decide if this message contains information worth remembering about this person or your relationship.

Worth noting:
- Topics discussed (projects, ideas, issues they brought up)
- Action items, commitments, requests
- Important updates, plans, decisions
- Personal news (life events, milestones)
- Opinions or concerns they expressed

NOT worth noting:
- Brief acknowledgments ("thanks!", "got it", "sounds good")
- Routine pleasantries with no substance
- Automated messages, notifications, receipts

Respond with JSON only:
{{"worth_noting": true/false, "context": "Brief 1-sentence summary capturing the key topic or information, otherwise empty string"}}"""


class PeopleAutoTracker:
    """
    Automatically tracks contacts from scanned communications.

    Runs as a background job to:
    1. Find recent inbound communications linked to People via entity_mappings
    2. Update last_contact_date for those people
    3. Use Haiku to extract noteworthy context to add to notes
    """

    def __init__(self, user_id: int):
        self.user_id = user_id
        self._client = AsyncAnthropic(api_key=Config.ANTHROPIC_API_KEY)
        self._user_identifiers: Optional[set[str]] = None  # Lazy-loaded

    async def run(self) -> dict:
        """
        Main entry point - run the auto-tracker for this user.

        Returns:
            Summary dict: {people_updated, contexts_added, skipped_manual, tokens_used}
        """
        # Calculate since time (last 24 hours or use stored last run time)
        since = datetime.now(timezone.utc) - timedelta(hours=24)

        # Get recent communications linked to people
        communications = await self.analyze_recent_communications(since)

        if not communications:
            logger.debug("PeopleAutoTracker: No linked communications for user %d", self.user_id)
            return {
                "people_updated": 0,
                "contexts_added": 0,
                "skipped_manual": 0,
                "tokens_used": 0,
            }

        # Group by person and get most recent per person
        by_person = self._group_by_person(communications)

        # Evaluate context and update
        result = await self.update_contacts(by_person)

        logger.info(
            "PeopleAutoTracker: user=%d updated=%d contexts=%d skipped=%d",
            self.user_id, result["people_updated"],
            result["contexts_added"], result["skipped_manual"]
        )

        return result

    async def analyze_recent_communications(self, since: datetime) -> list[dict]:
        """
        Find recent inbound communications linked to tracked people.

        Queries scanned_items joined with entity_mappings where:
        - entity_mappings.person_id IS NOT NULL
        - scanned_items.detected_at > since
        - source IN ('gmail', 'slack', 'telegram')
        - Message is INBOUND (not from user's own addresses)

        Args:
            since: Only look at items detected after this time

        Returns:
            List of communication dicts with person info and content
        """
        # Load user's own identifiers for filtering out outbound messages
        await self._load_user_identifiers()

        results = []
        since_str = since.isoformat()

        try:
            with get_db() as conn:
                cursor = conn.cursor()

                # Query scanned_items linked to people via entity_mappings
                cursor.execute("""
                    SELECT
                        si.id as scanned_item_id,
                        si.source,
                        si.source_id,
                        si.source_metadata,
                        si.detected_at,
                        em.person_id,
                        em.source_identifier,
                        em.display_name,
                        p.name as person_name,
                        p.last_contact_date
                    FROM scanned_items si
                    JOIN entity_mappings em ON (
                        si.user_id = em.user_id
                        AND (
                            -- Gmail: match 'from' email in metadata
                            (si.source = 'gmail' AND em.source = 'gmail')
                            -- Slack: match user_id in metadata
                            OR (si.source = 'slack' AND em.source = 'slack')
                            -- Telegram: match sender_id in metadata
                            OR (si.source = 'telegram' AND em.source = 'telegram')
                        )
                    )
                    JOIN people p ON em.person_id = p.id
                    WHERE si.user_id = %s
                    AND em.person_id IS NOT NULL
                    AND si.detected_at > %s
                    AND si.source IN ('gmail', 'slack', 'telegram')
                    ORDER BY si.detected_at DESC
                """, (self.user_id, since_str))

                for row in cursor.fetchall():
                    metadata = {}
                    try:
                        metadata = json.loads(row['source_metadata'] or '{}')
                    except (json.JSONDecodeError, TypeError):
                        pass

                    # Check if this matches the entity mapping (sender matches source_identifier)
                    sender_identifier = self._extract_sender_identifier(row['source'], metadata)
                    em_identifier = row['source_identifier']

                    # Skip if sender doesn't match entity mapping
                    if not self._identifiers_match(sender_identifier, em_identifier, row['source']):
                        continue

                    # Filter out outbound (user's own messages)
                    if self._is_outbound(row['source'], metadata, sender_identifier):
                        continue

                    # Extract content for Haiku evaluation
                    content = self._extract_content(row['source'], metadata)

                    results.append({
                        'scanned_item_id': row['scanned_item_id'],
                        'source': row['source'],
                        'person_id': row['person_id'],
                        'person_name': row['person_name'],
                        'sender_identifier': sender_identifier,
                        'detected_at': row['detected_at'],
                        'last_contact_date': row['last_contact_date'],
                        'content': content,
                    })

        except Exception as e:
            logger.error("PeopleAutoTracker: Error analyzing communications: %r", e)

        return results

    def _group_by_person(self, communications: list[dict]) -> dict[int, dict]:
        """
        Group communications by person_id, keeping most recent per person.

        Returns:
            Dict mapping person_id to most recent communication dict
        """
        by_person = {}

        for comm in communications:
            person_id = comm['person_id']

            if person_id not in by_person:
                by_person[person_id] = comm
            else:
                # Keep most recent
                existing_dt = by_person[person_id].get('detected_at', '')
                new_dt = comm.get('detected_at', '')
                if new_dt > existing_dt:
                    by_person[person_id] = comm

        return by_person

    async def update_contacts(self, by_person: dict[int, dict]) -> dict:
        """
        Update People records with new contact dates and optional context.

        For each person:
        1. Check if scanned date is newer than existing last_contact_date
        2. If newer, call Haiku to evaluate if content is worth noting
        3. Update last_contact_date (always if newer)
        4. Add context to notes (only if worth_noting=true)

        Args:
            by_person: Dict mapping person_id to most recent communication

        Returns:
            Summary dict with counts
        """
        people_updated = 0
        contexts_added = 0
        skipped_manual = 0
        tokens_used = 0

        for person_id, comm in by_person.items():
            try:
                # Parse dates for comparison
                detected_at = comm.get('detected_at', '')
                last_contact = comm.get('last_contact_date')

                # Convert detected_at to date string for comparison
                try:
                    detected_dt = datetime.fromisoformat(detected_at.replace('Z', '+00:00'))
                    scanned_date = detected_dt.strftime('%Y-%m-%d')
                except (ValueError, AttributeError):
                    scanned_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')

                # Skip if user's manual update is more recent
                if last_contact and last_contact >= scanned_date:
                    skipped_manual += 1
                    logger.debug(
                        "PeopleAutoTracker: Skipping %s (manual date %s >= scanned %s)",
                        comm['person_name'], last_contact, scanned_date
                    )
                    continue

                # Evaluate context with Haiku (only if content exists)
                context_to_add = None
                content = comm.get('content', '')

                if content and len(content.strip()) > 10:
                    try:
                        result = await self.evaluate_context(content, comm['person_name'])
                        tokens_used += result.get('tokens', 0)

                        if result.get('worth_noting') and result.get('context'):
                            context_to_add = result['context']
                            contexts_added += 1
                    except Exception as e:
                        logger.warning(
                            "PeopleAutoTracker: Haiku evaluation failed for %s: %r",
                            comm['person_name'], e
                        )

                # Update the person record
                update_fields = {'last_contact_date': scanned_date}

                if context_to_add:
                    # Append context to notes with date prefix
                    existing = await self._get_person_notes(person_id)
                    if existing:
                        new_notes = f"{existing}\n\n[{scanned_date}] {context_to_add}"
                    else:
                        new_notes = f"[{scanned_date}] {context_to_add}"
                    update_fields['notes'] = new_notes

                success = db_update_person(person_id, **update_fields)
                if success:
                    people_updated += 1
                    logger.debug(
                        "PeopleAutoTracker: Updated %s (date=%s, context=%s)",
                        comm['person_name'], scanned_date, bool(context_to_add)
                    )

                    # Log activity for transparency/undo capability
                    try:
                        activity_log_service = ActivityLogService(self.user_id)
                        source_context = self._build_source_context(comm)
                        await activity_log_service.log_activity(
                            person_id=person_id,
                            action_type='auto_update_contact',
                            old_value=last_contact,
                            new_value=scanned_date,
                            context_added=context_to_add,
                            source=comm['source'],
                            source_context=source_context
                        )
                    except Exception as log_err:
                        # Don't fail the update if logging fails
                        logger.warning(
                            "PeopleAutoTracker: Failed to log activity for %s: %r",
                            comm['person_name'], log_err
                        )

            except Exception as e:
                logger.error(
                    "PeopleAutoTracker: Error updating person %d: %r",
                    person_id, e
                )

        return {
            'people_updated': people_updated,
            'contexts_added': contexts_added,
            'skipped_manual': skipped_manual,
            'tokens_used': tokens_used,
        }

    async def evaluate_context(self, content: str, person_name: str) -> dict:
        """
        Use Haiku to decide if message is noteworthy and extract context.

        Args:
            content: Message content (truncated to MAX_CONTENT_CHARS)
            person_name: Name of the person who sent the message

        Returns:
            Dict with worth_noting, context, and tokens used
        """
        # Truncate content
        content = content[:MAX_CONTENT_CHARS]

        prompt = CONTEXT_EVALUATION_PROMPT.format(
            person_name=person_name,
            content=content
        )

        try:
            response = await self._client.messages.create(
                model=CONTEXT_MODEL,
                max_tokens=150,
                messages=[{"role": "user", "content": prompt}]
            )

            response_text = response.content[0].text.strip()
            tokens = response.usage.input_tokens + response.usage.output_tokens

            # Parse JSON response
            result = self._parse_response(response_text)
            result['tokens'] = tokens

            return result

        except Exception as e:
            logger.warning("PeopleAutoTracker: Haiku call failed: %r", e)
            return {'worth_noting': False, 'context': '', 'tokens': 0}

    def _parse_response(self, response_text: str) -> dict:
        """Parse Haiku JSON response with markdown fence handling."""
        # Strip markdown code fences
        if '```' in response_text:
            lines = response_text.split('\n')
            response_text = '\n'.join(
                line for line in lines
                if not line.startswith('```')
            )
        # Extract JSON object — handles trailing text after closing brace
        start = response_text.find('{')
        end = response_text.rfind('}')
        if start != -1 and end != -1:
            response_text = response_text[start:end + 1]

        try:
            parsed = json.loads(response_text)
            return {
                'worth_noting': parsed.get('worth_noting', False),
                'context': parsed.get('context', ''),
            }
        except json.JSONDecodeError as e:
            logger.warning("PeopleAutoTracker: JSON parse failed: %r", e)
            return {'worth_noting': False, 'context': ''}

    async def _load_user_identifiers(self):
        """Load user's own identifiers (emails, Slack IDs, Telegram ID) for outbound filtering."""
        if self._user_identifiers is not None:
            return

        self._user_identifiers = set()

        try:
            with get_db() as conn:
                cursor = conn.cursor()

                # Get user's Gmail accounts
                cursor.execute("""
                    SELECT email FROM google_tokens WHERE user_id = %s
                """, (self.user_id,))
                for row in cursor.fetchall():
                    email = row['email']
                    if email:
                        self._user_identifiers.add(email.lower())

                # Get user's Slack user IDs
                cursor.execute("""
                    SELECT authed_user_id FROM slack_tokens WHERE user_id = %s
                """, (self.user_id,))
                for row in cursor.fetchall():
                    slack_id = row['authed_user_id']
                    if slack_id:
                        self._user_identifiers.add(slack_id)

                # Get user's Telegram chat IDs (private chat ID = Telegram user ID)
                cursor.execute("""
                    SELECT telegram_chat_id FROM telegram_bot_user_links WHERE user_id = %s
                """, (self.user_id,))
                for row in cursor.fetchall():
                    tg_id = row['telegram_chat_id']
                    if tg_id:
                        self._user_identifiers.add(str(tg_id))

        except Exception as e:
            logger.warning("PeopleAutoTracker: Error loading user identifiers: %r", e)

    def _is_outbound(self, source: str, metadata: dict, sender_identifier: str) -> bool:
        """Check if message is outbound (sent by user, not to user)."""
        if not sender_identifier:
            return False

        # Check against user's known identifiers
        sender_lower = sender_identifier.lower()

        for user_id in self._user_identifiers or []:
            if sender_lower == user_id.lower():
                return True
            # For email, check if sender contains the email
            if '@' in sender_lower and user_id in sender_lower:
                return True

        # Additional source-specific checks
        if source == 'telegram':
            # Check is_outgoing flag in Telegram metadata
            if metadata.get('is_outgoing'):
                return True

        return False

    def _extract_sender_identifier(self, source: str, metadata: dict) -> str:
        """Extract sender identifier from scanned item metadata."""
        if source == 'gmail':
            from_field = metadata.get('from', '')
            # Parse "Name <email>" format
            if '<' in from_field and '>' in from_field:
                return from_field[from_field.index('<') + 1:from_field.index('>')].lower()
            elif '@' in from_field:
                return from_field.lower()
            return from_field

        elif source == 'slack':
            user_id = metadata.get('user_id', '')
            team_id = metadata.get('team_id', '')
            if team_id and user_id:
                return f"{team_id}:{user_id}"
            return user_id

        elif source == 'telegram':
            sender_id = metadata.get('sender_id')
            return str(sender_id) if sender_id else ''

        return ''

    def _identifiers_match(self, sender: str, mapping: str, source: str) -> bool:
        """Check if sender identifier matches entity mapping identifier."""
        if not sender or not mapping:
            return False

        sender_lower = sender.lower().strip()
        mapping_lower = mapping.lower().strip()

        # Direct match
        if sender_lower == mapping_lower:
            return True

        # For email, check if one contains the other
        if source == 'gmail' and '@' in sender_lower and '@' in mapping_lower:
            # Extract just the email part if there's extra content
            return sender_lower == mapping_lower

        return False

    def _extract_content(self, source: str, metadata: dict) -> str:
        """Extract message content from metadata for Haiku evaluation."""
        if source == 'gmail':
            subject = metadata.get('subject', '')
            # Note: Gmail scanner stores metadata only, not body
            # Full body would require fetching, but we can use subject for now
            return f"Subject: {subject}"

        elif source == 'slack':
            return metadata.get('text', '')[:MAX_CONTENT_CHARS]

        elif source == 'telegram':
            return metadata.get('text', '')[:MAX_CONTENT_CHARS]

        return ''

    async def _get_person_notes(self, person_id: int) -> str:
        """Get current notes for a person."""
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT notes FROM people WHERE id = %s", (person_id,))
                row = cursor.fetchone()
                return row['notes'] if row and row['notes'] else ''
        except Exception:
            return ''

    def _build_source_context(self, comm: dict) -> dict:
        """
        Build source_context dict for activity logging.

        Includes sender, snippet, timestamp, and message_id.
        Truncates content to first ~100 chars for snippet.
        """
        content = comm.get('content', '')
        snippet = content[:100] + '...' if len(content) > 100 else content

        return {
            'sender': comm.get('sender_identifier', ''),
            'snippet': snippet,
            'timestamp': comm.get('detected_at', ''),
            'message_id': str(comm.get('scanned_item_id', '')),
        }
