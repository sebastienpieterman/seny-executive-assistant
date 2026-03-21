"""
Nudge Service - Proactive notifications with urgency routing and deduplication.

Processes pending detected_actions, urgent classifications, and overdue tasks
to generate nudges. Handles quiet hours, rate limiting, batching, and delivery.

Phase 16 - Autonomous Nudges

Supports delivery to:
- Telegram (Saved Messages / self-chat)
- Slack (Self-DM via conversations.open)
- Push notifications (Web Push via pywebpush)

Fallback chain: preferred channel → push → telegram → slack
"""

import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic
from src.core.config import Config

from web.core.database import (
    get_db,
    create_nudge,
    get_recent_nudges,
    get_nudge_for_source,
    get_recent_nudge_for_source,
    get_nudge_preferences,
    update_nudge_preferences,
    get_pending_actions,
    update_nudge_status,
    get_first_telegram_session,
    get_first_slack_token,
    record_nudge_response,
    record_feedback,
    create_email_feedback_token,
    get_next_drip_nudge,
    get_priority_items,
    get_sender_for_detected_action,
    is_sender_nudge_suppressed,
    add_nudge_suppressed_sender,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Loop Closure Classification
# ============================================================================

# Nudge types eligible for loop closure check.
# These have source_type/source_id that can be resolved to a conversation thread
# in scanned_items via item_classifications.thread_id.
#
# Resolution chain for each INCLUDE type:
#   detected_action:
#     nudge.source_id -> detected_actions.id
#     -> detected_actions.scanned_item_id -> scanned_items.id
#     -> item_classifications.scanned_item_id -> item_classifications.thread_id
#   overdue_task: lightweight DB check on tasks.status (Phase 75-01)
#   nudge_followup: lightweight DB check on nudges.user_response/acted_at (Phase 75-01)
#   relationship_check: person name -> outbound message search (Phase 75-01)
#   open_followup: person name -> outbound message search (Phase 75-01)
CLOSURE_CHECK_INCLUDE = frozenset([
    "detected_action",
    "overdue_task",
    "nudge_followup",
    "relationship_check",
    "open_followup",
])

# Nudge types explicitly excluded from closure check.
# Time-sensitive: checking/delaying would make them fire after the event.
CLOSURE_CHECK_EXCLUDE_TIME_SENSITIVE = frozenset([
    "meeting_prep",
    "calendar_event",
    "urgent_item",
])

# Nudge types with no thread to resolve AND no lightweight closure handler.
# These skip closure checks entirely:
#   needs_reply: digest-only category, not created as standalone drip nudges
#   relationship_checkin_prompt: source_type='person' -> person-level, no thread
#   priority_context: source_type='priority_context' -> priority items have no thread
# NOTE: overdue_task, relationship_check, open_followup moved to INCLUDE in Phase 75-01
CLOSURE_CHECK_NO_THREAD = frozenset([
    "needs_reply",
    "relationship_checkin_prompt",
    "priority_context",
])


class NudgeService:
    """
    Proactive notification service with urgency routing and deduplication.

    Follows per-user service pattern. Processes pending items and queues
    nudges respecting quiet hours, rate limits, and deduplication.

    Integrates with PatternLearningService for personalized urgency adjustments
    and item type suppression based on user feedback history.

    Usage:
        nudge_service = NudgeService(user_id)
        result = await nudge_service.process_pending_nudges()
        batch_result = await nudge_service.send_batch_if_due()
    """

    def __init__(self, user_id: int):
        """
        Initialize NudgeService for a specific user.

        Args:
            user_id: User's database ID
        """
        self.user_id = user_id
        self._prefs: Optional[dict] = None
        self._pattern_service = None  # Lazy init to avoid circular imports

    async def process_pending_nudges(self) -> dict:
        """
        Query pending items and queue appropriate nudges.

        Processes:
        1. Pending detected_actions (from inbound classification)
        2. Urgent classifications (urgency='urgent')
        3. Overdue tasks

        Applies:
        - Quiet hours check (skips if in quiet window)
        - Deduplication (skips if nudge already exists for source)
        - Rate limiting (respects nudge_max_urgent_per_hour)

        Returns:
            Stats dict with counts: {queued, skipped_quiet, skipped_dup,
            skipped_rate_limit, urgent_count, normal_count}
        """
        stats = {
            "queued": 0,
            "skipped_quiet": 0,
            "skipped_dup": 0,
            "skipped_rate_limit": 0,
            "urgent_count": 0,
            "normal_count": 0,
        }

        # Load preferences
        prefs = self._get_preferences()
        if not prefs.get('nudge_enabled', True):
            logger.info("Nudges disabled for user %d", self.user_id)
            return stats

        # Check quiet hours
        if self.is_quiet_hours():
            logger.debug("In quiet hours for user %d, skipping nudge processing", self.user_id)
            # We still track what we would have processed
            pending_actions = get_pending_actions(self.user_id, limit=50)
            urgent_items = self._get_urgent_classifications(limit=20)
            overdue_tasks = self._get_overdue_tasks(limit=10)
            stats["skipped_quiet"] = len(pending_actions) + len(urgent_items) + len(overdue_tasks)
            return stats

        # Process pending detected actions
        pending_actions = get_pending_actions(self.user_id, limit=50)
        for action in pending_actions:
            result = await self._process_detected_action(action, stats)
            if result == "queued":
                stats["queued"] += 1

        # Process urgent classifications
        urgent_items = self._get_urgent_classifications(limit=20)
        for item in urgent_items:
            result = await self._process_urgent_classification(item, stats)
            if result == "queued":
                stats["queued"] += 1

        # Process overdue tasks
        overdue_tasks = self._get_overdue_tasks(limit=10)
        for task in overdue_tasks:
            result = await self._process_overdue_task(task, stats)
            if result == "queued":
                stats["queued"] += 1

        # Process user-flagged priority context items
        # These are items the user explicitly told Claude were critical/urgent in chat.
        # Time-windowed dedup (1 day) allows re-nudging until the user resolves the item.
        priority_items = self._get_priority_context_items()
        for item in priority_items:
            result = await self._process_priority_context_item(item, stats)
            if result == "queued":
                stats["queued"] += 1

        suppressed = stats.get("skipped_suppressed", 0)
        logger.info(
            "Nudge processing for user %d: %d queued, %d skipped (dup=%d, rate=%d, suppressed=%d)",
            self.user_id, stats["queued"], stats["skipped_dup"] + stats["skipped_rate_limit"] + suppressed,
            stats["skipped_dup"], stats["skipped_rate_limit"], suppressed
        )

        return stats

    def classify_urgency(self, item: dict) -> str:
        """
        Determine urgency level for an item.

        Urgent criteria:
        - Classification urgency is 'urgent'
        - Action type is 'deadline' with deadline within 24h
        - Task is overdue by more than 24h
        - Sender is a known VIP contact (future enhancement)

        Args:
            item: Dict with source info (classification, action, or task)

        Returns:
            'urgent' or 'normal'
        """
        # Check explicit urgency field from classification
        if item.get('urgency') == 'urgent':
            return 'urgent'

        # Check deadline proximity for actions
        deadline = item.get('deadline')
        if deadline:
            try:
                if isinstance(deadline, str):
                    deadline_dt = datetime.fromisoformat(deadline.replace('Z', '+00:00'))
                else:
                    deadline_dt = deadline
                hours_until = (deadline_dt - datetime.now(deadline_dt.tzinfo)).total_seconds() / 3600
                if hours_until < 24:
                    return 'urgent'
            except (ValueError, TypeError):
                pass

        # Check overdue duration for tasks
        due_date = item.get('due_date')
        if due_date and item.get('source_type') == 'task':
            try:
                if isinstance(due_date, str):
                    due_dt = datetime.fromisoformat(due_date.replace('Z', '+00:00'))
                else:
                    due_dt = due_date
                hours_overdue = (datetime.now(due_dt.tzinfo) - due_dt).total_seconds() / 3600
                if hours_overdue > 24:
                    return 'urgent'
            except (ValueError, TypeError):
                pass

        return 'normal'

    async def classify_urgency_with_patterns(self, item: dict) -> str:
        """
        Determine urgency level with learned pattern adjustments.

        Applies base classification, then adjusts based on user's feedback history.
        If user consistently dismisses a type, downgrades urgency.
        If user consistently marks helpful, upgrades urgency.

        Args:
            item: Dict with source info (classification, action, or task)

        Returns:
            'urgent' or 'normal' (may differ from base classification)
        """
        # Get base classification
        base_urgency = self.classify_urgency(item)

        # Get item type for pattern lookup
        item_type = item.get('nudge_type') or item.get('source_type') or 'unknown'

        # Get urgency adjustment from pattern learning
        try:
            pattern_service = self._get_pattern_service()
            adjustment = await pattern_service.get_urgency_adjustment(item_type)

            # Apply adjustment thresholds
            # Downgrade urgent -> normal if adjustment < 0.7
            if base_urgency == 'urgent' and adjustment < 0.7:
                logger.info(
                    "Urgency adjusted from urgent to normal for %s (factor=%.2f)",
                    item_type, adjustment
                )
                return 'normal'

            # Upgrade normal -> urgent if adjustment > 1.3
            if base_urgency == 'normal' and adjustment > 1.3:
                logger.info(
                    "Urgency adjusted from normal to urgent for %s (factor=%.2f)",
                    item_type, adjustment
                )
                return 'urgent'

        except Exception as e:
            logger.warning("Pattern adjustment error: %s, using base classification", repr(e))

        return base_urgency

    def is_quiet_hours(self) -> bool:
        """
        Check if current time is within user's quiet hours window.

        Uses digest_timezone from user_settings for timezone conversion.

        Returns:
            True if in quiet hours, False otherwise
        """
        prefs = self._get_preferences()
        tz_str = prefs.get('digest_timezone', 'America/Chicago')
        quiet_start = prefs.get('nudge_quiet_start', '22:00')
        quiet_end = prefs.get('nudge_quiet_end', '08:00')

        try:
            tz = ZoneInfo(tz_str)
            now = datetime.now(tz)

            # Weekend check
            if prefs.get('nudge_quiet_skip_weekend', False) and now.weekday() >= 5:
                return True  # Saturday=5, Sunday=6 — treat as quiet hours

            current_time = now.time()

            start_h, start_m = map(int, quiet_start.split(':'))
            end_h, end_m = map(int, quiet_end.split(':'))

            from datetime import time
            quiet_start_time = time(start_h, start_m)
            quiet_end_time = time(end_h, end_m)

            # Handle overnight quiet hours (e.g., 22:00 to 08:00)
            if quiet_start_time > quiet_end_time:
                # In quiet hours if after start OR before end
                return current_time >= quiet_start_time or current_time < quiet_end_time
            else:
                # Simple case: quiet hours within same day
                return quiet_start_time <= current_time < quiet_end_time

        except Exception as e:
            logger.error("Error checking quiet hours: %s", repr(e))
            return False

    def _recent_screen_nudge(self, minutes: int = 10) -> bool:
        """Return True if screen agent fired a nudge for this user within the last N minutes."""
        try:
            from web.core.database import get_db
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT id FROM nudges"
                    " WHERE user_id = %s AND nudge_type = 'screen_agent'"
                    " AND sent_at > NOW() - INTERVAL '%s minutes'"
                    " LIMIT 1",
                    (self.user_id, minutes)
                )
                return cur.fetchone() is not None
        except Exception as e:
            logger.warning("_recent_screen_nudge check failed: %s", repr(e))
            return False  # Fail open — don't suppress drip on error

    def is_duplicate(self, source_type: str, source_id: int) -> bool:
        """
        Check if a nudge already exists for this source item.

        Args:
            source_type: Type of source item
            source_id: ID of source item

        Returns:
            True if nudge already exists, False otherwise
        """
        existing = get_nudge_for_source(self.user_id, source_type, source_id)
        return existing is not None

    def check_rate_limit(self) -> bool:
        """
        Check if user has exceeded urgent nudge rate limit.

        Uses nudge_max_urgent_per_hour from preferences.

        Returns:
            True if within limit (can send), False if exceeded
        """
        prefs = self._get_preferences()
        max_per_hour = prefs.get('nudge_max_urgent_per_hour', 3)

        # Count urgent nudges in the last hour
        recent = get_recent_nudges(self.user_id, hours=1, limit=100)
        urgent_count = sum(1 for n in recent if n.get('urgency') == 'urgent')

        return urgent_count < max_per_hour

    async def send_batch_if_due(self) -> dict:
        """
        Check if batch interval has elapsed and send batched nudges if due.

        Batches normal-priority pending nudges into a single mini-digest,
        formats using format_batch_digest(), and delivers via send_nudge().

        Returns:
            Dict with {sent: bool, nudge_count: int, batch_id: str or None, channel: str}
        """
        result = {
            "sent": False,
            "nudge_count": 0,
            "batch_id": None,
            "channel": None,
        }

        prefs = self._get_preferences()
        if not prefs.get('nudge_enabled', True):
            return result

        # Check if in quiet hours
        if self.is_quiet_hours():
            return result

        # User status check (matches drip system behavior)
        try:
            from web.core.database import get_user_status
            active_status = get_user_status(self.user_id)
            if active_status:
                logger.debug(
                    "Skipping batch nudge — user %d has active status: %s",
                    self.user_id,
                    active_status.get('status_text', '')
                )
                return result
        except Exception as e:
            logger.warning("user_status check failed in batch delivery: %s", repr(e))
            # Fail open

        # Check if batch interval has elapsed
        interval_minutes = prefs.get('nudge_batch_interval_minutes', 60)
        last_batch = prefs.get('nudge_last_batch_at')

        if last_batch:
            try:
                last_batch_dt = datetime.fromisoformat(last_batch.replace('Z', '+00:00'))
                elapsed = datetime.now(last_batch_dt.tzinfo) - last_batch_dt
                if elapsed.total_seconds() < interval_minutes * 60:
                    # Not time yet
                    return result
            except (ValueError, TypeError):
                pass

        # Get pending normal-priority nudges
        pending_nudges = self._get_pending_normal_nudges()
        if not pending_nudges:
            return result

        # Phase 73: Per-nudge freshness filtering -- dismiss stale nudges before batching
        fresh_nudges = []
        for pn in pending_nudges:
            try:
                is_stale, stale_reason = await self.is_nudge_stale(pn)
                if is_stale:
                    try:
                        with get_db() as conn:
                            cursor = conn.cursor()
                            cursor.execute(
                                "UPDATE nudges SET status = 'dismissed', dismiss_reason = %s WHERE id = %s",
                                (f"freshness_gate: {stale_reason}", pn.get('id'))
                            )
                        logger.info(
                            "Batch freshness: dismissed stale nudge %s for user %d: %s",
                            pn.get('id'), self.user_id, stale_reason
                        )
                    except Exception:
                        pass  # Fail open -- dismiss is best-effort
                    continue
            except Exception:
                pass  # Fail open -- include nudge on error
            fresh_nudges.append(pn)

        if not fresh_nudges:
            logger.info("All %d batch nudges were stale for user %d", len(pending_nudges), self.user_id)
            return result
        pending_nudges = fresh_nudges

        # Generate batch ID and format the digest
        batch_id = str(uuid.uuid4())[:8]
        title, body = self.format_batch_digest(pending_nudges)

        # Send the batch nudge via delivery channels
        send_result = await self.send_nudge(
            nudge_type='batch',
            title=title,
            body=body,
            urgency='normal',
            batch_id=batch_id
        )

        if send_result.get('success'):
            # Update last batch time
            update_nudge_preferences(
                self.user_id,
                nudge_last_batch_at=datetime.now().isoformat()
            )

            # Mark the batched nudges as part of this batch
            self._mark_nudges_as_batched(pending_nudges, batch_id)

            result["sent"] = True
            result["nudge_count"] = len(pending_nudges)
            result["batch_id"] = batch_id
            result["channel"] = send_result.get('channel')

            logger.info(
                "Sent batch nudge for user %d via %s: %d items, batch_id=%s",
                self.user_id, result["channel"], len(pending_nudges), batch_id
            )
        else:
            logger.warning(
                "Failed to send batch nudge for user %d: %s",
                self.user_id, send_result.get('error')
            )

        return result

    def _mark_nudges_as_batched(self, nudges: list[dict], batch_id: str) -> None:
        """Mark pending nudges as part of a batch."""
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                nudge_ids = [n.get('id') for n in nudges if n.get('id')]
                if nudge_ids:
                    placeholders = ','.join(['%s'] * len(nudge_ids))
                    cursor.execute(f"""
                        UPDATE nudges
                        SET batch_id = %s, status = 'batched'
                        WHERE id IN ({placeholders})
                    """, [batch_id] + nudge_ids)
        except Exception as e:
            logger.error("Failed to mark nudges as batched: %s", repr(e))

    # =========================================================================
    # Delivery Methods
    # =========================================================================

    def _get_app_url(self) -> str:
        """Get the base app URL for feedback links."""
        return os.environ.get('APP_URL', 'http://localhost:8000').rstrip('/')

    def _generate_feedback_link(self, nudge_id: int, feedback_action: str) -> Optional[str]:
        """
        Generate a feedback link for a nudge.

        Args:
            nudge_id: ID of the nudge
            feedback_action: 'helpful' or 'not_helpful'

        Returns:
            Full URL for the feedback link, or None on error
        """
        try:
            token = create_email_feedback_token(
                user_id=self.user_id,
                item_type='nudge',
                feedback_action=feedback_action,
                item_id=nudge_id,
            )
            if token:
                return f"{self._get_app_url()}/api/feedback/email/{token}"
        except Exception as e:
            logger.error("Failed to generate feedback link: %s", repr(e))
        return None

    def _generate_feedback_links_text(self, nudge_id: int, format_type: str = 'telegram') -> str:
        """
        Generate feedback links text for nudge messages.

        Args:
            nudge_id: ID of the nudge
            format_type: 'telegram' (markdown) or 'slack' (mrkdwn)

        Returns:
            Formatted feedback links string with up to 3 links:
            👍 Helpful  •  👎 Not useful  •  ✅ Already handled
        """
        helpful_url = self._generate_feedback_link(nudge_id, 'helpful')
        not_helpful_url = self._generate_feedback_link(nudge_id, 'not_helpful')
        handled_url = self._generate_feedback_link(nudge_id, 'already_handled')

        if not helpful_url or not not_helpful_url:
            return ""

        if format_type == 'slack':
            # Slack mrkdwn format
            base = f"\n\n<{helpful_url}|👍 Helpful>  •  <{not_helpful_url}|👎 Not useful>"
            if handled_url:
                base += f"  •  <{handled_url}|✅ Already handled>"
            return base
        else:
            # Telegram markdown format
            base = f"\n\n[👍 Helpful]({helpful_url})  •  [👎 Not useful]({not_helpful_url})"
            if handled_url:
                base += f"  •  [✅ Already handled]({handled_url})"
            return base

    async def send_nudge(
        self,
        nudge_type: str,
        title: str,
        body: str,
        urgency: str,
        source_type: str = None,
        source_id: int = None,
        batch_id: str = None
    ) -> dict:
        """
        Send a nudge to the user via their preferred channel with fallback.

        Determines channel from user preferences:
        - urgent → nudge_channels (primary channel)
        - normal → batched via push by default

        If preferred channel fails, tries fallback order: push → telegram → slack.

        Args:
            nudge_type: Type of nudge (detected_action, urgent_item, overdue_task, batch)
            title: Nudge title
            body: Nudge body text
            urgency: 'urgent' or 'normal'
            source_type: Source type for tracking
            source_id: Source ID for tracking
            batch_id: Batch ID if this is a batch nudge

        Returns:
            Dict with {success: bool, channel: str, nudge_id: int, error: str (if failed)}
        """
        result = {
            "success": False,
            "channel": None,
            "nudge_id": None,
            "telegram_message_id": None,
            "error": None
        }

        # Shared feedback state: if user already actioned this source item
        # (from ANY delivery path), don't re-surface it.
        # Screen agent nudges exempt: ephemeral evaluations with no persistent source item.
        if source_type and source_id and source_type != 'screen_agent':
            try:
                from web.core.database import get_recent_feedback_for_source
                recent_fb = get_recent_feedback_for_source(
                    self.user_id, source_type, source_id, hours=24
                )
                if recent_fb:
                    logger.info(
                        "Suppressing nudge for user %d: source %s/%s already has '%s' feedback within 24h",
                        self.user_id, source_type, source_id, recent_fb.get('feedback_type')
                    )
                    return {"success": False, "suppressed": True, "reason": "feedback_exists"}
            except Exception as e:
                logger.warning("Feedback pre-check failed for user %d: %s", self.user_id, repr(e))
                # Fail open — never block delivery on a DB error

        # Phase 73: Universal freshness gate -- check is_nudge_stale() before creating
        # the nudge record. Skip for batch (aggregate wrapper) and screen_agent (ephemeral).
        if nudge_type != 'batch' and source_type != 'screen_agent':
            try:
                freshness_item = {
                    'id': None,
                    'title': title,
                    'body': body,
                    'nudge_type': nudge_type,
                    'source_type': source_type,
                    'source_id': source_id,
                    'created_at': datetime.now().isoformat(),
                }
                is_stale, stale_reason = await self.is_nudge_stale(freshness_item)
                if is_stale:
                    logger.info(
                        "Freshness gate suppressed nudge for user %d: %s -- %s",
                        self.user_id, nudge_type, stale_reason
                    )
                    return {"success": False, "suppressed": True, "reason": f"freshness_gate: {stale_reason}"}
            except Exception as e:
                logger.warning("Freshness gate error for user %d: %s", self.user_id, repr(e))
                # Fail open -- never let a freshness check block delivery on error

        # Create nudge record first to get ID for feedback links
        nudge_id = create_nudge(
            user_id=self.user_id,
            nudge_type=nudge_type,
            channel='pending',
            title=title,
            body=body,
            urgency=urgency,
            source_type=source_type,
            source_id=source_id,
            batch_id=batch_id,
        )

        if not nudge_id:
            result["error"] = "Failed to create nudge record"
            return result

        result["nudge_id"] = nudge_id

        prefs = self._get_preferences()
        channels_json = prefs.get('nudge_channels', '["push"]')
        try:
            preferred_channels = json.loads(channels_json) if isinstance(channels_json, str) else channels_json
        except json.JSONDecodeError:
            preferred_channels = ['push']

        # Determine delivery channel
        # urgent → first preferred channel
        # normal (batch) → nudge_batch_channel preference (defaults to push)
        if urgency == 'urgent' and preferred_channels:
            primary_channel = preferred_channels[0]
        else:
            primary_channel = prefs.get('nudge_batch_channel', 'push')

        # Try delivery with fallback chain
        # Order: preferred → push → telegram → slack
        fallback_order = [primary_channel]
        for channel in ['push', 'telegram', 'slack']:
            if channel not in fallback_order:
                fallback_order.append(channel)

        delivered = False
        delivered_channel = None
        telegram_message_id = None

        for channel in fallback_order:
            success = False
            if channel == 'telegram':
                success, telegram_message_id = await self._deliver_telegram(title, body, nudge_id)
            elif channel == 'slack':
                success = await self._deliver_slack(title, body, nudge_id)
            elif channel == 'push':
                success = await self._deliver_push(title, body)

            if success:
                delivered = True
                delivered_channel = channel
                logger.info(
                    "Nudge delivered via %s for user %d: %s",
                    channel, self.user_id, title[:50]
                )
                break
            else:
                logger.debug(
                    "Nudge delivery failed via %s for user %d, trying next",
                    channel, self.user_id
                )

        # Update nudge status
        if delivered:
            update_nudge_status(nudge_id, 'sent', sent_at=datetime.now().isoformat())
            # Update channel in database
            try:
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE nudges SET channel = %s WHERE id = %s",
                        (delivered_channel, nudge_id)
                    )
            except Exception as e:
                logger.error("Failed to update nudge channel: %s", repr(e))
            # Store Telegram message ID for reply threading
            if telegram_message_id and nudge_id:
                try:
                    with get_db() as conn:
                        conn.cursor().execute(
                            "UPDATE nudges SET telegram_message_id = %s WHERE id = %s",
                            (str(telegram_message_id), nudge_id)
                        )
                except Exception as e:
                    logger.error("Failed to store telegram_message_id: %s", repr(e))
        else:
            update_nudge_status(nudge_id, 'failed')

        result["success"] = delivered
        result["channel"] = delivered_channel
        result["telegram_message_id"] = telegram_message_id

        if not delivered:
            result["error"] = "All delivery channels failed"
            logger.error(
                "Failed to deliver nudge for user %d via any channel: %s",
                self.user_id, title[:50]
            )

        return result

    async def _deliver_telegram(self, title: str, body: str, nudge_id: Optional[int] = None) -> tuple:
        """
        Deliver nudge via Telegram bot DM (preferred) or Saved Messages (fallback).

        Tries bot DM first if user has linked their Telegram to the bot.
        Falls back to Saved Messages if no bot link exists.

        Args:
            title: Nudge title
            body: Nudge body text (optional)
            nudge_id: Optional nudge ID for feedback links

        Returns:
            Tuple of (success: bool, telegram_message_id: Optional[int])
            telegram_message_id is set only when delivered via bot DM
        """
        # Format message
        message = f"🔔 **{title}**"
        if body:
            message += f"\n\n{body}"

        # Add feedback links if we have a nudge_id
        if nudge_id:
            feedback_links = self._generate_feedback_links_text(nudge_id, 'telegram')
            message += feedback_links

        # Try bot DM first (preferred - user can reply inline)
        telegram_message_id = await self._deliver_telegram_via_bot(message)
        if telegram_message_id is not None:
            return True, telegram_message_id

        # Fall back to Saved Messages via user's Telegram account
        success = await self._deliver_telegram_via_saved_messages(message)
        return success, None

    async def _deliver_telegram_via_bot(self, message: str) -> Optional[int]:
        """
        Deliver message via Telegram bot to user's linked chat.

        Args:
            message: Formatted message to send

        Returns:
            Telegram message_id (int) on success, None if no bot link or failed
        """
        try:
            # Check if user has linked Telegram bot chat
            from web.core.database import get_telegram_bot_user_links_for_user
            links = get_telegram_bot_user_links_for_user(self.user_id)

            if not links:
                logger.debug("No Telegram bot link for user %d, trying fallback", self.user_id)
                return None

            # Use first linked chat
            chat_id = links[0]["telegram_chat_id"]

            # Import TelegramBotService
            from web.services.telegram_bot_service import TelegramBotService
            bot_service = TelegramBotService()

            if not bot_service.is_configured():
                logger.debug("Telegram bot not configured, trying fallback")
                return None

            # Send via bot — returns message dict with message_id on success, error dict on failure
            result = await bot_service.send_message(chat_id, message)

            if result and not result.get("error"):
                message_id = result.get("message_id")
                logger.info("Delivered nudge via Telegram bot to user %d (msg_id=%s)", self.user_id, message_id)
                return message_id
            else:
                logger.warning("Telegram bot send failed for user %d: %s", self.user_id, result)
                return None

        except Exception as e:
            logger.error("Telegram bot delivery error for user %d: %s", self.user_id, repr(e))
            return None

    async def _deliver_telegram_via_saved_messages(self, message: str) -> bool:
        """
        Deliver message to user's Telegram Saved Messages (fallback).

        Args:
            message: Formatted message to send

        Returns:
            True if delivered, False if failed or no Telegram connection
        """
        try:
            # Check if user has Telegram connected
            session_data = get_first_telegram_session(self.user_id)
            if not session_data:
                logger.debug("No Telegram session for user %d", self.user_id)
                return False

            # Import TelegramService here to avoid circular imports
            from web.services.telegram_service import TelegramService

            phone_number = session_data['phone_number']
            telegram = TelegramService(self.user_id, phone_number)

            if not await telegram.connect():
                logger.warning("Could not connect to Telegram for user %d", self.user_id)
                return False

            # Resolve "me" to get Saved Messages chat
            saved_messages_id = await telegram.resolve_chat("me")
            if not saved_messages_id:
                logger.warning("Could not resolve Saved Messages for user %d", self.user_id)
                return False

            # Send to Saved Messages
            result = await telegram.send_message(saved_messages_id, message)

            if result.get('error'):
                logger.warning("Telegram send failed: %s", result.get('error'))
                return False

            logger.info("Delivered nudge via Telegram Saved Messages to user %d", self.user_id)
            return True

        except Exception as e:
            logger.error("Telegram Saved Messages delivery error for user %d: %s", self.user_id, repr(e))
            return False

    async def _deliver_slack(self, title: str, body: str, nudge_id: Optional[int] = None) -> bool:
        """
        Deliver nudge via Slack to user's self-DM.

        Uses conversations.open with the user's own ID to get a self-DM channel.

        Args:
            title: Nudge title
            body: Nudge body text (optional)
            nudge_id: Optional nudge ID for feedback links

        Returns:
            True if delivered, False if failed or no Slack connection
        """
        try:
            # Check if user has Slack connected
            token_data = get_first_slack_token(self.user_id)
            if not token_data:
                logger.debug("No Slack token for user %d", self.user_id)
                return False

            # Import SlackService here to avoid circular imports
            from web.services.slack_service import SlackService, SlackCircuitOpenError

            slack = SlackService(self.user_id)

            if not slack.is_connected():
                logger.debug("Slack not connected for user %d", self.user_id)
                return False

            # Get user's own Slack ID to open self-DM
            authed_user_id = token_data.get('authed_user_id')
            if not authed_user_id:
                logger.warning("No authed_user_id for Slack user %d", self.user_id)
                return False

            # Open conversation with self (creates self-DM channel)
            try:
                open_result = await slack._api_call(
                    "conversations.open",
                    json_body={"users": authed_user_id}
                )
                if not open_result.get('ok'):
                    logger.warning(
                        "Could not open Slack self-DM for user %d: %s",
                        self.user_id, open_result.get('error')
                    )
                    return False

                dm_channel_id = open_result.get('channel', {}).get('id')
                if not dm_channel_id:
                    logger.warning("No channel ID in conversations.open response")
                    return False

            except SlackCircuitOpenError as e:
                logger.warning("Slack circuit open for user %d: %s", self.user_id, repr(e))
                return False

            # Format message with Slack mrkdwn
            message = f"*{title}*"
            if body:
                message += f"\n{body}"

            # Add feedback links if we have a nudge_id
            if nudge_id:
                feedback_links = self._generate_feedback_links_text(nudge_id, 'slack')
                message += feedback_links

            # Send to self-DM
            result = await slack.send_message(dm_channel_id, message)

            if result is None:
                logger.warning("Slack send_message returned None for user %d", self.user_id)
                return False

            # Store Slack message ts for reply threading
            slack_ts = result.get('ts') if isinstance(result, dict) else None
            if slack_ts and nudge_id:
                try:
                    with get_db() as conn:
                        conn.cursor().execute(
                            "UPDATE nudges SET slack_message_ts = %s WHERE id = %s",
                            (str(slack_ts), nudge_id)
                        )
                except Exception as e:
                    logger.error("Failed to store slack_message_ts: %s", repr(e))

            return True

        except Exception as e:
            logger.error("Slack delivery error for user %d: %s", self.user_id, repr(e))
            return False

    async def _deliver_push(self, title: str, body: str) -> bool:
        """
        Deliver nudge via Web Push notification.

        Push notifications have ~100 char body limit, so body is truncated.

        Args:
            title: Nudge title
            body: Nudge body text (will be truncated for push)

        Returns:
            True if at least one device received the notification, False otherwise
        """
        try:
            # Import NotificationService here to avoid circular imports
            from web.services.notification_service import NotificationService

            notification = NotificationService(self.user_id)

            # Truncate body for push notification (100 char limit)
            push_body = body[:97] + '...' if body and len(body) > 100 else body

            result = await notification.send_notification(
                title=title,
                body=push_body,
                url="/digest",
                notification_type="nudge"
            )

            # Check if any devices received it
            sent_count = result.get('sent', 0)
            if sent_count > 0:
                return True
            else:
                logger.debug(
                    "Push notification sent to 0 devices for user %d: %s",
                    self.user_id, result.get('errors', [])
                )
                return False

        except Exception as e:
            logger.error("Push delivery error for user %d: %s", self.user_id, repr(e))
            return False

    # =========================================================================
    # Formatting Methods
    # =========================================================================

    def format_batch_digest(self, items: list[dict]) -> tuple[str, str]:
        """
        Format a batch of items into a mini-digest.

        Groups items by type and creates a structured body with bullet points.

        Args:
            items: List of nudge item dicts with nudge_type, title, body, person_name, etc.

        Returns:
            Tuple of (title, body) for the batch nudge
        """
        count = len(items)
        title = f"📋 {count} item{'s' if count != 1 else ''} need{'s' if count == 1 else ''} attention"

        # Group items by type
        groups = {
            'detected_action': [],
            'overdue_task': [],
            'urgent_item': [],
            'cross_reference': [],
            'other': []
        }

        for item in items[:10]:  # Max 10 items in batch
            nudge_type = item.get('nudge_type', 'other')
            if nudge_type in groups:
                groups[nudge_type].append(item)
            else:
                groups['other'].append(item)

        # Build body with grouped sections, using sequential numbering across all groups
        body_sections = []
        item_counter = [0]  # mutable list so inner helper can increment it

        def numbered_bullet(text: str) -> str:
            item_counter[0] += 1
            return f"{item_counter[0]}. {text}"

        if groups['detected_action']:
            section_lines = ["**Action Items**"]

            # Group by person_name so multiple items from the same conversation
            # are consolidated into one bullet instead of repeating separately
            person_groups: dict = {}
            no_person_items = []
            for item in groups['detected_action']:
                person = item.get('person_name')
                if person:
                    person_groups.setdefault(person, []).append(item)
                else:
                    no_person_items.append(item)

            for person, person_items in person_groups.items():
                if len(person_items) == 1:
                    text = person_items[0].get('action_text') or person_items[0].get('title', 'Action needed')
                    deadline = person_items[0].get('deadline')
                    bullet = text
                    if deadline:
                        bullet = self._add_deadline_hint(bullet, deadline)
                    section_lines.append(numbered_bullet(bullet))
                else:
                    # Multiple items from same person — consolidate into one entry
                    topics = []
                    for pi in person_items[:3]:
                        t = (pi.get('action_text') or pi.get('title', '')).rstrip('.')
                        if t:
                            topics.append(t)
                    bullet = f"Several things to address with {person}: {'; '.join(topics)}"
                    if len(person_items) > 3:
                        bullet += f" (+{len(person_items) - 3} more)"
                    section_lines.append(numbered_bullet(bullet))

            for item in no_person_items:
                text = item.get('action_text') or item.get('title', 'Action needed')
                deadline = item.get('deadline')
                bullet = text
                if deadline:
                    bullet = self._add_deadline_hint(bullet, deadline)
                section_lines.append(numbered_bullet(bullet))

            body_sections.append("\n".join(section_lines))

        if groups['overdue_task']:
            section_lines = ["**Overdue Tasks**"]
            for item in groups['overdue_task']:
                task_title = item.get('title', 'Task')
                due_date = item.get('due_date')

                bullet = task_title
                if due_date:
                    bullet = self._add_overdue_hint(bullet, due_date)
                section_lines.append(numbered_bullet(bullet))
            body_sections.append("\n".join(section_lines))

        if groups['urgent_item']:
            section_lines = ["**Urgent Items**"]
            for item in groups['urgent_item']:
                summary = item.get('summary') or item.get('title', 'Item')
                section_lines.append(numbered_bullet(summary))
            body_sections.append("\n".join(section_lines))

        if groups['cross_reference']:
            section_lines = ["**Connections Found**"]
            for item in groups['cross_reference']:
                summary = item.get('summary') or item.get('title', 'Connection')
                section_lines.append(numbered_bullet(summary))
            body_sections.append("\n".join(section_lines))

        if groups['other']:
            for item in groups['other']:
                summary = item.get('title', 'Item')
                body_sections.append(numbered_bullet(summary))

        body = "\n\n".join(body_sections)

        # Add "more items" note if truncated
        if count > 10:
            body += f"\n\n+ {count - 10} more items"

        # Add feedback tip when there are multiple items so users know they can reference by number
        if count >= 2:
            body += "\n\n💬 Reply with \"1 good, 2 wrong because X\" to give feedback"

        return title, body

    def format_urgent_nudge(self, item: dict) -> tuple[str, str]:
        """
        Format a single urgent nudge with type-specific title.

        Args:
            item: Nudge item dict with nudge_type, action_text, person_name, etc.

        Returns:
            Tuple of (title, body) for the urgent nudge
        """
        nudge_type = item.get('nudge_type', 'urgent_item')
        action_text = item.get('action_text') or item.get('title', 'Item')
        person_name = item.get('person_name')
        summary = item.get('summary', '')
        deadline = item.get('deadline')
        due_date = item.get('due_date')
        source = item.get('source', '')

        # Type-specific title patterns
        if nudge_type == 'deadline' or (deadline and self._is_due_today(deadline)):
            title = f"⏰ Due today: {action_text}"
        elif nudge_type == 'reply' or 'reply' in action_text.lower():
            if person_name:
                title = f"💬 Waiting for your reply: {person_name}"
            else:
                title = f"💬 Reply needed: {action_text}"
        elif nudge_type == 'urgent_item' or item.get('urgency') == 'urgent':
            title = f"🔴 Urgent: {summary or action_text}"
        elif nudge_type == 'overdue_task':
            task_title = item.get('title', action_text)
            title = f"⚠️ Overdue: {task_title}"
        else:
            title = f"🔔 {action_text}"

        # Build body with full context
        body_parts = []

        # Full action text if longer than title
        if len(action_text) > 50:
            body_parts.append(action_text)

        # Add context
        if person_name:
            body_parts.append(f"From: {person_name}")
        if source:
            body_parts.append(f"Source: {source}")
        if deadline:
            body_parts.append(f"Deadline: {self._format_deadline(deadline)}")
        elif due_date:
            body_parts.append(f"Due: {self._format_deadline(due_date)}")

        body = "\n".join(body_parts) if body_parts else None

        return title, body

    def _add_deadline_hint(self, text: str, deadline: str) -> str:
        """Add deadline hint to text if deadline is within 7 days."""
        try:
            if isinstance(deadline, str):
                deadline_dt = datetime.fromisoformat(deadline.replace('Z', '+00:00'))
            else:
                deadline_dt = deadline

            now = datetime.now(deadline_dt.tzinfo) if deadline_dt.tzinfo else datetime.now()
            days_until = (deadline_dt - now).days

            if days_until < 0:
                return f"{text} (overdue)"
            elif days_until == 0:
                return f"{text} (today)"
            elif days_until == 1:
                return f"{text} (tomorrow)"
            elif days_until <= 7:
                return f"{text} ({days_until} days)"
            return text
        except (ValueError, TypeError):
            return text

    def _add_overdue_hint(self, text: str, due_date: str) -> str:
        """Add overdue days hint to text."""
        try:
            if isinstance(due_date, str):
                due_dt = datetime.fromisoformat(due_date.replace('Z', '+00:00'))
            else:
                due_dt = due_date

            now = datetime.now()
            days_overdue = (now - due_dt.replace(tzinfo=None)).days

            if days_overdue > 0:
                return f"{text} ({days_overdue} day{'s' if days_overdue != 1 else ''} overdue)"
            return text
        except (ValueError, TypeError):
            return text

    def _is_due_today(self, deadline: str) -> bool:
        """Check if deadline is today."""
        try:
            if isinstance(deadline, str):
                deadline_dt = datetime.fromisoformat(deadline.replace('Z', '+00:00'))
            else:
                deadline_dt = deadline

            now = datetime.now(deadline_dt.tzinfo) if deadline_dt.tzinfo else datetime.now()
            return deadline_dt.date() == now.date()
        except (ValueError, TypeError):
            return False

    def _format_deadline(self, deadline: str) -> str:
        """Format deadline for display."""
        try:
            if isinstance(deadline, str):
                deadline_dt = datetime.fromisoformat(deadline.replace('Z', '+00:00'))
            else:
                deadline_dt = deadline

            return deadline_dt.strftime("%b %d, %Y %H:%M")
        except (ValueError, TypeError):
            return str(deadline)

    # =========================================================================
    # Private Helper Methods
    # =========================================================================

    def _get_preferences(self) -> dict:
        """Get cached nudge preferences."""
        if self._prefs is None:
            self._prefs = get_nudge_preferences(self.user_id)
        return self._prefs

    def _get_pattern_service(self):
        """Get pattern learning service with lazy initialization."""
        if self._pattern_service is None:
            from web.services.pattern_learning_service import PatternLearningService
            self._pattern_service = PatternLearningService(self.user_id)
        return self._pattern_service

    async def _process_detected_action(self, action: dict, stats: dict) -> str:
        """
        Process a single detected action and queue nudge if appropriate.

        Returns:
            'queued', 'skipped_dup', 'skipped_rate_limit', or 'skipped_suppressed'
        """
        action_id = action.get('id')

        # Dedup check
        if self.is_duplicate('detected_action', action_id):
            stats["skipped_dup"] += 1
            return 'skipped_dup'

        # Check if item type should be suppressed based on user patterns
        try:
            pattern_service = self._get_pattern_service()
            if await pattern_service.should_suppress_item_type('detected_action'):
                logger.info("Suppressing detected_action nudge due to user preference")
                stats.setdefault("skipped_suppressed", 0)
                stats["skipped_suppressed"] += 1
                return 'skipped_suppressed'
        except Exception as e:
            logger.warning("Pattern suppression check error: %s", repr(e))

        # Check sender-level nudge suppression
        try:
            sender_info = get_sender_for_detected_action(action_id)
            if sender_info:
                src_type, sender_id = sender_info
                if is_sender_nudge_suppressed(self.user_id, src_type, sender_id):
                    logger.info(
                        "Nudge suppressed: sender %s/%s is nudge-suppressed for user %d",
                        src_type, sender_id, self.user_id,
                    )
                    stats.setdefault("skipped_sender_suppressed", 0)
                    stats["skipped_sender_suppressed"] += 1
                    return 'skipped_sender_suppressed'
        except Exception as e:
            logger.warning("Sender nudge suppression check error: %s", repr(e))
            # Fail open — proceed with nudge creation

        # Skip if the event deadline has already passed (e.g. calendar appointments)
        deadline = action.get('deadline')
        if deadline:
            try:
                from datetime import timezone as _tz
                deadline_dt = datetime.fromisoformat(str(deadline).replace('Z', '+00:00'))
                if deadline_dt.tzinfo is None:
                    deadline_dt = deadline_dt.replace(tzinfo=_tz.utc)
                if deadline_dt < datetime.now(deadline_dt.tzinfo) - timedelta(hours=2):
                    logger.info(
                        "Skipping detected_action nudge for user %d: deadline %s is in the past",
                        self.user_id, str(deadline)[:19]
                    )
                    stats.setdefault("skipped_past_deadline", 0)
                    stats["skipped_past_deadline"] += 1
                    return 'skipped_past_deadline'
            except (ValueError, TypeError):
                pass  # Unparseable deadline — proceed normally

        # Classify urgency with pattern adjustments
        action['nudge_type'] = 'detected_action'
        urgency = await self.classify_urgency_with_patterns(action)

        # Rate limit check for urgent
        if urgency == 'urgent' and not self.check_rate_limit():
            stats["skipped_rate_limit"] += 1
            return 'skipped_rate_limit'

        # Build nudge content
        action_text = action.get('action_text', 'Action needed')
        source = action.get('source', 'inbound')
        person_name = action.get('person_name')

        title = action_text
        body = None
        if person_name:
            body = f"Related to: {person_name}"

        # Queue the nudge (channel='pending_delivery' until delivery is implemented)
        nudge_id = create_nudge(
            user_id=self.user_id,
            nudge_type='detected_action',
            channel='pending_delivery',
            title=title,
            body=body,
            urgency=urgency,
            source_type='detected_action',
            source_id=action_id,
        )

        if nudge_id:
            if urgency == 'urgent':
                stats["urgent_count"] += 1
            else:
                stats["normal_count"] += 1
            return 'queued'

        return 'error'

    async def _process_urgent_classification(self, item: dict, stats: dict) -> str:
        """
        Process an urgent classification and queue nudge if appropriate.

        Returns:
            'queued', 'skipped_dup', 'skipped_rate_limit', or 'skipped_suppressed'
        """
        classification_id = item.get('id')

        # Dedup check
        if self.is_duplicate('classification', classification_id):
            stats["skipped_dup"] += 1
            return 'skipped_dup'

        # Check if item type should be suppressed based on user patterns
        try:
            pattern_service = self._get_pattern_service()
            if await pattern_service.should_suppress_item_type('urgent_item'):
                logger.info("Suppressing urgent_item nudge due to user preference")
                stats.setdefault("skipped_suppressed", 0)
                stats["skipped_suppressed"] += 1
                return 'skipped_suppressed'
        except Exception as e:
            logger.warning("Pattern suppression check error: %s", repr(e))

        # Check rate limit (urgent by definition, but pattern could downgrade)
        item['nudge_type'] = 'urgent_item'
        urgency = await self.classify_urgency_with_patterns(item)

        if urgency == 'urgent' and not self.check_rate_limit():
            stats["skipped_rate_limit"] += 1
            return 'skipped_rate_limit'

        # Build nudge content
        summary = item.get('summary', 'Urgent item needs attention')
        source = item.get('source', 'inbound')

        title = f"Urgent: {summary}"

        # Queue the nudge
        nudge_id = create_nudge(
            user_id=self.user_id,
            nudge_type='urgent_item',
            channel='pending_delivery',
            title=title,
            body=None,
            urgency=urgency,
            source_type='classification',
            source_id=classification_id,
        )

        if nudge_id:
            if urgency == 'urgent':
                stats["urgent_count"] += 1
            else:
                stats["normal_count"] += 1
            return 'queued'

        return 'error'

    async def _process_overdue_task(self, task: dict, stats: dict) -> str:
        """
        Process an overdue task and queue nudge if appropriate.

        Returns:
            'queued', 'skipped_dup', 'skipped_rate_limit', or 'skipped_suppressed'
        """
        task_id = task.get('id')

        # Dedup check
        if self.is_duplicate('task', task_id):
            stats["skipped_dup"] += 1
            return 'skipped_dup'

        # Check if item type should be suppressed based on user patterns
        try:
            pattern_service = self._get_pattern_service()
            if await pattern_service.should_suppress_item_type('overdue_task'):
                logger.info("Suppressing overdue_task nudge due to user preference")
                stats.setdefault("skipped_suppressed", 0)
                stats["skipped_suppressed"] += 1
                return 'skipped_suppressed'
        except Exception as e:
            logger.warning("Pattern suppression check error: %s", repr(e))

        # Add source_type for urgency classification with pattern adjustment
        task['source_type'] = 'task'
        task['nudge_type'] = 'overdue_task'
        urgency = await self.classify_urgency_with_patterns(task)

        # Rate limit check for urgent
        if urgency == 'urgent' and not self.check_rate_limit():
            stats["skipped_rate_limit"] += 1
            return 'skipped_rate_limit'

        # Build nudge content
        title = task.get('title', 'Task')
        due_date = task.get('due_date')

        if due_date:
            try:
                due_dt = datetime.fromisoformat(due_date.replace('Z', '+00:00'))
                days_overdue = (datetime.now() - due_dt.replace(tzinfo=None)).days
                if days_overdue > 0:
                    title = f"Overdue ({days_overdue}d): {title}"
                else:
                    title = f"Overdue: {title}"
            except (ValueError, TypeError):
                title = f"Overdue: {title}"

        # Queue the nudge
        nudge_id = create_nudge(
            user_id=self.user_id,
            nudge_type='overdue_task',
            channel='pending_delivery',
            title=title,
            body=task.get('description'),
            urgency=urgency,
            source_type='task',
            source_id=task_id,
        )

        if nudge_id:
            if urgency == 'urgent':
                stats["urgent_count"] += 1
            else:
                stats["normal_count"] += 1
            return 'queued'

        return 'error'

    def _get_urgent_classifications(self, limit: int = 20) -> list[dict]:
        """Get classifications marked as urgent that haven't been nudged."""
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                since_24h = (datetime.utcnow() - timedelta(hours=24)).isoformat()
                cursor.execute("""
                    SELECT ic.id, ic.scanned_item_id, ic.relevance, ic.urgency,
                           ic.summary, ic.classified_at,
                           si.source, si.source_id, si.source_metadata
                    FROM item_classifications ic
                    JOIN scanned_items si ON ic.scanned_item_id = si.id
                    WHERE ic.user_id = %s
                      AND ic.urgency = 'urgent'
                      AND ic.classified_at >= %s
                    ORDER BY ic.classified_at DESC
                    LIMIT %s
                """, (self.user_id, since_24h, limit))
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error("Failed to get urgent classifications: %s", repr(e))
            return []

    def _get_overdue_tasks(self, limit: int = 10) -> list[dict]:
        """Get overdue tasks for the user."""
        try:
            now = datetime.now()
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, title, description, status, priority, due_date,
                           category, project, type, created_at
                    FROM tasks
                    WHERE user_id = %s
                      AND due_date < %s
                      AND status NOT IN ('completed', 'cancelled')
                    ORDER BY due_date ASC
                    LIMIT %s
                """, (self.user_id, now.isoformat(), limit))
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error("Failed to get overdue tasks: %s", repr(e))
            return []

    def _get_priority_context_items(self, limit: int = 20) -> list[dict]:
        """
        Fetch active priority_context items that should generate nudges.

        Candidates: status='active', priority_level >= 1 (high or critical).
        Items with due_at more than 24h in the future are skipped — not yet time.
        Items with due_at more than 2h in the past are skipped — too late to act.
        Items with no due_at are always candidates (time-windowed dedup decides frequency).
        """
        try:
            items = get_priority_items(self.user_id, status='active', limit=limit)
            candidates = []
            for item in items:
                if item.get('priority_level', 0) < 1:
                    continue
                due_at = item.get('due_at')
                if due_at:
                    # Parse due_at and apply timing window
                    try:
                        from datetime import timezone
                        due_dt = datetime.fromisoformat(due_at.replace('Z', '+00:00'))
                        now_dt = datetime.now(timezone.utc)
                        delta = due_dt - now_dt
                        # Skip if more than 24h away (not urgent yet)
                        if delta.total_seconds() > 86400:
                            continue
                        # Skip if more than 2h past (window has closed)
                        if delta.total_seconds() < -7200:
                            continue
                    except Exception:
                        pass  # If we can't parse due_at, include the item anyway
                candidates.append(item)
            return candidates
        except Exception as e:
            logger.warning("_get_priority_context_items error: %s", repr(e))
            return []

    async def _process_priority_context_item(self, item: dict, stats: dict) -> str:
        """
        Process a single priority_context item and queue a nudge if appropriate.

        Uses time-windowed dedup (1 day) instead of hard dedup — allows re-nudging
        daily until the user resolves the item. This is intentional: "aggressive
        follow-up" means the nudge system keeps surfacing it, not just fires once.

        Returns: 'queued', 'skipped_dup', 'skipped_rate_limit', or 'skipped_suppressed'
        """
        item_id = item.get('id')

        # Time-windowed dedup: skip if already nudged about this item in last 24h
        recent = get_recent_nudge_for_source(
            self.user_id, 'priority_context', item_id, days=1
        )
        if recent:
            stats["skipped_dup"] += 1
            return 'skipped_dup'

        # Check if item type should be suppressed based on user patterns
        try:
            pattern_service = self._get_pattern_service()
            if await pattern_service.should_suppress_item_type('priority_context'):
                logger.info("Suppressing priority_context nudge due to user preference")
                stats.setdefault("skipped_suppressed", 0)
                stats["skipped_suppressed"] += 1
                return 'skipped_suppressed'
        except Exception as e:
            logger.warning("Pattern suppression check error: %s", repr(e))

        # Build urgency from priority_level
        # priority_level=2 (critical) → urgent, priority_level=1 (high) → normal
        # (high items still nudge daily, just don't consume the urgent rate limit)
        priority_level = item.get('priority_level', 1)
        urgency = 'urgent' if priority_level >= 2 else 'normal'

        # Rate limit check for urgent
        if urgency == 'urgent' and not self.check_rate_limit():
            stats["skipped_rate_limit"] += 1
            return 'skipped_rate_limit'

        # Build nudge content
        title = item.get('title', 'Priority item needs attention')
        description = item.get('description')
        due_at = item.get('due_at')

        body = None
        if description:
            body = description
        if due_at:
            # Append due date context if present
            try:
                due_dt = datetime.fromisoformat(due_at.replace('Z', '+00:00'))
                due_str = due_dt.strftime('%a %b %-d at %-I:%M %p')
                body = (body + f" | Due: {due_str}") if body else f"Due: {due_str}"
            except Exception:
                pass

        nudge_id = create_nudge(
            user_id=self.user_id,
            nudge_type='priority_context',
            channel='pending_delivery',
            title=title,
            body=body,
            urgency=urgency,
            source_type='priority_context',
            source_id=item_id,
        )

        if nudge_id:
            if urgency == 'urgent':
                stats["urgent_count"] += 1
            else:
                stats["normal_count"] += 1
            return 'queued'

        return 'error'

    def _get_pending_normal_nudges(self) -> list[dict]:
        """Get pending normal-priority nudges not yet batched.

        JOINs detected_actions to recover person_name and action_text,
        which are needed for per-person deduplication in format_batch_digest().

        Excludes nudges whose source has recent 'already_handled' or 'dismissed'
        feedback (within 24h) so batch digests don't re-surface actioned items.
        """
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT n.id, n.nudge_type, n.title, n.body, n.urgency,
                           n.source_type, n.source_id, n.created_at,
                           da.person_name, da.action_text
                    FROM nudges n
                    LEFT JOIN detected_actions da
                        ON n.source_type = 'detected_action' AND n.source_id = da.id
                    WHERE n.user_id = %s
                      AND n.status = 'pending'
                      AND n.urgency = 'normal'
                      AND n.batch_id IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM user_feedback uf
                          JOIN nudges n2 ON n2.id = uf.item_id
                          WHERE uf.user_id = n.user_id
                            AND uf.item_type = 'nudge'
                            AND uf.feedback_type IN ('already_handled', 'dismissed')
                            AND n.source_type IS NOT NULL
                            AND n2.source_type = n.source_type
                            AND n2.source_id = n.source_id
                            AND uf.created_at > NOW() - INTERVAL '24 hours'
                      )
                    ORDER BY n.created_at ASC
                """, (self.user_id,))
                nudges = [dict(row) for row in cursor.fetchall()]

            # Python-side filter: exclude nudges from nudge-suppressed senders.
            # We resolve each detected_action nudge's sender and drop it if
            # that sender is on the suppression list.
            try:
                filtered = []
                for nudge in nudges:
                    if nudge.get('source_type') == 'detected_action' and nudge.get('source_id'):
                        sender_info = get_sender_for_detected_action(nudge['source_id'])
                        if sender_info:
                            src_type, sender_ident = sender_info
                            if is_sender_nudge_suppressed(self.user_id, src_type, sender_ident):
                                logger.debug(
                                    "Filtering suppressed-sender nudge %d (%s/%s) from batch",
                                    nudge['id'], src_type, sender_ident,
                                )
                                continue
                    filtered.append(nudge)
                return filtered
            except Exception as e:
                logger.warning(
                    "Sender suppression filter in _get_pending_normal_nudges failed: %s",
                    repr(e),
                )
                return nudges  # Fail open — return unfiltered list

        except Exception as e:
            logger.error("Failed to get pending normal nudges: %s", repr(e))
            return []

    # =========================================================================
    # User Response Methods
    # =========================================================================

    def record_response(
        self,
        nudge_id: int,
        response_type: str,
        snooze_until: Optional[str] = None
    ) -> dict:
        """
        Record user's response to a nudge.

        Updates the nudge's user_response field and also records to the
        user_feedback table for pattern learning.

        Args:
            nudge_id: ID of the nudge being responded to
            response_type: Response type ('helpful', 'dismissed', 'snoozed')
            snooze_until: Optional ISO timestamp for when to resurface (if snoozed)

        Returns:
            Dict with {success: bool, error: str (if failed)}
        """
        result = {"success": False, "error": None}

        # Validate response_type
        valid_responses = {'helpful', 'dismissed', 'snoozed', 'already_handled'}
        if response_type not in valid_responses:
            result["error"] = f"Invalid response_type. Must be one of: {', '.join(valid_responses)}"
            return result

        # Verify the nudge exists and belongs to this user
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, user_id, nudge_type
                    FROM nudges
                    WHERE id = %s AND user_id = %s
                """, (nudge_id, self.user_id))
                nudge = cursor.fetchone()

                if not nudge:
                    result["error"] = "Nudge not found or does not belong to this user"
                    return result

        except Exception as e:
            logger.error("Failed to verify nudge %d: %s", nudge_id, repr(e))
            result["error"] = "Database error verifying nudge"
            return result

        # Update the nudge's user_response field
        success = record_nudge_response(nudge_id, response_type)
        if not success:
            result["error"] = "Failed to update nudge response"
            return result

        # Map nudge response to feedback_type for pattern learning
        feedback_type_map = {
            'helpful': 'helpful',
            'dismissed': 'not_helpful',
            'snoozed': 'snooze',
            'already_handled': 'already_handled',
        }
        feedback_type = feedback_type_map.get(response_type, response_type)

        # Build feedback context
        feedback_context = None
        if snooze_until:
            import json
            feedback_context = json.dumps({"snooze_until": snooze_until})

        # Record to user_feedback table for pattern learning
        feedback_id = record_feedback(
            user_id=self.user_id,
            item_type=nudge['nudge_type'],
            item_id=nudge_id,
            feedback_type=feedback_type,
            feedback_context=feedback_context,
        )

        if feedback_id:
            logger.info(
                "Recorded nudge response for user %d: nudge=%d, response=%s, feedback_id=%d",
                self.user_id, nudge_id, response_type, feedback_id
            )
        else:
            logger.warning(
                "Nudge response recorded but feedback tracking failed: user=%d, nudge=%d",
                self.user_id, nudge_id
            )

        # Propagate dismissal to related pending nudges from the same sender.
        # When a user marks a nudge "already_handled" or "dismissed", suppress
        # other pending detected_action nudges from the same person (by name
        # or sender email) to avoid whack-a-mole on multi-email topics.
        if response_type in ('already_handled', 'dismissed'):
            try:
                self._propagate_feedback_to_related_nudges(nudge_id)
            except Exception as e:
                logger.warning(
                    "Feedback propagation failed for nudge %d: %s", nudge_id, repr(e)
                )

            # Check if repeated dismissals should escalate to sender-level suppression
            try:
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT source_type, source_id FROM nudges
                        WHERE id = %s AND user_id = %s
                    """, (nudge_id, self.user_id))
                    src_row = cursor.fetchone()
                if src_row:
                    src_row = dict(src_row) if hasattr(src_row, 'keys') else {
                        'source_type': src_row[0], 'source_id': src_row[1]
                    }
                    n_source_type = src_row.get('source_type')
                    n_source_id = src_row.get('source_id')
                    if n_source_type and n_source_id:
                        self._check_sender_escalation(nudge_id, n_source_type, n_source_id)
            except Exception as e:
                logger.warning(
                    "Sender escalation check failed for nudge %d: %s", nudge_id, repr(e)
                )

        result["success"] = True
        return result

    def _check_sender_escalation(
        self,
        nudge_id: int,
        source_type: str,
        source_id: int,
    ) -> None:
        """
        Auto-escalate repeated dismissals to sender-level nudge suppression.

        When a user dismisses (or marks 'already_handled') >= 3 nudges from the
        same sender within the last 30 days, the sender is added to the
        nudge_suppressed_senders table.  Any remaining pending nudges from that
        sender are also marked as 'suppressed'.

        Only applies to detected_action nudges — other types have no sender.
        Fails open: errors are logged but never prevent the response from being
        recorded.
        """
        if source_type != 'detected_action':
            return

        sender_info = get_sender_for_detected_action(source_id)
        if not sender_info:
            return

        src_type, sender_ident = sender_info

        # Already suppressed — nothing to do
        if is_sender_nudge_suppressed(self.user_id, src_type, sender_ident):
            return

        # Count dismissed nudges from this sender in the last 30 days.
        # Strategy: fetch all dismissed/already_handled detected_action nudges
        # in the window, resolve each sender, and count matches.
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT n.source_id
                    FROM nudges n
                    WHERE n.user_id = %s
                      AND n.source_type = 'detected_action'
                      AND n.user_response IN ('dismissed', 'already_handled', 'auto_dismissed')
                      AND n.created_at > NOW() - INTERVAL '30 days'
                """, (self.user_id,))
                dismissed_rows = cursor.fetchall()
        except Exception as e:
            logger.warning("Sender escalation query failed: %s", repr(e))
            return

        match_count = 0
        for row in dismissed_rows:
            da_id = row['source_id'] if isinstance(row, dict) else row[0]
            if da_id is None:
                continue
            try:
                other_sender = get_sender_for_detected_action(da_id)
                if other_sender and other_sender == (src_type, sender_ident):
                    match_count += 1
            except Exception:
                continue

        if match_count < 3:
            return

        # Escalate: add sender to suppression list
        reason = f"{match_count} dismissed nudges in last 30 days"
        added = add_nudge_suppressed_sender(
            self.user_id, src_type, sender_ident, reason=reason
        )
        if added:
            logger.info(
                "Sender escalated to nudge suppression for user %d: %s/%s (%s)",
                self.user_id, src_type, sender_ident, reason,
            )

        # Mark remaining pending nudges from this sender as 'suppressed'
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                # Fetch pending detected_action nudges
                cursor.execute("""
                    SELECT n.id, n.source_id
                    FROM nudges n
                    WHERE n.user_id = %s
                      AND n.source_type = 'detected_action'
                      AND n.status = 'pending'
                      AND n.batch_id IS NULL
                """, (self.user_id,))
                pending_rows = cursor.fetchall()

            suppressed_ids = []
            for row in pending_rows:
                row = dict(row) if hasattr(row, 'keys') else {'id': row[0], 'source_id': row[1]}
                da_id = row.get('source_id')
                if da_id is None:
                    continue
                try:
                    other_sender = get_sender_for_detected_action(da_id)
                    if other_sender and other_sender == (src_type, sender_ident):
                        suppressed_ids.append(row['id'])
                except Exception:
                    continue

            if suppressed_ids:
                with get_db() as conn:
                    cursor = conn.cursor()
                    # Update in a single statement using ANY
                    cursor.execute("""
                        UPDATE nudges
                        SET status = 'suppressed', user_response = 'sender_suppressed'
                        WHERE id = ANY(%s) AND user_id = %s
                    """, (suppressed_ids, self.user_id))
                    logger.info(
                        "Suppressed %d pending nudges from escalated sender %s/%s for user %d",
                        len(suppressed_ids), src_type, sender_ident, self.user_id,
                    )
        except Exception as e:
            logger.warning(
                "Failed to suppress pending nudges after sender escalation: %s", repr(e)
            )

    def _propagate_feedback_to_related_nudges(self, nudge_id: int) -> int:
        """
        Auto-dismiss other pending nudges related to the given nudge.

        Two-pass approach:
        1. **Name/email match:** Dismiss pending nudges with the same person_name
           (case-insensitive) or same sender email from scanned_items.source_metadata.
        2. **Haiku smart dedup (if enabled):** For remaining pending nudges not caught
           by pass 1, use Haiku to compare action_text and dismiss topic-level duplicates.

        Only affects pending detected_action nudges that haven't been batched yet.
        Scoped to same source_type to avoid cross-type suppression.

        Returns total count of nudges dismissed across both passes.
        """
        total_dismissed = 0

        try:
            with get_db() as conn:
                cursor = conn.cursor()

                # Get the dismissed nudge's details
                cursor.execute("""
                    SELECT n.source_type, n.source_id,
                           da.person_name, da.action_text, da.scanned_item_id
                    FROM nudges n
                    LEFT JOIN detected_actions da
                        ON n.source_type = 'detected_action' AND n.source_id = da.id
                    WHERE n.id = %s AND n.user_id = %s
                """, (nudge_id, self.user_id))
                row = cursor.fetchone()
                if not row:
                    return 0

                row = dict(row)
                source_type = row.get('source_type')
                person_name = row.get('person_name')
                action_text = row.get('action_text')
                scanned_item_id = row.get('scanned_item_id')

                # Only propagate for detected_action nudges
                if source_type != 'detected_action':
                    return 0

                # Extract sender email from the scanned item's metadata
                sender_email = None
                if scanned_item_id:
                    cursor.execute("""
                        SELECT source_metadata FROM scanned_items
                        WHERE id = %s AND user_id = %s
                    """, (scanned_item_id, self.user_id))
                    si_row = cursor.fetchone()
                    if si_row:
                        import json as _json
                        try:
                            meta = _json.loads(
                                si_row['source_metadata'] if isinstance(si_row, dict)
                                else si_row[0]
                            ) if (si_row['source_metadata'] if isinstance(si_row, dict) else si_row[0]) else {}
                            sender_email = meta.get('from', '').strip().lower() or None
                        except (ValueError, TypeError, KeyError):
                            pass

                # --- Pass 1: Name/email match ---
                if person_name or sender_email:
                    conditions = []
                    params = [self.user_id, nudge_id]

                    if person_name:
                        conditions.append("LOWER(da2.person_name) = LOWER(%s)")
                        params.append(person_name)

                    if sender_email:
                        conditions.append(
                            "LOWER(si2.source_metadata::text) LIKE %s"
                        )
                        params.append(f'%"from": "{sender_email}"%')

                    if conditions:
                        match_clause = " OR ".join(conditions)

                        cursor.execute(f"""
                            UPDATE nudges n_target
                            SET status = 'dismissed',
                                user_response = 'auto_dismissed'
                            FROM detected_actions da2
                            JOIN scanned_items si2 ON da2.scanned_item_id = si2.id
                            WHERE n_target.user_id = %s
                              AND n_target.id != %s
                              AND n_target.source_type = 'detected_action'
                              AND n_target.source_id = da2.id
                              AND n_target.status = 'pending'
                              AND n_target.batch_id IS NULL
                              AND ({match_clause})
                        """, params)

                        pass1_dismissed = cursor.rowcount
                        total_dismissed += pass1_dismissed
                        if pass1_dismissed > 0:
                            logger.info(
                                "Feedback propagation pass 1 (name/email) from nudge %d: "
                                "auto-dismissed %d nudges (person=%s, email=%s)",
                                nudge_id, pass1_dismissed, person_name, sender_email
                            )

                # --- Pass 2: Haiku smart dedup ---
                prefs = get_nudge_preferences(self.user_id)
                smart_dedup = prefs.get('nudge_smart_dedup', True)

                if smart_dedup and action_text:
                    try:
                        pass2 = self._haiku_smart_dedup(nudge_id, action_text)
                        total_dismissed += pass2
                    except Exception as e:
                        logger.warning(
                            "Haiku smart dedup failed for nudge %d: %s", nudge_id, repr(e)
                        )

                return total_dismissed

        except Exception as e:
            logger.error("_propagate_feedback_to_related_nudges failed: %s", repr(e))
            return 0

    def _haiku_smart_dedup(self, dismissed_nudge_id: int, dismissed_action_text: str) -> int:
        """
        Use Haiku to identify remaining pending nudges that are about the same topic
        as the dismissed nudge, even if name/email don't match.

        Sends a single Haiku call with the dismissed action and all remaining pending
        action texts. Haiku returns which ones are duplicates.

        Returns count of nudges dismissed.
        """
        import json as _json

        try:
            with get_db() as conn:
                cursor = conn.cursor()

                # Get remaining pending detected_action nudges not yet dismissed
                cursor.execute("""
                    SELECT n.id, da.action_text
                    FROM nudges n
                    JOIN detected_actions da ON n.source_id = da.id
                    WHERE n.user_id = %s
                      AND n.id != %s
                      AND n.source_type = 'detected_action'
                      AND n.status = 'pending'
                      AND n.batch_id IS NULL
                      AND da.action_text IS NOT NULL
                """, (self.user_id, dismissed_nudge_id))
                candidates = [dict(r) for r in cursor.fetchall()]

                if not candidates:
                    return 0

                # Build the comparison prompt
                candidate_lines = []
                for i, c in enumerate(candidates[:15]):  # Cap at 15 to keep prompt small
                    candidate_lines.append(f'{i}: {c["action_text"][:200]}')

                candidates_block = "\n".join(candidate_lines)

                prompt = (
                    "The user just dismissed this action item:\n"
                    f'"{dismissed_action_text[:300]}"\n\n'
                    "Below are other pending action items. Which ones are about the SAME underlying "
                    "topic or request? Only include items that are clearly duplicates or follow-ups "
                    "to the same issue — not merely from the same person about a different topic.\n\n"
                    f"{candidates_block}\n\n"
                    'Return a JSON array of the index numbers that are duplicates. '
                    'If none are duplicates, return []. '
                    'Return ONLY the JSON array, no explanation.'
                )

                from anthropic import Anthropic
                client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
                response = client.messages.create(
                    model='claude-haiku-4-5-20251001',
                    max_tokens=100,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw = response.content[0].text.strip()

                # Parse response
                raw = raw.strip().strip('`').strip()
                if raw.startswith('json'):
                    raw = raw[4:].strip()
                duplicate_indices = _json.loads(raw)

                if not isinstance(duplicate_indices, list) or not duplicate_indices:
                    return 0

                # Dismiss the matching nudges
                nudge_ids_to_dismiss = []
                for idx in duplicate_indices:
                    if isinstance(idx, int) and 0 <= idx < len(candidates):
                        nudge_ids_to_dismiss.append(candidates[idx]['id'])

                if not nudge_ids_to_dismiss:
                    return 0

                placeholders = ','.join(['%s'] * len(nudge_ids_to_dismiss))
                cursor.execute(f"""
                    UPDATE nudges
                    SET status = 'dismissed',
                        user_response = 'auto_dismissed_smart'
                    WHERE id IN ({placeholders})
                      AND user_id = %s
                      AND status = 'pending'
                """, nudge_ids_to_dismiss + [self.user_id])

                dismissed = cursor.rowcount
                if dismissed > 0:
                    logger.info(
                        "Feedback propagation pass 2 (Haiku smart dedup) from nudge %d: "
                        "auto-dismissed %d topic-duplicate nudges",
                        dismissed_nudge_id, dismissed
                    )
                return dismissed

        except Exception as e:
            logger.warning("_haiku_smart_dedup failed: %s", repr(e))
            return 0

    # =========================================================================
    # Drip Queue Methods - : Conversational Nudge Flow
    # =========================================================================

    def format_drip_message(self, item: dict) -> str:
        """
        Format a single nudge as one short conversational sentence.

        No emoji walls, no bullet points, no headers. Direct and casual —
        a check-in from someone who actually cares. Keeps it under 100 chars.

        Args:
            item: Nudge dict with nudge_type, title, body, etc.

        Returns:
            Single conversational sentence string
        """
        nudge_type = item.get('nudge_type', '')
        title = item.get('title', '').strip()

        if nudge_type == 'overdue_task':
            return f"Still need to {title}?"
        elif nudge_type == 'detected_action':
            return f"Did you {title}?"
        elif nudge_type == 'urgent_item':
            return f"This one needs attention — {title}."
        elif nudge_type == 'relationship_checkin_prompt':
            return f"Any updates on {title}?"
        else:
            return f"Still need to handle: {title}?"

    async def is_nudge_stale(self, item: dict) -> tuple[bool, str]:
        """
        Use Claude Haiku to determine if a queued drip nudge is still actionable.

        Checks whether the nudge content remains relevant today given recent
        communications and the user's active priorities. Fails open — on any
        error returns (False, "") so the nudge is still sent.

        Args:
            item: Nudge dict with id, title, body, nudge_type, created_at, etc.

        Returns:
            Tuple of (is_stale: bool, reason: str).
            (True, reason) means dismiss the nudge.
            (False, "") means send it.

        Phase 42: Stale Nudge Resolution
        """
        try:
            try:
                from web.api.settings import get_user_settings as _gus
                _s = _gus(self.user_id)
                _tz = ZoneInfo(_s.get('digest_timezone', 'America/Chicago') if _s else 'America/Chicago')
            except Exception:
                _tz = ZoneInfo('America/Chicago')
            today = datetime.now(ZoneInfo('UTC')).astimezone(_tz).strftime("%Y-%m-%d %A")
            title = item.get('title', '')
            body = item.get('body', '') or ''
            nudge_type = item.get('nudge_type', '')
            created_at = item.get('created_at', '')

            # Deterministic check: titles containing time-relative language ("in X hours",
            # "in 1 hour", "in 30 minutes") are time-bound events. If the nudge is older
            # than 4 hours, the referenced event has already passed regardless of nudge_type.
            # This covers meeting_prep, calendar_event, AND urgent_item calendar classifications.
            import re as _re
            _time_ref = _re.compile(
                r'\bin\s+(?:a|an|\d+)\s*(?:hour|hr|minute|min)',
                _re.IGNORECASE
            )
            if _time_ref.search(title):
                try:
                    from datetime import timezone as _tz_tr
                    ca_tr = str(created_at).replace('Z', '+00:00')
                    created_dt_tr = datetime.fromisoformat(ca_tr)
                    if created_dt_tr.tzinfo is None:
                        created_dt_tr = created_dt_tr.replace(tzinfo=_tz_tr.utc)
                    age_hours_tr = (datetime.now(_tz_tr.utc) - created_dt_tr).total_seconds() / 3600
                    if age_hours_tr > 1.5:
                        age_min_tr = int(age_hours_tr * 60)
                        reason = f"Title references 'in X hours/minutes' but nudge is {age_min_tr}min old -- event has passed"
                        logger.info("Stale (time-reference expired) nudge %s: %s", item.get('id'), reason)
                        return (True, reason)
                except (ValueError, TypeError):
                    pass  # Unparseable created_at — fall through

            # Deterministic check: calendar/meeting nudges expire after 6 hours.
            # These nudges (meeting_prep, calendar_event) are created when an event is ~4h
            # away. If delivery failed at the time, they must not resurface days/weeks later
            # — the event has long since passed. No deadline field exists on these nudge
            # types (LEFT JOIN only covers detected_action source), so check age directly.
            if nudge_type in ('meeting_prep', 'calendar_event'):
                try:
                    from datetime import timezone as _tz_cal
                    ca = str(created_at).replace('Z', '+00:00')
                    created_dt = datetime.fromisoformat(ca)
                    if created_dt.tzinfo is None:
                        created_dt = created_dt.replace(tzinfo=_tz_cal.utc)
                    age_hours = (datetime.now(_tz_cal.utc) - created_dt).total_seconds() / 3600
                    max_age = 1.5 if nudge_type == 'meeting_prep' else 5.0
                    if age_hours > max_age:
                        age_min = int(age_hours * 60)
                        reason = f"{nudge_type} nudge is {age_min}min old (limit {int(max_age * 60)}min) -- event has passed"
                        logger.info("Stale (calendar age) nudge %s: %s", item.get('id'), reason)
                        return (True, reason)
                except (ValueError, TypeError):
                    pass  # Unparseable created_at — fall through to Claude check

            # Deterministic check: if we have a known deadline and it's in the past, mark stale
            # immediately without spending a Claude Haiku call.
            deadline = item.get('deadline')
            if deadline:
                try:
                    from datetime import timezone as _tz
                    deadline_dt = datetime.fromisoformat(str(deadline).replace('Z', '+00:00'))
                    if deadline_dt.tzinfo is None:
                        deadline_dt = deadline_dt.replace(tzinfo=_tz.utc)
                    if deadline_dt < datetime.now(deadline_dt.tzinfo):
                        reason = f"Event deadline {str(deadline)[:19]} has already passed"
                        logger.info("Stale (deadline passed) nudge %s: %s", item.get('id'), reason)
                        return (True, reason)
                except (ValueError, TypeError):
                    pass  # Unparseable deadline — fall through to Claude check

            # Phase 73: Task completion check -- overdue_task nudges for completed tasks
            if nudge_type == 'overdue_task' and item.get('source_type') == 'task' and item.get('source_id'):
                try:
                    with get_db() as conn:
                        cursor = conn.cursor()
                        cursor.execute(
                            "SELECT status FROM tasks WHERE id = %s AND user_id = %s",
                            (item['source_id'], self.user_id)
                        )
                        task_row = cursor.fetchone()
                        if task_row:
                            task_status = task_row['status'] if isinstance(task_row, dict) else task_row[0]
                            if task_status == 'completed':
                                reason = f"Task {item['source_id']} is already completed"
                                logger.info("Stale (task completed) nudge %s: %s", item.get('id'), reason)
                                return (True, reason)
                except Exception:
                    pass  # Fail open

            # Phase 73: Calendar event cancellation check
            if nudge_type in ('meeting_prep', 'calendar_event') and item.get('source_id'):
                try:
                    with get_db() as conn:
                        cursor = conn.cursor()
                        cursor.execute(
                            "SELECT status FROM calendar_event_nudges WHERE id = %s AND user_id = %s",
                            (item['source_id'], self.user_id)
                        )
                        evt_row = cursor.fetchone()
                        if evt_row:
                            evt_status = evt_row['status'] if isinstance(evt_row, dict) else evt_row[0]
                            if evt_status == 'cancelled':
                                reason = f"Calendar event {item['source_id']} was cancelled"
                                logger.info("Stale (event cancelled) nudge %s: %s", item.get('id'), reason)
                                return (True, reason)
                except Exception:
                    pass  # Fail open

            # Phase 73: Detected action thread reply check -- if user already replied
            if nudge_type == 'detected_action' and item.get('source_type') == 'detected_action' and item.get('source_id'):
                try:
                    with get_db() as conn:
                        cursor = conn.cursor()
                        # Resolve: detected_actions -> scanned_items -> item_classifications.thread_id
                        cursor.execute("""
                            SELECT ic.thread_id
                            FROM detected_actions da
                            JOIN scanned_items si ON si.id = da.scanned_item_id
                            JOIN item_classifications ic ON ic.scanned_item_id = si.id
                            WHERE da.id = %s
                            AND ic.thread_id IS NOT NULL
                            LIMIT 1
                        """, (item['source_id'],))
                        thread_row = cursor.fetchone()
                        if thread_row:
                            thread_id = thread_row['thread_id'] if isinstance(thread_row, dict) else thread_row[0]
                            # Check if user sent outbound message in same thread after nudge creation
                            nudge_created = item.get('created_at', '')
                            cursor.execute("""
                                SELECT 1 FROM scanned_items si2
                                JOIN item_classifications ic2 ON ic2.scanned_item_id = si2.id
                                WHERE si2.user_id = %s
                                  AND ic2.thread_id = %s
                                  AND si2.direction = 'outbound'
                                  AND si2.detected_at > %s
                                LIMIT 1
                            """, (self.user_id, thread_id, str(nudge_created)))
                            reply_row = cursor.fetchone()
                            if reply_row:
                                reason = f"User already replied in thread {thread_id} after nudge creation"
                                logger.info("Stale (thread reply) nudge %s: %s", item.get('id'), reason)
                                return (True, reason)
                except Exception:
                    pass  # Fail open

            # Phase 73: LCD Layer 2 observation word-match -- if recent observations
            # contain >=2 significant words from the nudge title, the topic was
            # recently observed and the nudge may be redundant.
            try:
                from web.core.database import get_recent_lcd_observations
                _words = [w.lower() for w in title.split() if len(w) >= 4]
                if _words:
                    lcd_obs = get_recent_lcd_observations(self.user_id, limit=30)
                    for obs in lcd_obs:
                        obs_text = (obs.get('content') or '').lower()
                        matched = sum(1 for w in _words if w in obs_text)
                        if matched >= 2:
                            reason = f"LCD observation matches {matched} words from title"
                            logger.info("Stale (LCD observation match) nudge %s: %s", item.get('id'), reason)
                            return (True, reason)
            except Exception:
                pass  # Fail open

            # Gather active priority context items
            priority_items = get_priority_items(self.user_id, status='active', limit=20)
            if priority_items:
                priority_summary = "\n".join(
                    f"- {p.get('title', '(no title)')}"
                    for p in priority_items
                )
            else:
                priority_summary = "(none)"

            # Gather recent scanned items (last 3 days)
            three_days_ago = (datetime.now() - timedelta(days=3)).isoformat()
            scanned_lines = []
            try:
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT source, item_type, source_metadata, detected_at, direction"
                        " FROM scanned_items"
                        " WHERE user_id=%s AND detected_at > %s"
                        " ORDER BY detected_at DESC LIMIT 15",
                        (self.user_id, three_days_ago)
                    )
                    rows = cursor.fetchall()
                    for row in rows:
                        source = row['source'] if isinstance(row, dict) else row[0]
                        meta_raw = row['source_metadata'] if isinstance(row, dict) else row[2]
                        direction = (row['direction'] if isinstance(row, dict) else row[4]) or 'inbound'
                        label = 'SENT' if direction == 'outbound' else 'RECEIVED'
                        snippet = ''
                        if meta_raw:
                            try:
                                meta = json.loads(meta_raw)
                                snippet = (
                                    meta.get('subject') or
                                    meta.get('text') or
                                    meta.get('body') or
                                    meta.get('title') or ''
                                )[:100]
                            except (json.JSONDecodeError, TypeError):
                                pass
                        scanned_lines.append(f"- [{source}] [{label}] {snippet}")
            except Exception as e:
                logger.warning("is_nudge_stale: failed to query scanned_items for user %d: %s", self.user_id, repr(e))

            scanned_summary = "\n".join(scanned_lines) if scanned_lines else "(none)"

            body_line = f"\n{body}" if body else ""
            prompt = f"""Today is {today}. A reminder was queued to tell the user:

"{title}"{body_line}

Queued at: {created_at}

Recent communications (last 3 days — [SENT] = sent by user, [RECEIVED] = received by user):
{scanned_summary}

User's active commitments and priorities:
{priority_summary}

Decide: is this reminder still worth sending TODAY?

Rules:
- EVENTS (meetings, appointments, calls, demos, interviews, scheduled sessions): These are time-bound. If the event time has clearly passed based on the queued date or context clues, mark as STALE. Events more than 90 minutes old are almost certainly past.
- DELIVERABLES (tasks, emails to write, documents, follow-ups, blog posts, messages to send): These are NOT time-bound. Even if overdue or days old, they are still worth sending. Mark as NOT stale.
- When uncertain whether something is an event or deliverable: mark as NOT stale. Better to send an unnecessary reminder than drop a valid one.

Respond with JSON only, no other text:
{{"stale": true or false, "reason": "one sentence explanation"}}"""

            client = AsyncAnthropic(api_key=Config.ANTHROPIC_API_KEY)
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}]
            )

            response_text = response.content[0].text.strip()

            # Strip markdown code fences if present
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                response_text = "\n".join(
                    line for line in lines if not line.startswith("```")
                ).strip()

            data = json.loads(response_text)
            if data.get("stale") is True:
                return (True, data.get("reason", ""))
            return (False, "")

        except Exception as e:
            logger.warning("is_nudge_stale error for nudge %s: %s", item.get('id'), repr(e))
            return (False, "")

    async def send_drip_if_due(self) -> dict:
        """
        Check if drip interval has elapsed and send one conversational nudge if due.

        Mirrors send_batch_if_due but sends one item at a time as a natural
        conversational sentence instead of a structured digest.

        Phase 37-02: Conversational Nudge Flow.

        Returns:
            Dict with {sent: bool, nudge_id: int or None, channel: str or None}
        """
        result = {"sent": False, "nudge_id": None, "channel": None}

        prefs = self._get_preferences()
        if not prefs.get('nudge_enabled', True):
            return result
        if self.is_quiet_hours():
            return result

        if self._recent_screen_nudge(minutes=20):
            logger.debug("Skipping drip — screen agent fired within 20 min for user %d", self.user_id)
            return result

        # Suppress drip nudges when user is in a declared focus/context state
        try:
            from web.core.database import get_user_status
            _active_status = get_user_status(self.user_id)
            if _active_status:
                logger.info(
                    "Suppressing drip nudge for user %d — active status: %s",
                    self.user_id, _active_status.get('status_text', '')
                )
                return result
        except Exception as _status_err:
            logger.warning("user_status check failed (proceeding with drip): %s", repr(_status_err))

        # Check if drip interval has elapsed
        interval_minutes = prefs.get('nudge_drip_interval_minutes', 15)
        last_drip = prefs.get('nudge_last_drip_at')
        if last_drip:
            try:
                last_drip_dt = datetime.fromisoformat(last_drip.replace('Z', '+00:00'))
                elapsed = datetime.now(last_drip_dt.tzinfo) - last_drip_dt
                if elapsed.total_seconds() < interval_minutes * 60:
                    return result
            except (ValueError, TypeError):
                pass

        # Try up to 10 drip candidates; skip stale ones, send first valid one
        MAX_STALE_CHECKS = 10
        item = None
        for _ in range(MAX_STALE_CHECKS):
            candidate = get_next_drip_nudge(self.user_id)
            if not candidate:
                break  # No more pending nudges
            stale, reason = await self.is_nudge_stale(candidate)
            if stale:
                # Dismiss stale nudge and try next candidate
                with get_db() as conn:
                    conn.cursor().execute(
                        "UPDATE nudges SET status='dismissed', dismiss_reason=%s WHERE id=%s",
                        (reason, candidate['id'])
                    )
                logger.info(
                    "Dismissed stale drip nudge %d for user %d: %s",
                    candidate['id'], self.user_id, reason
                )
                # Close the detected_action permanently so it can never re-queue
                if candidate.get('source_type') == 'detected_action' and candidate.get('source_id'):
                    from web.core.database import update_detected_action_status
                    update_detected_action_status(
                        int(candidate['source_id']), 'dismissed'
                    )
                    logger.info(
                        "Stale-dismissed detected_action %d for user %d",
                        candidate['source_id'], self.user_id
                    )
                continue
            item = candidate
            break

        if not item:
            return result

        # -- Loop closure check (Phase 70.1-02, expanded Phase 75-01) ----------
        # Type-routed closure detection:
        #   detected_action -> Haiku thread analysis (get_closure_context + check_nudge_closure)
        #   overdue_task -> DB check on tasks.status
        #   nudge_followup -> DB check on nudges.user_response/acted_at
        #   relationship_check / open_followup -> person name + outbound message search
        # Failures always default to sending -- never block on closure check error.
        try:
            from web.services.closure_check import (
                get_closure_context,
                check_nudge_closure,
                check_overdue_task_closure,
                check_nudge_followup_closure,
                check_person_contact_closure,
                CLOSURE_CHECK_INCLUDE,
                CLOSURE_CHECK_EXCLUDE_TIME_SENSITIVE,
            )

            nudge_type = item.get('nudge_type', '')
            should_check = (
                nudge_type in CLOSURE_CHECK_INCLUDE
                and nudge_type not in CLOSURE_CHECK_EXCLUDE_TIME_SENSITIVE
            )

            if should_check:
                delay_count = item.get('closure_delay_count', 0) or 0

                if delay_count >= 1:
                    # Already been delayed once -- send regardless
                    logger.info(
                        "[closure] nudge %d type=%s: max delay reached -- sending",
                        item['id'], nudge_type
                    )
                else:
                    loop_closed = False

                    if nudge_type == 'detected_action':
                        # Original Haiku thread analysis path (unchanged)
                        context = get_closure_context(item, self.user_id)
                        if context is None:
                            logger.debug(
                                "[closure] nudge %d: no context resolved -- sending",
                                item['id']
                            )
                        else:
                            loop_closed = await check_nudge_closure(item, context)

                    elif nudge_type == 'overdue_task':
                        loop_closed = check_overdue_task_closure(item, self.user_id)

                    elif nudge_type == 'nudge_followup':
                        loop_closed = check_nudge_followup_closure(item, self.user_id)

                    elif nudge_type in ('relationship_check', 'open_followup'):
                        loop_closed = check_person_contact_closure(item, self.user_id)

                    if loop_closed:
                        # Delay 48h and skip this send
                        with get_db() as conn:
                            conn.cursor().execute("""
                                UPDATE nudges
                                SET closure_hold_until = NOW() + INTERVAL '48 hours',
                                    closure_delay_count = closure_delay_count + 1
                                WHERE id = %s
                            """, (item['id'],))
                        logger.info(
                            "[closure] nudge %d type=%s: loop appears closed -- delayed 48h",
                            item['id'], nudge_type
                        )
                        return result  # sent=False; nudge stays pending, hold set

        except Exception as _closure_err:
            logger.warning(
                "[closure] nudge %d: closure check failed unexpectedly: %r -- sending",
                item['id'], _closure_err
            )
        # -- End closure check ------------------------------------------------

        # Format as conversational message
        body = self.format_drip_message(item)

        # Send via existing send_nudge infrastructure
        send_result = await self.send_nudge(
            nudge_type=item.get('nudge_type', 'drip'),
            title=body,  # drip uses body as the whole message
            body=None,
            urgency='normal',
            source_type=item.get('source_type'),
            source_id=item.get('source_id'),
        )

        if send_result.get('success'):
            update_nudge_preferences(
                self.user_id,
                nudge_last_drip_at=datetime.now().isoformat()
            )
            # Mark the source nudge as batched so it doesn't get dripped again
            if item.get('id'):
                with get_db() as conn:
                    conn.cursor().execute(
                        "UPDATE nudges SET status = 'batched' WHERE id = %s",
                        (item['id'],)
                    )
            result['sent'] = True
            result['nudge_id'] = send_result.get('nudge_id')
            result['channel'] = send_result.get('channel')

            logger.info(
                "Drip nudge sent for user %d via %s: nudge_id=%s",
                self.user_id, result['channel'], result['nudge_id']
            )
        else:
            logger.warning(
                "Failed to send drip nudge for user %d: %s",
                self.user_id, send_result.get('error')
            )

        return result
