"""
Inbound Classifier - AI classification of scanned items from external sources.

Uses Claude Haiku (default) or Sonnet (premium) to analyze inbound items
that passed the pre-filter: emails, Slack messages, Telegram messages,
calendar events, and Drive file changes.

Key difference from ClassificationService: that service classifies what the
USER said to Seny. This classifier analyzes what OTHERS sent to the user.

Phase 14 - Inbound Classification & Cross-Referencing
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from anthropic import AsyncAnthropic

from web.services.semantic_search_service import SemanticSearchService
from web.core.database import (
    get_db,
    get_people_by_user,
    get_projects_by_user,
    insert_item_classification,
    mark_scanned_item_processed,
    is_sender_ignored,
    get_scanner_preferences,
    get_user_identifiers,
)
from src.core.config import Config

logger = logging.getLogger(__name__)

# Default model for classification
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# Maximum content length sent to Haiku (keeps token cost low)
MAX_CONTENT_CHARS = 2000


def _relative_time(dt_input) -> str:
    """Convert a datetime string or Unix timestamp to a human-readable relative time.

    Accepts ISO datetime strings (Telegram) or Unix timestamp strings/floats (Slack).
    Returns strings like '5m ago', '2h ago', '3d ago', or 'just now'.
    Returns '' on any parse failure.
    """
    try:
        if isinstance(dt_input, (int, float)):
            dt = datetime.fromtimestamp(float(dt_input), tz=timezone.utc)
        elif isinstance(dt_input, str) and dt_input:
            try:
                dt = datetime.fromtimestamp(float(dt_input), tz=timezone.utc)
            except ValueError:
                dt = datetime.fromisoformat(dt_input.replace('Z', '+00:00'))
        else:
            return ''

        now = datetime.now(timezone.utc)
        # Clamp negative deltas (future timestamps) to 0
        total_seconds = max(0, int((now - dt).total_seconds()))

        if total_seconds < 60:
            return 'just now'
        elif total_seconds < 3600:
            return f'{total_seconds // 60}m ago'
        elif total_seconds < 86400:
            return f'{total_seconds // 3600}h ago'
        else:
            return f'{total_seconds // 86400}d ago'
    except Exception:
        return ''


# Classification prompt template for inbound items
INBOUND_CLASSIFICATION_PROMPT = """You are analyzing an inbound message to determine if it needs attention.

Item details:
- Source: {source}
- From: {sender}
- Date: {date}
- Content: {content}

Related past context (semantically similar past items):
{related_context}

Tracked people: {people_names}
Active projects: {project_names}
{user_names_instruction}
Use related context to:
- Detect if this continues a known conversation or topic
- Recognize this sender's communication patterns
- Connect the message to known projects or ideas
- Improve urgency assessment (e.g. a long-pending item is more urgent)
If related context is empty, classify based on content alone.

Analyze and respond with JSON:
{{
  "relevance": "actionable|informational|noise",
  "urgency": "urgent|normal|low",
  "summary": "One-line summary written in second person (use 'you' not 'the user')",
  "people_mentioned": ["Name1", "Name2"],
  "projects_related": ["Project Name"],
  "ideas_related": ["Idea keyword"],
  "actions": [
    {{"action": "What you need to do (use 'you' not 'the user')", "type": "reply|follow_up|commitment|deadline|review", "person": "Name or null", "deadline": "ISO date or null"}}
  ],
  "reasoning": "Brief explanation"
}}

Relevance guide:
- actionable: Requires response or action (direct messages, questions, requests, deadlines)
- informational: Useful to know but no action needed (FYI emails, status updates, shared docs)
- noise: Not useful (automated notifications that passed pre-filter, irrelevant chatter)

Urgency guide (only for actionable):
- urgent: Deadline today/tomorrow, someone waiting on a response, time-sensitive
- normal: Should address this week
- low: No time pressure

CRITICAL: You are always writing for the SYSTEM OWNER — the person who reads these action items. "You" always means the owner. Never generate action items for the other party in a conversation. Your only job is to surface what the OWNER needs to do.
- If the owner received a message → identify what they need to do in response
- If the owner sent a message → extract any commitments they made that they still need to follow through on
- NEVER generate items like "reply to [owner's name]" or "follow up with [owner's name]" — the owner cannot send messages to themselves

IMPORTANT: In summaries and actions, always use "you" instead of "the user". Write as if speaking directly to the owner.

IMPORTANT: Consider the date of this message carefully. Ask yourself: given when this was sent and what it's about, is this situation likely still open and actionable? Time-sensitive conversations (decisions with natural deadlines, in-progress negotiations, event-based discussions) that are several days old have often already resolved. If the action item is likely stale, classify as "noise" rather than "actionable". Use your judgment — err on the side of not bothering the user with things that have probably already been handled.

IMPORTANT: Some messages in thread context are marked [YOU] with a timestamp (e.g. "[YOU] (2h ago): ..."). These are messages the owner themselves sent.
- NEVER generate action items telling the owner to respond to, contact, or follow up with themselves
- If thread context shows recent [YOU] messages (within the last few hours), this is an ACTIVE conversation — classify as "noise" unless there is a NEW, specific, unresolved request in the CURRENT message that clearly has not yet been addressed
- If the [YOU] messages are older (hours or days before the current message), the owner may genuinely need to re-engage — apply normal judgment
- DO reason critically about whether any [YOU] message contains a commitment, promise, or obligation the owner took on — if so, surface it as an action item they still need to follow through on
- Think broadly about implied commitments, not just explicit phrases like "I'll do this" — consider context and intent

Respond with ONLY valid JSON. No explanation outside the JSON."""


class InboundClassifier:
    """Classifies scanned items from external sources using Claude AI."""

    def __init__(self, user_id: int):
        self.user_id = user_id
        self._client = AsyncAnthropic(api_key=Config.ANTHROPIC_API_KEY)
        self._model: Optional[str] = None  # lazy-loaded from user_settings
        self._people_names: Optional[list[str]] = None  # lazy-loaded
        self._project_names: Optional[list[str]] = None  # lazy-loaded
        self._semantic_search: Optional[SemanticSearchService] = None  # lazy-loaded
        self._user_identifiers: Optional[dict] = None  # lazy-loaded for ISS-003 outbound filter

    async def classify_item(self, item: dict) -> Optional[dict]:
        """
        Classify a single scanned_item that passed pre-filter.

        Args:
            item: scanned_item dict with id, source, source_id, source_metadata, etc.

        Returns:
            Classification result dict, or None if classification failed.
        """
        item_id = item.get('id')
        source = item.get('source', '')

        try:
            # Check if sender is ignored before spending AI tokens
            sender_identifier = self._extract_sender_identifier(item)
            if sender_identifier and is_sender_ignored(self.user_id, source, sender_identifier):
                logger.info(f"Skipping ignored sender in classification: {source}/{sender_identifier}")
                mark_scanned_item_processed(item_id, 'ignored_sender')
                return {
                    'relevance': 'filtered',
                    'urgency': 'none',
                    'summary': 'Ignored sender',
                    'reason': 'ignored_sender'
                }

            # HF-04: direction column hard gate — if HF-03 already marked this outbound, trust it
            if item.get('direction') == 'outbound':
                logger.info(f"Skipping outbound item (direction flag) in classification: {source}/{item_id}")
                mark_scanned_item_processed(item_id, 'outbound')
                return {'classification': 'outbound', 'summary': 'Sent by user', 'reason': 'outbound_direction'}

            # ISS-003: Skip messages sent by the user themselves
            if not self._user_identifiers:
                await self._load_user_identifiers()
            if self._is_outbound(item):
                logger.info(f"Skipping outbound item in classification: {source}/{item_id}")
                mark_scanned_item_processed(item_id, 'outbound')
                return {'classification': 'outbound', 'summary': 'Sent by user', 'reason': 'outbound'}

            # Enrich content (e.g. fetch Gmail body, Slack thread context)
            content = await self._enrich_content(item)

            # Extract thread sidecar data stashed by _enrich_* (if applicable)
            thread_context = item.pop('_thread_context', None)
            thread_id = item.pop('_thread_id', None)
            thread_summary = None

            # Summarize long thread contexts to keep classification prompt within token budget
            if thread_context and len(thread_context) > 2500:
                source = item.get('source', '')
                thread_summary = await self._summarize_thread_context(thread_context, source)
                # Replace oversized content with summary + current message only
                parts = content.rsplit('\n\n', 1)
                current_msg_part = parts[-1] if len(parts) > 1 else content
                content = f"[Thread summary]: {thread_summary}\n\n{current_msg_part}"

            # Build prompt with people/project context
            prompt = await self._build_prompt(item, content)

            # Get model preference
            model = await self._get_model()

            # Call Claude
            response = await self._client.messages.create(
                model=model,
                max_tokens=500,
                messages=[{
                    'role': 'user',
                    'content': prompt,
                }]
            )

            response_text = response.content[0].text.strip()
            classification = self._parse_response(response_text)

            if classification is None:
                # JSON parse failed — mark processed, skip
                mark_scanned_item_processed(item_id, 'parse_error')
                return None

            relevance = classification.get('relevance', 'noise')
            urgency = classification.get('urgency', 'normal')
            summary = classification.get('summary', '')

            # Map relevance to confidence for scanned_items table
            confidence_map = {
                'actionable': 0.9,
                'informational': 0.5,
                'noise': 0.1,
            }
            confidence = confidence_map.get(relevance, 0.1)

            # Store in item_classifications
            insert_item_classification(
                user_id=self.user_id,
                scanned_item_id=item_id,
                relevance=relevance,
                urgency=urgency,
                summary=summary,
                extracted_entities=json.dumps({
                    'people_mentioned': classification.get('people_mentioned', []),
                    'projects_related': classification.get('projects_related', []),
                    'ideas_related': classification.get('ideas_related', []),
                }),
                extracted_actions=json.dumps(classification.get('actions', [])),
                model_used=model,
                thread_context=thread_context,
                thread_summary=thread_summary,
                thread_id=thread_id,
            )

            # Mark scanned item as processed
            mark_scanned_item_processed(item_id, relevance)

            logger.info(
                "Classified item %d (%s): %s (urgency=%s) — %s",
                item_id, source, relevance, urgency, summary[:60]
            )

            return classification

        except Exception as e:
            # Haiku call or other failure — mark processed so we don't retry forever
            logger.error(
                "Classification failed for item %d (%s): %s",
                item_id, source, repr(e)
            )
            mark_scanned_item_processed(item_id, 'error')
            return None

    async def _enrich_content(self, item: dict) -> str:
        """
        Fetch full content for sources that need enrichment.

        Gmail: fetch full email body via GmailService.read_email().
        Other sources: use metadata text directly.

        Args:
            item: scanned_item dict

        Returns:
            Content string for the classification prompt.
        """
        source = item.get('source', '')
        metadata = item.get('source_metadata')
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        elif metadata is None:
            metadata = {}

        if source == 'gmail':
            content, thread_id = await self._enrich_gmail(item, metadata)
            item['_thread_id'] = thread_id
            # thread_context is content only when it includes thread chain (has "[Email thread context")
            item['_thread_context'] = content if '[Email thread context' in content else None
            return content
        elif source == 'slack':
            content, thread_id = await self._enrich_slack(item, metadata)
            # Store thread_id on item dict for classify_item() to pick up
            item['_thread_id'] = thread_id
            item['_thread_context'] = content if thread_id else None
            return content
        elif source == 'telegram':
            content, thread_id = await self._enrich_telegram(item, metadata)
            item['_thread_id'] = thread_id
            item['_thread_context'] = content if 'Recent messages in' in content else None
            return content
        elif source == 'calendar':
            return self._enrich_calendar(metadata)
        elif source == 'drive':
            return self._enrich_drive(metadata)

        # Fallback
        return metadata.get('text', metadata.get('subject', 'No content available'))

    async def _enrich_gmail(self, item: dict, metadata: dict) -> tuple:
        """Fetch full Gmail body + thread chain. Returns (content, thread_id)."""
        source_id = item.get('source_id', '')
        account_email = metadata.get('account', metadata.get('account_email', ''))
        thread_id = metadata.get('threadId') or metadata.get('thread_id')

        # --- Existing body-fetch logic (unchanged) ---
        fallback = f"Subject: {metadata.get('subject', '(no subject)')}\nFrom: {metadata.get('from', 'unknown')}"
        snippet = metadata.get('snippet', '')
        if snippet:
            fallback += f"\nSnippet: {snippet}"

        body_content = fallback
        if source_id and account_email:
            try:
                from web.services.gmail_service import GmailService
                gmail = GmailService(self.user_id, account_email)
                email_data = await gmail.read_email(source_id)
                if email_data and email_data.get('body_text'):
                    body = email_data['body_text'][:MAX_CONTENT_CHARS]
                    body_content = (
                        f"Subject: {email_data.get('subject', '(no subject)')}\n"
                        f"From: {email_data.get('from', 'unknown')}\n"
                        f"Body:\n{body}"
                    )
            except Exception as e:
                logger.warning(
                    "Gmail body fetch failed for %s: %s — using metadata",
                    source_id, repr(e)
                )

        # --- New: fetch thread chain for context ---
        if not thread_id or not account_email:
            return body_content, thread_id

        try:
            from web.services.gmail_service import GmailService
            gmail = GmailService(self.user_id, account_email)
            thread_emails = await gmail.get_email_thread(thread_id, exclude_message_id=source_id)
        except Exception as e:
            logger.warning("Gmail thread context fetch failed: %s", repr(e))
            thread_emails = []

        if not thread_emails:
            return body_content, thread_id

        # Build thread context prefix (cap at 1200 chars)
        thread_lines = ["[Email thread context — earlier messages:]"]
        char_count = 0
        user_emails = {e.lower() for e in (self._user_identifiers or {}).get('emails', [])}
        for em in thread_emails:
            from_addr = (em.get('from') or '').lower()
            is_own = any(e in from_addr for e in user_emails)
            sender_label = "[YOU]" if is_own else f"From: {em['from']}"
            line = f"  {sender_label} | Date: {em['date']}\n  Subject: {em['subject']}\n  {em['snippet']}"
            if char_count + len(line) > 1200:
                break
            thread_lines.append(line)
            char_count += len(line)

        thread_prefix = "\n".join(thread_lines)
        full_content = f"{thread_prefix}\n\n[Current email:]\n{body_content}"
        return full_content, thread_id

    async def _enrich_slack(self, item: dict, metadata: dict) -> tuple:
        """
        Build content string from Slack metadata, fetching thread replies if available.
        Returns (content_string, thread_id_or_None).
        """
        from web.services.slack_service import SlackService

        text = metadata.get('text', '')[:MAX_CONTENT_CHARS]
        channel_id = metadata.get('channel_id', '')
        channel_name = metadata.get('channel_name', channel_id)
        username = metadata.get('username', metadata.get('user_id', 'unknown'))
        thread_ts = metadata.get('thread_ts')
        msg_ts = metadata.get('ts', '')
        team_id = metadata.get('team_id')

        # thread_ts present and differs from msg ts = this message IS a reply
        # thread_ts equals msg ts = this message IS the thread root (may have replies)
        is_in_thread = bool(thread_ts)
        thread_id = thread_ts if is_in_thread else None

        base_content = f"Channel: {channel_name}\nFrom: {username}\nMessage: {text}"

        if not is_in_thread or not channel_id or not team_id:
            return base_content, thread_id

        # Fetch thread replies
        try:
            workspaces = SlackService.list_connected_workspaces(self.user_id)
            slack = None
            for ws in workspaces:
                if ws.get('team_id') == team_id:
                    slack = SlackService(self.user_id, ws['team_id'])
                    break
            if slack is None:
                slack = SlackService(self.user_id, team_id)

            replies = await slack.get_thread_replies(channel_id, thread_ts, limit=10)
        except Exception as e:
            logger.warning("Slack thread fetch failed for %s/%s: %s", channel_id, thread_ts, repr(e))
            return base_content, thread_id

        if not replies:
            return base_content, thread_id

        # Build thread context (cap at 1500 chars total)
        thread_lines = [f"[Thread in #{channel_name}]"]
        char_count = 0
        slack_ids = {sid.lower() for sid in (self._user_identifiers or {}).get('slack_ids', [])}
        for r in replies:
            user_id = (r.get('user') or '').lower()
            rel_time = _relative_time(r.get('ts', ''))
            time_str = f" ({rel_time})" if rel_time else ""
            if user_id and user_id in slack_ids:
                line = f"  [YOU]{time_str}: {r['text']}"
            else:
                line = f"  {r['user']}{time_str}: {r['text']}"
            if char_count + len(line) > 1500:
                break
            thread_lines.append(line)
            char_count += len(line)

        thread_context_str = "\n".join(thread_lines)
        content = f"{thread_context_str}\n\nCurrent message ({username}): {text}"
        return content, thread_id

    async def _enrich_telegram(self, item: dict, metadata: dict) -> tuple:
        """
        Build content string from Telegram metadata, fetching recent chat history.
        Returns (content_string, thread_id_or_None).

        thread_id for Telegram = "{chat_id}" (no per-message threading in Telegram,
        so the chat itself is the thread context unit).
        """
        from web.services.telegram_service import TelegramService

        text = metadata.get('text', '')[:MAX_CONTENT_CHARS]
        chat_id = metadata.get('chat_id')
        chat_name = metadata.get('chat_name', metadata.get('chat_title', str(chat_id)))
        sender = metadata.get('sender_name', metadata.get('from', 'unknown'))
        msg_id = metadata.get('message_id') or item.get('source_id', '').split(':')[-1]

        # thread_id for Telegram = the chat_id (chat is the context boundary)
        thread_id = str(chat_id) if chat_id else None
        base_content = f"Chat: {chat_name}\nFrom: {sender}\nMessage: {text}"

        if not chat_id:
            return base_content, thread_id

        # Fetch recent chat history for context
        try:
            telegram = TelegramService(self.user_id)
            if not telegram.is_configured() or not telegram.is_connected():
                return base_content, thread_id
            if not await telegram.connect():
                return base_content, thread_id

            # Fetch last 8 messages from this chat (includes current msg — filter it out)
            messages = await telegram.get_messages(chat_id, limit=8)

            # Exclude the current message (match by text as proxy since msg_id format varies)
            history = [
                m for m in messages
                if (m.get('text', '') or '').strip() != text.strip()
            ][:6]  # keep at most 6 context messages

        except Exception as e:
            logger.warning(
                "Telegram history fetch failed for chat %s: %s", chat_id, repr(e)
            )
            return base_content, thread_id

        if not history:
            return base_content, thread_id

        # Build context string (cap at 1500 chars)
        context_lines = [f"[Recent messages in {chat_name}:]"]
        char_count = 0
        for m in reversed(history):  # oldest first for narrative order
            name = m.get('sender', 'unknown')
            msg_text = (m.get('text', '') or '')[:200]
            rel_time = _relative_time(m.get('date', ''))
            time_str = f" ({rel_time})" if rel_time else ""
            if m.get('is_outgoing'):
                line = f"  [YOU]{time_str}: {msg_text}"
            else:
                line = f"  {name}{time_str}: {msg_text}"
            if char_count + len(line) > 1500:
                break
            context_lines.append(line)
            char_count += len(line)

        context_prefix = "\n".join(context_lines)
        full_content = f"{context_prefix}\n\nCurrent message (from {sender}): {text}"
        return full_content, thread_id

    async def _summarize_thread_context(self, context: str, source: str) -> str:
        """
        Summarize a long thread context using Haiku.
        Called only when len(context) > 2500 chars.
        Result cached in memory for 10 minutes using context hash as key.

        Returns the summary string.
        """
        from web.core.cache import response_cache
        import hashlib

        cache_key = f"thread_summary:{hashlib.md5(context.encode()).hexdigest()}"
        cached = response_cache.get(cache_key)
        if cached:
            return cached

        summary_prompt = (
            "Summarize this conversation thread in 2-4 sentences. "
            "Focus on: who is involved, what is being discussed, and what outcome or decision emerged (if any). "
            "Be factual and concise. Do not editorialize.\n\n"
            f"Thread ({source}):\n{context[:4000]}"
        )

        try:
            response = await self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{'role': 'user', 'content': summary_prompt}]
            )
            summary = response.content[0].text.strip()
            response_cache.set(cache_key, summary, ttl_seconds=600)  # 10-min TTL
            return summary

        except Exception as e:
            logger.warning("Thread summarization failed: %s", repr(e))
            # Fallback: truncate to 2000 chars
            return context[:2000] + "... [truncated]"

    def _enrich_calendar(self, metadata: dict) -> str:
        """Build content string from Calendar event metadata."""
        summary = metadata.get('summary', '(untitled event)')
        attendees = metadata.get('attendees', [])
        description = metadata.get('description_snippet', metadata.get('description', ''))
        start = metadata.get('start', '')
        end = metadata.get('end', '')

        parts = [f"Event: {summary}"]
        if start:
            parts.append(f"Time: {start} — {end}")
        if attendees:
            if isinstance(attendees, list):
                parts.append(f"Attendees: {', '.join(str(a) for a in attendees)}")
            else:
                parts.append(f"Attendees: {attendees}")
        if description:
            parts.append(f"Description: {str(description)[:500]}")

        return '\n'.join(parts)

    def _enrich_drive(self, metadata: dict) -> str:
        """Build content string from Drive file metadata."""
        filename = metadata.get('name', metadata.get('filename', 'unknown file'))
        mime_type = metadata.get('mime_type', '')
        modified = metadata.get('modified_time', metadata.get('modified_date', ''))
        owner = metadata.get('owner', metadata.get('last_modifying_user', ''))

        parts = [f"File: {filename}"]
        if mime_type:
            parts.append(f"Type: {mime_type}")
        if owner:
            parts.append(f"Modified by: {owner}")
        if modified:
            parts.append(f"Modified: {modified}")

        return '\n'.join(parts)

    async def _build_prompt(self, item: dict, content: str) -> str:
        """Build source-aware prompt with people/project context injected."""
        metadata = item.get('source_metadata')
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        elif metadata is None:
            metadata = {}

        source = item.get('source', 'unknown')
        sender = metadata.get('from', metadata.get('username', metadata.get('sender_name', 'unknown')))
        date = metadata.get('date', metadata.get('timestamp', item.get('created_at', '')))

        people_names = await self._get_people_context()
        project_names = await self._get_project_context()

        related_context = await self._get_semantic_context(content)
        if not related_context:
            related_context = "(none found)"

        # Build user-names instruction to prevent "respond to yourself" action items
        user_display_names = (self._user_identifiers or {}).get('display_names', [])
        if user_display_names:
            names_str = ', '.join(f'"{n}"' for n in user_display_names)
            user_names_instruction = (
                f"IMPORTANT: The user's own names are: {names_str}. "
                "NEVER generate action items asking the user to respond to, contact, "
                "or follow up with any of these names — they always refer to the user themselves."
            )
        else:
            user_names_instruction = ""

        return INBOUND_CLASSIFICATION_PROMPT.format(
            source=source,
            sender=sender,
            date=date,
            content=content,
            related_context=related_context,
            people_names=', '.join(people_names) if people_names else '(none tracked yet)',
            project_names=', '.join(project_names) if project_names else '(none active)',
            user_names_instruction=user_names_instruction,
        )

    async def _get_model(self) -> str:
        """
        Load classification model based on user's tier preference.

        Phase 18-02: Uses classification_tier ('haiku' or 'full') to select model.
        - 'haiku': Fast & economical (claude-haiku-4-5-20251001)
        - 'full': Thorough analysis (claude-sonnet-4-5-20250929)
        """
        if self._model is not None:
            return self._model

        try:
            prefs = get_scanner_preferences(self.user_id)
            tier = prefs.get('classification_tier', 'haiku')

            if tier == 'full':
                self._model = "claude-sonnet-4-5-20250929"
                logger.debug("Using Sonnet model for user %d (tier=full)", self.user_id)
            else:
                self._model = DEFAULT_MODEL
                logger.debug("Using Haiku model for user %d (tier=haiku)", self.user_id)
        except Exception as e:
            logger.warning("Failed to load classification tier setting: %s", repr(e))
            self._model = DEFAULT_MODEL

        return self._model

    async def _get_people_context(self) -> list[str]:
        """Load user's People names for context injection."""
        if self._people_names is not None:
            return self._people_names

        try:
            people = get_people_by_user(self.user_id, limit=200)
            self._people_names = [p['name'] for p in people if p.get('name')]
        except Exception as e:
            logger.warning("Failed to load people context: %s", repr(e))
            self._people_names = []

        return self._people_names

    async def _get_project_context(self) -> list[str]:
        """Load user's active Project names for context injection."""
        if self._project_names is not None:
            return self._project_names

        try:
            projects = get_projects_by_user(self.user_id, status='active', limit=100)
            self._project_names = [p['name'] for p in projects if p.get('name')]
        except Exception as e:
            logger.warning("Failed to load project context: %s", repr(e))
            self._project_names = []

        return self._project_names

    async def _get_semantic_context(self, content: str) -> str:
        """
        Query ChromaDB for semantically similar past items to enrich classification.

        Returns a formatted string of top-3 similar items (capped at 600 chars),
        or empty string if embeddings are disabled, no results found, or any error occurs.

        Phase 25: Never lets errors propagate — classification must always complete.
        """
        try:
            # Lazy-init SemanticSearchService
            if self._semantic_search is None:
                self._semantic_search = SemanticSearchService()

            # Graceful degradation when VOYAGE_API_KEY is missing
            if not self._semantic_search.embedding_service.enabled:
                return ""

            results = self._semantic_search.search(
                self.user_id,
                content,
                entity_types=["items", "notes", "conversations"],
                n_results=5,
                threshold=1.3,
            )

            logger.debug(
                "Semantic context for user %d: %d results found", self.user_id, len(results)
            )

            if not results:
                return ""

            # Format top-3 results
            lines = []
            total_chars = 0
            for r in results[:3]:
                entity_type = r.get("entity_type", "unknown")
                similarity = r.get("similarity", 0.0)
                text = r.get("text", "")[:150]
                line = f"- [{entity_type}] (similarity: {similarity:.2f}): {text}"
                if total_chars + len(line) > 600:
                    # Truncate at word boundary
                    remaining = 600 - total_chars
                    if remaining > 20:
                        line = line[:remaining].rsplit(" ", 1)[0]
                        lines.append(line)
                    break
                lines.append(line)
                total_chars += len(line)

            return "\n".join(lines)

        except Exception as e:
            logger.warning("Semantic context fetch failed: %s", repr(e))
            return ""

    async def _load_user_identifiers(self) -> dict:
        """
        Load and cache the user's own identifiers for outbound filtering (ISS-003).

        Queries google_tokens, slack_tokens, and user_settings to collect the user's
        own email addresses, Slack member IDs, and Telegram user IDs.

        Returns:
            Dict with 'emails', 'slack_ids', 'telegram_ids' lists.
            Cached in self._user_identifiers after first call.
        """
        if self._user_identifiers is not None:
            return self._user_identifiers

        try:
            self._user_identifiers = get_user_identifiers(self.user_id)
        except Exception as e:
            logger.warning("InboundClassifier: failed to load user identifiers: %s", repr(e))
            self._user_identifiers = {'emails': [], 'slack_ids': [], 'telegram_ids': []}

        return self._user_identifiers

    def _is_outbound(self, item: dict) -> bool:
        """
        Return True if this item was sent BY the user (not to them).

        ISS-003: Prevents the classifier from flagging the user's own sent messages
        as items needing attention (e.g. "Reply to yourself").

        Uses self._user_identifiers — call _load_user_identifiers() first.
        Safe default: returns False if identifiers are not yet loaded.

        Args:
            item: scanned_item dict with source and source_metadata.

        Returns:
            True if the sender matches any of the user's own identifiers.
        """
        if not self._user_identifiers:
            return False  # Safe default — never skip if identifiers aren't loaded

        source = item.get('source', '')
        sender_identifier = self._extract_sender_identifier(item)

        if not sender_identifier:
            return False

        sender_lower = sender_identifier.lower()

        if source == 'gmail':
            for email in self._user_identifiers.get('emails', []):
                if email and (sender_lower == email or email in sender_lower):
                    return True

        elif source == 'slack':
            # sender_identifier format is "channel_id:user_id" or just "user_id"
            for slack_id in self._user_identifiers.get('slack_ids', []):
                if slack_id and slack_id.lower() in sender_lower:
                    return True

        elif source == 'telegram':
            # sender_identifier format is "chat_id:sender_id" or just "sender_name"
            for tg_id in self._user_identifiers.get('telegram_ids', []):
                if tg_id and tg_id in sender_lower:
                    return True

        return False

    def _parse_response(self, response_text: str) -> Optional[dict]:
        """
        Parse Haiku JSON response with markdown fence handling.

        Same pattern as ClassificationService: strip ```json fences if present.
        """
        # Handle markdown code fences
        if response_text.startswith('```'):
            lines = response_text.split('\n')
            response_text = '\n'.join(
                line for line in lines
                if not line.startswith('```')
            )

        try:
            return json.loads(response_text)
        except json.JSONDecodeError as e:
            logger.warning(
                "Failed to parse classification JSON: %s — response was: %s",
                repr(e), response_text[:200]
            )
            return None

    def _extract_sender_identifier(self, item: dict) -> Optional[str]:
        """
        Extract sender identifier from a scanned item for ignore list checking.

        Phase 18-01: Pre-classification check to skip AI costs for ignored senders.

        Args:
            item: scanned_item dict with source_metadata

        Returns:
            Sender identifier string or None
        """
        source = item.get('source', '')
        metadata = {}

        try:
            metadata = json.loads(item.get('source_metadata') or '{}')
        except (ValueError, TypeError):
            return None

        if source == 'gmail':
            # Email: use 'from' address
            return metadata.get('from') or metadata.get('sender')
        elif source == 'slack':
            # Slack: channel:sender_id or just sender_name
            sender = metadata.get('sender_id') or metadata.get('sender_name', '')
            channel = metadata.get('channel_id', '')
            if channel and sender:
                return f"{channel}:{sender}"
            return sender if sender else None
        elif source == 'telegram':
            # Telegram: chat_id:sender_id or just sender_name
            sender = metadata.get('sender_id') or metadata.get('sender_name', '')
            chat_id = metadata.get('chat_id', '')
            if chat_id and sender:
                return f"{chat_id}:{sender}"
            return sender if sender else None
        else:
            return metadata.get('from') or metadata.get('sender')
