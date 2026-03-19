"""
Scanner Service - Orchestrates background scanning of all data sources.

Manages scan lifecycle (start, execute, complete/fail) for each source,
with deduplication and incremental scanning based on last scan time.

Phase 13 - Scanner Engine & Entity Resolution
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from web.core.database import (
    create_scanner_run,
    complete_scanner_run,
    get_last_scanner_run,
    get_db,
    get_people_by_user,
    insert_scanned_item,
    get_channel_exclusion_preferences,
    get_scanner_interval_for_source,
)

logger = logging.getLogger(__name__)


# Maps source name to scan method and default interval
SOURCE_CONFIGS = {
    'gmail': {'interval_minutes': 15, 'method': '_scan_gmail'},
    'slack': {'interval_minutes': 15, 'method': '_scan_slack'},
    'telegram': {'interval_minutes': 5, 'method': '_scan_telegram'},
    'calendar': {'interval_minutes': 60, 'method': '_scan_calendar'},
    'drive': {'interval_minutes': 240, 'method': '_scan_drive'},
    'contacts': {'interval_minutes': 60, 'method': '_scan_contacts'},
    'notes': {'interval_minutes': 1440, 'method': '_scan_notes'},
    'tasks': {'interval_minutes': 1440, 'method': '_scan_tasks'},
    'location': {'interval_minutes': 1440, 'method': '_scan_location'},
    'conversations': {'interval_minutes': 60, 'method': '_scan_conversations'},
}


class ScannerService:
    """Orchestrates scanning across all data sources."""

    def __init__(self, user_id: int):
        self.user_id = user_id

    async def run_scan(self, source: str) -> dict:
        """
        Run a scan for a single source.

        Args:
            source: Data source name (must be a key in SOURCE_CONFIGS)

        Returns:
            Dict with scan results: {source, items_found, items_new, duration_seconds, status}
        """
        if source not in SOURCE_CONFIGS:
            logger.error("Unknown scan source: %s", source)
            return {'source': source, 'status': 'error', 'error': f'Unknown source: {source}'}

        # Auto-cleanup stuck scans (scans that have been "running" for >2 hours are stuck)
        self._cleanup_stuck_scans(source, max_runtime_hours=2)

        # Check if a scan is already running for this user+source
        if self._is_scan_running(source):
            logger.info("Scan already running for user %d source %s, skipping", self.user_id, source)
            return {'source': source, 'status': 'skipped', 'reason': 'already_running'}

        # Get last completed scan time for incremental scanning
        last_run = get_last_scanner_run(self.user_id, source)
        last_scan_time = last_run['completed_at'] if last_run else None

        # Check user-specific scan interval
        # For configurable sources, respect user's interval preference
        user_interval = get_scanner_interval_for_source(self.user_id, source)
        if user_interval is not None and last_scan_time:
            try:
                last_dt = last_scan_time if isinstance(last_scan_time, datetime) else datetime.fromisoformat(str(last_scan_time))
                now = datetime.now(timezone.utc)
                # Ensure last_dt is timezone-aware
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                minutes_since_last = (now - last_dt).total_seconds() / 60
                is_override = user_interval != SOURCE_CONFIGS[source]['interval_minutes']

                if minutes_since_last < user_interval:
                    logger.debug(
                        "Skipping scan for user %d source %s: %.1f min since last (interval=%d min, override=%s)",
                        self.user_id, source, minutes_since_last, user_interval, is_override
                    )
                    return {
                        'source': source,
                        'status': 'skipped',
                        'reason': 'interval_not_elapsed',
                        'minutes_since_last': round(minutes_since_last, 1),
                        'user_interval': user_interval,
                    }

                logger.info(
                    "Using interval %d min for %s (user override: %s)",
                    user_interval, source, is_override
                )
            except (ValueError, TypeError) as e:
                logger.warning("Error parsing last scan time for user %d source %s: %s", self.user_id, source, e)

        # Create scanner run record
        run_id = create_scanner_run(self.user_id, source)
        if run_id is None:
            logger.error("Failed to create scanner run for user %d source %s", self.user_id, source)
            return {'source': source, 'status': 'error', 'error': 'Failed to create run record'}

        start_time = time.time()
        try:
            # Call source-specific scanner method
            method_name = SOURCE_CONFIGS[source]['method']
            scan_method = getattr(self, method_name)
            items_found, items_new = await scan_method(
                run_id=run_id, last_scan_time=last_scan_time
            )

            duration = time.time() - start_time
            complete_scanner_run(run_id, 'completed', items_found, items_new)

            logger.info(
                "Scan completed: user=%d source=%s found=%d new=%d duration=%.1fs",
                self.user_id, source, items_found, items_new, duration
            )

            return {
                'source': source,
                'status': 'completed',
                'items_found': items_found,
                'items_new': items_new,
                'duration_seconds': round(duration, 1),
            }

        except Exception as e:
            duration = time.time() - start_time
            error_msg = repr(e)
            complete_scanner_run(run_id, 'failed', 0, 0, error_message=error_msg)

            logger.error(
                "Scan failed: user=%d source=%s error=%s duration=%.1fs",
                self.user_id, source, error_msg, duration
            )

            raise

    async def run_all_scans(self, resolve_entities: bool = True) -> dict:
        """
        Run scans for all sources sequentially, then optionally resolve entities.

        Catches per-source errors so one failure doesn't stop others.

        Args:
            resolve_entities: If True, run entity resolution after all scans complete.

        Returns:
            Dict with scan_results list and optional entity_resolution summary.
        """
        scan_results = []
        for source in SOURCE_CONFIGS:
            try:
                result = await self.run_scan(source)
                scan_results.append(result)
            except Exception as e:
                logger.error("Scan failed for source %s: %r", source, e)
                scan_results.append({
                    'source': source,
                    'status': 'failed',
                    'error': repr(e),
                })

        result = {'scan_results': scan_results}

        if resolve_entities:
            try:
                resolution = await self.run_entity_resolution()
                result['entity_resolution'] = resolution
            except Exception as e:
                logger.error("Entity resolution failed for user %d: %r", self.user_id, e)
                result['entity_resolution'] = {'status': 'failed', 'error': repr(e)}

        return result

    async def run_entity_resolution(self) -> dict:
        """Run entity resolution after scanning completes."""
        from web.services.entity_resolver import EntityResolver

        resolver = EntityResolver(self.user_id)
        return await resolver.resolve_all()

    async def backfill_entity_mappings(self) -> dict:
        """
        One-time backfill: seed entity_mappings from existing People DB entries
        and then run full resolution on all scanned_items.

        For each person with a google_contact_id, pre-populate entity_mappings
        with their known email addresses so future scans match immediately.

        Then run the full entity resolution pipeline on all existing scanned_items.

        Returns:
            Dict with seeded_count and resolution summary.
        """
        from web.services.entity_resolver import EntityResolver

        seeded = 0

        # Seed from People with google_contact_id
        try:
            people = get_people_by_user(self.user_id, limit=500)
            for person in people:
                google_contact_id = person.get("google_contact_id")
                if not google_contact_id:
                    continue

                # Look up contact emails
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT emails, display_name FROM google_contacts
                        WHERE resource_name = %s
                    """, (google_contact_id,))
                    row = cursor.fetchone()
                    if not row or not row["emails"]:
                        continue

                    try:
                        emails_data = json.loads(row["emails"])
                    except (json.JSONDecodeError, TypeError):
                        continue

                    display_name = row.get("display_name") or person.get("name", "")

                    for email_entry in emails_data:
                        email = None
                        if isinstance(email_entry, str):
                            email = email_entry
                        elif isinstance(email_entry, dict):
                            email = email_entry.get("value")

                        if email:
                            from web.core.database import upsert_entity_mapping
                            mapping_id = upsert_entity_mapping(
                                user_id=self.user_id,
                                source="contacts",
                                source_identifier=email.lower(),
                                display_name=display_name,
                                person_id=person["id"],
                                confidence=1.0,
                            )
                            if mapping_id:
                                seeded += 1

        except Exception as e:
            logger.error("Error seeding entity mappings for user %d: %r", self.user_id, e)

        # Run full resolution on all scanned_items
        resolver = EntityResolver(self.user_id)
        resolution = await resolver.resolve_all()

        logger.info(
            "Backfill complete: user=%d seeded=%d resolution=%s",
            self.user_id, seeded, resolution,
        )

        return {
            "seeded_from_people": seeded,
            "resolution": resolution,
        }

    async def get_scan_status(self) -> dict:
        """
        Get last scan times and status for all sources.

        Returns:
            Dict mapping source name to {last_scan, status, items_found}
        """
        status = {}
        for source in SOURCE_CONFIGS:
            last_run = get_last_scanner_run(self.user_id, source)
            if last_run:
                status[source] = {
                    'last_scan': last_run['completed_at'],
                    'status': last_run['status'],
                    'items_found': last_run['items_found'],
                }
            else:
                status[source] = {
                    'last_scan': None,
                    'status': 'never_run',
                    'items_found': 0,
                }
        return status

    def _is_scan_running(self, source: str) -> bool:
        """Check if a scan is currently running for this user+source."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) as count FROM scanner_runs
                WHERE user_id = %s AND source = %s AND status = 'running'
            """, (self.user_id, source))
            row = cursor.fetchone()
            return row['count'] > 0

    def _cleanup_stuck_scans(self, source: str, max_runtime_hours: int = 2):
        """
        Auto-cleanup scans stuck in 'running' status.

        If a scan has been running for longer than max_runtime_hours, it's
        stuck (app crashed during scan) and should be marked as failed.

        Args:
            source: Scanner source to check
            max_runtime_hours: Max hours a scan can run before considered stuck
        """
        from datetime import datetime, timedelta, timezone

        with get_db() as conn:
            cursor = conn.cursor()

            # Find scans that have been running too long
            max_age = datetime.now(timezone.utc) - timedelta(hours=max_runtime_hours)
            cursor.execute("""
                SELECT id, source, started_at
                FROM scanner_runs
                WHERE user_id = %s AND source = %s AND status = 'running'
                AND started_at < %s
            """, (self.user_id, source, max_age.isoformat()))

            stuck = cursor.fetchall()
            if stuck:
                stuck_ids = [row['id'] for row in stuck]
                placeholders = ','.join(['%s'] * len(stuck_ids))
                now_iso = datetime.now(timezone.utc).isoformat()
                cursor.execute(f"""
                    UPDATE scanner_runs
                    SET status = 'failed',
                        error_message = 'Auto-cleaned: scan exceeded {max_runtime_hours}h runtime',
                        completed_at = %s
                    WHERE id IN ({placeholders})
                """, [now_iso] + stuck_ids)
                conn.commit()
                logger.warning(
                    "Auto-cleaned %d stuck scan(s) for user %d source %s: %s",
                    len(stuck), self.user_id, source, stuck_ids
                )

    # ========================================================================
    # Gmail Scanner (Plan 02, Task 1)
    # ========================================================================

    async def _scan_gmail(
        self, run_id: int, last_scan_time: Optional[str] = None
    ) -> tuple[int, int]:
        """
        Scan Gmail for new emails since last scan.

        Fetches metadata only (no bodies). Handles multiple connected accounts.
        One failing account does not stop others.

        Returns (items_found, items_new).
        """
        from web.core.database import list_gmail_tokens
        from web.services.gmail_service import GmailService

        accounts = list_gmail_tokens(self.user_id)
        if not accounts:
            logger.info("Gmail scanner: no connected accounts for user %d", self.user_id)
            return (0, 0)

        total_found = 0
        total_new = 0

        for account in accounts:
            email = account.get('email')
            if not email:
                continue

            try:
                gmail = GmailService(self.user_id, email)

                # Build query based on last scan time
                epoch = None
                if last_scan_time:
                    try:
                        dt = last_scan_time if isinstance(last_scan_time, datetime) else datetime.fromisoformat(str(last_scan_time))
                        epoch = int(dt.timestamp())
                        query = f"after:{epoch}"
                    except (ValueError, TypeError):
                        query = "newer_than:1d"
                else:
                    query = "newer_than:1d"

                emails = await gmail.search_emails(query=query, max_results=50)

                for msg in emails:
                    total_found += 1

                    source_id = msg.get('id', '')
                    from_field = msg.get('from', '')
                    has_attachments = False  # metadata format doesn't include this directly

                    # Note: We scan ALL emails including user's own for context.
                    # Filtering happens at digest level to avoid "respond to yourself" items.

                    metadata = {
                        "from": from_field,
                        "to": msg.get('to', ''),
                        "subject": msg.get('subject', ''),
                        "date": msg.get('date', ''),
                        "labels": msg.get('labelIds', []),
                        "has_attachments": has_attachments,
                        "account": email,
                        "thread_id": msg.get('threadId', ''),
                        "snippet": (msg.get('snippet', '') or '')[:300],
                    }

                    item_id = insert_scanned_item(
                        self.user_id, run_id, 'gmail', source_id,
                        json.dumps(metadata), 'email'
                    )
                    if item_id is not None:
                        total_new += 1

                # Scan sent emails (HF-03: outbound communications blind spot)
                sent_query = f"in:sent after:{epoch}" if epoch else "in:sent newer_than:1d"
                try:
                    sent_emails = await gmail.search_emails(query=sent_query, max_results=50)

                    for msg in sent_emails:
                        total_found += 1
                        source_id = msg.get('id', '')
                        metadata = {
                            "subject": msg.get('subject', ''),
                            "to": msg.get('to', ''),
                            "from": msg.get('from', ''),
                            "snippet": (msg.get('snippet', '') or '')[:300],
                            "date": msg.get('date', ''),
                            "direction": "outbound",
                            "account": email,
                            "thread_id": msg.get('threadId', ''),
                        }
                        item_id = insert_scanned_item(
                            self.user_id, run_id, 'gmail', source_id,
                            json.dumps(metadata), 'sent_email',
                            direction='outbound'
                        )
                        # Note: INSERT OR IGNORE handles the case where an email appears in both
                        # inbound and sent (e.g., emails to yourself). The first insert wins.
                        if item_id is not None:
                            total_new += 1
                except Exception as e:
                    logger.warning(
                        "Gmail scanner: error scanning sent emails for account %s user %d: %r",
                        email, self.user_id, e
                    )

            except Exception as e:
                logger.warning(
                    "Gmail scanner: error scanning account %s for user %d: %s",
                    email, self.user_id, e
                )
                continue

        return (total_found, total_new)

    # ========================================================================
    # Calendar Scanner (Plan 02, Task 2)
    # ========================================================================

    async def _scan_calendar(
        self, run_id: int, last_scan_time: Optional[str] = None
    ) -> tuple[int, int]:
        """
        Scan Google Calendar for upcoming events (next 7 days).

        Re-scans each time because events can be modified. The UNIQUE constraint
        on (user_id, source, source_id) handles dedup — modified events just
        won't insert (fine for Phase 13; Phase 14 handles change detection).

        Returns (items_found, items_new).
        """
        from web.core.database import list_google_tokens
        from web.services.calendar_service import CalendarService

        accounts = list_google_tokens(self.user_id)
        if not accounts:
            logger.info("Calendar scanner: no connected accounts for user %d", self.user_id)
            return (0, 0)

        total_found = 0
        total_new = 0

        for account in accounts:
            email = account.get('email')
            if not email:
                continue

            try:
                cal = CalendarService(self.user_id, email)
                if not cal.is_connected():
                    continue

                events = await cal.get_all_events(days_ahead=7)

                for event in events:
                    total_found += 1

                    source_id = event.get('id', '')
                    description = event.get('description', '') or ''
                    description_snippet = description[:200] if description else ''

                    # Collect attendee emails
                    attendees = event.get('attendees', [])
                    attendee_emails = []
                    if isinstance(attendees, list):
                        for a in attendees:
                            if isinstance(a, dict):
                                attendee_emails.append(a.get('email', ''))

                    metadata = {
                        "summary": event.get('summary', ''),
                        "start": event.get('start', ''),
                        "end": event.get('end', ''),
                        "location": event.get('location', ''),
                        "attendees": attendee_emails,
                        "has_video": event.get('has_video', False),
                        "description_snippet": description_snippet,
                        "account": email,
                    }

                    item_id = insert_scanned_item(
                        self.user_id, run_id, 'calendar', source_id,
                        json.dumps(metadata), 'calendar_event'
                    )
                    if item_id is not None:
                        total_new += 1

            except Exception as e:
                logger.warning(
                    "Calendar scanner: error scanning account %s for user %d: %s",
                    email, self.user_id, e
                )
                continue

        return (total_found, total_new)

    # ========================================================================
    # Contacts Scanner (Plan 02, Task 2)
    # ========================================================================

    async def _scan_contacts(
        self, run_id: int, last_scan_time: Optional[str] = None
    ) -> tuple[int, int]:
        """
        Scan Google Contacts using incremental sync.

        Runs contacts sync (which uses sync tokens internally), then logs
        all contacts to scanned_items for entity resolution.

        Returns (items_found, items_new).
        """
        from web.core.database import list_gmail_tokens
        from web.services.contacts_service import ContactsService

        # Contacts use Gmail tokens (same OAuth flow)
        accounts = list_gmail_tokens(self.user_id)
        if not accounts:
            logger.info("Contacts scanner: no connected accounts for user %d", self.user_id)
            return (0, 0)

        total_found = 0
        total_new = 0

        for account in accounts:
            email = account.get('email')
            if not email:
                continue

            try:
                contacts_svc = ContactsService(self.user_id, email)
                if not contacts_svc.is_connected():
                    continue

                # Run incremental sync (updates local google_contacts table)
                sync_result = await contacts_svc.sync_contacts()
                if "error" in sync_result:
                    logger.warning(
                        "Contacts scanner: sync error for %s: %s",
                        email, sync_result["error"]
                    )
                    continue

                # Now list all contacts and log to scanned_items
                contacts = await contacts_svc.list_contacts(limit=500)

                for contact in contacts:
                    total_found += 1

                    source_id = contact.get('resource_name', '')

                    metadata = {
                        "name": contact.get('display_name', ''),
                        "emails": [contact['email']] if contact.get('email') else [],
                        "phones": [contact['phone']] if contact.get('phone') else [],
                        "organization": contact.get('company', ''),
                        "account": email,
                    }

                    item_id = insert_scanned_item(
                        self.user_id, run_id, 'contacts', source_id,
                        json.dumps(metadata), 'contact'
                    )
                    if item_id is not None:
                        total_new += 1

            except Exception as e:
                logger.warning(
                    "Contacts scanner: error scanning account %s for user %d: %s",
                    email, self.user_id, e
                )
                continue

        return (total_found, total_new)

    # ========================================================================
    # Remaining scanner stubs (implemented in Plan 03)
    # ========================================================================

    async def _scan_slack(
        self, run_id: int, last_scan_time: Optional[str] = None
    ) -> tuple[int, int]:
        """
        Scan Slack for new messages across channels and DMs.

        Skips bot messages. Limits to 20 most recent messages per channel
        to avoid API rate limits.

        Returns (items_found, items_new).
        """
        # HF-09: Slack scanning is handled by the continuous drip loop
        # (slack_drip_service.py). Batch scanning was causing persistent 429 rate
        # limiting by bursting 50+ API calls every 4 hours via the full sweep job.
        # The drip loop scans one channel every 10 seconds — no batch scanning needed.
        logger.info(
            "Slack batch scan skipped for user %d — handled by drip loop (slack_drip_service.py)",
            self.user_id
        )
        return (0, 0)

        from web.services.slack_service import SlackService, SlackScanAbortError

        # Check if user has any Slack workspace connected
        workspaces = SlackService.list_connected_workspaces(self.user_id)
        if not workspaces:
            logger.info("Slack scanner: no connected workspaces for user %d", self.user_id)
            return (0, 0)

        total_found = 0
        total_new = 0

        # Convert last_scan_time to Slack timestamp format (Unix epoch string)
        oldest_ts = None
        if last_scan_time:
            try:
                dt = last_scan_time if isinstance(last_scan_time, datetime) else datetime.fromisoformat(str(last_scan_time))
                oldest_ts = str(dt.timestamp())
            except (ValueError, TypeError):
                oldest_ts = None

        for workspace in workspaces:
            team_id = workspace.get('team_id')
            if not team_id:
                continue

            try:
                slack = SlackService(self.user_id, team_id)
                if not slack.is_connected():
                    continue

                # Record scan start time so _api_call can abort on sustained rate limiting
                slack._scan_start_time = time.time()

                # Get user map for resolving names
                users_map = await slack.get_users_map()

                # Scan channels
                channels = await slack.list_channels(
                    types="public_channel,private_channel", limit=100
                )

                # Also get DMs and group DMs
                dms = await slack.list_dms(limit=50)
                mpims = await slack.list_group_dms(limit=50)

                # Combine channels, DMs, and group DMs into one list
                all_conversations = []
                for ch in channels:
                    all_conversations.append({
                        'id': ch['id'],
                        'name': ch.get('name', ch['id']),
                        'is_dm': False,
                    })
                for dm in dms:
                    all_conversations.append({
                        'id': dm['id'],
                        'name': dm.get('user_name') or dm.get('user_id', dm['id']),
                        'is_dm': True,
                    })
                for mpim in mpims:
                    all_conversations.append({
                        'id': mpim['id'],
                        'name': mpim.get('name', mpim['id']),
                        'is_dm': True,
                    })

                # Load channel exclusion preferences
                exclusions = get_channel_exclusion_preferences(self.user_id)
                excluded_channels = set(exclusions.get('slack_excluded_channels', []))

                for conv in all_conversations:
                    # Skip excluded channels
                    channel_id = conv.get('id')
                    if channel_id in excluded_channels:
                        logger.debug("Skipping excluded Slack channel: %s", channel_id)
                        continue

                    try:
                        messages = await slack.get_messages(
                            conv['id'], limit=20, oldest=oldest_ts
                        )

                        for msg in messages:
                            # Skip bot/system messages — except huddle summaries which contain
                            # useful meeting context (identified by their text prefix)
                            if not msg.get('user'):
                                if '[Huddle summary]' not in msg.get('text', ''):
                                    continue

                            # Note: We scan ALL messages including user's own for context.
                            # Filtering happens at digest level to avoid "respond to yourself" items.

                            total_found += 1

                            source_id = f"{conv['id']}:{msg.get('ts', '')}"
                            username = users_map.get(msg.get('user'), msg.get('user', ''))

                            metadata = {
                                "channel_id": conv['id'],
                                "channel_name": conv['name'],
                                "user_id": msg.get('user', ''),
                                "username": username,
                                "text": (msg.get('text', '') or '')[:500],
                                "thread_ts": msg.get('thread_ts'),
                                "is_dm": conv['is_dm'],
                                "team_id": team_id,
                            }

                            # Detect outbound: message is from the authenticated user
                            authed_user_id = workspace.get('authed_user_id') or workspace.get('user_id', '')
                            msg_user_id = msg.get('user', '')
                            if authed_user_id and msg_user_id == authed_user_id:
                                direction = 'outbound'
                            else:
                                direction = 'inbound'

                            item_id = insert_scanned_item(
                                self.user_id, run_id, 'slack', source_id,
                                json.dumps(metadata), 'slack_message',
                                direction=direction
                            )
                            if item_id is not None:
                                total_new += 1

                    except SlackScanAbortError:
                        raise  # let it propagate to the workspace-level abort handler
                    except Exception as e:
                        logger.warning(
                            "Slack scanner: error scanning conversation %s: %s",
                            conv['id'], e
                        )
                        continue

            except SlackScanAbortError as e:
                logger.warning(
                    "Slack scan aborted after sustained rate limiting for workspace %s "
                    "user %d — will retry next scheduled window. (%s)",
                    team_id, self.user_id, e
                )
                return (total_found, total_new)

            except Exception as e:
                logger.warning(
                    "Slack scanner: error scanning workspace %s for user %d: %s",
                    team_id, self.user_id, e
                )
                continue

        return (total_found, total_new)

    async def _scan_telegram(
        self, run_id: int, last_scan_time: Optional[str] = None
    ) -> tuple[int, int]:
        """
        Scan Telegram for new messages across dialogs.

        Skips outgoing messages (from the bot/user itself) and media-only messages.
        Limits to 30 dialogs and 20 messages per dialog.

        Returns (items_found, items_new).
        """
        from web.services.telegram_service import TelegramService

        telegram = TelegramService(self.user_id)
        if not telegram.is_configured() or not telegram.is_connected():
            logger.info("Telegram scanner: not configured/connected for user %d", self.user_id)
            return (0, 0)

        if not await telegram.connect():
            logger.warning("Telegram scanner: failed to connect for user %d", self.user_id)
            return (0, 0)

        total_found = 0
        total_new = 0

        # Parse last_scan_time for filtering
        last_scan_dt = None
        if last_scan_time:
            try:
                last_scan_dt = last_scan_time if isinstance(last_scan_time, datetime) else datetime.fromisoformat(str(last_scan_time))
                # Ensure timezone-aware if needed
                if last_scan_dt.tzinfo is None:
                    last_scan_dt = last_scan_dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                last_scan_dt = None

        try:
            dialogs = await telegram.list_dialogs(limit=30)

            # Load channel exclusion preferences
            exclusions = get_channel_exclusion_preferences(self.user_id)
            excluded_chats = set(exclusions.get('telegram_excluded_chats', []))

            for dialog in dialogs:
                chat_id = dialog.get('id')
                chat_name = dialog.get('name', str(chat_id))

                # Skip excluded chats (ensure string comparison)
                if str(chat_id) in excluded_chats:
                    logger.debug("Skipping excluded Telegram chat: %s", chat_id)
                    continue

                try:
                    messages = await telegram.get_messages(chat_id, limit=20)

                    for msg in messages:
                        # Note: We scan ALL messages including user's own for context.
                        # Filtering happens at digest level to avoid "respond to yourself" items.
                        # We store is_outgoing in metadata so the digest can filter appropriately.

                        # Filter by last_scan_time
                        if last_scan_dt and msg.get('date'):
                            try:
                                msg_dt = datetime.fromisoformat(msg['date'])
                                if msg_dt.tzinfo is None:
                                    msg_dt = msg_dt.replace(tzinfo=timezone.utc)
                                if msg_dt <= last_scan_dt:
                                    continue
                            except (ValueError, TypeError):
                                pass

                        total_found += 1

                        source_id = f"{chat_id}:{msg.get('id', '')}"

                        metadata = {
                            "chat_id": chat_id,
                            "chat_name": chat_name,
                            "sender_id": msg.get('sender_id'),
                            "sender_name": msg.get('sender', ''),
                            "text": (msg.get('text', '') or '')[:500],
                            "date": msg.get('date'),
                            "is_outgoing": msg.get('is_outgoing', False),
                        }

                        direction = 'outbound' if msg.get('is_outgoing', False) else 'inbound'
                        item_id = insert_scanned_item(
                            self.user_id, run_id, 'telegram', source_id,
                            json.dumps(metadata), 'telegram_message',
                            direction=direction
                        )
                        if item_id is not None:
                            total_new += 1

                except Exception as e:
                    logger.warning(
                        "Telegram scanner: error scanning chat %s: %s",
                        chat_id, e
                    )
                    continue

        except Exception as e:
            logger.warning(
                "Telegram scanner: error listing dialogs for user %d: %s",
                self.user_id, e
            )

        return (total_found, total_new)

    async def _scan_notes(
        self, run_id: int, last_scan_time: Optional[str] = None
    ) -> tuple[int, int]:
        """
        Scan internal notes for new or updated notes since last scan.

        Queries the notes table directly (internal data, no API needed).

        Returns (items_found, items_new).
        """
        total_found = 0
        total_new = 0

        try:
            with get_db() as conn:
                cursor = conn.cursor()

                if last_scan_time:
                    cursor.execute("""
                        SELECT n.id, n.title, n.content, n.updated_at
                        FROM notes n
                        WHERE n.user_id = %s
                          AND (n.created_at >= %s OR n.updated_at >= %s)
                        ORDER BY n.updated_at DESC
                    """, (self.user_id, last_scan_time, last_scan_time))
                else:
                    cursor.execute("""
                        SELECT n.id, n.title, n.content, n.updated_at
                        FROM notes n
                        WHERE n.user_id = %s
                        ORDER BY n.updated_at DESC
                        LIMIT 100
                    """, (self.user_id,))

                for row in cursor.fetchall():
                    total_found += 1

                    source_id = str(row['id'])
                    content = row['content'] or ''

                    # Get tags for this note
                    cursor.execute(
                        "SELECT tag FROM note_tags WHERE note_id = %s",
                        (row['id'],)
                    )
                    tags = [r['tag'] for r in cursor.fetchall()]

                    metadata = {
                        "title": row['title'],
                        "tags": tags,
                        "updated_at": row['updated_at'],
                        "snippet": content[:200],
                    }

                    item_id = insert_scanned_item(
                        self.user_id, run_id, 'notes', source_id,
                        json.dumps(metadata), 'note'
                    )
                    if item_id is not None:
                        total_new += 1

        except Exception as e:
            logger.warning("Notes scanner: error for user %d: %r", self.user_id, e)

        return (total_found, total_new)

    async def _scan_tasks(
        self, run_id: int, last_scan_time: Optional[str] = None
    ) -> tuple[int, int]:
        """
        Scan internal tasks for new, updated, or completed tasks since last scan.

        Queries the tasks table directly (internal data, no API needed).

        Returns (items_found, items_new).
        """
        total_found = 0
        total_new = 0

        try:
            with get_db() as conn:
                cursor = conn.cursor()

                if last_scan_time:
                    cursor.execute("""
                        SELECT id, title, status, priority, due_date, project, type,
                               created_at, updated_at
                        FROM tasks
                        WHERE user_id = %s
                          AND (created_at >= %s OR updated_at >= %s)
                        ORDER BY updated_at DESC
                    """, (self.user_id, last_scan_time, last_scan_time))
                else:
                    cursor.execute("""
                        SELECT id, title, status, priority, due_date, project, type,
                               created_at, updated_at
                        FROM tasks
                        WHERE user_id = %s
                        ORDER BY updated_at DESC
                        LIMIT 100
                    """, (self.user_id,))

                for row in cursor.fetchall():
                    total_found += 1

                    source_id = str(row['id'])

                    metadata = {
                        "title": row['title'],
                        "status": row['status'],
                        "priority": row['priority'],
                        "due_date": row['due_date'],
                        "project": row['project'],
                        "type": row['type'],
                    }

                    item_id = insert_scanned_item(
                        self.user_id, run_id, 'tasks', source_id,
                        json.dumps(metadata), 'task'
                    )
                    if item_id is not None:
                        total_new += 1

        except Exception as e:
            logger.warning("Tasks scanner: error for user %d: %r", self.user_id, e)

        return (total_found, total_new)

    async def _scan_drive(
        self, run_id: int, last_scan_time: Optional[str] = None
    ) -> tuple[int, int]:
        """
        Scan Google Drive for recently modified files.

        Uses DriveService.list_recent() to find files modified in the last day.

        Returns (items_found, items_new).
        """
        from web.core.database import list_gmail_tokens
        from web.services.drive_service import DriveService

        accounts = list_gmail_tokens(self.user_id)
        if not accounts:
            logger.info("Drive scanner: no connected accounts for user %d", self.user_id)
            return (0, 0)

        total_found = 0
        total_new = 0

        for account in accounts:
            email = account.get('email')
            if not email:
                continue

            try:
                drive = DriveService(self.user_id, email)
                if not drive.is_connected():
                    continue

                files = await drive.list_recent(days=1, limit=50)

                for f in files:
                    total_found += 1

                    source_id = f.get('file_id', '')

                    metadata = {
                        "name": f.get('name', ''),
                        "mimeType": f.get('mime_type', ''),
                        "modifiedTime": f.get('modified_time', ''),
                        "webViewLink": f.get('web_view_link', ''),
                        "account": email,
                    }

                    item_id = insert_scanned_item(
                        self.user_id, run_id, 'drive', source_id,
                        json.dumps(metadata), 'drive_file'
                    )
                    if item_id is not None:
                        total_new += 1

            except Exception as e:
                logger.warning(
                    "Drive scanner: error scanning account %s for user %d: %s",
                    email, self.user_id, e
                )
                continue

        return (total_found, total_new)

    async def _scan_location(
        self, run_id: int, last_scan_time: Optional[str] = None
    ) -> tuple[int, int]:
        """
        Scan location history for recent place visits.

        Queries location_history table directly for entries with place names
        from the last day.

        Returns (items_found, items_new).
        """
        total_found = 0
        total_new = 0

        try:
            with get_db() as conn:
                cursor = conn.cursor()

                if last_scan_time:
                    cursor.execute("""
                        SELECT id, latitude, longitude, timestamp,
                               place_name, address, duration_minutes
                        FROM location_history
                        WHERE user_id = %s
                          AND timestamp >= %s
                          AND place_name IS NOT NULL
                        ORDER BY timestamp DESC
                        LIMIT 50
                    """, (self.user_id, last_scan_time))
                else:
                    cursor.execute("""
                        SELECT id, latitude, longitude, timestamp,
                               place_name, address, duration_minutes
                        FROM location_history
                        WHERE user_id = %s
                          AND place_name IS NOT NULL
                        ORDER BY timestamp DESC
                        LIMIT 50
                    """, (self.user_id,))

                for row in cursor.fetchall():
                    total_found += 1

                    ts = row['timestamp'] or ''
                    source_id = f"{row['latitude']}:{row['longitude']}:{ts}"

                    metadata = {
                        "place_name": row['place_name'],
                        "latitude": row['latitude'],
                        "longitude": row['longitude'],
                        "timestamp": ts,
                        "address": row['address'],
                        "duration_minutes": row['duration_minutes'],
                    }

                    item_id = insert_scanned_item(
                        self.user_id, run_id, 'location', source_id,
                        json.dumps(metadata), 'location_visit'
                    )
                    if item_id is not None:
                        total_new += 1

        except Exception as e:
            logger.warning("Location scanner: error for user %d: %r", self.user_id, e)

        return (total_found, total_new)

    async def _scan_conversations(
        self, run_id: int, last_scan_time: Optional[str] = None
    ) -> tuple[int, int]:
        """
        Scan Seny conversations for messages that may contain commitments.

        Scans both assistant messages (may contain promises) and user messages
        (may contain things the user said they'd do).

        Returns (items_found, items_new).
        """
        total_found = 0
        total_new = 0

        try:
            with get_db() as conn:
                cursor = conn.cursor()

                if last_scan_time:
                    cursor.execute("""
                        SELECT id, conversation_id, role, content, created_at
                        FROM messages
                        WHERE conversation_id IN (
                            SELECT id FROM conversations WHERE user_id = %s
                        )
                        AND created_at >= %s
                        ORDER BY created_at DESC
                        LIMIT 200
                    """, (self.user_id, last_scan_time))
                else:
                    cursor.execute("""
                        SELECT id, conversation_id, role, content, created_at
                        FROM messages
                        WHERE conversation_id IN (
                            SELECT id FROM conversations WHERE user_id = %s
                        )
                        ORDER BY created_at DESC
                        LIMIT 200
                    """, (self.user_id,))

                for row in cursor.fetchall():
                    total_found += 1

                    source_id = str(row['id'])
                    content = row['content'] or ''

                    metadata = {
                        "conversation_id": row['conversation_id'],
                        "role": row['role'],
                        "text": content[:500],
                        "created_at": row['created_at'],
                    }

                    item_id = insert_scanned_item(
                        self.user_id, run_id, 'conversations', source_id,
                        json.dumps(metadata), 'conversation'
                    )
                    if item_id is not None:
                        total_new += 1

        except Exception as e:
            logger.warning("Conversations scanner: error for user %d: %r", self.user_id, e)

        return (total_found, total_new)
