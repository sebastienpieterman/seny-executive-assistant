"""
Scheduling Extractor — detects scheduling intent in Gmail items and extracts event details.

Uses a dedicated Haiku call separate from InboundClassifier to avoid bloating the
shared classification prompt used across all 5 sources.

Phase 51 — Email → Calendar Pipeline
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from anthropic import AsyncAnthropic

from src.core.config import Config

logger = logging.getLogger(__name__)

SCHEDULING_EXTRACTION_PROMPT = """You are extracting scheduling information from an email.

Subject: {subject}
From: {sender}
Date: {email_date}
Body/Snippet:
{content}

Today's date: {today}

Does this email propose or confirm a specific meeting with a concrete date AND time?

Respond with JSON:
{{
  "has_specific_time": true,
  "event_title": "Brief meeting title (10 words max)",
  "start_datetime": "ISO 8601 datetime e.g. 2026-03-12T15:00:00",
  "end_datetime": "ISO 8601 datetime",
  "location": "string or null",
  "attendees": ["name or email"],
  "notes": "one sentence of context or null"
}}

OR if no specific time:
{{
  "has_specific_time": false
}}

Rules:
- has_specific_time: true ONLY when BOTH a specific day AND a specific time are mentioned.
  Examples that qualify: "Thursday at 3pm", "March 12 at 2:30", "tomorrow at noon", "Monday 10am".
  Examples that do NOT qualify: "next week", "soon", "let's find a time", "are you free?",
  "Tuesday" (day only, no time), "3pm" (time only, no day).
- start_datetime: Resolve relative references ("tomorrow", "Thursday") using today's date.
  If timezone not mentioned, omit timezone suffix.
- end_datetime: If duration mentioned, calculate it. Otherwise, add 1 hour to start.
- Ignore: marketing/sales emails ("schedule a demo"), automated notifications, calendar
  digest summaries, confirmation emails for already-scheduled meetings already on calendar.
- If has_specific_time is false, return ONLY {{"has_specific_time": false}}.

Respond with ONLY valid JSON. No explanation outside the JSON."""


class SchedulingExtractor:
    """
    Extracts scheduling details from a Gmail scanned_item.

    Called by InboundProcessor after InboundClassifier marks an item as
    actionable or informational. Returns None if no specific time is found
    or if the email is stale (> 14 days old).
    """

    STALENESS_DAYS = 14

    def __init__(self, user_id: int):
        self.user_id = user_id
        self._client = AsyncAnthropic(api_key=Config.ANTHROPIC_API_KEY)

    async def extract(self, item: dict, today: str) -> Optional[dict]:
        """
        Extract scheduling details from a Gmail scanned_item.

        Args:
            item: scanned_item dict (must have source_metadata with subject, from,
                  snippet, date, thread_id fields populated by scanner_service)
            today: ISO date string for today (e.g. "2026-03-06")

        Returns:
            Dict with event_title, start_datetime, end_datetime, location, attendees,
            notes — or None if no specific time found, email is stale, or any error.
        """
        try:
            metadata = item.get('source_metadata') or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (json.JSONDecodeError, TypeError):
                    metadata = {}

            # Staleness gate: skip emails older than STALENESS_DAYS
            email_date_str = metadata.get('date', '')
            if email_date_str:
                try:
                    # Gmail date format varies; try common patterns
                    from email.utils import parsedate_to_datetime
                    email_dt = parsedate_to_datetime(email_date_str)
                    cutoff = datetime.now(email_dt.tzinfo) - timedelta(days=self.STALENESS_DAYS)
                    if email_dt < cutoff:
                        print(f"[SchedulingExtractor] item {item.get('id')} STALE — skipping (date={email_date_str})", flush=True)
                        return None
                except Exception:
                    pass  # Can't parse date — proceed with extraction

            subject = metadata.get('subject', '(no subject)')
            sender = metadata.get('from', 'unknown')
            snippet = metadata.get('snippet', '')
            content = snippet or '(no preview available)'

            print(f"[SchedulingExtractor] item {item.get('id')} — subject={subject[:60]!r} sender={sender[:40]!r} snippet={content[:80]!r}", flush=True)

            prompt = SCHEDULING_EXTRACTION_PROMPT.format(
                subject=subject,
                sender=sender,
                email_date=email_date_str or 'unknown',
                content=content,
                today=today,
            )

            response = await self._client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=400,
                messages=[{'role': 'user', 'content': prompt}],
            )

            response_text = response.content[0].text.strip()

            # Strip markdown code fences if present
            if response_text.startswith('```'):
                lines = response_text.split('\n')
                response_text = '\n'.join(
                    line for line in lines
                    if not line.startswith('```')
                ).strip()

            result = json.loads(response_text)

            if not result.get('has_specific_time'):
                print(f"[SchedulingExtractor] item {item.get('id')} — has_specific_time=false (no proposal)", flush=True)
                return None

            # Validate required fields
            if not result.get('start_datetime'):
                print(f"[SchedulingExtractor] item {item.get('id')} — has_specific_time=true but no start_datetime", flush=True)
                return None

            print(f"[SchedulingExtractor] item {item.get('id')} — MATCH title={result.get('event_title','')[:40]!r} start={result.get('start_datetime')}", flush=True)
            return {
                'event_title': result.get('event_title', subject[:60]),
                'start_datetime': result['start_datetime'],
                'end_datetime': result.get('end_datetime'),
                'location': result.get('location'),
                'attendees': result.get('attendees', []),
                'notes': result.get('notes'),
            }

        except Exception as e:
            logger.warning(
                "SchedulingExtractor.extract failed for item %s: %s",
                item.get('id'), repr(e)
            )
            return None
