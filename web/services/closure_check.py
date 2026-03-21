"""
Loop closure detection for drip nudges.

Before sending an eligible nudge, fetches conversation context from
scanned_items and asks Haiku whether the situation was already resolved.

Phase 70.1-02.
"""
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Nudge type sets (defined in Plan 01 — import from nudge_service)
from web.services.nudge_service import (
    CLOSURE_CHECK_INCLUDE,
    CLOSURE_CHECK_EXCLUDE_TIME_SENSITIVE,
    CLOSURE_CHECK_NO_THREAD,
)

MESSAGES_BEFORE = 12   # messages before triggering item for context
MESSAGES_AFTER = 38    # messages after triggering item to detect closure
MAX_DAYS_AFTER = 7     # don't look more than 7 days after nudge creation


def get_person_memories(user_id: int, person_name: str) -> list:
    """
    Fetch user memories that mention a specific person by name.

    Uses word-boundary matching to avoid false matches
    (e.g. 'Ken' should not match 'Kennedy' or 'Kenneth').

    Returns list of memory content strings, empty list on any failure.
    """
    from web.core.database import get_db

    if not person_name or not person_name.strip():
        return []

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT content FROM user_memories WHERE user_id = %s",
                (user_id,)
            )
            rows = cursor.fetchall()

        # Word-boundary match — escape the name for regex safety
        escaped = re.escape(person_name.strip())
        pattern = re.compile(rf'\b{escaped}\b', re.IGNORECASE)

        matched = [r['content'] for r in rows if pattern.search(r['content'] or '')]

        if not matched:
            logger.debug("[closure] no person memories found for '%s'", person_name)
        else:
            logger.debug("[closure] found %d memories for '%s'", len(matched), person_name)

        return matched

    except Exception as e:
        logger.warning("[closure] get_person_memories failed: %r", e)
        return []


def _extract_person_name_from_nudge(nudge: dict) -> Optional[str]:
    """
    Try to extract a person's name from nudge title/body.
    Looks for patterns like "Did you respond to NAME" or "Follow up with NAME".
    Returns first plausible name found, or None.
    """
    text = f"{nudge.get('title', '')} {nudge.get('body', '')}"
    # Common patterns: "respond to X", "follow up with X", "X's message"
    patterns = [
        r"respond to ([A-Z][a-z]+ [A-Z][a-z]+)",    # "respond to First Last"
        r"respond to ([A-Z][a-z]+)",                   # "respond to First"
        r"follow.?up with ([A-Z][a-z]+ [A-Z][a-z]+)",
        r"follow.?up with ([A-Z][a-z]+)",
        r"([A-Z][a-z]+ [A-Z][a-z]+)'s",              # "First Last's message"
        r"from ([A-Z][a-z]+ [A-Z][a-z]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return None


def get_closure_context(nudge: dict, user_id: int) -> Optional[dict]:
    """
    Resolve a nudge to its conversation thread and fetch surrounding messages
    from scanned_items.

    Args:
        nudge:   Dict returned by get_next_drip_nudge().
        user_id: The owning user's id (not in the nudge dict — passed explicitly).

    Returns:
        {
            'thread_id': str,
            'source': str,
            'trigger_detected_at': str,
            'messages_before': [{'sender': str, 'content': str, 'detected_at': str}],
            'messages_after':  [{'sender': str, 'content': str, 'detected_at': str}],
        }
        or None if resolution fails (caller defaults to sending nudge).
    """
    from web.core.database import get_db

    nudge_id = nudge['id']
    source_type = nudge.get('source_type')
    source_id = nudge.get('source_id')

    if not source_type or not source_id:
        logger.debug("[closure] nudge %d: no source_type/source_id — skipping", nudge_id)
        return None

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Step 1: Resolve nudge source to a scanned_item id.
            # Resolution chains differ by nudge type:
            #   detected_action: nudge.source_id → detected_actions.scanned_item_id
            #   direct sources (gmail/telegram/slack): source_id IS the scanned_item id
            #   followup: open_followups.scanned_item_id (future-proofing; not in INCLUDE now)

            scanned_item_id = None

            if source_type == 'detected_action':
                cursor.execute(
                    "SELECT scanned_item_id FROM detected_actions WHERE id = %s",
                    (source_id,)
                )
                row = cursor.fetchone()
                if row:
                    scanned_item_id = row['scanned_item_id']

            elif source_type in ('gmail', 'telegram', 'slack'):
                # source_id IS the scanned_item id
                scanned_item_id = source_id

            elif source_type == 'followup':
                cursor.execute(
                    "SELECT scanned_item_id FROM open_followups WHERE id = %s",
                    (source_id,)
                )
                row = cursor.fetchone()
                if row:
                    scanned_item_id = row.get('scanned_item_id')

            if not scanned_item_id:
                logger.debug(
                    "[closure] nudge %d: could not resolve source %s/%s to scanned_item",
                    nudge_id, source_type, source_id
                )
                return None

            # Step 2: Get the scanned_item and its thread_id via item_classifications
            cursor.execute("""
                SELECT si.source, si.source_id, si.detected_at, si.source_metadata,
                       ic.thread_id
                FROM scanned_items si
                LEFT JOIN item_classifications ic ON ic.scanned_item_id = si.id
                WHERE si.id = %s
                LIMIT 1
            """, (scanned_item_id,))
            trigger = cursor.fetchone()

            if not trigger or not trigger['thread_id']:
                logger.debug(
                    "[closure] nudge %d: no thread_id on scanned_item %s",
                    nudge_id, scanned_item_id
                )
                return None

            source = trigger['source']
            thread_id = trigger['thread_id']
            trigger_at = trigger['detected_at']

            # Step 3: Fetch messages before the trigger (conversation context)
            cursor.execute("""
                SELECT si.source_metadata, si.detected_at
                FROM scanned_items si
                JOIN item_classifications ic ON ic.scanned_item_id = si.id
                WHERE si.user_id = %s
                  AND si.source = %s
                  AND ic.thread_id = %s
                  AND si.detected_at < %s
                ORDER BY si.detected_at DESC
                LIMIT %s
            """, (user_id, source, thread_id, trigger_at, MESSAGES_BEFORE))
            before_rows = list(reversed(cursor.fetchall()))

            # Step 4: Fetch messages after the trigger (closure signal)
            # Cap at MAX_DAYS_AFTER to avoid pulling in messages from much later
            if isinstance(trigger_at, datetime):
                cutoff = trigger_at + timedelta(days=MAX_DAYS_AFTER)
            else:
                cutoff = datetime.utcnow()

            cursor.execute("""
                SELECT si.source_metadata, si.detected_at
                FROM scanned_items si
                JOIN item_classifications ic ON ic.scanned_item_id = si.id
                WHERE si.user_id = %s
                  AND si.source = %s
                  AND ic.thread_id = %s
                  AND si.detected_at > %s
                  AND si.detected_at <= %s
                ORDER BY si.detected_at ASC
                LIMIT %s
            """, (user_id, source, thread_id, trigger_at, cutoff, MESSAGES_AFTER))
            after_rows = cursor.fetchall()

        def parse_message(row) -> dict:
            try:
                meta = json.loads(row['source_metadata'] or '{}')
            except Exception:
                meta = {}
            return {
                'sender': (
                    meta.get('sender_name')
                    or meta.get('sender')
                    or meta.get('from')
                    or 'Unknown'
                ),
                'content': (
                    meta.get('content')
                    or meta.get('body')
                    or meta.get('snippet')
                    or ''
                ),
                'detected_at': str(row['detected_at']),
            }

        return {
            'thread_id': thread_id,
            'source': source,
            'trigger_detected_at': str(trigger_at),
            'messages_before': [parse_message(r) for r in before_rows],
            'messages_after': [parse_message(r) for r in after_rows],
        }

    except Exception as e:
        logger.warning("[closure] nudge %d: context resolution failed: %r", nudge_id, e)
        return None


async def check_nudge_closure(nudge: dict, context: dict) -> bool:
    """
    Ask Haiku whether the nudge situation has already been resolved.

    Returns:
        True  = loop appears closed (skip/delay this nudge)
        False = loop still open, or confidence too low (send the nudge)

    Always returns False on error — default is to send.
    """
    import anthropic

    nudge_title = nudge.get('title', '')
    nudge_body = nudge.get('body', '')

    messages_before = context.get('messages_before', [])
    messages_after = context.get('messages_after', [])

    if not messages_after:
        # No messages after the trigger — loop definitely still open
        logger.debug("[closure] nudge %d: no messages after trigger — sending", nudge['id'])
        return False

    # Fetch person-specific memories to inform the decision
    person_name = _extract_person_name_from_nudge(nudge)
    person_memories = []
    if person_name and 'user_id' in nudge:
        person_memories = get_person_memories(nudge['user_id'], person_name)

    def format_messages(msgs: list, label: str) -> str:
        if not msgs:
            return f"[No {label} messages]"
        lines = []
        for m in msgs:
            sender = m.get('sender', 'Unknown')
            content = (m.get('content') or '')[:300]  # cap per message
            lines.append(f"{sender}: {content}")
        return "\n".join(lines)

    before_text = format_messages(messages_before, "before")
    after_text = format_messages(messages_after, "after")

    memories_section = ""
    if person_memories:
        memories_text = "\n".join(f"- {m}" for m in person_memories[:5])  # cap at 5
        memories_section = f"""
WHAT YOU KNOW ABOUT THIS PERSON (from past patterns):
{memories_text}

Use this context to inform your judgment, but the conversation messages are the primary evidence.
"""

    prompt = f"""You are reviewing whether a follow-up reminder is still needed.

REMINDER THAT WAS QUEUED:
{nudge_title}
{nudge_body}
{memories_section}
CONVERSATION CONTEXT (messages BEFORE the triggering message):
{before_text}

---TRIGGERING MESSAGE POINT---

MESSAGES AFTER (what happened next):
{after_text}

IMPORTANT CONTEXT ABOUT HUMAN CONVERSATIONS:
- Humans naturally drift between topics and may start talking about something completely unrelated without having resolved the previous topic
- A topic change alone does NOT mean the original situation was addressed
- A reaction (like ❤️, 👍, "lol") or a single-word reply does NOT count as genuinely addressing the original situation
- Only count as resolved if there is clear evidence the user substantively responded to or engaged with the original topic

Based on the messages after the triggering point, has this situation already been resolved?

Respond with ONLY a JSON object:
{{"resolved": true/false, "confidence": "high"/"medium"/"low", "reason": "one sentence"}}

If you are unsure, respond with resolved=false. When in doubt, do not suppress the reminder."""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()

        # Extract JSON — Haiku may wrap it in extra text
        start = raw.find('{')
        end = raw.rfind('}')
        if start == -1 or end == -1:
            logger.warning(
                "[closure] nudge %d: Haiku returned no JSON: %r",
                nudge['id'], raw[:100]
            )
            return False

        parsed = json.loads(raw[start:end + 1])
        resolved = parsed.get('resolved', False)
        confidence = parsed.get('confidence', 'low')
        reason = parsed.get('reason', '')

        logger.info(
            "[closure] nudge %d: resolved=%s confidence=%s reason=%s",
            nudge['id'], resolved, confidence, reason
        )

        # Only suppress if resolved AND confidence is high or medium
        # Low confidence → default to sending
        if resolved and confidence in ('high', 'medium'):
            return True

        return False

    except Exception as e:
        logger.warning(
            "[closure] nudge %d: Haiku call failed: %r — defaulting to send",
            nudge['id'], e
        )
        return False


# -- Lightweight DB-based closure checks (Phase 75-01) ----------------------


def check_overdue_task_closure(nudge: dict, user_id: int) -> bool:
    """
    Check whether the task referenced by an overdue_task nudge has been completed.

    Queries tasks.status via nudge['source_id'].
    Returns True if status == 'completed', False otherwise.
    Always fails open (returns False on error).
    """
    from web.core.database import get_db

    source_id = nudge.get('source_id')
    if not source_id:
        logger.debug(
            "[closure] nudge %d: overdue_task has no source_id -- sending",
            nudge.get('id')
        )
        return False

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT status FROM tasks WHERE id = %s",
                (source_id,)
            )
            row = cursor.fetchone()

        if not row:
            logger.debug(
                "[closure] nudge %d: task %s not found -- sending",
                nudge.get('id'), source_id
            )
            return False

        status = row['status'] if isinstance(row, dict) else row[0]
        if status == 'completed':
            logger.info(
                "[closure] nudge %d: task %s already completed -- closed",
                nudge.get('id'), source_id
            )
            return True

        return False

    except Exception as e:
        logger.warning(
            "[closure] nudge %d: check_overdue_task_closure failed: %r -- sending",
            nudge.get('id'), e
        )
        return False


def check_nudge_followup_closure(nudge: dict, user_id: int) -> bool:
    """
    Check whether the original nudge referenced by a nudge_followup has been actioned.

    Queries nudges.user_response and nudges.acted_at via nudge['source_id'].
    Returns True if either is set (original nudge was responded to).
    Always fails open (returns False on error).
    """
    from web.core.database import get_db

    source_id = nudge.get('source_id')
    if not source_id:
        logger.debug(
            "[closure] nudge %d: nudge_followup has no source_id -- sending",
            nudge.get('id')
        )
        return False

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_response, acted_at FROM nudges WHERE id = %s",
                (source_id,)
            )
            row = cursor.fetchone()

        if not row:
            logger.debug(
                "[closure] nudge %d: source nudge %s not found -- sending",
                nudge.get('id'), source_id
            )
            return False

        user_response = row['user_response'] if isinstance(row, dict) else row[0]
        acted_at = row['acted_at'] if isinstance(row, dict) else row[1]

        if user_response or acted_at:
            logger.info(
                "[closure] nudge %d: source nudge %s already actioned "
                "(response=%s, acted_at=%s) -- closed",
                nudge.get('id'), source_id, user_response, acted_at
            )
            return True

        return False

    except Exception as e:
        logger.warning(
            "[closure] nudge %d: check_nudge_followup_closure failed: %r -- sending",
            nudge.get('id'), e
        )
        return False


def check_person_contact_closure(nudge: dict, user_id: int) -> bool:
    """
    Check whether outbound contact has been made with the person referenced
    by a relationship_check or open_followup nudge.

    Two-strategy name resolution:
      a. If source_type='person' and source_id set -> look up people.name
      b. Fallback: _extract_person_name_from_nudge() regex on title/body

    Then searches scanned_items for direction='outbound' messages with
    source_metadata ILIKE '%{name}%' after nudge created_at.

    Returns True if outbound contact found, False otherwise.
    Always fails open (returns False on error).
    """
    from web.core.database import get_db

    nudge_id = nudge.get('id')

    # -- Resolve person name --------------------------------------------------
    person_name = None

    # Strategy A: look up from people table if source_type='person'
    source_type = nudge.get('source_type')
    source_id = nudge.get('source_id')

    if source_type == 'person' and source_id:
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT name FROM people WHERE id = %s",
                    (source_id,)
                )
                row = cursor.fetchone()
                if row:
                    person_name = row['name'] if isinstance(row, dict) else row[0]
        except Exception as e:
            logger.debug(
                "[closure] nudge %d: people lookup failed: %r -- trying regex",
                nudge_id, e
            )

    # Strategy B: regex extraction from nudge text
    if not person_name:
        person_name = _extract_person_name_from_nudge(nudge)

    if not person_name:
        logger.debug(
            "[closure] nudge %d: could not resolve person name -- sending",
            nudge_id
        )
        return False

    # -- Search for outbound contact after nudge creation ---------------------
    created_at = nudge.get('created_at')
    if not created_at:
        logger.debug(
            "[closure] nudge %d: no created_at -- sending",
            nudge_id
        )
        return False

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id FROM scanned_items
                WHERE user_id = %s
                  AND direction = 'outbound'
                  AND source_metadata ILIKE %s
                  AND detected_at > %s
                LIMIT 1
            """, (user_id, f'%{person_name}%', created_at))
            row = cursor.fetchone()

        if row:
            logger.info(
                "[closure] nudge %d: outbound contact found for '%s' -- closed",
                nudge_id, person_name
            )
            return True

        logger.debug(
            "[closure] nudge %d: no outbound contact for '%s' -- sending",
            nudge_id, person_name
        )
        return False

    except Exception as e:
        logger.warning(
            "[closure] nudge %d: check_person_contact_closure failed: %r -- sending",
            nudge_id, e
        )
        return False
