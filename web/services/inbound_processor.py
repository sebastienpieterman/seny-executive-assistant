"""
Inbound Processor - Batch pipeline orchestrating pre-filter, classifier, and cross-reference resolver.

Connects all Phase 14 components into an automated pipeline that processes
new scanned items continuously. Designed to run as a background job via APScheduler.

Pipeline: pre-filter → classify → cross-reference
- Pre-filter eliminates noise cheaply (rules-based, no AI)
- Classifier uses Haiku for remaining items (sequential with 0.1s delay)
- Cross-reference resolver links classified items to People, Projects, Ideas, Tasks

Phase 14-04 - Inbound Classification & Cross-Referencing
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from web.core.database import (
    get_db,
    increment_classification_attempts,
    mark_scanned_item_processed,
    get_pending_action_by_source_ref,
    create_pending_action,
    get_nudge_preferences,
    get_daily_classification_count,
    get_scanner_preferences,
)
from web.services.prefilter_service import PreFilterService
from web.services.inbound_classifier import InboundClassifier
from web.services.cross_reference_resolver import CrossReferenceResolver
from web.services.scheduling_extractor import SchedulingExtractor

logger = logging.getLogger(__name__)

# Maximum classification attempts before skipping an item permanently
MAX_CLASSIFICATION_ATTEMPTS = 3


class InboundProcessor:
    """Orchestrates the full inbound processing pipeline."""

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.prefilter = PreFilterService(user_id)
        self.classifier = InboundClassifier(user_id)
        self.resolver = CrossReferenceResolver(user_id)

    async def process_batch(self, batch_size: int = 50) -> dict:
        """
        Process a batch of unprocessed scanned_items.

        Pipeline:
        1. Query unprocessed scanned_items (processed=0), limit batch_size
        2. Run pre-filter: split into passed/filtered/internal
        3. For passed items: classify with Haiku (sequential, 0.1s delay between calls)
        4. For classified items: resolve cross-references
        5. Return stats dict

        Returns:
            Stats dict with counts for each stage of the pipeline.
        """
        start = time.time()

        stats = {
            "total": 0,
            "filtered": 0,
            "internal": 0,
            "classified": 0,
            "failed": 0,
            "cost_limited": 0,
            "actions_detected": 0,
            "cross_references_created": 0,
            "calendar_proposals_created": 0,
            "duration_seconds": 0.0,
        }

        # Step 1: Fetch unprocessed items
        items = self._fetch_unprocessed(batch_size)
        if not items:
            stats["duration_seconds"] = time.time() - start
            return stats

        stats["total"] = len(items)

        # Filter out items that have exceeded max classification attempts
        eligible_items = []
        for item in items:
            attempts = item.get("classification_attempts") or 0
            if attempts >= MAX_CLASSIFICATION_ATTEMPTS:
                # Skip permanently — mark as processed with 'max_retries' label
                mark_scanned_item_processed(item["id"], "max_retries")
                stats["failed"] += 1
                logger.warning(
                    "Skipping item %d after %d failed attempts",
                    item["id"], attempts
                )
            else:
                eligible_items.append(item)

        if not eligible_items:
            stats["duration_seconds"] = time.time() - start
            return stats

        # Step 2: Pre-filter
        await self.prefilter.load_user_context()
        filter_result = await self.prefilter.filter_batch(eligible_items)
        stats["filtered"] = filter_result["filtered"]
        stats["internal"] = filter_result["internal"]

        passed_items = filter_result["passed"]
        if not passed_items:
            stats["duration_seconds"] = time.time() - start
            return stats

        # Step 2.5: Check daily classification limit
        prefs = get_scanner_preferences(self.user_id)
        daily_limit = prefs.get("daily_classification_limit", 200)
        remaining_quota = None  # None = unlimited

        if daily_limit > 0:
            used_today = get_daily_classification_count(self.user_id)
            remaining_quota = max(0, daily_limit - used_today)
            if remaining_quota == 0:
                # Cap reached — mark all passed items as cost_limited
                for item in passed_items:
                    mark_scanned_item_processed(item["id"], "cost_limited")
                stats["cost_limited"] = len(passed_items)
                stats["duration_seconds"] = time.time() - start
                logger.warning(
                    "Daily classification limit reached (%d/%d). Skipping %d items.",
                    used_today, daily_limit, len(passed_items)
                )
                return stats

        # Step 3: Classify with Haiku (sequential with delay)
        classified_items = []
        for item in passed_items:
            # Check per-item quota before classification
            if remaining_quota is not None:
                if remaining_quota <= 0:
                    mark_scanned_item_processed(item["id"], "cost_limited")
                    stats["cost_limited"] = stats.get("cost_limited", 0) + 1
                    continue
                remaining_quota -= 1

            try:
                classification = await self.classifier.classify_item(item)
                if classification:
                    classified_items.append((item, classification))
                    stats["classified"] += 1
                else:
                    # Classification returned None (parse error or other issue)
                    # Item was already marked processed by classifier
                    stats["failed"] += 1
            except Exception as e:
                logger.error(
                    "Classification error for item %d: %s", item["id"], repr(e)
                )
                # Increment attempt counter for retry
                increment_classification_attempts(item["id"])
                stats["failed"] += 1

            # Rate limiting: 0.1s delay between Haiku calls
            if passed_items.index(item) < len(passed_items) - 1:
                await asyncio.sleep(0.1)

        # Step 4: Cross-reference resolution for classified items
        for item, classification in classified_items:
            try:
                xref_result = await self.resolver.resolve_item(item, classification)
                stats["actions_detected"] += xref_result.get("actions", 0)
                stats["cross_references_created"] += sum(
                    xref_result.get(k, 0) for k in ("people", "projects", "ideas", "tasks")
                )
            except Exception as e:
                logger.error(
                    "Cross-reference error for item %d: %s", item["id"], repr(e)
                )

        # Step 5: Scheduling proposal detection (Gmail only)
        extractor = SchedulingExtractor(self.user_id)
        try:
            _prefs = get_nudge_preferences(self.user_id)
            _tz = ZoneInfo(_prefs.get('digest_timezone', 'America/Chicago'))
        except Exception:
            _tz = ZoneInfo('America/Chicago')
        today_str = datetime.now(_tz).date().isoformat()

        # item_type='email' captures all email sources (gmail, outlook, qa_inject, etc.)
        # This is cleaner than an explicit source allowlist which would block qa_inject items.

        gmail_candidates = 0
        for item, classification in classified_items:
            item_id = item.get('id')
            # Only email-type items — skip Slack, Telegram, Calendar, etc.
            if item.get('item_type') != 'email':
                continue
            # Skip only explicitly ignored senders — do not filter by direction or relevance.
            # SchedulingExtractor makes its own independent Haiku judgment.
            early_class = classification.get('classification')
            if early_class == 'filtered':
                print(f"[Scheduling] item {item_id} skipped — ignored sender", flush=True)
                continue

            # Build source_ref for dedup
            metadata = item.get('source_metadata') or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (json.JSONDecodeError, TypeError):
                    metadata = {}

            thread_id = metadata.get('thread_id', '')
            source_ref = (
                f"gmail_thread:{thread_id}" if thread_id
                else f"gmail_msg:{item.get('source_id', '')}"
            )

            # Dedup: skip if a proposal already exists for this thread
            existing = get_pending_action_by_source_ref(self.user_id, source_ref)
            if existing:
                print(f"[Scheduling] item {item_id} skipped — proposal already exists ({source_ref})", flush=True)
                continue

            gmail_candidates += 1
            subject = metadata.get('subject', '(no subject)')
            print(f"[Scheduling] item {item_id} candidate — subject={subject[:60]!r} ref={source_ref}", flush=True)

            # Extract scheduling details
            result = await extractor.extract(item, today_str)
            if result is None:
                continue

            # Build calendar_proposal content_json
            event_title = result['event_title']
            content_json = json.dumps({
                'title': event_title,
                'start_datetime': result['start_datetime'],
                'end_datetime': result.get('end_datetime'),
                'location': result.get('location'),
                'description': result.get('notes'),
                'calendar_id': None,
            })

            card_title = f"Schedule: {event_title}"[:80]

            action_id = create_pending_action(
                self.user_id,
                'calendar_proposal',
                card_title,
                content_json,
                source='scanner',
                source_ref=source_ref,
            )

            if action_id:
                stats['calendar_proposals_created'] += 1
                logger.info(
                    "Created calendar_proposal %d for %s (thread=%s)",
                    action_id, event_title[:40], source_ref
                )

        print(
            f"[InboundProcessor] batch done — total={stats['total']} filtered={stats['filtered']} "
            f"classified={stats['classified']} failed={stats['failed']} "
            f"gmail_candidates={gmail_candidates} proposals_created={stats['calendar_proposals_created']} "
            f"duration={time.time() - start:.1f}s",
            flush=True
        )
        stats["duration_seconds"] = round(time.time() - start, 2)
        return stats

    async def process_all_pending(self, max_batches: int = 10) -> dict:
        """
        Process all pending items in batches until done or max_batches reached.

        Prevents runaway processing if there's a huge backlog.

        Returns:
            Aggregated stats across all batches.
        """
        aggregated = {
            "total": 0,
            "filtered": 0,
            "internal": 0,
            "classified": 0,
            "failed": 0,
            "actions_detected": 0,
            "cross_references_created": 0,
            "batches_run": 0,
            "duration_seconds": 0.0,
        }

        for i in range(max_batches):
            result = await self.process_batch(batch_size=50)

            if result["total"] == 0:
                break

            for key in ("total", "filtered", "internal", "classified", "failed",
                        "actions_detected", "cross_references_created"):
                aggregated[key] += result.get(key, 0)
            aggregated["duration_seconds"] += result["duration_seconds"]
            aggregated["batches_run"] += 1

        aggregated["duration_seconds"] = round(aggregated["duration_seconds"], 2)
        return aggregated

    async def get_processing_stats(self) -> dict:
        """
        Get current processing state.

        Returns:
            Dict with unprocessed_count, classified_count, actionable_count,
            actions_pending, cross_references_total.
        """
        stats = {
            "unprocessed_count": 0,
            "classified_count": 0,
            "actionable_count": 0,
            "actions_pending": 0,
            "cross_references_total": 0,
        }

        try:
            with get_db() as conn:
                cursor = conn.cursor()

                # Unprocessed items waiting
                cursor.execute(
                    "SELECT COUNT(*) as cnt FROM scanned_items WHERE processed = 0 AND user_id = %s",
                    (self.user_id,)
                )
                row = cursor.fetchone()
                stats["unprocessed_count"] = row["cnt"] if row else 0

                # Total classified items
                cursor.execute(
                    "SELECT COUNT(*) as cnt FROM item_classifications WHERE user_id = %s",
                    (self.user_id,)
                )
                row = cursor.fetchone()
                stats["classified_count"] = row["cnt"] if row else 0

                # Actionable items
                cursor.execute(
                    "SELECT COUNT(*) as cnt FROM item_classifications WHERE user_id = %s AND relevance = 'actionable'",
                    (self.user_id,)
                )
                row = cursor.fetchone()
                stats["actionable_count"] = row["cnt"] if row else 0

                # Pending detected actions
                cursor.execute(
                    "SELECT COUNT(*) as cnt FROM detected_actions WHERE user_id = %s AND status = 'pending'",
                    (self.user_id,)
                )
                row = cursor.fetchone()
                stats["actions_pending"] = row["cnt"] if row else 0

                # Total cross-references
                cursor.execute(
                    "SELECT COUNT(*) as cnt FROM cross_references WHERE user_id = %s",
                    (self.user_id,)
                )
                row = cursor.fetchone()
                stats["cross_references_total"] = row["cnt"] if row else 0

        except Exception as e:
            logger.error("Failed to get processing stats: %s", repr(e))

        return stats

    def _fetch_unprocessed(self, batch_size: int) -> list[dict]:
        """Fetch unprocessed scanned items for this user."""
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, user_id, source, source_id, source_metadata,
                           item_type, direction, detected_at, processed, classification,
                           classification_attempts
                    FROM scanned_items
                    WHERE processed = 0 AND user_id = %s
                    ORDER BY detected_at ASC
                    LIMIT %s
                """, (self.user_id, batch_size))
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error("Failed to fetch unprocessed items: %s", repr(e))
            return []
