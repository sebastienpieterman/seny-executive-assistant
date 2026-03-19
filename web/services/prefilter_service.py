"""
Pre-Filter Service - Rules-based triage of scanned items before AI classification.

Eliminates noise cheaply before any AI tokens are spent. Internal sources
(notes, tasks, contacts, location, conversations) are marked as internal
references for cross-referencing without AI classification.

Phase 14 - Inbound Classification & Cross-Referencing
"""

import json
import logging
import re
from typing import Optional

from web.core.database import (
    get_db,
    insert_item_classification,
    mark_scanned_item_processed,
)

logger = logging.getLogger(__name__)

# Internal sources that bypass AI classification entirely
INTERNAL_SOURCES = {'notes', 'tasks', 'contacts', 'location', 'conversations'}

# Gmail labels that indicate noise
GMAIL_NOISE_LABELS = {
    'CATEGORY_PROMOTIONS', 'CATEGORY_SOCIAL', 'CATEGORY_UPDATES',
    'SPAM', 'TRASH',
}

# Email sender patterns that indicate automated/bulk mail
GMAIL_NOISE_SENDER_PATTERNS = re.compile(
    r'^(noreply|no-reply|notifications|marketing|mailer-daemon|'
    r'news|newsletter|donotreply)@',
    re.IGNORECASE
)

# Domains known to send bulk/automated emails
GMAIL_NOISE_DOMAINS = {
    'googleusercontent.com',
    'facebookmail.com',
    'linkedin.com',
    'quora.com',
    'medium.com',
}


class PreFilterService:
    """Rules-based pre-filter that triages scanned items before AI classification."""

    def __init__(self, user_id: int):
        self.user_id = user_id
        self._user_emails: Optional[set] = None
        self._user_slack_ids: Optional[dict] = None
        self._telegram_bot_chat_ids: Optional[set] = None

    async def filter_batch(self, items: list[dict]) -> dict:
        """
        Filter a batch of unprocessed scanned_items.

        Args:
            items: List of scanned_item dicts (must have id, source, source_metadata, item_type)

        Returns:
            {'passed': [items for AI], 'filtered': count, 'internal': count}
        """
        passed = []
        filtered_count = 0
        internal_count = 0

        for item in items:
            result = self._filter_item(item)

            if result == 'pass':
                passed.append(item)
            elif result == 'internal_reference':
                internal_count += 1
                self._mark_item(item['id'], 'internal_reference')
            elif result == 'filtered':
                filtered_count += 1
                self._mark_item(item['id'], 'filtered')

        total = len(items)
        logger.info(
            "Pre-filter: %d items → %d for AI, %d noise, %d internal refs",
            total, len(passed), filtered_count, internal_count
        )

        return {
            'passed': passed,
            'filtered': filtered_count,
            'internal': internal_count,
        }

    def _filter_item(self, item: dict) -> str:
        """
        Determine disposition of a single scanned item.

        Returns:
            'pass', 'filtered', or 'internal_reference'
        """
        # Outbound items (messages the user sent) are internal references.
        # Check this before any AI call — most efficient gate for outbound traffic.
        if item.get('direction') == 'outbound':
            return 'internal_reference'

        source = item.get('source', '')

        # Internal sources are reference data, not for AI classification
        if source in INTERNAL_SOURCES:
            return 'internal_reference'

        # Parse metadata once
        metadata = item.get('source_metadata')
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        elif metadata is None:
            metadata = {}

        # Source-specific filtering
        if source == 'gmail':
            return self._filter_gmail(metadata)
        elif source == 'slack':
            return self._filter_slack(metadata)
        elif source == 'telegram':
            return self._filter_telegram(metadata)
        elif source == 'calendar':
            return self._filter_calendar(metadata)
        elif source == 'drive':
            return self._filter_drive(metadata)

        # Unknown sources pass through to AI
        return 'pass'

    def _filter_gmail(self, metadata: dict) -> str:
        """Filter Gmail items by labels and sender patterns."""
        # Check labels
        labels = set(metadata.get('labels', []))
        if labels & GMAIL_NOISE_LABELS:
            return 'filtered'

        # Check sender address
        from_addr = metadata.get('from', '').lower()
        if GMAIL_NOISE_SENDER_PATTERNS.search(from_addr):
            return 'filtered'

        # Check sender domain
        if '@' in from_addr:
            domain = from_addr.split('@')[-1].rstrip('>')
            if domain in GMAIL_NOISE_DOMAINS:
                return 'filtered'

        return 'pass'

    def _filter_slack(self, metadata: dict) -> str:
        """Filter Slack items: skip bots and self-messages."""
        # Bot messages: user_id starts with 'B' or username contains 'bot'
        sender_id = metadata.get('user_id', '')
        if sender_id.startswith('B'):
            return 'filtered'

        username = metadata.get('username', '').lower()
        if 'bot' in username:
            return 'filtered'

        # Self-messages: check if sender matches user's Slack ID
        team_id = metadata.get('team_id', '')
        if team_id and self._user_slack_ids:
            user_slack_id = self._user_slack_ids.get(team_id)
            if user_slack_id and sender_id == user_slack_id:
                return 'filtered'

        return 'pass'

    def _filter_telegram(self, metadata: dict) -> str:
        """Filter Telegram items: skip bot-originated messages.

        The screen agent sends messages via the Bot API. These appear in the user's
        Telegram dialog as inbound messages (is_outgoing=False) from the bot.
        Filter them by matching the chat_id against known bot link chat IDs.
        """
        if not self._telegram_bot_chat_ids:
            return 'pass'

        channel_id = str(metadata.get('channel_id', ''))
        is_outgoing = metadata.get('is_outgoing', False)

        # Bot→user: is_outgoing=False AND channel is the bot's private chat
        if channel_id in self._telegram_bot_chat_ids and not is_outgoing:
            return 'filtered'

        return 'pass'

    def _filter_calendar(self, metadata: dict) -> str:
        """Filter calendar events: skip personal time blocks (no attendees)."""
        attendees = metadata.get('attendees', [])
        if not attendees or len(attendees) == 0:
            return 'filtered'
        return 'pass'

    def _filter_drive(self, metadata: dict) -> str:
        """Filter Drive items: skip files modified by the user themselves."""
        modifier = metadata.get('last_modifying_user', '').lower()
        if self._user_emails and modifier in self._user_emails:
            return 'filtered'
        return 'pass'

    def _mark_item(self, item_id: int, classification: str) -> None:
        """Mark a scanned item as processed and insert classification record."""
        mark_scanned_item_processed(item_id, classification)
        insert_item_classification(
            user_id=self.user_id,
            scanned_item_id=item_id,
            relevance=classification,
            urgency=None,
            summary=None,
            model_used=None,
        )

    async def _get_user_emails(self) -> set:
        """Lazy-load user's email addresses from google_tokens."""
        if self._user_emails is not None:
            return self._user_emails
        self._user_emails = set()
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT email FROM google_tokens WHERE user_id = %s",
                    (self.user_id,)
                )
                for row in cursor.fetchall():
                    self._user_emails.add(row['email'].lower())
        except Exception as e:
            logger.error("Failed to load user emails: %s", repr(e))
        return self._user_emails

    async def _get_user_slack_ids(self) -> dict:
        """Lazy-load user's Slack user_ids keyed by team_id."""
        if self._user_slack_ids is not None:
            return self._user_slack_ids
        self._user_slack_ids = {}
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT team_id, authed_user_id FROM slack_tokens WHERE user_id = %s",
                    (self.user_id,)
                )
                for row in cursor.fetchall():
                    self._user_slack_ids[row['team_id']] = row['authed_user_id']
        except Exception as e:
            logger.error("Failed to load user Slack IDs: %s", repr(e))
        return self._user_slack_ids

    async def load_user_context(self) -> None:
        """Pre-load user emails, Slack IDs, and Telegram bot chat IDs for filtering. Call before filter_batch."""
        await self._get_user_emails()
        await self._get_user_slack_ids()
        # Load Telegram bot chat IDs to filter screen agent messages
        try:
            from web.core.database import get_telegram_bot_user_links_for_user
            links = get_telegram_bot_user_links_for_user(self.user_id)
            self._telegram_bot_chat_ids = {
                str(link['telegram_chat_id'])
                for link in links
                if link.get('telegram_chat_id')
            }
        except Exception as e:
            logger.warning("Failed to load telegram bot chat IDs: %s", repr(e))
            self._telegram_bot_chat_ids = set()
